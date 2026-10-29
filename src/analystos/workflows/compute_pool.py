"""The compute queue's process pool: every task in a spawned child process, on every platform (S07: never in-process).

Why not a bare `concurrent.futures.ProcessPoolExecutor` (each one stalled every run of the 2026-10-28 Windows load
test, 0/20 finished):

* One dead child breaks a stdlib pool for good (`BrokenProcessPool`: a crash, an OOM kill, a killed hung statistic,
  a child that cannot read its task queue), and temporalio answers a `BrokenExecutor` by shutting the *whole*
  activity worker down: the compute queue loses its worker and every run waits on it. Here only the attempt that
  hit the dead child fails, with `ComputeChildDied` (retryable; Temporal retries it), and the next task gets a fresh
  pool. The cause is logged and counted (`replaced_broken`).
* `max_tasks_per_child` loses capacity. In CPython 3.12 a child that exits after its quota is often not replaced (the
  idle-worker semaphore still counts it), so after a few recycles the pool has no processes and queued activities
  wait until Temporal times them out (4 tasks on a pool of 2 with a quota of 2 leave no process). Here the whole
  *generation* is recycled instead: after `max_workers x max_tasks_per_child` tasks new tasks go to a fresh pool and
  the old one drains and exits.
* A freshly spawned child imports the platform before it can run anything (seconds on Windows), and that time counts
  against the activity's heartbeat window. Each generation is warmed when it is created: its children start and
  import the activity modules before a task needs them.

Isolation is the stdlib pool's: spawn (never fork, which would copy DB pools and Temporal threads), one task per
child at a time, at most `max_workers` children per generation; a draining generation only finishes the tasks it
already runs (Temporal's `max_concurrent_activities` bounds the tasks of both together).
"""
from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import Executor, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from analystos.core.logging import get_logger

log = get_logger(__name__)

WARM_MODULES = ("analystos.workflows.activities",)


class ComputeChildDied(RuntimeError):
    """The pool process running this attempt died. Not a `BrokenExecutor`, so temporalio fails this attempt only
    (retryable) and keeps the worker; the pool has already been replaced."""


def _warm(modules: tuple[str, ...]) -> int:
    """Runs in a pool child: import what the activities need, so the first real task does not pay for it."""
    import importlib

    for name in modules:
        importlib.import_module(name)
    return os.getpid()


class _Task(Future):
    """What `submit` returns: the child's outcome, with `BrokenProcessPool` turned into `ComputeChildDied`.
    Cancelling it cancels the queued child task (asyncio's `run_in_executor` cancels through this)."""

    def __init__(self, inner: Future) -> None:
        super().__init__()
        self._inner = inner

    def cancel(self) -> bool:
        return self._inner.cancel() and super().cancel()

    def _settle(self, inner: Future) -> None:
        """Runs on the stdlib pool's thread, possibly under its lock: takes no lock of ComputePool's."""
        if inner.cancelled():
            super().cancel()
        elif isinstance(inner.exception(), BrokenProcessPool):
            remote = inner.exception().__cause__  # the stdlib's _RemoteTraceback, when it knows why
            exc = ComputeChildDied(f"the compute pool process running this task died: {inner.exception()}"
                                   + (f" ({str(remote).strip()[-600:]})" if remote is not None else ""))
            exc.__cause__ = inner.exception()
            self.set_exception(exc)
        elif inner.exception() is not None:
            self.set_exception(inner.exception())
        else:
            self.set_result(inner.result())


class ComputePool(Executor):
    """A `ProcessPoolExecutor` that recycles by generation and replaces itself when broken (see the module doc)."""

    def __init__(self, max_workers: int, *, max_tasks_per_child: int | None = None, mp_context: Any = None,
                 warm_modules: tuple[str, ...] = WARM_MODULES) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._max_workers = max_workers
        self._ctx = mp_context or multiprocessing.get_context("spawn")
        if self._ctx.get_start_method() != "spawn":
            raise ValueError("the compute pool spawns its children (fork would copy DB pools and Temporal threads)")
        self._quota = max_workers * max_tasks_per_child if max_tasks_per_child else None
        self._warm_modules = warm_modules
        self._lock = threading.Lock()
        self._pool: ProcessPoolExecutor | None = None
        self._submitted = 0  # tasks given to the current generation
        self._retired: list[ProcessPoolExecutor] = []
        self._warming: list[Future] = []
        self._shutdown = False
        self.generations = 0
        self.replaced_broken = 0
        with self._lock:
            self._pool = self._new_pool()  # at worker start: the first tasks find warm children

    # ------------------------------------------------------------------ generations (all under self._lock)
    def _new_pool(self) -> ProcessPoolExecutor:
        pool = ProcessPoolExecutor(max_workers=self._max_workers, mp_context=self._ctx)
        self.generations += 1
        self._submitted = 0
        if self._warm_modules:  # one per child: the stdlib pool spawns a child per queued task
            self._warming = [pool.submit(_warm, self._warm_modules) for _ in range(self._max_workers)]
        return pool

    def wait_ready(self, timeout: float) -> bool:
        """Block until the current generation's children have imported the activity modules (at worker start, before
        polling: a task taken earlier would wait behind the warm-up, inside its heartbeat window). A warm-up that fails
        is logged; the pool still serves, and a dead child is replaced at the next submit."""
        with self._lock:
            warming = list(self._warming)
        done, pending = wait(warming, timeout=timeout)
        failed = [f.exception() for f in done if f.exception() is not None]
        if failed or pending:
            log.warning("compute pool warm-up: %d of %d children ready (%s)", len(done) - len(failed), len(warming),
                        failed[0] if failed else f"{len(pending)} still starting after {timeout:.0f} s")
        return not failed and not pending

    def _retire(self, pool: ProcessPoolExecutor) -> None:
        """Stop giving `pool` tasks; its running tasks finish, then its children exit."""
        pool.shutdown(wait=False)
        self._retired = [p for p in self._retired if _alive(p)] + [pool]

    def _drop_broken(self, pool: ProcessPoolExecutor, cause: Any) -> None:
        log.error("compute pool: a child process died (%s); starting a fresh pool, failed attempts are retried",
                  cause or "BrokenProcessPool")
        self.replaced_broken += 1
        self._retire(pool)
        self._pool = None

    def _current(self) -> ProcessPoolExecutor:
        """A broken generation is noticed here, at the next submit, not in a done-callback: the stdlib fails a broken
        pool's futures while holding that pool's lock, which `submit` takes while holding ours."""
        if self._pool is not None and getattr(self._pool, "_broken", False):
            self._drop_broken(self._pool, self._pool._broken)
        if self._pool is None:
            self._pool = self._new_pool()
        elif self._quota is not None and self._submitted >= self._quota:
            self._retire(self._pool)
            self._pool = self._new_pool()
        return self._pool

    # ------------------------------------------------------------------ Executor
    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            for attempt in (1, 2):
                if self._shutdown:
                    raise RuntimeError("cannot schedule new futures after shutdown")
                pool = self._current()
                try:
                    inner = pool.submit(fn, *args, **kwargs)
                except BrokenProcessPool as exc:  # broke between the check above and this submit
                    self._drop_broken(pool, exc)
                    if attempt == 2:
                        raise ComputeChildDied(f"the compute pool could not start a process: {exc}") from exc
                    continue
                self._submitted += 1
                break
        task = _Task(inner)
        inner.add_done_callback(task._settle)
        return task

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            pools = [*self._retired, *([self._pool] if self._pool is not None else [])]
            self._pool, self._retired = None, []
        for pool in pools:
            pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def processes(self) -> list[Any]:
        """Live children of every generation (for teardown and tests)."""
        with self._lock:
            pools = [*self._retired, *([self._pool] if self._pool is not None else [])]
        return [p for pool in pools for p in list((getattr(pool, "_processes", None) or {}).values()) if p.is_alive()]


def _alive(pool: ProcessPoolExecutor) -> bool:
    return any(p.is_alive() for p in list((getattr(pool, "_processes", None) or {}).values()))

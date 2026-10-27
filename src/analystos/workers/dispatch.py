"""Dispatching work to an isolated pool, on the control-plane side (ADR-0022, P7-06).

`dispatch_isolated(pool, envelope)` is the one entry point for every consumer (recipe snapshot jobs
today, P5-04 ML jobs, P7-12 notebook cells): it checks the pool is configured and serves the spec's
kind, mints the task's scoped token (the envelope's inputs to read, its required outputs to write),
records the task, hands it to the transport and records the events and the result. Workers get no
other credential. `raise_for_result` turns a failed result back into its `core/errors.py` class.

Transports:
* `SubprocessTransport`: long-lived local worker processes (`python -m analystos.workers.main --stdio`)
  started with an environment built from scratch (the lite profile; the conformance suite);
* `TemporalTransport`: `IsolatedTaskWorkflow` on the analysis queue runs `run_isolated_task` on
  `<prefix>-<pool>`, where `analystos worker --queues <pool>` deployments poll.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from analystos.contracts.worker import (
    ISOLATED_POOLS,
    Budget,
    CapabilityRef,
    TaskDispatch,
    TaskEnvelope,
    TaskEvent,
    TaskResult,
    WorkerError,
)
from analystos.core.errors import (
    AnalystOSError,
    FeatureUnavailable,
    InvalidInput,
    WorkerUnavailable,
    error_from_dict,
)
from analystos.core.logging import get_logger
from analystos.workers.handlers import handler_for, handler_hash
from analystos.workers.isolation import EX_CONFIG, WorkerMisconfigured
from analystos.workers.tokens import issue, secret_from_settings

log = get_logger(__name__)
MAX_ATTEMPTS = 3
REMEDY = ("set ANALYSTOS_ISOLATED_POOLS={pool} and run `analystos worker --queues {pool}` as its own deployment "
          "(compose profile `isolated`, Helm workers.{pool}.replicas); see docs/30-runbooks/04-lite-and-profiles.md")


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from analystos.core.config import get_settings

    return get_settings()


def pool_reason(pool: str, settings: Any = None) -> str | None:
    """None when this installation runs the isolated pool, else why not and how to add it."""
    if pool not in ISOLATED_POOLS:
        return f"unknown isolated pool {pool!r} (pools: {', '.join(ISOLATED_POOLS)})"
    if pool in _settings(settings).isolated_pool_set:
        return None
    return f"needs the isolated `{pool}` worker pool, which this installation does not run; " + REMEDY.format(pool=pool)


def pool_configured(pool: str, settings: Any = None) -> bool:
    return pool_reason(pool, settings) is None


def require_pool(pool: str, feature: str, settings: Any = None) -> None:
    reason = pool_reason(pool, settings)
    if reason:
        raise FeatureUnavailable(f"{feature} is unavailable: {reason}", details={"pool": pool, "feature": feature})


def capability_ref(kind: str, capability_id: str | None = None) -> CapabilityRef:
    from analystos.workers.handlers import HANDLERS

    return CapabilityRef(id=capability_id or kind, version=HANDLERS[kind].version, content_hash=handler_hash(kind))


def default_budget(pool: str, **overrides: Any) -> Budget:
    from analystos.workflows.queues import load_config

    spec = load_config()[0][pool]
    return Budget(**{**(spec.budget or {}), **{k: v for k, v in overrides.items() if v is not None}})


def token_ttl(envelope: TaskEnvelope) -> int:
    """Short-lived: long enough for every attempt of this task, never more than six hours."""
    return max(60, min(envelope.budget.wall_seconds * MAX_ATTEMPTS + 120, 6 * 3600))


def build_dispatch(pool: str, envelope: TaskEnvelope, *, secret: bytes | None = None, purposes: tuple[str, ...] = (),
                   max_model_calls: int = 0, settings: Any = None) -> TaskDispatch:
    handler_for(envelope.spec.kind, pool, conformance=envelope.spec.kind == "conformance.probe")
    verbs = ["read", "write"] + (["model"] if purposes else [])
    token = issue(secret if secret is not None else secret_from_settings(_settings(settings)), task_id=envelope.task_id,
                  workspace_id=envelope.workspace_id, idempotency_key=envelope.idempotency_key, run_id=envelope.run_id,
                  reads=[r.artifact_id for r in envelope.input_artifacts], writes=envelope.required_outputs, verbs=verbs,
                  purposes=purposes, max_output_bytes=envelope.budget.max_output_bytes, max_model_calls=max_model_calls,
                  ttl_seconds=token_ttl(envelope))
    return TaskDispatch(pool=pool, envelope=envelope, token=token)


def raise_for_result(result: TaskResult) -> TaskResult:
    if result.status == "failed":
        raise error_from_dict((result.error or WorkerError(code="worker_task_failed", message="failed")).model_dump())
    return result


def failed_result(dispatch: TaskDispatch, err: AnalystOSError, events: list[TaskEvent] | None = None) -> TaskResult:
    env = dispatch.envelope
    events = list(events or [])
    events.append(TaskEvent(task_id=env.task_id, type="task.failed", seq=len(events), at=time.time(),
                            data={"error": err.to_dict()}))
    return TaskResult(task_id=env.task_id, idempotency_key=env.idempotency_key, status="failed",
                      error=WorkerError(**err.to_dict()), events=events)


# ------------------------------------------------------------------------------------ sinks
class TaskSink(Protocol):
    def on_dispatch(self, dispatch: TaskDispatch) -> None: ...
    def on_events(self, task_id: str, events: list[TaskEvent]) -> None: ...
    def on_result(self, dispatch: TaskDispatch, result: TaskResult) -> None: ...


class NullSink:
    def on_dispatch(self, dispatch: TaskDispatch) -> None:
        pass

    def on_events(self, task_id: str, events: list[TaskEvent]) -> None:
        pass

    def on_result(self, dispatch: TaskDispatch, result: TaskResult) -> None:
        pass


class ListSink(NullSink):
    """In-memory sink (tests)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.events: list[TaskEvent] = []
        self.results: list[TaskResult] = []

    def on_dispatch(self, dispatch: TaskDispatch) -> None:
        self.dispatched.append(dispatch.envelope.task_id)

    def on_events(self, task_id: str, events: list[TaskEvent]) -> None:
        self.events.extend(events)

    def on_result(self, dispatch: TaskDispatch, result: TaskResult) -> None:
        self.results.append(result)


# ------------------------------------------------------------------------------------ transports
class Transport(Protocol):
    records_events: bool  # True when the transport persists events itself (the Temporal workflow does)

    def run(self, dispatch: TaskDispatch, on_event: Callable[[TaskEvent], None]) -> TaskResult: ...


def launch_environment(pool: str, artifact_url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A local isolated worker's whole environment, built from scratch: never the API's own environment."""
    import analystos

    package_root = str(Path(analystos.__file__).resolve().parents[1])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
           "LANG": os.environ.get("LANG", "C.UTF-8"), "PYTHONPATH": package_root, "PYTHONUNBUFFERED": "1",
           "ANALYSTOS_WORKER_QUEUES": pool, "ANALYSTOS_WORKER_ARTIFACT_URL": artifact_url}
    if os.environ.get("ANALYSTOS_ENV"):
        env["ANALYSTOS_ENV"] = os.environ["ANALYSTOS_ENV"]
    return {**env, **(extra or {})}


class SubprocessTransport:
    """One long-lived local worker process per pool (started on first use, restarted when it dies).
    Tasks on one pool run one at a time; the pool's concurrency is its number of processes (here one)."""

    records_events = False

    def __init__(self, artifact_url: str, *, conformance: bool = False, python: str | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.artifact_url, self.conformance = artifact_url, conformance
        self.python = python or sys.executable
        self.extra_env = dict(extra_env or {})
        self._procs: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, Any] = {}
        self._bufs: dict[str, bytearray] = {}
        self._locks: dict[str, threading.Lock] = {p: threading.Lock() for p in ISOLATED_POOLS}

    def _spawn(self, pool: str) -> subprocess.Popen:
        log_file = tempfile.TemporaryFile()  # noqa: SIM115 - lives as long as the worker process; closed in close()
        argv = [self.python, "-m", "analystos.workers.main", "--queues", pool, "--stdio"]
        if self.conformance:
            argv.append("--conformance")
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file,  # noqa: S603
                                env=launch_environment(pool, self.artifact_url, self.extra_env), cwd=tempfile.gettempdir(),
                                close_fds=True)
        self._procs[pool], self._logs[pool] = proc, log_file
        self._bufs[pool] = bytearray()
        return proc

    def _stderr(self, pool: str) -> str:
        f = self._logs.get(pool)
        if f is None:
            return ""
        f.seek(0)
        return f.read().decode("utf-8", "replace")[-2000:]

    def _readline(self, pool: str, proc: subprocess.Popen, deadline: float) -> bytes | None:
        """One protocol line: the line, b"" at EOF, None on timeout. Reads the raw fd into our own buffer:
        a buffered reader would hold a second line where `select` cannot see it."""
        buf = self._bufs.setdefault(pool, bytearray())
        fd = proc.stdout.fileno()
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                return None
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b""
            buf.extend(chunk)
        line, _, rest = bytes(buf).partition(b"\n")
        buf[:] = rest
        return line + b"\n"

    def pid(self, pool: str) -> int | None:
        proc = self._procs.get(pool)
        return proc.pid if proc is not None and proc.poll() is None else None

    def run(self, dispatch: TaskDispatch, on_event: Callable[[TaskEvent], None]) -> TaskResult:
        pool = dispatch.pool
        with self._locks[pool]:
            proc = self._procs.get(pool)
            if proc is None or proc.poll() is not None:
                proc = self._spawn(pool)
            try:
                proc.stdin.write((json.dumps({"dispatch": dispatch.model_dump(mode="json")}) + "\n").encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # the read below reports why the worker is gone
            deadline = time.monotonic() + dispatch.envelope.budget.wall_seconds + 120
            while True:
                line = self._readline(pool, proc, deadline)
                if line is None:
                    with contextlib.suppress(OSError):
                        proc.send_signal(signal.SIGUSR1)  # its thread stacks go to the log before it is replaced
                        time.sleep(1)
                    proc.kill()
                    raise WorkerUnavailable(f"the {pool} worker stopped answering; it was restarted",
                                            details={"stderr": self._stderr(pool)[-3000:]})
                if not line:
                    code = proc.wait(timeout=10)
                    if code == EX_CONFIG:
                        err = self._stderr(pool)
                        try:
                            raise error_from_dict(json.loads(err.strip().splitlines()[-1])["error"])
                        except (ValueError, KeyError, IndexError):
                            raise WorkerMisconfigured(f"the {pool} worker refused its environment: {err[-300:]}") from None
                    raise WorkerUnavailable(f"the {pool} worker exited (code {code}) during the task",
                                            details={"stderr": self._stderr(pool)[-500:]})
                msg = json.loads(line)
                if "event" in msg:
                    on_event(TaskEvent.model_validate(msg["event"]))
                elif "result" in msg:
                    return TaskResult.model_validate(msg["result"])
                elif "error" in msg:
                    raise error_from_dict(msg["error"])

    def close(self) -> None:
        for pool, proc in list(self._procs.items()):
            if proc.poll() is None:
                try:
                    proc.stdin.close()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
            f = self._logs.pop(pool, None)
            if f is not None:
                f.close()
        self._procs.clear()


class TemporalTransport:
    """`IsolatedTaskWorkflow` (analysis queue) -> `run_isolated_task` on `<prefix>-<pool>`. The workflow id is
    derived from the workspace and idempotency key, so a duplicate dispatch joins the running task."""

    records_events = True

    def __init__(self, *, client: Any = None, prefix: str | None = None, options: dict[str, Any] | None = None) -> None:
        self.client, self.prefix, self.options = client, prefix, options

    @staticmethod
    def workflow_id(envelope: TaskEnvelope) -> str:
        key = hashlib.sha256(f"{envelope.workspace_id}\x00{envelope.idempotency_key}".encode()).hexdigest()[:32]
        return f"isolated-{key}"

    async def run_async(self, dispatch: TaskDispatch) -> TaskResult:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from analystos.workflows.queues import queue_name, workflow_options

        client = self.client
        if client is None:
            from analystos.workflows.orchestrator import _temporal_client

            client = await _temporal_client()
        prefix = self.prefix or _settings().temporal_queue_prefix
        opts = self.options or workflow_options(prefix)
        wid = self.workflow_id(dispatch.envelope)
        try:
            handle = await client.start_workflow("IsolatedTaskWorkflow", args=[dispatch.model_dump(mode="json"), opts],
                                                 id=wid, task_queue=queue_name(prefix, "analysis"))
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(wid)
            desc = await handle.describe()
            if desc.status != WorkflowExecutionStatus.RUNNING:
                raise
        return TaskResult.model_validate(await handle.result())

    def run(self, dispatch: TaskDispatch, on_event: Callable[[TaskEvent], None]) -> TaskResult:
        from concurrent.futures import Future

        from analystos.workflows.orchestrator import _submit

        fut: Future = _submit(self.run_async(dispatch))  # the workflow header carries the caller's tenant
        try:
            return fut.result(timeout=dispatch.envelope.budget.wall_seconds * MAX_ATTEMPTS + 300)
        except TimeoutError:
            raise WorkerUnavailable("the isolated task did not finish in time") from None


_default: dict[str, Any] = {}
_default_lock = threading.Lock()


def default_transport(settings: Any = None) -> Transport:
    settings = _settings(settings)
    mode = settings.isolated_transport
    if mode == "auto":
        mode = "temporal" if settings.orchestrator == "temporal" else "subprocess"
    with _default_lock:
        if mode not in _default:
            _default[mode] = TemporalTransport() if mode == "temporal" else SubprocessTransport(settings.worker_artifact_url)
        return _default[mode]


def reset_default_transport() -> None:
    with _default_lock:
        for t in _default.values():
            if hasattr(t, "close"):
                t.close()
        _default.clear()


def default_sink() -> TaskSink:
    from analystos.services.worker_tasks import DbTaskSink

    return DbTaskSink()


def dispatch_isolated(pool: str, envelope: TaskEnvelope, *, transport: Transport | None = None,
                      sink: TaskSink | None = None, settings: Any = None, secret: bytes | None = None,
                      purposes: tuple[str, ...] = (), max_model_calls: int = 0, attempts: int = MAX_ATTEMPTS,
                      require_configured: bool = True) -> TaskResult:
    """Run `envelope` on the isolated `pool` and return its result (failed results are returned, not raised:
    call `raise_for_result`). A retryable failure (a lost worker) is retried under the same task and key."""
    if require_configured:
        require_pool(pool, f"'{envelope.spec.kind}' jobs", settings)
    if envelope.capability.content_hash != handler_hash(envelope.spec.kind):
        raise InvalidInput("the envelope's capability hash is not this build's handler; build it with capability_ref()")
    dispatch = build_dispatch(pool, envelope, secret=secret, purposes=purposes, max_model_calls=max_model_calls,
                              settings=settings)
    transport = transport or default_transport(settings)
    sink = sink or default_sink()
    sink.on_dispatch(dispatch)
    live = (lambda e: sink.on_events(envelope.task_id, [e])) if not transport.records_events else (lambda e: None)
    result: TaskResult | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = transport.run(dispatch, live)
        except AnalystOSError as exc:
            result = failed_result(dispatch, exc)
            if not transport.records_events:
                sink.on_events(envelope.task_id, result.events[-1:])
        if result.status == "completed" or not (result.error and result.error.retryable) or attempt == attempts:
            break
        log.warning("isolated task %s attempt %s failed (%s); retrying", envelope.task_id, attempt, result.error.code)
    assert result is not None
    sink.on_result(dispatch, result)
    return result

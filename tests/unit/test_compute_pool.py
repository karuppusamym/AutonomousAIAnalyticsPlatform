"""The compute queue's process pool (workflows/compute_pool.py), no services.

Both failures of the 2026-10-28 Windows load test (0/20 runs finished on Temporal) are reproduced here:
`ProcessPoolExecutor(max_tasks_per_child=...)` stops replacing recycled children (CPython 3.12), and one dead
child breaks a stdlib pool for good (`BrokenProcessPool` on every later task). These tests spawn real children.
"""
from __future__ import annotations

import multiprocessing
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

import pytest
from tests import compute_pool_probe as probe

from analystos.workflows.compute_pool import ComputeChildDied, ComputePool

SPAWN = multiprocessing.get_context("spawn")


def test_recycling_children_keeps_the_pool_serving():
    # The shape that stalled: 2 children with a quota of 2 tasks each, then more work than one generation takes.
    with ComputePool(2, max_tasks_per_child=2, mp_context=SPAWN, warm_modules=()) as pool:
        futures = [pool.submit(probe.pid, i) for i in range(12)]
        done, pending = wait(futures, timeout=120)
        assert not pending, "queued tasks never ran: the pool lost its processes"
        pids = {f.result() for f in done}
        assert pool.generations >= 3  # 12 tasks / (2 x 2) per generation
        assert len(pids) >= 3  # new children per generation (the stdlib spawns lazily: one child may serve a quota)


def test_a_dead_child_fails_its_attempt_and_the_pool_recovers():
    with ComputePool(2, mp_context=SPAWN, warm_modules=()) as pool:
        assert pool.submit(probe.pid).result(timeout=120)
        with pytest.raises(ComputeChildDied) as died:
            pool.submit(probe.die).result(timeout=120)
        # Not a BrokenExecutor: temporalio shuts its whole activity worker down on one of those.
        assert not isinstance(died.value, BrokenExecutor) and isinstance(died.value.__cause__, BrokenProcessPool)
        # the stdlib pool raises BrokenProcessPool here forever; Temporal's retry must find a working pool
        assert pool.submit(probe.pid).result(timeout=120)
        assert pool.replaced_broken == 1


def test_cancelling_a_queued_task_cancels_it_in_the_pool():
    with ComputePool(1, mp_context=SPAWN, warm_modules=()) as pool:
        busy = pool.submit(probe.sleep, 1.0)
        queued = [pool.submit(probe.pid) for _ in range(4)]  # the stdlib pool hands a few to its call queue early
        assert queued[-1].cancel() and queued[-1].cancelled()
        assert busy.result(timeout=120) and all(f.result(timeout=120) for f in queued[:-1])


def test_the_stdlib_pool_stays_broken_after_one_dead_child():
    """Why ComputePool exists: documents the stdlib behaviour it replaces."""
    with ProcessPoolExecutor(1, mp_context=SPAWN) as pool:
        with pytest.raises(BrokenProcessPool):
            pool.submit(probe.die).result(timeout=120)
        with pytest.raises(BrokenProcessPool):
            pool.submit(probe.pid).result(timeout=120)


def test_warm_up_starts_children_before_the_first_task():
    with ComputePool(2, mp_context=SPAWN) as pool:
        assert len(pool.processes()) == 2  # spawned at creation, importing the activity modules
        assert pool.submit(probe.pid).result(timeout=120)


def test_children_are_spawned_never_forked_and_shutdown_is_final():
    if "fork" in multiprocessing.get_all_start_methods():
        with pytest.raises(ValueError, match="spawn"):
            ComputePool(1, mp_context=multiprocessing.get_context("fork"))
    pool = ComputePool(1, mp_context=SPAWN, warm_modules=())
    pool.shutdown()
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(probe.pid)


def test_worker_builds_the_compute_queue_on_compute_pool(monkeypatch):
    import contextlib

    pytest.importorskip("temporalio")
    from analystos.workflows import worker
    from analystos.workflows.queues import DEFAULT_SPECS

    built = {}

    class FakeWorker:
        def __init__(self, client, **kw):
            built[kw["task_queue"]] = kw

    monkeypatch.setattr("temporalio.worker.Worker", FakeWorker)
    with contextlib.ExitStack() as stack:
        worker.build_workers(object(), ["compute", "analysis"], prefix="t", stack=stack, specs=DEFAULT_SPECS,
                             activities={"compute": [probe.pid], "analysis": []}, workflows={})
        assert isinstance(built["t-compute"]["activity_executor"], ComputePool)
        assert built["t-compute"]["shared_state_manager"] is not None
        assert not isinstance(built["t-analysis"]["activity_executor"], ComputePool)


def test_wait_ready_reports_the_warm_generation():
    with ComputePool(2, mp_context=SPAWN) as pool:
        assert pool.wait_ready(timeout=120)
    with ComputePool(1, mp_context=SPAWN, warm_modules=("analystos_no_such_module",)) as pool:
        assert not pool.wait_ready(timeout=120)  # logged; the pool still serves
        assert pool.submit(probe.pid).result(timeout=120)

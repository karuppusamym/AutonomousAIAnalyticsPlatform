"""Temporal workers, one pool per workload queue (spec v3 §8, P4-S01).

`analystos worker --queues analysis,compute` serves only those queues, so each pool is deployed and
scaled on its own (compose `worker-*` services, `deploy/helm/analystos` (Helm chart, one Deployment per pool)). The compute pool
runs activities in a bounded *process* pool (`compute_pool.ComputePool`): CPU-bound statistics neither block this
process's event loop nor stop its heartbeats, and a statistic hung in native code only freezes its own pool process.
"""
from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from analystos.core.config import get_settings
from analystos.core.logging import configure_logging, get_logger
from analystos.workflows.queues import ISOLATED_POOLS, QueueSpec, load_config, parse_workloads, queue_name

log = get_logger(__name__)


def build_workers(client: Any, workloads: list[str], *, prefix: str, stack: contextlib.ExitStack,
                  specs: dict[str, QueueSpec] | None = None, activities: dict[str, list] | None = None,
                  workflows: dict[str, list] | None = None) -> list[Any]:
    """One Worker per workload. `activities`/`workflows` override the defaults (tests register probes)."""
    from temporalio.worker import SharedStateManager, Worker

    from analystos.workflows.activities import BY_WORKLOAD
    from analystos.workflows.analysis_workflow import (
        AnalysisWorkflow,
        CrawlWorkflow,
        IsolatedTaskWorkflow,
        MLComputeWorkflow,
        RecipeComputeWorkflow,
    )

    specs = specs or load_config()[0]
    activities = activities or BY_WORKLOAD
    from analystos.workflows.query_workflow import QueryWorkflow

    workflows = workflows or {"analysis": [AnalysisWorkflow, RecipeComputeWorkflow, IsolatedTaskWorkflow, MLComputeWorkflow],
                              "crawl": [CrawlWorkflow], "query": [QueryWorkflow]}
    workers = []
    for workload in workloads:
        spec = specs[workload]
        kwargs: dict[str, Any] = {}
        if spec.executor == "process":
            # spawn, not fork: a forked child would inherit this process's DB pools and Temporal threads.
            # The manager outlives the pool (entered first, exited last): pool processes heartbeat through it.
            # ComputePool, not ProcessPoolExecutor: it recycles children without losing capacity and outlives a
            # dead child (see compute_pool.py; both stalled every run on the 2026-10-28 Windows load test).
            from analystos.workflows.compute_pool import ComputePool

            ctx = multiprocessing.get_context("spawn")
            manager = stack.enter_context(ctx.Manager())
            executor = stack.enter_context(ComputePool(spec.max_concurrent, mp_context=ctx,
                                                       max_tasks_per_child=spec.max_tasks_per_child))
            _check_picklable(activities.get(workload, []))  # what temporalio checks for a ProcessPoolExecutor
            executor.wait_ready(timeout=max(spec.heartbeat_seconds, 30))
            kwargs["shared_state_manager"] = SharedStateManager.create_from_multiprocessing(manager)
        else:
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=spec.max_concurrent,
                                                              thread_name_prefix=f"act-{workload}"))
        workers.append(Worker(client, task_queue=queue_name(prefix, workload), workflows=workflows.get(workload, []),
                              activities=activities.get(workload, []), activity_executor=executor,
                              max_concurrent_activities=spec.max_concurrent, **kwargs))
        log.info("temporal worker: queue %s (%s pool of %d)", queue_name(prefix, workload), spec.executor, spec.max_concurrent)
    return workers


def _check_picklable(fns: list) -> None:
    """Fail at start-up, not at the first task, when an activity cannot cross into a pool process."""
    import pickle

    for fn in fns:
        try:
            pickle.dumps(fn)
        except Exception as exc:
            raise TypeError(f"activity {getattr(fn, '__name__', fn)!r} must be picklable for the process pool") from exc


async def _main(workloads: list[str]) -> None:
    from temporalio.client import Client

    settings = get_settings()
    from analystos.workflows.tenant_propagation import INTERCEPTORS

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
                                  interceptors=INTERCEPTORS)  # S01-T4: activities run as the run's tenant
    with contextlib.ExitStack() as stack:
        workers = build_workers(client, workloads, prefix=settings.temporal_queue_prefix, stack=stack)
        await _serve_until_terminated(workers)


async def _serve_until_terminated(workers: list[Any]) -> None:
    """Run the workers; on SIGTERM (`kill`, `docker stop`, a service manager) shut them down so the caller's
    ExitStack closes the compute process pool and its manager. Without this the default SIGTERM action ends
    this process at once and leaves the pool processes running as orphans, holding database connections."""
    import signal

    loop = asyncio.get_running_loop()
    terminated = asyncio.Event()
    with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError):  # Windows / not the main thread
        loop.add_signal_handler(signal.SIGTERM, terminated.set)
    runs = asyncio.ensure_future(asyncio.gather(*(w.run() for w in workers)))
    waiter = asyncio.ensure_future(terminated.wait())
    try:
        await asyncio.wait({runs, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if terminated.is_set() and not runs.done():
            log.info("SIGTERM: shutting down %d workers", len(workers))
            await asyncio.gather(*(w.shutdown() for w in workers))
        await runs
    finally:
        waiter.cancel()
        with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)


def run_worker(queues: str | None = None, *, conformance: bool = False) -> int:
    """`analystos worker`. Isolated pools (compute-py, compute-ml; ADR-0022) start a credential-free worker
    that never builds Settings, so the queue list is read from the argument or the environment first."""
    import os
    import sys

    from analystos.core.profiles import require_extra

    value = queues if queues is not None else (os.environ.get("ANALYSTOS_WORKER_QUEUES") or None)
    workloads = parse_workloads(value if value is not None else get_settings().worker_queues)
    if set(workloads) & set(ISOLATED_POOLS):
        from analystos.workers.main import run_isolated

        return run_isolated(tuple(workloads), conformance=conformance)
    if conformance:
        sys.stderr.write("--conformance applies to isolated pools only\n")
        return 2
    configure_logging()
    require_extra("temporal", "`analystos worker` (the standard profile)")
    asyncio.run(_main(workloads))
    return 0

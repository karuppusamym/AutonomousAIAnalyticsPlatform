"""Temporal activities: thin wrappers over the orchestrator-agnostic engine (sync; thread or process pool).

Long activities heartbeat from a pump thread in the process that runs the work. A process that
stops scheduling Python threads (a statistic stuck in native code holding the GIL, a deadlock, a
frozen or killed pool process) stops heartbeating, and Temporal declares the attempt hung after the
queue's `heartbeat_seconds` instead of its `start_to_close_seconds`.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Callable, Iterator
from typing import Any

from temporalio import activity

from analystos.core.logging import get_logger
from analystos.runtime import engine
from analystos.tenancy.background import owned_by

log = get_logger(__name__)


@contextlib.contextmanager
def heartbeat_pump(interval: float | None = None) -> Iterator[None]:
    """Heartbeat every `interval` seconds (default: a third of the heartbeat timeout) while the body runs."""
    if not activity.in_activity():
        yield
        return
    timeout = activity.info().heartbeat_timeout
    if interval is None:
        if not timeout:
            yield
            return
        interval = max(timeout.total_seconds() / 3, 0.2)
    stop = threading.Event()
    beat_ctx = contextvars.copy_context()  # the activity context, entered only by the pump thread

    def pump() -> None:
        while not stop.wait(interval):
            try:
                activity.heartbeat()
            except Exception as exc:  # noqa: BLE001 - a lost heartbeat must not kill the work
                log.warning("heartbeat failed: %s", exc)

    thread = threading.Thread(target=beat_ctx.run, args=(pump,), daemon=True, name="activity-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()


def heartbeating(fn: Callable[..., Any], *args: Any) -> Any:
    with heartbeat_pump():
        return fn(*args)


@owned_by("analysis_run", "run_id")
def release_timed_out_claim(run_id: str, key: str, attempt: int) -> bool:
    """A retry of `execute_task` means Temporal declared the previous attempt dead (heartbeat or
    start_to_close timeout): a retryable failure resets the task to NEW itself. Release that attempt's
    RUNNING claim so the retry runs the task now instead of reporting `in_progress` until the claim TTL."""
    from sqlalchemy import update

    from analystos.db.base import session_scope
    from analystos.db.models import RunTask

    with session_scope() as s:
        released = s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.key == key, RunTask.status == "RUNNING")
                             .values(started_at=None)).rowcount
    if released:
        log.warning("task %s of run %s: previous attempt timed out; claim released for attempt %s", key, run_id, attempt)
    return bool(released)


@activity.defn(name="plan_run")
def plan_run(run_id: str) -> dict:
    return heartbeating(engine.plan_run, run_id)


@activity.defn(name="get_state")
def get_state(run_id: str) -> dict:
    return engine.get_state(run_id)


@activity.defn(name="execute_task")
def execute_task(run_id: str, key: str) -> dict:
    """Registered on the analysis, compute and publish queues; the workflow picks the queue per step."""
    if activity.in_activity() and activity.info().attempt > 1:
        release_timed_out_claim(run_id, key, activity.info().attempt)
    return heartbeating(engine.execute_task, run_id, key)


@activity.defn(name="finish_run")
def finish_run(run_id: str, outcome: str, error: str | None) -> None:
    engine.finish_run(run_id, outcome, error)


@activity.defn(name="run_crawl")
def run_crawl(crawl_id: str, user_id: str) -> dict:
    from analystos.services.crawler import run_crawl as crawl

    return heartbeating(crawl, crawl_id, user_id)


@activity.defn(name="run_recipe_snapshot")
def run_recipe_snapshot(job: dict) -> dict:
    """A recipe's DuckDB statement over its immutable input snapshots (ADR-0023, P6-04), on the compute
    pool until isolated compute-py pools exist (P7-06). Reads only snapshot files, never a source."""
    from analystos.recipes.execute import run_snapshot_job

    return heartbeating(run_snapshot_job, job)


@activity.defn(name="run_ml_job")
def run_ml_job(job: dict) -> dict:
    """An ML job (ADR-0024): readiness, split, bounded search, holdout evaluation or scoring over immutable
    snapshots, on the compute pool until the isolated compute-ml pool exists (P7-06)."""
    from analystos.ml.jobs import run_ml_job as job_fn

    return heartbeating(job_fn, job)


@activity.defn(name="queue_ping")
def queue_ping() -> str:
    """Liveness probe answered by every pool (the only activity of the placeholder `elt` pool)."""
    return activity.info().task_queue


@activity.defn(name="record_task_events")
def record_task_events(task_id: str, events: list[dict]) -> int:
    """Persist the events an isolated worker signalled to IsolatedTaskWorkflow (P7-06). Control plane only:
    isolated workers never register this activity and hold no database credential."""
    from analystos.services.worker_tasks import record_events

    return record_events(task_id, events)


ENGINE_ACTIVITIES = [plan_run, get_state, execute_task, finish_run]
# Activities served by each workload's worker. The analysis worker also hosts AnalysisWorkflow.
BY_WORKLOAD: dict[str, list] = {
    "analysis": [*ENGINE_ACTIVITIES, record_task_events, queue_ping],
    "compute": [execute_task, run_recipe_snapshot, run_ml_job, queue_ping],
    "publish": [execute_task, queue_ping],
    "crawl": [run_crawl, queue_ping],
    "elt": [queue_ping],
}

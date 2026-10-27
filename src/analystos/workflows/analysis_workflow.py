"""Durable analysis workflow (ADR-0005). State lives in Postgres; Temporal provides durable
execution, retries with backoff, worker-crash recovery, and event-driven resume via the `nudge`
signal (sent on pause/resume/cancel, approvals and user feedback). The workflow never holds a
database transaction or a request open across model calls.

Each ready step runs on its workload's task queue (`workflows/queues.py`: statistics on `compute`,
BI side effects on `publish`, the rest on `analysis`) with that queue's `start_to_close` and
`heartbeat` timeouts. The run continues-as-new after `max_activities` activities so a long run's
history stays bounded; all state is in Postgres, so the new execution just resumes the loop."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from analystos.workflows.queues import fallback_options, workload_for_step

NON_RETRYABLE = ["Forbidden", "PolicyDenied", "SQLRejected", "ApprovalRequired", "InvalidInput", "BudgetExceeded",
                 "RunCancelled", "NotFound", "Conflict"]
QUICK = dict(start_to_close_timeout=timedelta(minutes=2),
             retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5))
NUDGE_BEFORE_STATE = "nudge-cleared-before-state"  # workflow.patched id (a determinism-safe behaviour change)
BUDGET_DEADLINE = "budget-deadline"  # S06 §3.4: the wait timer never outlasts the run's wall-clock budget
TASK_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                         maximum_interval=timedelta(seconds=30), maximum_attempts=3,
                         non_retryable_error_types=NON_RETRYABLE)


def _queue_kwargs(opts: dict[str, Any], workload: str) -> dict[str, Any]:
    q = opts["queues"][workload]
    return dict(task_queue=q["name"], start_to_close_timeout=timedelta(seconds=q["start_to_close"]),
                heartbeat_timeout=timedelta(seconds=q["heartbeat"]))


@workflow.defn(name="AnalysisWorkflow")
class AnalysisWorkflow:
    def __init__(self) -> None:
        self._nudged = False

    @workflow.signal
    def nudge(self) -> None:
        self._nudged = True

    async def _wait(self, seconds: int) -> None:
        if not workflow.patched(NUDGE_BEFORE_STATE):
            self._nudged = False  # histories recorded before the fix replay as they ran
        with contextlib.suppress(asyncio.TimeoutError):
            await workflow.wait_condition(lambda: self._nudged, timeout=timedelta(seconds=seconds))

    @workflow.run
    async def run(self, run_id: str, opts: dict[str, Any] | None = None) -> str:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-analysis"))
        analysis = _queue_kwargs(opts, "analysis")
        quick = {**QUICK, "task_queue": analysis["task_queue"]}
        plan = {**analysis, "start_to_close_timeout": timedelta(minutes=10), "retry_policy": RetryPolicy(maximum_attempts=3)}
        activities = 0
        if not opts.get("resumed"):
            await workflow.execute_activity("plan_run", run_id, **plan)
            activities += 1
        while True:
            if activities >= opts.get("max_activities", 400) or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[run_id, {**opts, "resumed": True}])
            if workflow.patched(NUDGE_BEFORE_STATE):
                # Cleared before the state is read, not when the wait starts: a nudge that lands while get_state runs
                # (an approval right after a resume) is kept, so the wait below returns at once instead of sleeping
                # its 300 s re-check (live 2026-09-27: a publication started five minutes after its approval).
                self._nudged = False
            state = await workflow.execute_activity("get_state", run_id, **quick)
            activities += 1
            if state.get("terminal"):
                return state.get("status", "COMPLETED")
            if state.get("control") == "cancel":
                await workflow.execute_activity("finish_run", args=[run_id, "CANCELLED", None], **quick)
                return "CANCELLED"
            if state.get("needs_plan"):
                await workflow.execute_activity("plan_run", run_id, **plan)
                activities += 1
                continue
            if state.get("fail"):
                await workflow.execute_activity("finish_run", args=[run_id, "FAILED", state["fail"]], **quick)
                return "FAILED"
            if state.get("done"):
                await workflow.execute_activity("finish_run", args=[run_id, "COMPLETED", None], **quick)
                return "COMPLETED"
            if state.get("limited"):  # a budget counter is used up (S06 §3.4): validated partials stand
                outcome = "EXPIRED" if state.get("expired") else "COMPLETED_WITH_LIMITATIONS"
                await workflow.execute_activity("finish_run", args=[run_id, outcome, state["limited"]], **quick)
                return outcome
            if state.get("ready"):
                results = await asyncio.gather(*[
                    workflow.execute_activity("execute_task", args=[run_id, key], retry_policy=TASK_RETRY,
                                              **_queue_kwargs(opts, workload_for_step(key)))
                    for key in state["ready"]], return_exceptions=True)
                activities += len(state["ready"])
                for r in results:
                    if isinstance(r, BaseException):
                        workflow.logger.warning("task activity failed after retries: %s", r)
                continue
            # paused, waiting for a person, or tasks still running elsewhere: wait for a signal (or re-check)
            seconds = 300 if state.get("control") == "pause" or state.get("waiting_user") else 15
            left = ((state.get("budget") or {}).get("remaining") or {}).get("max_duration_s")
            if left is not None and workflow.patched(BUDGET_DEADLINE):
                seconds = max(1, min(seconds, int(left) + 1))  # wake when the wall-clock budget runs out
            await self._wait(seconds)


@workflow.defn(name="RecipeComputeWorkflow")
class RecipeComputeWorkflow:
    """One recipe statement over its snapshots, run on the `compute` pool (ADR-0023, P6-04). Hosted by the
    analysis worker; the job is pure (snapshot files in, a result snapshot out), so a retry is safe."""

    @workflow.run
    async def run(self, job: dict[str, Any], opts: dict[str, Any] | None = None) -> dict:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-analysis"))
        return await workflow.execute_activity("run_recipe_snapshot", args=[job], retry_policy=TASK_RETRY,
                                               **_queue_kwargs(opts, "compute"))


ISOLATED_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                             maximum_interval=timedelta(seconds=30), maximum_attempts=3)


def _failed(dispatch: dict[str, Any], code: str, message: str, retryable: bool) -> dict[str, Any]:
    env = dispatch["envelope"]
    error = {"code": code, "message": message, "retryable": retryable, "details": {}}
    return {"task_id": env["task_id"], "idempotency_key": env["idempotency_key"], "status": "failed", "outputs": {},
            "result": {}, "error": error, "usage": {}, "events": [
                {"task_id": env["task_id"], "type": "task.failed", "seq": 0, "at": 0.0, "data": {"error": error}}]}


@workflow.defn(name="IsolatedTaskWorkflow")
class IsolatedTaskWorkflow:
    """One TaskEnvelope on an isolated pool (ADR-0022, P7-06). Hosted by the analysis worker; the task runs as
    `run_isolated_task` on `<prefix>-<pool>`, whose worker signals each task event here. Events are persisted
    through the control plane (`record_task_events`, analysis queue) as they arrive. A lost worker is a
    heartbeat timeout, retried under the same idempotency key; a task that never reports its end gets a
    synthetic `task.failed`."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._seen_end = False

    @workflow.signal
    def task_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)

    @workflow.run
    async def run(self, dispatch: dict[str, Any], opts: dict[str, Any] | None = None) -> dict:
        from temporalio.exceptions import ActivityError, ApplicationError

        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-analysis"))
        pool, task_id = dispatch["pool"], dispatch["envelope"]["task_id"]
        quick = {**QUICK, "task_queue": _queue_kwargs(opts, "analysis")["task_queue"]}
        handle = workflow.start_activity("run_isolated_task", args=[dispatch], retry_policy=ISOLATED_RETRY,
                                         **_queue_kwargs(opts, pool))

        async def flush() -> None:
            batch, self._events = self._events, []
            self._seen_end = self._seen_end or any(e.get("type") in ("task.completed", "task.failed") for e in batch)
            await workflow.execute_activity("record_task_events", args=[task_id, batch], **quick)

        while True:
            await workflow.wait_condition(lambda: bool(self._events) or handle.done())
            if self._events:
                await flush()
            if handle.done() and not self._events:
                break
        try:
            result = await handle
        except ActivityError as err:
            cause = err.cause
            if isinstance(cause, ApplicationError) and cause.details and isinstance(cause.details[0], dict):
                result = cause.details[0]  # a retryable job failure, after its last attempt
            else:
                result = _failed(dispatch, "worker_unavailable", f"the {pool} worker was lost: {cause or err}", True)
            if not self._seen_end:
                self._events = [e for e in result.get("events", []) if e.get("type") == "task.failed"][-1:]
                if self._events:
                    await flush()
        return result
@workflow.defn(name="MLComputeWorkflow")
class MLComputeWorkflow:
    """One ML job over its snapshots (ADR-0024, P5-01) on the `compute` pool until `compute-ml` exists (P7-06).
    The job is pure (content-addressed files in and out), so a retry is safe."""

    @workflow.run
    async def run(self, job: dict[str, Any], opts: dict[str, Any] | None = None) -> dict:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-analysis"))
        return await workflow.execute_activity("run_ml_job", args=[job], retry_policy=TASK_RETRY,
                                               **_queue_kwargs(opts, "compute"))


@workflow.defn(name="CrawlWorkflow")
class CrawlWorkflow:
    """One metadata crawl on the `crawl` pool. `run_crawl` records failure on the crawl_run row
    itself, so it is not retried (a retry would re-run a crawl the user already saw fail)."""

    @workflow.run
    async def run(self, crawl_id: str, user_id: str, opts: dict[str, Any] | None = None) -> dict:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-crawl"))
        return await workflow.execute_activity("run_crawl", args=[crawl_id, user_id], retry_policy=RetryPolicy(maximum_attempts=1),
                                               **_queue_kwargs(opts, "crawl"))

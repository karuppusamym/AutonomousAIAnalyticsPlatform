"""worker:query on Temporal (S02-T1): one short QueryWorkflow per statement, whose single activity runs the
gateway in the `query` pool. The workflow id is `query-<query_execution_id>`, so a caller cancels a statement
by cancelling that workflow: the cancellation reaches the activity through its heartbeat and the gateway
interrupts the statement on the source (`QueryGateway.cancel`).

Statements are not retried (a retry would run a possibly expensive scan twice); the caller decides.
"""
from __future__ import annotations

import contextvars
import threading
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from analystos.workflows.queues import fallback_options


@workflow.defn(name="QueryWorkflow")
class QueryWorkflow:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        prefix = workflow.info().task_queue.removesuffix("-query")
        q = fallback_options(prefix)["queues"]["query"]
        return await workflow.execute_activity(
            "query_gateway_op", payload, task_queue=workflow.info().task_queue,
            start_to_close_timeout=timedelta(seconds=q["start_to_close"]),
            heartbeat_timeout=timedelta(seconds=q["heartbeat"]),
            retry_policy=RetryPolicy(maximum_attempts=1),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)


@activity.defn(name="query_gateway_op")
def query_gateway_op(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one QueryRequest in this process (the worker:query role). Errors keep their class name as the
    ApplicationError type so the caller re-raises the same AnalystOSError."""
    from analystos.core.errors import AnalystOSError
    from analystos.gateway.protocol import InProcessQueryService, QueryRequest

    req = QueryRequest.model_validate(payload["request"])
    service = InProcessQueryService()
    stop = threading.Event()
    with_ctx = contextvars.copy_context()

    def pump() -> None:  # heartbeat; on a cancellation request interrupt the statement on the source
        while not stop.wait(2.0):
            try:
                activity.heartbeat()
                if activity.is_cancelled() and req.query_id:
                    service.cancel(req.query_id)
                    return
            except Exception:  # noqa: BLE001 - a lost heartbeat must not kill the statement
                continue

    if activity.in_activity():
        threading.Thread(target=with_ctx.run, args=(pump,), daemon=True, name="query-heartbeat").start()
    try:
        if payload.get("op") == "estimate":
            return service.estimate(req).model_dump(mode="json")
        return service.execute(req).model_dump(mode="json")
    except AnalystOSError as exc:
        raise ApplicationError(exc.message, exc.message, type=type(exc).__name__, non_retryable=True) from None
    finally:
        stop.set()

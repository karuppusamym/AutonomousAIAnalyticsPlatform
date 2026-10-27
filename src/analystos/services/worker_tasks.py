"""The control plane's record of isolated compute tasks (ADR-0022 decision 5, P7-06).

Workers never touch the database: their events arrive as Temporal signals (recorded by the
`record_task_events` activity on the analysis queue) or as the local pool's event lines (recorded by
`dispatch_isolated` through `DbTaskSink`). Each event is a `worker_task_event` row and a bus event of
the same type, run-scoped when the task belongs to a run; the result adds output refs, usage, the
structured error and lineage edges.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert

from analystos.contracts.worker import TaskDispatch, TaskEvent, TaskResult
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import WorkerTask, WorkerTaskEvent
from analystos.tenancy.background import owned_by

STATUS_FOR = {"task.started": "running", "task.completed": "completed", "task.failed": "failed"}


def record_dispatch(dispatch: TaskDispatch) -> None:
    from analystos.artifacts.registry import link

    env = dispatch.envelope
    with session_scope() as s:
        s.execute(insert(WorkerTask).values(
            id=env.task_id, workspace_id=env.workspace_id, run_id=env.run_id, work_order_id=env.work_order_id,
            pool=dispatch.pool, capability_id=env.capability.id, capability_version=env.capability.version,
            capability_hash=env.capability.content_hash, kind=env.spec.kind, idempotency_key=env.idempotency_key,
            envelope_hash=env.envelope_hash, status="queued", inputs=[r.model_dump() for r in env.input_artifacts],
            outputs={}, result={}, error=None, usage={}).on_conflict_do_nothing(index_elements=["id"]))
        if env.run_id:
            link(s, env.workspace_id, ("run", env.run_id), "dispatched", ("worker_task", env.task_id), run_id=env.run_id)
        if env.context_ref and ":" in env.context_ref:  # e.g. recipe_run:<id>, the object the task works for
            owner_type, owner_id = env.context_ref.split(":", 1)
            link(s, env.workspace_id, (owner_type, owner_id), "dispatched", ("worker_task", env.task_id))
        for ref in env.input_artifacts:
            link(s, env.workspace_id, ("worker_artifact", ref.artifact_id), "input_to", ("worker_task", env.task_id),
                 run_id=env.run_id)


@owned_by("worker_task", "task_id")
def record_events(task_id: str, events: list[dict[str, Any]] | list[TaskEvent]) -> int:
    from analystos.events.bus import emit

    parsed = [e if isinstance(e, TaskEvent) else TaskEvent.model_validate(e) for e in events]
    if not parsed:
        return 0
    with session_scope() as s:
        task = s.get(WorkerTask, task_id)
        if task is None:
            return 0
        for e in parsed:
            s.add(WorkerTaskEvent(task_id=task_id, seq=e.seq, type=e.type, data=e.data, worker_at=e.at))
            if e.type in STATUS_FOR and task.status not in ("completed", "failed"):
                task.status = STATUS_FOR[e.type]
            emit(task.workspace_id, e.type, {"task_id": task_id, "pool": task.pool, "kind": task.kind, "seq": e.seq,
                                              **_summary(e)}, run_id=task.run_id, actor=f"worker:{task.pool}", session=s)
    return len(parsed)


def _summary(e: TaskEvent) -> dict[str, Any]:
    keep = ("message", "fraction", "outputs", "error", "capability")
    return {k: e.data[k] for k in keep if k in e.data}


def record_result(result: TaskResult | dict[str, Any]) -> None:
    from analystos.artifacts.registry import link

    result = result if isinstance(result, TaskResult) else TaskResult.model_validate(result)
    with session_scope() as s:
        task = s.get(WorkerTask, result.task_id)
        if task is None:
            return
        task.status = result.status
        task.outputs = {n: r.model_dump() for n, r in result.outputs.items()}
        task.result = result.result
        task.error = result.error.model_dump() if result.error else None
        task.usage = result.usage.model_dump()
        task.finished_at = utcnow()
        for ref in result.outputs.values():
            link(s, task.workspace_id, ("worker_task", task.id), "produced", ("worker_artifact", ref.artifact_id),
                 run_id=task.run_id)


class DbTaskSink:
    def on_dispatch(self, dispatch: TaskDispatch) -> None:
        record_dispatch(dispatch)

    def on_events(self, task_id: str, events: list[TaskEvent]) -> None:
        record_events(task_id, events)

    def on_result(self, dispatch: TaskDispatch, result: TaskResult) -> None:
        record_result(result)

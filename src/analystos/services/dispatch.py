"""Dispatch outbox for runs (workbench API §4, P4-06).

`create_run` writes the run and a `dispatch_outbox` row in one transaction, then tries to dispatch at
once. Whatever happens after the commit, the row is the durable intent:

* crash before dispatch — the row is still `pending`; the relay (scheduler loop) starts the workflow;
* crash after dispatch, before the row was marked — the relay starts it again; the workflow id is
  stable (`analysis-<run>`), so Temporal answers "already started" and the run is only nudged, and the
  local orchestrator ignores a run it is already driving;
* orchestrator unavailable — the row stays pending with a backoff and the error; the relay retries;
* cancelled before dispatch — the relay never starts it and finishes the run as CANCELLED.

`reconcile` repairs what predates the outbox: a NEW run with neither an outbox row nor a workflow id.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.events import RUN_TERMINAL
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, DispatchOutbox
from analystos.events.bus import emit
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
MAX_BACKOFF = timedelta(minutes=5)
ORPHAN_AFTER = timedelta(minutes=2)


def enqueue(session: Session, run: AnalysisRun) -> DispatchOutbox:
    row = DispatchOutbox(id=new_id("dsp"), workspace_id=run.workspace_id, kind="run.start", run_id=run.id, status="pending",
                         attempts=0, available_at=utcnow())
    session.add(row)
    session.flush()
    return row


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(seconds=2 ** min(attempts, 9)), MAX_BACKOFF)


@owned_by("dispatch_outbox", "outbox_id")
def dispatch(outbox_id: str) -> str:
    """Deliver one row: 'dispatched' | 'cancelled' | 'pending' (failed, will retry) | 'skipped'."""
    from analystos.services import runs  # start_run is looked up at call time (tests drive the engine themselves)

    cancel = False
    with session_scope() as s:
        row = s.get(DispatchOutbox, outbox_id, with_for_update=True)
        if row is None or row.status != "pending":
            return "skipped"
        run = s.get(AnalysisRun, row.run_id)
        if run is None or run.status in RUN_TERMINAL or run.control == "cancel":
            row.status, row.last_error = "cancelled", "run cancelled or gone before dispatch"
            cancel = run is not None and run.status not in RUN_TERMINAL
            run_id = row.run_id
        else:
            row.attempts += 1
            try:
                wf = runs.start_run(row.run_id)
            except Exception as exc:  # recorded and retried by the relay
                row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                row.available_at = utcnow() + _backoff(row.attempts)
                emit(row.workspace_id, "run.dispatch_failed", {"attempts": row.attempts, "error": row.last_error[:300]},
                     run_id=row.run_id, session=s)
                log.warning("dispatch of run %s failed (attempt %s): %s", row.run_id, row.attempts, exc)
                return "pending"
            row.status, row.workflow_id, row.dispatched_at, row.last_error = "dispatched", wf, utcnow(), None
            run.workflow_id = wf
            emit(row.workspace_id, "run.dispatched", {"workflow_id": wf, "attempts": row.attempts}, run_id=row.run_id, session=s)
            return "dispatched"
    if cancel:
        from analystos.runtime.engine import finish_run

        finish_run(run_id, "CANCELLED")
    return "cancelled"


@platform_loop
def relay(limit: int = 20, now: Any = None) -> dict[str, int]:
    """Deliver due pending rows (the scheduler loop calls this every iteration)."""
    now = now or utcnow()
    with session_scope() as s:
        ids = list(s.scalars(select(DispatchOutbox.id).where(DispatchOutbox.status == "pending", DispatchOutbox.available_at <= now)
                             .order_by(DispatchOutbox.available_at).limit(limit).with_for_update(skip_locked=True)))
    counts: dict[str, int] = {}
    for oid in ids:
        outcome = dispatch(oid)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


@platform_loop
def reconcile(now: Any = None) -> dict[str, int]:
    """Give an outbox row to NEW runs that have none and no workflow (created before the outbox, or by a
    path that crashed between its own commit and dispatch), then relay."""
    now = now or utcnow()
    created = 0
    with session_scope() as s:
        has_row = select(DispatchOutbox.run_id).where(DispatchOutbox.run_id == AnalysisRun.id).exists()
        for run in s.scalars(select(AnalysisRun).where(AnalysisRun.status == "NEW", AnalysisRun.workflow_id.is_(None),
                                                       AnalysisRun.created_at < now - ORPHAN_AFTER, ~has_row).limit(50)):
            enqueue(s, run)
            created += 1
    return {"enqueued": created, **relay()}

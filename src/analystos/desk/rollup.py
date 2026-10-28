"""Desk background work (spec S13 §3.4, §3.6): the nightly value rollup into ``desk_daily`` and the SLA sweep.

Both follow the tenancy rule for background code (``tenancy/background.py``): a ``@platform_loop`` only picks which
workspaces have work (system scope, ids only), and the work itself runs ``@owned_by("workspace", ...)`` as the
tenant that owns the workspace, so every row it reads or writes is checked by row-level security.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.desk import value
from analystos.desk.models import DeskDaily, Request
from analystos.desk.states import QUEUE_STATES
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
WINDOW_DAYS = 14  # requests close after the day they were created: recompute this many trailing days each night
NIGHTLY_EVERY = timedelta(hours=20)
MAX_ROWS = 50_000  # per workspace window (ADR-014: bounded)
MARKER = "desk.value_rollup"
ROLLUP_FIELDS = ("n", "duplicates", "closed", "accepted", "auto_resolved", "auto_resolution_rate", "accepted_rate",
                 "tta_p50_seconds", "tta_p95_seconds", "human_minutes", "est_minutes_saved", "unbaselined", "cost_usd")


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _day_of(ts: datetime) -> date:
    return (ts if ts.tzinfo is None else ts.astimezone(UTC)).date()


def daily_rows(rows: list[dict[str, Any]], baselines: dict[str, float]) -> list[dict[str, Any]]:
    """Pure: one summary per (day created, class) plus an ``all`` row per day."""
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_day[_day_of(r["created_at"])].append(r)
    out = []
    for day, items in sorted(by_day.items()):
        summary = value.compute(items, baselines)
        out.append({"day": day, "request_class": "all", **{k: summary[k] for k in ROLLUP_FIELDS}})
        for cls, part in summary["by_class"].items():
            out.append({"day": day, "request_class": cls, **{k: part[k] for k in ROLLUP_FIELDS}})
    return out


def rollup_in(session: Session, workspace_id: str, start: date, end: date) -> int:
    """Recompute ``desk_daily`` for days ``start``..``end`` (inclusive) of one workspace in this session."""
    from analystos.desk.service import baselines

    cols = (Request.request_class, Request.state, Request.resolution, Request.created_at, Request.first_answer_at,
            Request.closed_at, Request.human_minutes, Request.cost_usd)
    found = session.execute(select(*cols).where(
        Request.workspace_id == workspace_id, Request.created_at >= _day_start(start),
        Request.created_at < _day_start(end + timedelta(days=1))).limit(MAX_ROWS)).all()
    rows = [{"class": r[0], "state": r[1], "resolution": r[2], "created_at": r[3], "first_answer_at": r[4],
             "closed_at": r[5], "human_minutes": r[6], "cost_usd": r[7]} for r in found]
    computed = daily_rows(rows, baselines(session, workspace_id))
    session.execute(delete(DeskDaily).where(DeskDaily.workspace_id == workspace_id, DeskDaily.day >= start,
                                            DeskDaily.day <= end))
    now = utcnow()
    session.add_all(DeskDaily(id=new_id("dd"), workspace_id=workspace_id, computed_at=now, **r) for r in computed)
    session.flush()
    return len(computed)


@owned_by("workspace", "workspace_id")
def rollup_workspace(workspace_id: str, start: date, end: date) -> int:
    """Tenant work: runs as the tenant owning the workspace (one short transaction)."""
    with session_scope() as s:
        return rollup_in(s, workspace_id, start, end)


def _locked(session: Session, key: str) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"desk:{key}"}))


@platform_loop
def maybe_rollup_nightly(*, now: datetime | None = None, force: bool = False) -> dict[str, Any] | None:
    """Once a day across scheduler processes: claim (advisory lock + an audit marker, one short transaction), pick the
    workspaces with requests in the window, then roll each up as its own tenant. One failure never stops the rest."""
    from analystos.db.models import AuditEvent
    from analystos.governance.audit import audit

    now = now or utcnow()
    end = now.date()
    start = end - timedelta(days=WINDOW_DAYS - 1)
    with session_scope() as s:
        if not _locked(s, "nightly"):
            return None
        last = s.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == MARKER))
        if last is not None and last.tzinfo is None:  # SQLite keeps no zone; the column is UTC
            last = last.replace(tzinfo=UTC)
        if not force and last is not None and now - last < NIGHTLY_EVERY:
            return None
        audit("scheduler", MARKER, decision="allow", details={"from": start.isoformat(), "to": end.isoformat()}, session=s)
        spaces = list(s.scalars(select(Request.workspace_id).where(Request.created_at >= _day_start(start)).distinct()))
    out: dict[str, Any] = {"workspaces": 0, "rows": 0, "failed": []}
    for ws in spaces:
        try:
            out["rows"] += rollup_workspace(ws, start, end)
            out["workspaces"] += 1
        except Exception:  # noqa: BLE001 - one workspace must not stop the others
            log.exception("desk value rollup failed for workspace %s", ws)
            out["failed"].append(ws)
    log.info("desk value rollup: %s", out)
    return out


@owned_by("workspace", "workspace_id")
def sweep_workspace(workspace_id: str, now: datetime | None = None) -> list[str]:
    from analystos.desk.service import sweep_sla

    with session_scope() as s:
        return sweep_sla(s, workspace_id=workspace_id, now=now)


@platform_loop
def sweep_sla_all(now: datetime | None = None, limit: int = 200) -> int:
    """Every scheduler iteration: workspaces with queue requests past their SLA, each swept as its own tenant."""
    now = now or utcnow()
    with session_scope() as s:
        spaces = list(s.scalars(select(Request.workspace_id).where(
            Request.state.in_(sorted(QUEUE_STATES)), Request.sla_due_at.is_not(None), Request.sla_due_at < now,
            Request.sla_breached_at.is_(None)).distinct().limit(limit)))
    flagged = 0
    for ws in spaces:
        try:
            flagged += len(sweep_workspace(ws, now))
        except Exception:  # noqa: BLE001
            log.exception("desk SLA sweep failed for workspace %s", ws)
    return flagged


def daily_series(session: Session, workspace_id: str, start: date, end: date,
                 request_class: str | None = "all") -> list[dict[str, Any]]:
    """Stored rollup rows for the Value screen (trend) and the export, oldest day first."""
    stmt = select(DeskDaily).where(DeskDaily.workspace_id == workspace_id, DeskDaily.day >= start, DeskDaily.day <= end)
    if request_class is not None:
        stmt = stmt.where(DeskDaily.request_class == request_class)
    out = []
    for r in session.scalars(stmt.order_by(DeskDaily.day, DeskDaily.request_class).limit(5000)):
        out.append({"day": r.day.isoformat(), "class": r.request_class, **{k: getattr(r, k) for k in ROLLUP_FIELDS},
                    "computed_at": r.computed_at.isoformat() if r.computed_at else None})
    return out

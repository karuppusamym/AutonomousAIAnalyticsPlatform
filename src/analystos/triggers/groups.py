"""Correlation (spec S09 §3.2): alerts whose `correlation_key` values match (for example the same metric and
processor) join one open alert group. People are notified when an alert group opens, not for every alert in it;
later alerts and suppressed re-entries are counted on the alert group."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Alert, User
from analystos.events.bus import emit
from analystos.governance.policy import require_role
from analystos.triggers.models import AlertGroup

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def correlation_key(workspace_id: str, values: dict[str, Any]) -> str:
    return stable_hash({"workspace_id": workspace_id, "values": values})[:64]


def _open(session: Session, workspace_id: str, key: str) -> AlertGroup | None:
    return session.scalar(select(AlertGroup).where(AlertGroup.workspace_id == workspace_id, AlertGroup.correlation_key == key,
                                                 AlertGroup.status == "open").order_by(AlertGroup.started_at).limit(1)
                          .with_for_update())


def attach(session: Session, alert: Alert, monitor_id: str, values: dict[str, Any]) -> tuple[AlertGroup, bool]:
    """Put `alert` into the open alert group for its key (True when this alert opened it)."""
    key = correlation_key(alert.workspace_id, values)
    now = utcnow()
    inc = _open(session, alert.workspace_id, key)
    if inc is None:
        label = ", ".join(f"{k}={v}" for k, v in values.items())
        inc = AlertGroup(id=new_id("agr"), workspace_id=alert.workspace_id, correlation_key=key, key_values=values,
                       title=f"{alert.title} ({label})"[:300] if label else alert.title, status="open",
                       severity=alert.severity, alert_count=1, suppressed_count=0, first_alert_id=alert.id,
                       monitor_ids=[monitor_id], started_at=now, last_alert_at=now)
        session.add(inc)
        session.flush()
        alert.group_id = inc.id
        emit(alert.workspace_id, "alert_group.opened", {"group_id": inc.id, "alert_id": alert.id, "key": values},
             session=session)
        return inc, True
    inc.alert_count = (inc.alert_count or 0) + 1
    inc.last_alert_at = now
    if SEVERITY_RANK.get(alert.severity, 1) > SEVERITY_RANK.get(inc.severity, 1):
        inc.severity = alert.severity
    if monitor_id not in (inc.monitor_ids or []):
        inc.monitor_ids = [*(inc.monitor_ids or []), monitor_id]
    alert.group_id = inc.id
    emit(alert.workspace_id, "alert_group.merged", {"group_id": inc.id, "alert_id": alert.id, "alerts": inc.alert_count},
         session=session)
    return inc, False


def note_suppressed(session: Session, workspace_id: str, values: dict[str, Any]) -> None:
    inc = _open(session, workspace_id, correlation_key(workspace_id, values))
    if inc is not None:
        inc.suppressed_count = (inc.suppressed_count or 0) + 1


def resolve_quiet(session: Session, workspace_id: str) -> list[str]:
    """An alert group is resolved once none of its alerts is still open."""
    resolved = []
    for inc in session.scalars(select(AlertGroup).where(AlertGroup.workspace_id == workspace_id, AlertGroup.status == "open")):
        still_open = session.scalar(select(Alert.id).where(Alert.group_id == inc.id, Alert.status != "resolved").limit(1))
        if still_open is None:
            inc.status, inc.ended_at = "resolved", utcnow()
            emit(workspace_id, "alert_group.resolved", {"group_id": inc.id}, session=session)
            resolved.append(inc.id)
    return resolved


def out(inc: AlertGroup) -> dict[str, Any]:
    return {c: getattr(inc, c) for c in ("id", "workspace_id", "title", "status", "severity", "key_values", "alert_count",
                                         "suppressed_count", "first_alert_id", "monitor_ids", "started_at", "last_alert_at",
                                         "ended_at")}


def list_groups(session: Session, user: User, workspace_id: str, *, status: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    stmt = select(AlertGroup).where(AlertGroup.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(AlertGroup.status == status)
    return [out(i) for i in session.scalars(stmt.order_by(AlertGroup.started_at.desc()).limit(min(limit, 500)))]

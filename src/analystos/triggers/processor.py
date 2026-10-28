"""Processing accepted trigger events (spec S09-T1/T2): a platform loop picks due events across tenants and
each event runs as the tenant that owns it. `evaluate` rules re-evaluate the monitor now (through the gateway,
like a scheduled evaluation); `observe` rules hand the event's typed payload to the monitor as the reading.
Either way the monitor's anti-noise gate decides whether anything fires."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, select, update

from analystos.core.errors import AnalystOSError
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import Monitor
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.tenancy.background import owned_by, platform_loop
from analystos.triggers.models import ChannelMessage, TriggerEvent, TriggerRule

log = get_logger(__name__)
LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 3


def _claim(event_row_id: str, now: datetime) -> bool:
    with session_scope() as s:
        result = s.execute(update(TriggerEvent).where(
            TriggerEvent.id == event_row_id,
            or_(TriggerEvent.status == "queued", and_(TriggerEvent.status == "processing", TriggerEvent.locked_until < now)))
            .values(status="processing", locked_until=now + LEASE, attempts=TriggerEvent.attempts + 1)
            .execution_options(synchronize_session=False))
        return result.rowcount == 1


def _finish(event_row_id: str, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        ev = s.get(TriggerEvent, event_row_id)
        ev.status, ev.result, ev.error, ev.locked_until, ev.processed_at = status, result or {}, error, None, utcnow()
        emit(ev.workspace_id, "trigger.processed", {"trigger_event_id": ev.id, "rule_id": ev.rule_id, "status": status,
                                                    "alert_id": (result or {}).get("alert_id"), "error": error}, session=s)
        audit(f"trigger:{ev.rule_id}", "trigger.processed", workspace_id=ev.workspace_id, target=ev.id,
              decision="allow" if status == "processed" else None, reasons=[error] if error else [],
              details={"status": status, "gate": (result or {}).get("noise")}, session=s)


@owned_by("trigger_event", "event_row_id")
def process_event(event_row_id: str, *, now: datetime | None = None) -> str | None:
    """Process one event. Returns its new status, or None when another worker holds it. Never raises."""
    from analystos.services.monitors import evaluate_monitor

    now = now or utcnow()
    if not _claim(event_row_id, now):
        return None
    with session_scope() as s:
        ev = s.get(TriggerEvent, event_row_id)
        rule = s.get(TriggerRule, ev.rule_id)
        monitor = s.get(Monitor, rule.monitor_id) if rule is not None else None
        attempts, payload, event_id = ev.attempts, dict(ev.payload or {}), ev.event_id
        action = rule.action if rule is not None else None
        usable = rule is not None and rule.enabled and monitor is not None and monitor.enabled
        rule_id, monitor_id = ev.rule_id, monitor.id if monitor is not None else None
    if not usable:
        _finish(event_row_id, "suppressed", error="the rule or its monitor is disabled or gone")
        return "suppressed"
    try:
        result = evaluate_monitor(monitor_id, trigger=f"trigger:{rule_id}:{event_id}",
                                  observation=payload if action == "observe" else None)
    except AnalystOSError as exc:
        if exc.retryable and attempts < MAX_ATTEMPTS:
            with session_scope() as s:
                ev = s.get(TriggerEvent, event_row_id)
                ev.status, ev.error, ev.locked_until = "queued", exc.message, None
            return "queued"
        _finish(event_row_id, "failed", error=f"{exc.code}: {exc.message}")
        return "failed"
    except Exception as exc:  # recorded, never propagated into the loop
        log.exception("trigger event %s failed", event_row_id)
        _finish(event_row_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return "failed"
    gate = result.get("noise") or {}
    status = "suppressed" if gate.get("action") in ("suppressed", "pending") else "processed"
    _finish(event_row_id, status, {"alert": bool(result.get("alert")), "alert_id": result.get("alert_id"),
                                   "noise": gate or None, "message": result.get("message")})
    return status


@platform_loop
def process_due(*, limit: int = 20, now: datetime | None = None) -> dict[str, int]:
    """Every scheduler iteration: process queued events (and those a crashed worker left leased)."""
    now = now or utcnow()
    with session_scope() as s:
        ids = list(s.scalars(select(TriggerEvent.id).where(or_(
            TriggerEvent.status == "queued",
            and_(TriggerEvent.status == "processing", TriggerEvent.locked_until < now)))
            .order_by(TriggerEvent.received_at).limit(limit)))
    counts: dict[str, int] = {}
    for event_row_id in ids:
        status = process_event(event_row_id, now=now)
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts


@platform_loop
def purge_expired(now: datetime | None = None) -> int:
    """Drop replay-cache entries past their seven days (finished events only) and old inbound message guards."""
    from analystos.core.config import get_settings

    now = now or utcnow()
    with session_scope() as s:
        n = s.execute(delete(TriggerEvent).where(TriggerEvent.expires_at < now,
                                                 TriggerEvent.status.in_(["processed", "suppressed", "failed"])))
        m = s.execute(delete(ChannelMessage).where(
            ChannelMessage.received_at < now - timedelta(days=int(get_settings().trigger_replay_days))))
        return int(n.rowcount or 0) + int(m.rowcount or 0)

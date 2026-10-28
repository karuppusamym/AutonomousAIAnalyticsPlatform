"""Signed webhook ingress (spec S09-T1) and the durable enqueue shared with the Kafka consumer.

`POST /v2/triggers/webhook/{rule_id}` carries `X-AnalystOS-Timestamp` (unix seconds), `X-Event-Id` and
`X-AnalystOS-Signature: sha256=<hex HMAC-SHA256 over "<timestamp>.<raw body>">` with the rule's own secret
(`secret_ref`, resolved per request). A timestamp more than five minutes away is refused, so a captured request
cannot be replayed later; within those five minutes (and for Kafka redeliveries) the replay cache — the unique
`(rule_id, event_id)` on `trigger_event`, kept seven days — recognises a second copy and processes nothing.
The body must satisfy the rule's typed payload schema. 202 is returned only after the event row is committed.

The route has no user session: the rule is looked up in system scope (an id is all the caller has) and every
write after the signature check runs as the tenant that owns the rule. Unknown, disabled and wrongly signed
requests all get the same 401, so the endpoint does not reveal which rule ids exist.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput, Unauthenticated
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.tenancy import system_scope
from analystos.tenancy.background import as_owner_of
from analystos.triggers.models import TriggerEvent, TriggerRule

log = get_logger(__name__)
SIGNATURE_HEADER = "x-analystos-signature"
TIMESTAMP_HEADER = "x-analystos-timestamp"
EVENT_ID_HEADER = "x-event-id"
MIN_SECRET_LENGTH = 16
MAX_EVENT_ID = 200


def _settings():  # noqa: ANN202
    from analystos.core.config import get_settings

    return get_settings()


@dataclass
class Accepted:
    event_id: str
    trigger_event_id: str
    duplicate: bool

    @property
    def status_code(self) -> int:
        return 200 if self.duplicate else 202

    def body(self) -> dict[str, Any]:
        return {"status": "duplicate" if self.duplicate else "accepted", "event_id": self.event_id,
                "trigger_event_id": self.trigger_event_id}


def enqueue(session: Session, rule: TriggerRule, *, event_id: str, payload: dict[str, Any], source: str,
            now: datetime | None = None) -> tuple[TriggerEvent, bool]:
    """Insert the event once per (rule, event id). Returns (row, duplicate). The caller commits; only then
    is the event acknowledged (202 / Kafka offset commit)."""
    now = now or utcnow()
    event_id = str(event_id)[:MAX_EVENT_ID]
    existing = session.scalar(select(TriggerEvent).where(TriggerEvent.rule_id == rule.id, TriggerEvent.event_id == event_id))
    if existing is not None:
        return existing, True
    row = TriggerEvent(id=new_id("tev"), workspace_id=rule.workspace_id, rule_id=rule.id, event_id=event_id,
                       source=source, payload=payload, payload_hash=stable_hash(payload), status="queued", attempts=0,
                       result={}, received_at=now,
                       expires_at=now + timedelta(days=int(_settings().trigger_replay_days)))
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:  # a concurrent copy of the same event won the insert
        return session.scalar(select(TriggerEvent).where(TriggerEvent.rule_id == rule.id,
                                                         TriggerEvent.event_id == event_id)), True
    emit(rule.workspace_id, "trigger.received", {"rule_id": rule.id, "trigger_event_id": row.id, "event_id": event_id,
                                                 "source": source}, session=session)
    audit(f"trigger:{rule.id}", "trigger.received", workspace_id=rule.workspace_id, target=row.id, decision="allow",
          details={"event_id": event_id, "source": source, "payload_hash": row.payload_hash}, session=session)
    return row, False


def _refuse(rule_id: str, reason: str) -> Unauthenticated:
    try:  # recorded for the operator; the caller only learns "not accepted"
        with system_scope(), session_scope() as s:
            audit("anonymous", "trigger.auth_failed", target=rule_id[:40], decision="deny", reasons=[reason], session=s)
    except Exception:  # an audit failure must not turn a 401 into a 500
        log.exception("could not audit a refused trigger")
    return Unauthenticated("the trigger request is not signed for this rule")


def accept_webhook(rule_id: str, headers: dict[str, str], body: bytes, *, now: float | None = None) -> Accepted:
    """Verify, validate and durably enqueue one webhook event. Raises Unauthenticated (401), InvalidInput (422)."""
    import time as _time

    from analystos.connectors.secrets import resolve_secret
    from analystos.services.deliveries import verify_signature

    h = {k.lower(): v for k, v in headers.items()}
    settings = _settings()
    if len(body) > int(settings.trigger_max_body_bytes):
        raise InvalidInput(f"the event body is larger than {settings.trigger_max_body_bytes} bytes")
    with system_scope(), session_scope() as s:  # an id is all an unauthenticated caller has
        rule = s.get(TriggerRule, rule_id)
        secret_ref = rule.secret_ref if rule is not None and rule.enabled and rule.kind == "webhook" else None
    if not secret_ref:
        raise _refuse(rule_id, "unknown, disabled or non-webhook rule")
    try:
        secret = resolve_secret(secret_ref)
    except AnalystOSError:
        secret = None
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise _refuse(rule_id, "the rule's signing secret is not configured")
    timestamp, signature = h.get(TIMESTAMP_HEADER, ""), h.get(SIGNATURE_HEADER, "")
    if not verify_signature(secret, timestamp, body, signature, tolerance_seconds=int(settings.trigger_skew_seconds),
                            now=now if now is not None else _time.time()):
        raise _refuse(rule_id, "bad signature or timestamp outside the allowed skew")
    event_id = (h.get(EVENT_ID_HEADER) or "").strip()
    if not event_id or len(event_id) > MAX_EVENT_ID:
        raise InvalidInput(f"X-Event-Id is required (at most {MAX_EVENT_ID} characters): it is the replay key")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise InvalidInput("the event body must be JSON") from None
    from analystos.triggers.rules import validate_payload

    with as_owner_of("trigger_rule", rule_id), session_scope() as s:
        rule = s.get(TriggerRule, rule_id)
        validate_payload(rule, payload)
        row, duplicate = enqueue(s, rule, event_id=event_id, payload=payload, source="webhook")
        out = Accepted(event_id=event_id, trigger_event_id=row.id, duplicate=duplicate)
    return out

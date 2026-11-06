"""Push subscriptions to the context change feed (review P1-6, spec S03 §5).

The ledger's change stream (``context_ledger_change``) is the source; ``retrieval.changes`` is its pull face, this
module its push face. A ``ContextWebhook`` carries **filters** and a **destination**:

* filters: ``kinds`` (ledger kinds, empty = all), ``changes`` (``create|update|deprecate``, empty = all),
  ``tag_added`` (``pii|sensitive|restricted``: a dataset gained that column tag) and ``schema_changed`` (a dataset's
  structural fingerprint changed). ``kinds`` and ``changes`` narrow; when either signal filter is set the change must
  also raise at least one of them.
* destination: a signed webhook (the same URL guard, HMAC signature and outbound call as ``services/deliveries``), or
  the platform's own *internal* consumer, which on ``schema_changed`` re-checks data quality for the asset (S09).

**Side effects are approvals bound to hashes.** A webhook is standing external egress: creating one, or changing its
filters, URL or secret reference, requests an approval (``context_webhook.authorize``, approver != requester) whose
payload is the filters + destination, and every send calls ``verify_for_execution`` immediately before it.

**Metadata only.** A delivery carries ids, kind, change, version and the two signals, never a title, summary or body:
a destination's owner may not hold the access that reads them. Personal and suggested entries are not announced.

Delivery is at least once with an idempotency key; retries are bounded exponential backoff ending in ``dead_letter``;
``context_webhook_delivery`` is the log. ``dispatch_due`` is the scheduler's platform loop, ``scan``/``attempt`` run as
the owning tenant (``@owned_by``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analystos.context.models import ContextLedgerChange, ContextLedgerEntry, ContextWebhook, ContextWebhookDelivery
from analystos.core.errors import (
    AnalystOSError,
    ApprovalRequired,
    Conflict,
    InvalidInput,
    NotFound,
    PolicyDenied,
)
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import Approval, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_in_workspace, require_role, scoped_loader
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)

APPROVAL_ACTION = "context_webhook.authorize"
CHANGES = {"create": "added", "update": "changed", "deprecate": "retired"}  # API word -> ledger op
OPS = {v: k for k, v in CHANGES.items()}
TAGS = ("pii", "sensitive", "restricted")
HIDDEN_TRUSTS = ("personal", "suggested")  # never announced: the existence of someone's note is theirs
EDITOR_ROLE = "editor"
SCAN_LIMIT = 200
LEASE = timedelta(minutes=5)
INTERNAL_NAME = "internal: re-check data quality on schema change"
REVIEW_SIGNALS = ("tag_added", "schema_changed")


# ------------------------------------------------------------------------------------------------ filters
def normalize_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """The canonical filter set the approval binds to; unknown keys and values are refused."""
    from analystos.context.ledger import KINDS

    f = dict(filters or {})
    extra = set(f) - {"kinds", "changes", "tag_added", "schema_changed"}
    if extra:
        raise InvalidInput(f"unknown filter fields: {sorted(extra)}")

    def pick(key: str, allowed: tuple[str, ...]) -> list[str]:
        raw = f.get(key) or []
        if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
            raise InvalidInput(f"filters.{key} must be a list of strings")
        bad = sorted({v.lower() for v in raw} - set(allowed))
        if bad:
            raise InvalidInput(f"filters.{key}: {bad} not within {list(allowed)}")
        return sorted({v.lower() for v in raw})

    schema = f.get("schema_changed", False)
    if not isinstance(schema, bool):
        raise InvalidInput("filters.schema_changed must be true or false")
    return {"kinds": pick("kinds", KINDS), "changes": pick("changes", tuple(CHANGES)), "tag_added": pick("tag_added", TAGS),
            "schema_changed": schema}


def signals_of(change: ContextLedgerChange, entry: ContextLedgerEntry, previous: ContextLedgerEntry | None) -> dict[str, Any]:
    """What a dataset change raises, from the version's provenance (the ledger records the structural fingerprint and the
    sensitive column tags): tags the new version has and the previous one lacked, and a changed structure."""
    if entry.kind != "dataset":
        return {"tag_added": [], "schema_changed": False}
    now_tags = set((entry.provenance or {}).get("tags") or [])
    before = set((previous.provenance or {}).get("tags") or []) if previous is not None else set()
    struct_now = (entry.provenance or {}).get("structure")
    struct_then = (previous.provenance or {}).get("structure") if previous is not None else None
    return {"tag_added": sorted(now_tags - before),
            "schema_changed": bool(change.op == "changed" and struct_now and struct_then and struct_now != struct_then)}


def matches(filters: dict[str, Any], kind: str, op: str, signals: dict[str, Any]) -> bool:
    """Whether a ledger change passes a subscription's filters (pure; the tests' main subject)."""
    if filters.get("kinds") and kind not in filters["kinds"]:
        return False
    if filters.get("changes") and OPS.get(op) not in filters["changes"]:
        return False
    wanted_tags, wants_schema = set(filters.get("tag_added") or []), bool(filters.get("schema_changed"))
    if not wanted_tags and not wants_schema:
        return True
    return bool(wanted_tags & set(signals.get("tag_added") or [])) or (wants_schema and bool(signals.get("schema_changed")))


# ------------------------------------------------------------------------------------------------ destination and hash
def destination_hash(workspace_id: str, kind: str, filters: dict[str, Any], url: str | None, secret_ref: str | None) -> str:
    return stable_hash({"workspace_id": workspace_id, "kind": kind, "filters": filters, "url": url, "secret_ref": secret_ref})


def authorization_payload(hook: ContextWebhook) -> dict[str, Any]:
    """What an approver authorizes, rebuilt from the row's *current* values at every send: an edit that bypassed the
    API changes the payload hash and ``verify_for_execution`` refuses."""
    return {"webhook_id": hook.id, "workspace_id": hook.workspace_id, "kind": hook.kind, "filters": hook.filters,
            "url": hook.url, "secret_ref": hook.secret_ref,
            "destination_hash": destination_hash(hook.workspace_id, hook.kind, hook.filters, hook.url, hook.secret_ref)}


def describe(hook: ContextWebhook) -> str:
    f = hook.filters or {}
    parts = [f"kinds {','.join(f.get('kinds') or []) or 'all'}", f"changes {','.join(f.get('changes') or []) or 'all'}"]
    if f.get("tag_added"):
        parts.append(f"tag added {','.join(f['tag_added'])}")
    if f.get("schema_changed"):
        parts.append("schema changed")
    return f"POST ledger change metadata to {hook.url} when " + "; ".join(parts)


def _normalize_destination(url: str, secret_ref: str) -> tuple[str, str]:
    from analystos.services.deliveries import normalize_config

    cfg = normalize_config("webhook", {"url": url, "secret_ref": secret_ref})
    return cfg["url"], cfg["secret_ref"]


def is_authorized(hook: ContextWebhook, now: datetime | None = None) -> bool:
    if hook.kind == "internal":
        return hook.status == "authorized"
    until = hook.authorized_until
    if until is not None and until.tzinfo is None:  # SQLite drops the zone
        from datetime import UTC

        until = until.replace(tzinfo=UTC)
    return (hook.status == "authorized" and hook.authorized_hash == hook.destination_hash
            and until is not None and until > (now or utcnow()))


# ------------------------------------------------------------------------------------------------ lifecycle
@scoped_loader
def load(session: Session, user: User, webhook_id: str, role: str = EDITOR_ROLE) -> ContextWebhook:
    return load_in_workspace(session, ContextWebhook, webhook_id, user=user, minimum=role, label="context webhook")


def _invalidate(session: Session, approval_id: str | None, reason: str) -> None:
    if approval_id:
        session.execute(update(Approval).where(Approval.id == approval_id, Approval.status.in_(["pending", "approved"]))
                        .values(status="invalidated", reason=reason))


def _request_authorization(session: Session, user: User, hook: ContextWebhook) -> Approval:
    from analystos.governance.approvals import request_approval

    ws = get_workspace(session, hook.workspace_id)
    _invalidate(session, hook.approval_id, "subscription changed; a new authorization was requested")
    approval = request_approval(session, workspace_id=hook.workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=authorization_payload(hook), plan_hash=None, policy_version=ws.policy_version,
                                requested_by=user.id, risk_tier="high", destination=f"context-webhook:{hook.name}"[:120],
                                affected_assets=[], evidence={"summary": describe(hook), "filters": hook.filters})
    hook.approval_id, hook.status, hook.authorized_hash, hook.authorized_until = approval.id, "pending", None, None
    emit(hook.workspace_id, "context.webhook_requested", {"webhook_id": hook.id, "approval_id": approval.id,
                                                          "destination_hash": hook.destination_hash},
         actor=f"user:{user.id}", session=session)
    return approval


def create(session: Session, user: User, workspace_id: str, *, name: str, url: str, secret_ref: str,
           filters: dict[str, Any] | None = None) -> ContextWebhook:
    require_role(session, user, workspace_id, EDITOR_ROLE)
    name = (name or "").strip()
    if not name:
        raise InvalidInput("a subscription needs a name")
    if session.scalar(select(ContextWebhook.id).where(ContextWebhook.workspace_id == workspace_id, ContextWebhook.name == name)):
        raise Conflict(f"a subscription named {name!r} already exists in this workspace")
    flt = normalize_filters(filters)
    url, secret_ref = _normalize_destination(url, secret_ref)
    hook = ContextWebhook(id=new_id("cwh"), workspace_id=workspace_id, name=name[:200], kind="webhook", url=url,
                          secret_ref=secret_ref, filters=flt, status="pending", created_by=user.id, revision=1,
                          destination_hash=destination_hash(workspace_id, "webhook", flt, url, secret_ref))
    session.add(hook)
    session.flush()
    _request_authorization(session, user, hook)
    audit(f"user:{user.id}", "context.webhook_created", workspace_id=workspace_id, target=hook.id,
          decision="approval_required", details={"destination_hash": hook.destination_hash}, session=session)
    return hook


def update_hook(session: Session, user: User, hook: ContextWebhook, *, name: str | None = None, url: str | None = None,
                secret_ref: str | None = None, filters: dict[str, Any] | None = None) -> ContextWebhook:
    """A rename keeps the authorization; changed filters, URL or secret reference need a new one (the hash moves)."""
    require_role(session, user, hook.workspace_id, EDITOR_ROLE)
    if hook.kind == "internal":
        raise Conflict("an internal consumer is defined by the platform")
    if hook.status == "revoked":
        raise Conflict("a revoked subscription cannot be edited; create a new one")
    if name and name.strip():
        hook.name = name.strip()[:200]
    flt = normalize_filters(filters) if filters is not None else hook.filters
    new_url, new_ref = _normalize_destination(url or hook.url or "", secret_ref or hook.secret_ref or "")
    new_hash = destination_hash(hook.workspace_id, hook.kind, flt, new_url, new_ref)
    changed = new_hash != hook.destination_hash
    if changed:
        hook.filters, hook.url, hook.secret_ref, hook.destination_hash = flt, new_url, new_ref, new_hash
        session.flush()
        _request_authorization(session, user, hook)
        _refuse_open(session, hook, "the subscription changed after this delivery was queued")
    hook.revision = (hook.revision or 1) + 1
    audit(f"user:{user.id}", "context.webhook_updated", workspace_id=hook.workspace_id, target=hook.id,
          decision="approval_required" if changed else None,
          details={"destination_hash": hook.destination_hash, "reauthorization": changed}, session=session)
    return hook


def revoke(session: Session, user: User, hook: ContextWebhook, reason: str | None = None) -> ContextWebhook:
    require_role(session, user, hook.workspace_id, EDITOR_ROLE)
    if hook.kind == "internal":
        raise Conflict("an internal consumer is defined by the platform")
    _invalidate(session, hook.approval_id, "subscription revoked")
    hook.status, hook.authorized_hash, hook.authorized_until = "revoked", None, None
    hook.revision = (hook.revision or 1) + 1
    refused = _refuse_open(session, hook, "the subscription was revoked")
    audit(f"user:{user.id}", "context.webhook_revoked", workspace_id=hook.workspace_id, target=hook.id,
          reasons=[reason or ""], details={"refused_deliveries": refused}, session=session)
    return hook


def reauthorize(session: Session, user: User, hook: ContextWebhook) -> ContextWebhook:
    require_role(session, user, hook.workspace_id, EDITOR_ROLE)
    if hook.kind == "internal" or hook.status == "revoked":
        raise Conflict("this subscription cannot be re-authorized")
    if is_authorized(hook):
        raise Conflict("the subscription is already authorized")
    _request_authorization(session, user, hook)
    hook.revision = (hook.revision or 1) + 1
    return hook


def apply_decision(session: Session, approval: Approval) -> ContextWebhook | None:
    """After an approver decides an authorization (api/routers/artifacts._decide). Approval starts the feed from *now*
    (cursor = the ledger's current head) the first time; after a lapse it resumes where it stopped."""
    hook = session.scalar(select(ContextWebhook).where(ContextWebhook.approval_id == approval.id))
    if hook is None or approval.action != APPROVAL_ACTION:
        return None
    if approval.status == "approved":
        if stable_hash(authorization_payload(hook)) != approval.payload_hash:
            _invalidate(session, approval.id, "subscription changed while the authorization was pending")
            raise Conflict("the subscription changed while its authorization was pending; a new approval is required")
        from analystos.core.config import get_settings

        until = utcnow() + timedelta(days=int(get_settings().delivery_authorization_days))
        hook.status, hook.authorized_hash, hook.authorized_until = "authorized", hook.destination_hash, until
        if hook.cursor is None:
            hook.cursor = _head(session, hook.workspace_id)
        session.execute(update(Approval).where(Approval.id == approval.id).values(expires_at=until))
        session.expire(approval)
        emit(hook.workspace_id, "context.webhook_authorized", {"webhook_id": hook.id, "approval_id": approval.id},
             actor=f"user:{approval.decided_by}", session=session)
    elif approval.status == "rejected":
        hook.status = "rejected"
    return hook


def _head(session: Session, workspace_id: str) -> int:
    return int(session.scalar(select(func.max(ContextLedgerChange.seq)).where(ContextLedgerChange.workspace_id == workspace_id)) or 0)


def _refuse_open(session: Session, hook: ContextWebhook, reason: str) -> int:
    result = session.execute(update(ContextWebhookDelivery).where(
        ContextWebhookDelivery.webhook_id == hook.id, ContextWebhookDelivery.status.in_(["queued", "retrying"]))
        .values(status="refused", last_error=reason, next_attempt_at=None))
    return result.rowcount or 0


def as_dict(hook: ContextWebhook) -> dict[str, Any]:
    return {"id": hook.id, "workspace_id": hook.workspace_id, "name": hook.name, "kind": hook.kind, "url": hook.url,
            "has_secret": bool(hook.secret_ref), "filters": hook.filters, "status": hook.status,
            "authorized": is_authorized(hook), "authorized_until": hook.authorized_until.isoformat() if hook.authorized_until else None,
            "approval_id": hook.approval_id, "destination_hash": hook.destination_hash, "cursor": hook.cursor,
            "revision": hook.revision, "summary": "internal: re-check data quality when a dataset's schema changes"
            if hook.kind == "internal" else describe(hook),
            "created_at": hook.created_at.isoformat() if hook.created_at else None}


def list_hooks(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    return [as_dict(h) for h in session.scalars(select(ContextWebhook).where(ContextWebhook.workspace_id == workspace_id)
                                                .order_by(ContextWebhook.created_at, ContextWebhook.id))]


def delivery_dict(d: ContextWebhookDelivery) -> dict[str, Any]:
    return {"id": d.id, "webhook_id": d.webhook_id, "seq": d.seq, "entry_id": d.entry_id, "status": d.status,
            "attempts": d.attempts, "max_attempts": d.max_attempts, "last_error": d.last_error,
            "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at else None,
            "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
            "created_at": d.created_at.isoformat() if d.created_at else None, "payload": d.payload}


def list_deliveries(session: Session, user: User, workspace_id: str, *, webhook_id: str | None = None,
                    status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    stmt = select(ContextWebhookDelivery).where(ContextWebhookDelivery.workspace_id == workspace_id)
    if webhook_id:
        stmt = stmt.where(ContextWebhookDelivery.webhook_id == webhook_id)
    if status:
        stmt = stmt.where(ContextWebhookDelivery.status == status)
    return [delivery_dict(d) for d in session.scalars(stmt.order_by(ContextWebhookDelivery.created_at.desc(),
                                                                    ContextWebhookDelivery.seq.desc()).limit(max(1, min(limit, 500))))]


@scoped_loader
def load_delivery(session: Session, user: User, delivery_id: str, role: str = EDITOR_ROLE) -> ContextWebhookDelivery:
    return load_in_workspace(session, ContextWebhookDelivery, delivery_id, user=user, minimum=role, label="context webhook delivery")


def redrive(session: Session, user: User, d: ContextWebhookDelivery) -> dict[str, Any]:
    """An editor re-queues a dead-lettered or refused delivery; every governance check runs again at send."""
    require_role(session, user, d.workspace_id, EDITOR_ROLE)
    if d.status not in ("dead_letter", "refused"):
        raise Conflict(f"only a dead-lettered or refused delivery can be redriven (this one is {d.status})")
    d.status, d.attempts, d.next_attempt_at, d.locked_until, d.last_error = "queued", 0, utcnow(), None, None
    audit(f"user:{user.id}", "context.webhook_delivery_redriven", workspace_id=d.workspace_id, target=d.id, session=session)
    return delivery_dict(d)


# ------------------------------------------------------------------------------------------------ scanning the feed
def payload_for(change: ContextLedgerChange, entry: ContextLedgerEntry, signals: dict[str, Any], delivery_id: str,
                key: str) -> dict[str, Any]:
    """Ledger metadata only: no title, summary, trust, refs or provenance."""
    return {"type": "analystos.context.change", "version": 1, "delivery_id": delivery_id, "idempotency_key": key,
            "workspace_id": change.workspace_id,
            "change": {"seq": change.seq, "entry_id": change.entry_id, "kind": change.kind, "change": OPS.get(change.op, change.op),
                       "version": change.version, "at": change.at.isoformat() if change.at else None},
            "signals": {"tag_added": signals.get("tag_added", []), "schema_changed": bool(signals.get("schema_changed"))},
            "ref": {"table": entry.ref_table, "id": entry.ref_id}}


@owned_by("context_webhook", "webhook_id")
def scan(webhook_id: str, *, limit: int = SCAN_LIMIT) -> int:
    """Read the ledger past the subscription's cursor, queue a delivery for every change that passes its filters, and
    advance the cursor past everything read. A subscription that is not authorized reads nothing."""
    from analystos.core.config import get_settings

    queued = 0
    with session_scope() as s:
        hook = s.get(ContextWebhook, webhook_id)
        if hook is None or not is_authorized(hook) or hook.cursor is None:
            return 0
        rows = s.execute(select(ContextLedgerChange, ContextLedgerEntry)
                         .join(ContextLedgerEntry, ContextLedgerEntry.id == ContextLedgerChange.entry_id)
                         .where(ContextLedgerChange.workspace_id == hook.workspace_id, ContextLedgerChange.seq > hook.cursor)
                         .order_by(ContextLedgerChange.seq).limit(limit)).all()
        for change, entry in rows:
            hook.cursor = change.seq
            if entry.trust in HIDDEN_TRUSTS:
                continue
            previous = None
            if entry.kind == "dataset" and entry.version > 1:
                previous = s.scalar(select(ContextLedgerEntry).where(
                    ContextLedgerEntry.workspace_id == entry.workspace_id, ContextLedgerEntry.ref_table == entry.ref_table,
                    ContextLedgerEntry.ref_id == entry.ref_id, ContextLedgerEntry.version == entry.version - 1))
            signals = signals_of(change, entry, previous)
            if not matches(hook.filters or {}, change.kind, change.op, signals):
                continue
            key = stable_hash({"webhook_id": hook.id, "seq": change.seq})
            did = new_id("cwd")
            d = ContextWebhookDelivery(id=did, workspace_id=hook.workspace_id, webhook_id=hook.id, seq=change.seq,
                                       entry_id=change.entry_id, destination_hash=hook.destination_hash,
                                       payload=payload_for(change, entry, signals, did, key), idempotency_key=key,
                                       status="queued", attempts=0, max_attempts=int(get_settings().delivery_max_attempts),
                                       next_attempt_at=utcnow(), response={})
            try:
                with s.begin_nested():
                    s.add(d)
                queued += 1
            except IntegrityError:  # the same change was queued before (a replay): at most once per (subscription, seq)
                continue
    return queued


# ------------------------------------------------------------------------------------------------ sending
def backoff(attempts: int) -> timedelta:
    from analystos.services.deliveries import backoff as delivery_backoff

    return delivery_backoff(attempts)


def _claim(delivery_id: str, now: datetime) -> bool:
    with session_scope() as s:
        result = s.execute(update(ContextWebhookDelivery).where(
            ContextWebhookDelivery.id == delivery_id,
            or_(and_(ContextWebhookDelivery.status.in_(["queued", "retrying"]), ContextWebhookDelivery.next_attempt_at <= now),
                and_(ContextWebhookDelivery.status == "sending", ContextWebhookDelivery.locked_until < now)))
            .values(status="sending", locked_until=now + LEASE, attempts=ContextWebhookDelivery.attempts + 1)
            .execution_options(synchronize_session=False))
        return result.rowcount == 1


def _authorize(session: Session, d: ContextWebhookDelivery, hook: ContextWebhook | None) -> Approval | None:
    """Immediately before the side effect (CLAUDE.md rule 6): the approval still binds exactly this subscription."""
    from analystos.governance.approvals import verify_for_execution
    from analystos.services.deliveries import normalize_config

    if hook is None or hook.status == "revoked":
        raise PolicyDenied("the subscription was removed or revoked")
    if hook.destination_hash != d.destination_hash:
        raise ApprovalRequired("the subscription changed after this delivery was queued")
    if hook.kind == "internal":
        return None
    if not is_authorized(hook):
        raise ApprovalRequired(f"the subscription is not authorized ({hook.status}); an approver must authorize it")
    try:  # the operator's current egress policy applies at send, not only at registration
        normalize_config("webhook", {"url": hook.url, "secret_ref": hook.secret_ref})
    except InvalidInput as exc:
        raise PolicyDenied(f"the destination no longer satisfies the egress policy: {exc.message}") from None
    try:
        return verify_for_execution(session, hook.approval_id or "", payload=authorization_payload(hook), plan_hash=None)
    except ApprovalRequired:
        hook.status, hook.authorized_hash, hook.authorized_until = "lapsed", None, None
        raise


def _send_internal(session: Session, d: ContextWebhookDelivery) -> dict[str, Any]:
    """Internal consumer (S09): a dataset's schema changed, so re-evaluate the workspace's enabled data-quality
    monitors that name the asset. The monitor's own owner, scope and anti-noise gate decide what fires."""
    from analystos.db.models import Monitor, SourceAsset

    if not (d.payload.get("signals") or {}).get("schema_changed"):
        return {"monitors": []}
    ref = d.payload.get("ref") or {}
    asset = session.get(SourceAsset, ref.get("id")) if ref.get("table") == "source_asset" else None
    if asset is None or asset.workspace_id != d.workspace_id:
        return {"monitors": [], "note": "asset gone"}
    fq = f"{asset.schema_name}.{asset.name}"
    ids = [m.id for m in session.scalars(select(Monitor).where(Monitor.workspace_id == d.workspace_id, Monitor.enabled.is_(True),
                                                               Monitor.kind == "data_quality"))
           if fq in (m.config or {}).get("assets", [])]
    return {"asset": fq, "monitors": ids}


def _run_monitors(ids: list[str]) -> None:
    from analystos.services.monitors import evaluate_monitor

    for monitor_id in ids:
        evaluate_monitor(monitor_id, trigger="schema_changed")


@owned_by("context_webhook_delivery", "delivery_id")
def attempt(delivery_id: str, *, transport: Any | None = None, now: datetime | None = None) -> str | None:
    """One send attempt. Returns the new status, or None when another sender holds the row. Never raises."""
    from analystos.services.deliveries import OutboundMessage, WebhookTransport
    from analystos.services.deliveries import _permanent as permanent

    now = now or utcnow()
    if not _claim(delivery_id, now):
        return None
    followup: list[str] = []
    with session_scope() as s:
        d = s.get(ContextWebhookDelivery, delivery_id)
        hook = s.get(ContextWebhook, d.webhook_id)
        try:
            approval = _authorize(s, d, hook)
            if hook.kind == "internal":
                response = _send_internal(s, d)
                followup = response.get("monitors", [])
            else:
                message = OutboundMessage(subject="context change", text="", body=d.payload, idempotency_key=d.idempotency_key,
                                          delivery_id=d.id)
                response = (transport or WebhookTransport()).send({"url": hook.url, "secret_ref": hook.secret_ref}, message)
        except (ApprovalRequired, PolicyDenied, NotFound) as exc:
            _close(s, d, "refused", exc.message)
            return d.status
        except AnalystOSError as exc:
            if permanent(exc) or d.attempts >= d.max_attempts:
                _close(s, d, "dead_letter", exc.message)
            else:
                _retry(s, d, exc.message, now)
            return d.status
        except Exception as exc:  # an unexpected transport bug is retried like a transient failure, bounded
            log.exception("context webhook delivery %s failed unexpectedly", delivery_id)
            if d.attempts >= d.max_attempts:
                _close(s, d, "dead_letter", f"unexpected error: {type(exc).__name__}")
            else:
                _retry(s, d, f"unexpected error: {type(exc).__name__}", now)
            return d.status
        d.status, d.delivered_at, d.locked_until, d.next_attempt_at = "delivered", utcnow(), None, None
        d.last_error, d.response = None, {**(d.response or {}), **response}
        emit(d.workspace_id, "context.webhook_delivered", {"delivery_id": d.id, "webhook_id": d.webhook_id, "seq": d.seq,
                                                           "attempt": d.attempts}, session=s)
        audit("system:context_webhook", "context.webhook_delivered", workspace_id=d.workspace_id, target=d.id, decision="allow",
              details={"webhook_id": d.webhook_id, "destination_hash": d.destination_hash,
                       "approval_id": approval.id if approval else None, "seq": d.seq, "attempt": d.attempts}, session=s)
        status = d.status
    if followup:  # after the delivery is recorded: a monitor failure never re-sends or loses the record
        try:
            _run_monitors(followup)
        except Exception:  # noqa: BLE001 - the monitor records its own failure; the feed keeps moving
            log.exception("context webhook: re-check after schema change failed")
    return status


def _retry(session: Session, d: ContextWebhookDelivery, error: str, now: datetime) -> None:
    d.status, d.last_error, d.locked_until = "retrying", error, None
    d.next_attempt_at = now + backoff(d.attempts)
    emit(d.workspace_id, "context.webhook_retrying", {"delivery_id": d.id, "attempt": d.attempts,
                                                      "next_attempt_at": d.next_attempt_at.isoformat(), "error": error},
         session=session)


def _close(session: Session, d: ContextWebhookDelivery, status: str, error: str) -> None:
    d.status, d.last_error, d.locked_until, d.next_attempt_at = status, error, None, None
    event = "context.webhook_dead_lettered" if status == "dead_letter" else "context.webhook_refused"
    emit(d.workspace_id, event, {"delivery_id": d.id, "webhook_id": d.webhook_id, "attempts": d.attempts, "error": error},
         session=session)
    audit("system:context_webhook", event, workspace_id=d.workspace_id, target=d.id,
          decision="deny" if status == "refused" else None, reasons=[error],
          details={"attempts": d.attempts, "webhook_id": d.webhook_id}, session=session)


# ------------------------------------------------------------------------------------------------ the scheduler loop
@platform_loop
def ensure_internal(*, limit: int = 50) -> int:
    """One internal consumer per workspace that has dataset context, starting from the current head (no replay)."""
    made = 0
    with session_scope() as s:
        have = select(ContextWebhook.workspace_id).where(ContextWebhook.kind == "internal")
        wss = list(s.scalars(select(ContextLedgerChange.workspace_id).where(ContextLedgerChange.kind == "dataset")
                             .where(ContextLedgerChange.workspace_id.not_in(have)).distinct().limit(limit)))
        for ws in wss:
            s.add(ContextWebhook(id=new_id("cwh"), workspace_id=ws, name=INTERNAL_NAME, kind="internal",
                                 filters={"kinds": ["dataset"], "changes": ["update"], "tag_added": [], "schema_changed": True},
                                 destination_hash=stable_hash({"internal": "reprofile", "workspace_id": ws}), status="authorized",
                                 cursor=_head(s, ws), created_by="system"))
            made += 1
    return made


@platform_loop
def dispatch_due(*, limit: int = 50, transport: Any | None = None, now: datetime | None = None) -> dict[str, int]:
    """Scheduler iteration: queue what the feed produced for each authorized subscription, then attempt what is due.
    Bookkeeping only here; scanning and sending run as the owning tenant."""
    now = now or utcnow()
    out = {"internal_created": ensure_internal(), "queued": 0}
    with session_scope() as s:
        hooks = list(s.scalars(select(ContextWebhook.id).where(ContextWebhook.status == "authorized",
                                                               ContextWebhook.cursor.is_not(None)).limit(500)))
    for hook_id in hooks:
        try:
            out["queued"] += scan(hook_id)
        except Exception:  # noqa: BLE001 - one subscription never stops the others
            log.exception("context webhook scan of %s failed", hook_id)
    due_at = max(now, utcnow())  # what the scans just queued is due now
    with session_scope() as s:
        due = list(s.scalars(select(ContextWebhookDelivery.id).where(or_(
            and_(ContextWebhookDelivery.status.in_(["queued", "retrying"]), ContextWebhookDelivery.next_attempt_at <= due_at),
            and_(ContextWebhookDelivery.status == "sending", ContextWebhookDelivery.locked_until < due_at)))
            .order_by(ContextWebhookDelivery.next_attempt_at).limit(limit)))
    for delivery_id in due:
        status = attempt(delivery_id, transport=transport, now=due_at)
        if status:
            out[status] = out.get(status, 0) + 1
    return out

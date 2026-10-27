"""Retention, deletion and legal hold for conversations (spec S04 §4.5).

Per-tenant policy in ``tenant.settings["conversations"]``: messages are kept ``messages_days`` (365), result
payloads ``results_days`` (30: the rows inside a stored answer are cleared, its query id and result hash stay, as a
result handle would expire), summaries are kept. Personal memory expires after its own TTL (180 days). A
conversation under legal hold is skipped by all of it, and by deletion requests.

``purge_conversation`` is the deletion cascade: messages, state versions, summaries, session and personal
corrections, citation sets of its messages, and every memory entry (with its embedding) derived from it. A team
correction that was proposed from it stays with the workspace's knowledge review, detached from the conversation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AskThread, AskTurn, Tenant, TenantMember, User
from analystos.governance.audit import audit
from analystos.tenancy.background import platform_loop

log = get_logger(__name__)
DEFAULTS = {"messages_days": 365, "results_days": 30}
LIMITS = {"messages_days": (1, 3650), "results_days": (1, 3650)}
SETTINGS_KEY = "conversations"
BATCH = 500


def policy_of(tenant: Tenant | None) -> dict[str, Any]:
    stored = ((tenant.settings or {}).get(SETTINGS_KEY) or {}) if tenant is not None else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if isinstance(stored.get(k), int) and LIMITS[k][0] <= stored[k] <= LIMITS[k][1]:
            out[k] = stored[k]
    return {**out, "summaries": "keep", "personal_memory_days": 180}


def get_policy(tenant_id: str) -> dict[str, Any]:
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        return {"tenant_id": tenant_id, **policy_of(s.get(Tenant, tenant_id))}


def set_policy(user: User, tenant_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """A tenant admin sets the tenant's retention. The app role cannot write ``tenant`` rows (RLS), so the
    authorised change is applied in system scope and audited."""
    from analystos.tenancy import system_scope

    bad = sorted(set(changes) - set(DEFAULTS))
    if bad:
        raise InvalidInput(f"unknown retention settings: {', '.join(bad)}")
    for k, v in changes.items():
        lo, hi = LIMITS[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise InvalidInput(f"{k} must be a whole number of days between {lo} and {hi}")
    with session_scope() as s:
        if not user.is_admin:
            role = s.scalar(select(TenantMember.role).where(TenantMember.tenant_id == tenant_id,
                                                            TenantMember.user_id == user.id))
            if role != "tenant_admin":
                raise Forbidden("conversation retention is set by a tenant admin")
    with system_scope(), session_scope() as s:
        tenant = s.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFound("tenant not found")
        settings = dict(tenant.settings or {})
        settings[SETTINGS_KEY] = {**(settings.get(SETTINGS_KEY) or {}), **changes}
        tenant.settings = settings
        audit(f"user:{user.id}", "conversation.retention_policy_set", target=f"tenant:{tenant_id}", decision="allow",
              details={"changes": changes}, session=s)
        return {"tenant_id": tenant_id, **policy_of(tenant)}


# ------------------------------------------------------------------------------------ deletion cascade
def _drop_messages(session: Session, turn_ids: list[str]) -> int:
    from analystos.db.models import EvidenceCitationSet

    if not turn_ids:
        return 0
    session.execute(delete(EvidenceCitationSet).where(EvidenceCitationSet.subject_type == "ask_turn",
                                                      EvidenceCitationSet.subject_id.in_(turn_ids)))
    return int(session.execute(delete(AskTurn).where(AskTurn.id.in_(turn_ids))).rowcount or 0)


def purge_conversation(session: Session, conv: AskThread) -> dict[str, Any]:
    """Delete the conversation and everything derived from it; returns counts (the tombstone's content)."""
    from analystos.conversations.ledger import get_ledger
    from analystos.conversations.models import (
        ConversationCorrection,
        ConversationMemoryEntry,
        ConversationState,
        ConversationSummary,
    )
    from analystos.conversations.service import detach_request

    cid, ws, owner = conv.id, conv.workspace_id, conv.user_id
    kept = list(session.scalars(select(ConversationCorrection.id).where(
        ConversationCorrection.conversation_id == cid, ConversationCorrection.scope == "workspace_proposed")))
    if kept:  # team knowledge under review outlives the conversation, without a link back to it
        session.execute(update(ConversationCorrection).where(ConversationCorrection.id.in_(kept))
                        .values(conversation_id=None, message_id=None))
        session.execute(update(ConversationMemoryEntry).where(ConversationMemoryEntry.ref_table == "conversation_correction",
                                                              ConversationMemoryEntry.ref_id.in_(kept))
                        .values(conversation_id=None))
    embeddings = int(session.scalar(select(func.count()).select_from(ConversationMemoryEntry).where(
        ConversationMemoryEntry.conversation_id == cid, ConversationMemoryEntry.embedding.is_not(None))) or 0)
    memory = get_ledger().remove_for_conversation(session, cid)
    corrections = int(session.execute(delete(ConversationCorrection).where(
        ConversationCorrection.conversation_id == cid)).rowcount or 0)
    summaries = int(session.execute(delete(ConversationSummary).where(ConversationSummary.conversation_id == cid))
                    .rowcount or 0)
    states = int(session.execute(delete(ConversationState).where(ConversationState.conversation_id == cid)).rowcount or 0)
    turn_ids = list(session.scalars(select(AskTurn.id).where(AskTurn.thread_id == cid)))
    messages = _drop_messages(session, turn_ids)
    branches = int(session.execute(update(AskThread).where(AskThread.parent_conversation_id == cid)
                                   .values(parent_conversation_id=None)).rowcount or 0)
    detach_request(session, cid)
    session.execute(delete(AskThread).where(AskThread.id == cid))
    session.flush()
    return {"workspace_id": ws, "owner_id": owner, "messages": messages, "states": states, "summaries": summaries,
            "corrections": corrections, "memory_entries": memory, "embeddings": embeddings,
            "team_corrections_kept": len(kept), "branches_detached": branches}


# ------------------------------------------------------------------------------------ retention sweep
def _held(session: Session) -> Any:
    return select(AskThread.id).where(AskThread.legal_hold.is_(True))


def apply_tenant(session: Session, tenant: Tenant | None, now: datetime) -> dict[str, int]:
    """One tenant's retention in the current transaction (bound to that tenant by the caller)."""
    from analystos.conversations.ledger import get_ledger
    from analystos.conversations.models import ConversationCorrection as C

    pol = policy_of(tenant)
    held = _held(session)
    old = list(session.scalars(select(AskTurn.id).where(
        AskTurn.created_at < now - timedelta(days=pol["messages_days"]), AskTurn.thread_id.not_in(held)).limit(BATCH)))
    messages = _drop_messages(session, old)
    results = 0
    for turn in session.scalars(select(AskTurn).where(
            AskTurn.created_at < now - timedelta(days=pol["results_days"]), AskTurn.thread_id.not_in(held),
            AskTurn.result.is_not(None)).limit(BATCH)):
        result = turn.result or {}
        if result.get("expired"):
            continue
        turn.result = {k: result.get(k) for k in ("query_id", "columns", "row_count", "result_hash", "referenced_assets")}
        turn.result["rows"], turn.result["expired"] = [], True
        if isinstance(turn.analysis, dict):
            turn.analysis = {**turn.analysis, "steps": [{**st, "result": {"expired": True}} if isinstance(st, dict)
                                                        else st for st in turn.analysis.get("steps") or []]}
        results += 1
    expired = list(session.scalars(select(C).where(C.scope == "personal", C.status == "active",
                                                   C.expires_at.is_not(None), C.expires_at < now).limit(BATCH)))
    for c in expired:
        c.status = "expired"
    get_ledger().remove(session, ref_table="conversation_correction", ref_ids=[c.id for c in expired])
    return {"messages": messages, "result_payloads": results, "personal_expired": len(expired)}


@platform_loop
def apply_retention(now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Every tenant's conversation retention, each in its own tenant binding; audited per tenant when it removed
    anything. Legal-hold conversations are skipped."""
    from analystos.tenancy import tenant_scope

    now = now or utcnow()
    with session_scope() as s:
        tenants = list(s.scalars(select(Tenant.id)))
    out: dict[str, dict[str, int]] = {}
    for tid in tenants:
        try:
            with tenant_scope(tid), session_scope() as s:
                counts = apply_tenant(s, s.get(Tenant, tid), now)
                if any(counts.values()):
                    audit("process:conversation_retention", "conversation.retention_applied", target=f"tenant:{tid}",
                          decision="allow", details=counts, session=s)
                out[tid] = counts
        except Exception:  # noqa: BLE001 - one tenant never stops the others
            log.exception("conversation retention for tenant %s failed", tid)
    return out


_last_sweep: list[float] = []


def apply_retention_quietly(every_seconds: float = 3600.0) -> dict[str, Any]:
    """From the scheduler loop: at most once per ``every_seconds`` per process; never raises."""
    import time

    if _last_sweep and time.monotonic() - _last_sweep[0] < every_seconds:
        return {}
    _last_sweep[:] = [time.monotonic()]
    try:
        return apply_retention()
    except Exception:  # noqa: BLE001
        log.exception("conversation retention failed")
        return {}

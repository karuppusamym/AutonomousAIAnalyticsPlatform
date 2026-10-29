"""Workspace playbook signing (S06-T6, spec §3.7; S10 tenant signing keys).

A workspace playbook is enabled by publishing it, and it publishes only signed:

1. **Simulate.** The draft passes its simulation (`definitions.test`, the playbook tester); the evidence is
   bound to its content hash and an edit makes it a draft again.
2. **Request a signature.** An approval (`playbook.sign`, `governance/approvals.py`) whose payload is the
   definition, its content hash and the simulation digests. Editing the draft invalidates it.
3. **Approve.** Someone other than the requester, with approver rights (separation of duties always).
4. **Publish.** `verify_for_execution` runs immediately before signing; the tenant's ed25519 key signs
   `(purpose, tenant, key, version, content hash, approval)`; the approval is consumed; the version is
   frozen and enabled.

The runtime refuses a workspace playbook version that is unsigned, whose signature does not verify, or
whose spec no longer hashes to what was signed (`verify_definition`, called from
`definitions.require_runnable` before any run starts and from schedule pin checks).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.core.ids import stable_hash, utcnow
from analystos.db.models import Approval, Definition, User
from analystos.events.bus import emit
from analystos.governance.audit import audit

ACTION = "playbook.sign"
PURPOSE = "analystos.playbook-signature.v1"


def applies(row: Definition) -> bool:
    return row.kind == "playbook"


def _simulations(row: Definition) -> list[dict[str, Any]]:
    evidence = row.test_evidence or {}
    return [{"fixture": s.get("fixture"), "outcome": s.get("outcome"), "digest": s.get("digest")}
            for s in evidence.get("simulations") or []]


def approval_payload(row: Definition) -> dict[str, Any]:
    """What the approver signs off, rebuilt from the row at every check: a changed spec gives another hash."""
    from analystos.services.definitions import content_hash

    return {"definition_id": row.id, "workspace_id": row.workspace_id, "kind": row.kind, "key": row.key,
            "version": row.version, "content_hash": content_hash(row.kind, row.spec), "simulations": _simulations(row)}


def _event(session: Session, row: Definition, type_: str, actor: str, **extra: Any) -> None:
    payload = {"definition_id": row.id, "key": row.key, "version": row.version, "content_hash": row.content_hash, **extra}
    emit(row.workspace_id, type_, payload, actor=actor, session=session)
    audit(actor, type_, workspace_id=row.workspace_id, target=row.id, details=payload, session=session)


def state(row: Definition) -> dict[str, Any]:
    """The signing state the editor shows: none, requested (with the approval's status), rejected, stale or signed."""
    sig = dict(row.signature or {})
    if not sig:
        return {"state": "unsigned"}
    if sig.get("state") == "signed":
        return {"state": "signed", "key_id": sig.get("key_id"), "alg": sig.get("alg"),
                "approval_id": (sig.get("payload") or {}).get("approval_id"),
                "signed_at": (sig.get("payload") or {}).get("signed_at")}
    out = {k: sig.get(k) for k in ("state", "approval_id", "content_hash", "requested_by", "requested_at", "reason")}
    if sig.get("content_hash") != row.content_hash:
        out["state"] = "stale"
    return out


def request_signature(session: Session, user: User, row: Definition, expected_revision: int | None) -> Approval:
    """Ask an approver to authorize signing this draft. It must have passed its simulation as it is now."""
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace, require_role
    from analystos.services import definitions as defs

    if not applies(row):
        raise InvalidInput(f"{row.kind} definitions are not signed")
    require_role(session, user, row.workspace_id, defs.author_role(row.kind))
    if row.status not in defs.EDITABLE:
        raise Conflict(f"version {row.version} is {row.status}; only a draft is signed")
    defs._check_revision(row, expected_revision)
    row.spec = defs._validate(session, row.workspace_id, row.kind, row.key, row.spec)
    row.content_hash = defs.content_hash(row.kind, row.spec)
    if row.status != "tested" or (row.test_evidence or {}).get("content_hash") != row.content_hash:
        raise Conflict(f"playbook {row.key} v{row.version} must pass its simulation before it is signed "
                       "(POST .../playbook-editor/drafts/{id}/simulate)", details={"status": row.status})
    payload = approval_payload(row)
    current = session.get(Approval, (row.signature or {}).get("approval_id") or "")
    if current is not None and current.status in ("pending", "approved") and current.payload_hash == stable_hash(payload):
        return current
    if current is not None:
        invalidate(session, row, "a new signature was requested")
    ws = get_workspace(session, row.workspace_id)
    approval = request_approval(session, workspace_id=row.workspace_id, run_id=None, action=ACTION, payload=payload,
                                plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="high",
                                destination=f"playbook:{row.key}"[:120], affected_assets=[row.key],
                                evidence={"summary": f"sign {row.key} v{row.version} ({row.content_hash[:12]}) for this "
                                                     "workspace; simulations: " + ", ".join(
                                                         f"{s['fixture']} {s['outcome']}" for s in payload["simulations"])})
    row.signature = {"state": "requested", "approval_id": approval.id, "content_hash": row.content_hash,
                     "requested_by": user.id, "requested_at": utcnow().isoformat()}
    row.revision += 1
    session.flush()
    _event(session, row, "definition.signature_requested", f"user:{user.id}", approval_id=approval.id)
    return approval


def invalidate(session: Session, row: Definition, reason: str) -> None:
    """A changed draft (or a fresh request) voids the open signing approval: it bound other content."""
    from sqlalchemy import update

    approval_id = (row.signature or {}).get("approval_id")
    if not approval_id or (row.signature or {}).get("state") == "signed":
        return
    result = session.execute(update(Approval).where(Approval.id == approval_id, Approval.status.in_(["pending", "approved"]))
                             .values(status="invalidated", reason=reason))
    row.signature = {**(row.signature or {}), "state": "invalidated", "reason": reason}
    if result.rowcount:
        _event(session, row, "definition.signature_invalidated", "system", approval_id=approval_id, reason=reason)


def apply_decision(session: Session, approval: Approval) -> Definition | None:
    """After the approver decides (approvals inbox): record it on the draft, if the draft is still what was approved."""
    if approval.action != ACTION:
        return None
    row = session.get(Definition, (approval.payload or {}).get("definition_id") or "")
    if row is None or row.workspace_id != approval.workspace_id or (row.signature or {}).get("approval_id") != approval.id:
        return None
    if approval.status == "approved" and stable_hash(approval_payload(row)) != approval.payload_hash:
        invalidate(session, row, "the playbook changed while its signature was pending")
        raise Conflict("the playbook changed while its signature was pending; a new signature request is required")
    state_ = "approved" if approval.status == "approved" else "rejected"
    row.signature = {**(row.signature or {}), "state": state_, "decided_by": approval.decided_by,
                     **({"reason": approval.reason} if state_ == "rejected" else {})}
    _event(session, row, f"definition.signature_{state_}", f"user:{approval.decided_by}", approval_id=approval.id)
    return row


def sign_for_publish(session: Session, user: User, row: Definition) -> None:
    """Called by `definitions.publish` immediately before the version is frozen: the approval must still bind
    this exact content (and both people must still hold their rights), then the tenant key signs it."""
    from analystos.governance.approvals import consume, verify_for_execution
    from analystos.toolreg.signing import sign_payload, tenant_of

    approval_id = (row.signature or {}).get("approval_id")
    if not approval_id:
        raise Conflict(f"playbook {row.key} v{row.version} is unsigned: request a signature and have it approved before "
                       "publishing (POST .../playbook-editor/drafts/{id}/request-signature)",
                       details={"signature": state(row)})
    approval = verify_for_execution(session, approval_id, payload=approval_payload(row), plan_hash=None)
    tenant = tenant_of(session, row.workspace_id)
    payload = {"purpose": PURPOSE, "tenant": tenant, "kind": row.kind, "key": row.key, "version": row.version,
               "content_hash": row.content_hash, "approval_id": approval.id, "approved_by": approval.decided_by,
               "signed_by": user.id, "signed_at": utcnow().isoformat()}
    signature = sign_payload(session, tenant, payload, actor=f"user:{user.id}")
    consume(session, approval)
    row.signature = {"state": "signed", **signature.model_dump(), "payload": payload}
    _event(session, row, "definition.signed", f"user:{user.id}", approval_id=approval.id, key_id=signature.key_id)


def verify_definition(session: Session, row: Definition) -> None:
    """`ManifestRefused` unless the version is signed by this workspace's tenant key over exactly its content."""
    import hmac

    from analystos.services.definitions import content_hash
    from analystos.toolreg.signing import ManifestRefused, tenant_of, verify_payload

    if not applies(row):
        return
    label = f"playbook {row.key} v{row.version}"
    sig = dict(row.signature or {})
    if sig.get("state") != "signed":
        raise ManifestRefused(f"{label} is unsigned: publish a new version through simulation and signing",
                              details={"reason": "unsigned", "definition_id": row.id})
    payload = dict(sig.get("payload") or {})
    actual = content_hash(row.kind, row.spec)
    if not hmac.compare_digest(str(payload.get("content_hash", "")), actual) or actual != row.content_hash:
        raise ManifestRefused(f"{label} changed after it was signed", details={"reason": "tampered", "definition_id": row.id})
    try:
        tenant = tenant_of(session, row.workspace_id)
    except Forbidden:
        raise ManifestRefused(f"{label}: no tenant to verify its signature with", details={"reason": "key"}) from None
    if payload.get("purpose") != PURPOSE or payload.get("tenant") != tenant or payload.get("key") != row.key:
        raise ManifestRefused(f"{label} carries a signature made for something else",
                              details={"reason": "tampered", "definition_id": row.id})
    verify_payload(session, tenant, payload, {k: sig.get(k) for k in ("alg", "key_id", "value")}, label=label)

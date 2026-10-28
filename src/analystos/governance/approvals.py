"""Plan- and payload-bound approvals (§39, BI-010).

An approval is an immutable proposal: action, destination, affected assets, the exact payload
and its hash, the plan hash and the policy version. Execution re-verifies all of them *and*
re-checks current authorization of both requester and approver immediately before acting.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import AnalysisRun, Approval, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_policy, member_role
from analystos.security.auth import APPROVER_ROLES, role_at_least

# Actions whose approver must differ from the requester whatever the workspace policy says (P4-K03).
ALWAYS_SEPARATE_DUTIES = {"semantic_model.approve", "semantic_relationship.accept",
                          "delivery_destination.authorize"}  # N-3: external egress is never self-authorized


def request_approval(session: Session, *, workspace_id: str, run_id: str | None, action: str, payload: dict[str, Any],
                     plan_hash: str | None, policy_version: int, requested_by: str, risk_tier: str,
                     destination: str | None, affected_assets: list[str], evidence: dict[str, Any] | None = None) -> Approval:
    payload_hash = stable_hash(payload)
    # A retry reuses the open request for the same payload; one past its expiry can never be decided or
    # executed, so returning it would leave the requester stuck: a fresh request replaces it.
    existing = session.scalar(select(Approval).where(
        Approval.workspace_id == workspace_id, Approval.run_id == run_id, Approval.payload_hash == payload_hash,
        Approval.plan_hash == plan_hash, Approval.action == action, Approval.status.in_(["pending", "approved"]),
        Approval.expires_at > utcnow()).order_by(Approval.created_at.desc()).limit(1))
    if existing:
        return existing
    ws = get_workspace(session, workspace_id)
    ttl = load_policy(session, ws).approval_ttl_hours
    approval = Approval(id=new_id("apr"), workspace_id=workspace_id, run_id=run_id, action=action, risk_tier=risk_tier,
                        destination=destination, affected_assets=affected_assets, payload=payload, payload_hash=payload_hash,
                        plan_hash=plan_hash, policy_version=policy_version, requested_by=requested_by, status="pending",
                        expires_at=utcnow() + timedelta(hours=ttl), evidence=evidence or {})
    session.add(approval)
    session.flush()
    emit(workspace_id, "approval.requested", {"approval_id": approval.id, "action": action, "destination": destination,
                                              "risk_tier": risk_tier}, run_id=run_id, session=session)
    audit(f"user:{requested_by}", "approval.requested", workspace_id=workspace_id, run_id=run_id, target=approval.id,
          decision="approval_required", details={"action": action, "payload_hash": payload_hash}, session=session)
    return approval


def claim(session: Session, approval: Approval, *, expect: str, to: str, **values: Any) -> Approval:
    """Compare-and-set the approval's status: one guarded ``UPDATE ... WHERE status = expect`` and a
    rowcount check (P7-10). Two deciders or two executors that both read ``expect`` cannot both win,
    whatever the isolation level or row locking of the database; the loser gets ``Conflict``."""
    result = session.execute(
        update(Approval).where(Approval.id == approval.id, Approval.status == expect).values(status=to, **values)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.expire(approval)
        raise Conflict(f"approval {approval.id} is no longer {expect}; it was claimed concurrently")
    session.expire(approval)
    return approval


def consume(session: Session, approval: Approval) -> Approval:
    """Single use: claim a verified approval (approved -> executed) immediately before the side effect."""
    return claim(session, approval, expect="approved", to="executed")


def decide(session: Session, approval_id: str, user: User, *, approve: bool, reason: str | None = None) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise NotFound(f"approval {approval_id} not found")
    role = member_role(session, user, approval.workspace_id)
    if role is None:
        raise NotFound(f"approval {approval_id} not found")
    if role not in APPROVER_ROLES and not user.is_admin:
        raise Forbidden(f"role '{role}' cannot approve")
    if approval.status != "pending":
        raise Conflict(f"approval is {approval.status}")
    if approval.expires_at < utcnow():
        approval.status = "expired"
        raise Conflict("approval expired")
    ws = get_workspace(session, approval.workspace_id)
    policy = load_policy(session, ws)
    if (policy.separation_of_duties or approval.action in ALWAYS_SEPARATE_DUTIES) and approval.requested_by == user.id:
        audit(f"user:{user.id}", "approval.self_approval_blocked", workspace_id=approval.workspace_id,
              target=approval.id, decision="deny", reasons=["separation_of_duties"], session=session)
        raise Forbidden("separation of duties: the requester cannot approve their own action")
    if ws.policy_version != approval.policy_version:
        approval.status = "invalidated"
        approval.reason = "policy changed since the proposal was created"
        raise Conflict("policy changed since the proposal was created; a new approval is required")
    claim(session, approval, expect="pending", to="approved" if approve else "rejected", decided_by=user.id,
          decided_at=utcnow(), reason=reason)
    emit(approval.workspace_id, "approval.completed", {"approval_id": approval.id, "status": approval.status},
         run_id=approval.run_id, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "approval.decided", workspace_id=approval.workspace_id, run_id=approval.run_id,
          target=approval.id, decision="allow" if approve else "deny", reasons=[reason or ""], session=session)
    return approval


def verify_for_execution(session: Session, approval_id: str, *, payload: dict[str, Any], plan_hash: str | None) -> Approval:
    """Called immediately before the side effect. Any mismatch blocks execution."""
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalRequired("no approval on record")
    if approval.status != "approved":
        raise ApprovalRequired(f"approval is {approval.status}")
    if approval.expires_at < utcnow():
        approval.status = "expired"
        raise ApprovalRequired("approval expired")
    return _verify_binding(session, approval, payload=payload, plan_hash=plan_hash)


def verify_for_reapply(session: Session, approval_id: str, *, payload: dict[str, Any], plan_hash: str | None) -> Approval:
    """Reconcile repair (S08): re-apply exactly what an executed approval already published, e.g. recreate a
    dashboard someone deleted in the BI tool. The approval must be `executed` and still bind the payload,
    plan and policy version, and both people must still hold their rights; anything else needs a new approval.
    Its decision expiry does not apply: it limits deciding, not keeping the approved state in place."""
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalRequired("no approval on record")
    if approval.status != "executed":
        raise ApprovalRequired(f"approval is {approval.status}; only an executed approval can be re-applied")
    return _verify_binding(session, approval, payload=payload, plan_hash=plan_hash, mark=False)


def _verify_binding(session: Session, approval: Approval, *, payload: dict[str, Any], plan_hash: str | None,
                    mark: bool = True) -> Approval:
    """Payload, plan and policy binding plus both parties' current rights. `mark` records an invalidation
    on the approval (a re-apply check leaves an executed approval as it is)."""
    def invalid(reason: str) -> None:
        if mark:
            approval.status, approval.reason = "invalidated", reason

    if stable_hash(payload) != approval.payload_hash:
        invalid("payload changed after approval")
        raise ApprovalRequired("payload changed after approval; a new approval is required")
    if plan_hash != approval.plan_hash:
        invalid("plan changed after approval")
        raise ApprovalRequired("plan changed after approval; a new approval is required")
    ws = session.get(Workspace, approval.workspace_id)
    if ws is None or ws.status != "active" or ws.deleted_at is not None or ws.policy_version != approval.policy_version:
        invalid("policy changed after approval")
        raise ApprovalRequired("policy changed after approval; a new approval is required")
    requester = session.get(User, approval.requested_by)
    approver = session.get(User, approval.decided_by) if approval.decided_by else None
    requester_role = member_role(session, requester, approval.workspace_id) if requester and requester.active else None
    approver_role = member_role(session, approver, approval.workspace_id) if approver and approver.active else None
    if not role_at_least(requester_role, "editor"):
        raise PolicyDenied("requester no longer holds publish rights in this workspace")
    if approver_role not in APPROVER_ROLES and not (approver and approver.is_admin):
        raise PolicyDenied("approver no longer holds approval rights in this workspace")
    return approval


def invalidate_run_approvals(session: Session, run_id: str, reason: str) -> int:
    result = session.execute(update(Approval).where(Approval.run_id == run_id, Approval.status.in_(["pending", "approved"]))
                             .values(status="invalidated", reason=reason))
    run = session.get(AnalysisRun, run_id)
    if result.rowcount and run:
        emit(run.workspace_id, "approval.invalidated", {"reason": reason, "count": result.rowcount}, run_id=run_id, session=session)
    return result.rowcount or 0

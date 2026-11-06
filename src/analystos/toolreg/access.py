"""Consumer access requests for data products (review item P2-9).

A workspace member asks for what calling a product needs: the workspace role its permissions require (the
"Reader/Analyst" of a data product) or an MCP grant of the product for an MCP client they created. The request is
an approval (`governance/approvals.py`, action `data_product.access`) bound to the payload
(product, version, manifest hash, requester, role or grant). A person decides it, never the requester (separation
of duties is always on for this action) and never a model. On approval, code applies it immediately after
`verify_for_execution`, which re-checks the binding, the policy version and both people's current rights; the
approval is consumed so it applies once. Applying only raises: a role is never lowered, a grant only gains the
product. The risk tier is a rule of the product's data classification and what is asked.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.tool_manifest import ToolManifestV2
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.db.models import Approval, McpClient, McpGrant, User, WorkspaceMember
from analystos.events.bus import emit
from analystos.governance import approvals
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, member_role, require_role
from analystos.security.auth import ROLE_RANK, role_at_least

ACTION = "data_product.access"
REQUESTABLE_ROLES = ("viewer", "analyst")  # never editor, approver or owner through a product request
HIGH_CLASSES = ("confidential", "restricted")


def _product(session: Session, workspace_id: str, tool_id: str) -> tuple[Any, ToolManifestV2]:
    from analystos.toolreg import store

    row = store.resolve(session, workspace_id, tool_id)
    m = ToolManifestV2.model_validate(row.manifest)
    if m.kind != "data_product":
        raise InvalidInput(f"{row.tool_id} is a {m.kind}, not a data product")
    return row, m


def risk_tier(m: ToolManifestV2, *, role: str | None, grant: bool) -> str:
    if m.data_classification_max in HIGH_CLASSES:
        return "high"
    return "medium" if grant or role == "analyst" else "low"


def request_access(session: Session, user: User, workspace_id: str, tool_id: str, *, role: str | None = None,
                   mcp_client_id: str | None = None, reason: str | None = None) -> Approval:
    """Open (or return the open) approval for this consumer's access to the product's current version."""
    from analystos.toolreg.invoke import min_role

    current = require_role(session, user, workspace_id, "viewer")
    row, m = _product(session, workspace_id, tool_id)
    if (role is None) == (mcp_client_id is None):
        if role is None and mcp_client_id is None:
            role = min_role(m)  # what calling it needs
        else:
            raise InvalidInput("ask for a workspace role or an MCP grant, not both")
    grant = None
    if mcp_client_id is not None:
        client = session.get(McpClient, mcp_client_id)
        if client is None or client.status != "active":
            raise NotFound(f"MCP client {mcp_client_id} not found")
        if client.created_by != user.id:
            raise Forbidden("only the person who created an MCP client may request grants for it")
        grant = {"client_id": client.id, "tools": [row.tool_id], "role": "analyst"}
    else:
        if role not in REQUESTABLE_ROLES:
            raise InvalidInput(f"a product access request asks for one of {', '.join(REQUESTABLE_ROLES)}")
        if role_at_least(current, role):
            raise Conflict(f"you already hold {current}, which covers {role}")
    ws = get_workspace(session, workspace_id)
    payload = {"product": row.tool_id, "version": row.version, "manifest_hash": row.manifest_hash, "requester": user.id,
               "role": role if grant is None else None, "grant": grant}
    tier = risk_tier(m, role=role, grant=grant is not None)
    approval = approvals.request_approval(
        session, workspace_id=workspace_id, run_id=None, action=ACTION, payload=payload, plan_hash=None,
        policy_version=ws.policy_version, requested_by=user.id, risk_tier=tier,
        destination=f"mcp_client:{grant['client_id']}" if grant else f"workspace_role:{role}",
        affected_assets=[row.tool_id], evidence={"reason": (reason or "")[:1000], "current_role": current,
                                                 "data_classification_max": m.data_classification_max})
    emit(workspace_id, "data_product.access_requested", {"approval_id": approval.id, "tool_id": row.tool_id,
                                                          "version": row.version, "risk_tier": tier},
         actor=f"user:{user.id}", session=session)
    return approval


def _access_approval(session: Session, approval_id: str) -> Approval:
    a = session.get(Approval, approval_id)
    if a is None or a.action != ACTION:
        raise NotFound(f"access request {approval_id} not found")
    return a


def decide(session: Session, approver: User, approval_id: str, *, approve: bool, reason: str | None = None) -> dict[str, Any]:
    """A person decides; an approval is applied right away (verify, consume, apply)."""
    a = _access_approval(session, approval_id)
    approvals.decide(session, a.id, approver, approve=approve, reason=reason)
    if not approve:
        return {"approval_id": a.id, "status": "rejected"}
    return apply(session, a.id)


def apply(session: Session, approval_id: str) -> dict[str, Any]:
    """Apply an approved request. Refused if the product version is no longer certified or its manifest changed,
    or if the binding, policy or either person's rights changed since the decision."""
    from analystos.mcp import grants as G
    from analystos.toolreg import store

    a = _access_approval(session, approval_id)
    payload = dict(a.payload or {})
    rows = [r for r in store.versions(session, a.workspace_id, payload["product"]) if r.version == payload["version"]]
    if not rows or rows[0].status != "certified" or rows[0].manifest_hash != payload["manifest_hash"]:
        a.status, a.reason = "invalidated", "the product version changed after the request"
        raise Conflict("the product version requested is no longer the certified one; request access again")
    approvals.verify_for_execution(session, a.id, payload=payload, plan_hash=None, requester_min_role="viewer")
    approvals.consume(session, a)
    requester = session.get(User, payload["requester"])
    approver = session.get(User, a.decided_by)
    if payload.get("grant"):
        g = payload["grant"]
        existing = session.scalar(select(McpGrant).where(McpGrant.client_id == g["client_id"],
                                                         McpGrant.workspace_id == a.workspace_id))
        tools = list(dict.fromkeys([*(existing.tools if existing else []), *g["tools"]]))
        role = "analyst" if existing is None or ROLE_RANK.get(existing.role, 0) <= ROLE_RANK["analyst"] else existing.role
        G.set_grant(session, approver, g["client_id"], a.workspace_id, role=role, tools=tools,
                    quotas=dict(existing.quotas or {}) if existing else None,
                    tool_patterns=list(existing.tool_patterns or []) if existing else None,
                    rate_per_min=existing.rate_per_min if existing else None,
                    expires_at=existing.expires_at if existing else None)
        applied = {"mcp_grant": {"client_id": g["client_id"], "role": role, "tools": tools}}
    else:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == a.workspace_id,
                                                              WorkspaceMember.user_id == requester.id))
        before = member.role if member else None
        if member is None:
            session.add(WorkspaceMember(workspace_id=a.workspace_id, user_id=requester.id, role=payload["role"]))
        elif not role_at_least(member.role, payload["role"]):
            member.role = payload["role"]  # only ever raised
        session.flush()
        applied = {"workspace_role": {"user_id": requester.id, "from": before,
                                      "to": member_role(session, requester, a.workspace_id)}}
    audit(f"user:{a.decided_by}", "data_product.access_granted", workspace_id=a.workspace_id, target=a.id, decision="allow",
          details={"product": payload["product"], "version": payload["version"], "requester": payload["requester"],
                   **applied}, session=session)
    emit(a.workspace_id, "data_product.access_granted", {"approval_id": a.id, "tool_id": payload["product"], **applied},
         actor=f"user:{a.decided_by}", session=session)
    return {"approval_id": a.id, "status": "executed", **applied}


def requests_for(session: Session, workspace_id: str, tool_id: str | None = None, *, requester: str | None = None) -> list[dict[str, Any]]:
    q = select(Approval).where(Approval.workspace_id == workspace_id, Approval.action == ACTION)
    if requester:
        q = q.where(Approval.requested_by == requester)
    out = []
    for a in session.scalars(q.order_by(Approval.created_at.desc()).limit(200)):
        p = a.payload or {}
        if tool_id and p.get("product") != tool_id:
            continue
        out.append({"approval_id": a.id, "product": p.get("product"), "version": p.get("version"),
                    "requester": p.get("requester"), "role": p.get("role"), "grant": p.get("grant"), "status": a.status,
                    "risk_tier": a.risk_tier, "reason": (a.evidence or {}).get("reason"), "decided_by": a.decided_by,
                    "decided_at": a.decided_at, "created_at": a.created_at, "expires_at": a.expires_at})
    return out

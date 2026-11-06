"""Declared quality rules (P1-7) and data-product contracts and access requests (P2-9).

`router` (/api): declare, list, run, recommend from the profile (review drafts), the asset trust signal, and the
rules as an OKF pack. `products` (/v2, also under /api like the tool registry): a product's contract state and
consumer access requests, decided by a person and applied by code after `verify_for_execution`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.contracts.quality import QualityRule
from analystos.core.errors import InvalidInput
from analystos.db.models import Approval, SourceAsset, User
from analystos.governance.policy import load_in_workspace, require_role
from analystos.quality import okf_rules, recommend, service
from analystos.quality.models import QualityRuleRow
from analystos.toolreg import access

router = APIRouter(prefix="/api", tags=["quality"])
products = APIRouter(prefix="/v2", tags=["tools"])
MAX_IMPORT_BYTES = 16 * 1024 * 1024


class RuleStatusIn(BaseModel):
    status: str


class RunRulesIn(BaseModel):
    rule_ids: list[str] | None = Field(default=None, max_length=500)
    assets: list[str] | None = Field(default=None, max_length=200)


@router.get("/workspaces/{workspace_id}/quality-rules")
def list_rules(workspace_id: str, asset: str | None = None, status: str | None = None, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return [service.rule_view(r) for r in service.list_rules(session, workspace_id, asset=asset, status=status)]


@router.post("/workspaces/{workspace_id}/quality-rules", status_code=201)
def declare_rule(workspace_id: str, body: QualityRule, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """An owner declares a rule on an asset of this workspace's catalog: active at once (a person decided it)."""
    row, created = service.declare(session, user, workspace_id, body, origin="owner")
    return {**service.rule_view(row), "created": created}


@router.get("/quality-rules/{rule_id}")
def get_rule(rule_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    row = load_in_workspace(session, QualityRuleRow, rule_id, user=user, label="quality rule")
    return service.rule_view(row, service.history(session, row.id))


@router.post("/quality-rules/{rule_id}/status")
def set_rule_status(rule_id: str, body: RuleStatusIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    row = load_in_workspace(session, QualityRuleRow, rule_id, user=user, minimum="editor", label="quality rule")
    return service.rule_view(service.set_status(session, user, row.id, body.status))


@router.post("/workspaces/{workspace_id}/quality-rules:run")
def run_rules(workspace_id: str, body: RunRulesIn, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """Run active rules now through the gateway, in the caller's own data scope (analyst or above)."""
    require_role(session, user, workspace_id, "analyst")
    results = service.run_rules(session, user, workspace_id, rule_ids=body.rule_ids, assets=body.assets,
                                trigger=f"user:{user.id}")
    return {"results": results, "trust": service.asset_trust(session, workspace_id, sorted({r["asset"] for r in results}))}


@router.post("/workspaces/{workspace_id}/assets/{asset_id}/quality-rules:recommend")
def recommend_rules(workspace_id: str, asset_id: str, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    """Deterministic recommendations from the stored profile, queued as review drafts (kind `quality_rule`)."""
    a = load_in_workspace(session, SourceAsset, asset_id, workspace_id, user=user, minimum="editor", label="asset")
    return recommend.recommend(session, user, workspace_id, a.id)


@router.get("/workspaces/{workspace_id}/quality-trust")
def quality_trust(workspace_id: str, assets: list[str] | None = Query(default=None), user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return service.asset_trust(session, workspace_id, assets)


@router.get("/workspaces/{workspace_id}/quality-rules/export")
def export_rules(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Active rules as an OKF v0.2 pack (zip): rules kept as code."""
    from analystos.context.interchange import zip_files

    out = okf_rules.export_pack(session, user, workspace_id)
    return Response(zip_files(out["files"]), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="quality-rules-{out["digest"][:12]}.okf.zip"'})


@router.post("/workspaces/{workspace_id}/quality-rules/import")
async def import_rules(workspace_id: str, file: UploadFile = File(...), user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    """An OKF rules pack into the review queue: every rule is a draft until an owner approves it."""
    require_role(session, user, workspace_id, "editor")
    data = await file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise InvalidInput("the pack is too large")
    return okf_rules.import_pack(session, user, workspace_id, data)


# ------------------------------------------------------------------------------------ data products (P2-9)
class AccessRequestIn(BaseModel):
    workspace_id: str
    role: str | None = None  # viewer | analyst; omitted (and no client): what calling the product needs
    mcp_client_id: str | None = None
    reason: str | None = Field(default=None, max_length=1000)


class AccessDecisionIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=1000)


@products.get("/tools/{tool_id}/contract")
def product_contract(tool_id: str, workspace_id: str, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")) -> dict[str, Any]:
    """The product's contract and its state now (freshness, rule results): breaches are shown on the product."""
    from analystos.quality.contracts import contract_status

    require_role(session, user, workspace_id, "viewer")
    return contract_status(session, workspace_id, tool_id)


@products.post("/tools/{tool_id}/access-requests", status_code=201)
def request_product_access(tool_id: str, body: AccessRequestIn, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    a = access.request_access(session, user, body.workspace_id, tool_id, role=body.role,
                              mcp_client_id=body.mcp_client_id, reason=body.reason)
    return {"approval_id": a.id, "status": a.status, "risk_tier": a.risk_tier, "payload_hash": a.payload_hash,
            "expires_at": a.expires_at}


@products.get("/tools/{tool_id}/access-requests")
def list_product_access(tool_id: str, workspace_id: str, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    """Owners and approvers see every request for the product; anyone else sees their own."""
    from analystos.security.auth import APPROVER_ROLES

    role = require_role(session, user, workspace_id, "viewer")
    mine = None if (role in APPROVER_ROLES or user.is_admin) else user.id
    return access.requests_for(session, workspace_id, tool_id, requester=mine)


@products.post("/access-requests/{approval_id}:decide")
def decide_product_access(approval_id: str, body: AccessDecisionIn, user: User = Depends(current_user),
                          session: Session = Depends(db, scope="function")):
    a = load_in_workspace(session, Approval, approval_id, user=user, label="access request")
    return access.decide(session, user, a.id, approve=body.approve, reason=body.reason)


@products.post("/access-requests/{approval_id}:apply")
def apply_product_access(approval_id: str, user: User = Depends(current_user),
                         session: Session = Depends(db, scope="function")):
    """Apply a request approved elsewhere (the generic approvals screen). Needs an approver role."""
    from analystos.core.errors import Forbidden
    from analystos.security.auth import APPROVER_ROLES

    a = load_in_workspace(session, Approval, approval_id, user=user, label="access request")
    if require_role(session, user, a.workspace_id, "viewer") not in APPROVER_ROLES and not user.is_admin:
        raise Forbidden("applying an access request needs an approver role")
    return access.apply(session, a.id)

"""Tool registry API v2 (spec S10 §4): list, manifest, versions, usage, health, publish-as-tool, invoke,
deprecate, external MCP admission and classification, tenant signing keys.

`POST /v2/tools/{tool_id}:invoke` accepts a user's token or an MCP/API client's `client_id.secret`; a client
acts as its service user within its grants. Every other route is for users.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import _authenticate, admin_user, current_user, db
from analystos.contracts.tool_manifest import split_ref
from analystos.core.errors import InvalidInput, NotFound, Unauthenticated
from analystos.db.base import session_scope
from analystos.db.models import McpServer, User
from analystos.governance.policy import load_in_workspace, member_role, require_role
from analystos.toolreg import external, signing, store
from analystos.toolreg import publish as pub
from analystos.toolreg.invoke import Caller, invoke, min_role

router = APIRouter(prefix="/v2", tags=["tools"])
CLIENT_PREFIX = "mcpc_"


class ToolPublishIn(BaseModel):
    workspace_id: str
    source_type: str  # metric | verified_query | dataset | playbook
    ref: str  # metric name, verified query id/name, dataset id/name, playbook key
    options: pub.PublishOptions = Field(default_factory=pub.PublishOptions)


class ToolInvokeIn(BaseModel):
    workspace_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None


class ToolDeprecateIn(BaseModel):
    workspace_id: str
    version: str | None = None  # one version; omitted = every certified version
    reason: str | None = Field(default=None, max_length=1000)
    retire: bool = False  # retired versions never run, even pinned


class ToolClassifyIn(BaseModel):
    workspace_id: str
    side_effect: str


class McpAdmitIn(BaseModel):
    workspace_id: str


class ToolKeyRotateIn(BaseModel):
    workspace_id: str
    revoke_previous: bool = False
    resign: bool = True


def _caller(authorization: str | None) -> Caller:
    """A user (JWT) or an MCP/API client (`client_id.secret`, bound to its own tenant)."""
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if token.startswith(CLIENT_PREFIX):
        from analystos.mcp.server import _authenticate as client_auth
        from analystos.tenancy import set_current_tenant

        principal = client_auth(token)
        set_current_tenant(principal.tenant_id)
        with session_scope() as s:
            user = s.get(User, principal.user_id)
            if user is None or not user.active:
                raise Unauthenticated("the client's service identity is inactive")
            s.expunge(user)
        return Caller(user=user, actor=f"mcp_client:{principal.client_id}", channel="rest", principal=principal)
    if not token:
        raise Unauthenticated("missing bearer token")
    with session_scope() as s:
        user = _authenticate(s, authorization, None)
    return Caller(user=user, actor=f"user:{user.id}", channel="rest")


def _may_use(session: Session, caller: Caller, ws: str, row: Any) -> bool:
    from analystos.mcp import grants as G
    from analystos.security.auth import role_at_least

    m = row.manifest or {}
    if caller.principal is not None:
        grant = caller.principal.grants.get(ws)
        return bool(grant and not G.expired(grant) and (m.get("exposure") or {}).get("rest", True)
                    and G.grants_tool(grant["tools"], row.tool_id, grant.get("tool_patterns") or ()))
    from analystos.contracts.tool_manifest import ToolManifestV2

    try:
        need = min_role(ToolManifestV2.model_validate(m))
    except ValueError:
        return False
    return role_at_least(member_role(session, session.merge(caller.user), ws), need)


# ------------------------------------------------------------------------------------ read
@router.get("/tools")
def list_tools(workspace_id: str, kind: str | None = None, q: str | None = Query(default=None, max_length=200),
               include_quarantined: bool = False, authorization: str | None = Header(default=None)):
    """Only tools the caller may use; owners may also see quarantined and refused tools awaiting a decision."""
    caller = _caller(authorization)
    with session_scope() as s:
        if caller.principal is None:
            role = require_role(s, s.merge(caller.user), workspace_id, "viewer")
        else:
            from analystos.mcp import grants as G

            G.check_grant(caller.principal, workspace_id, "resources/read")
            role = caller.principal.grants[workspace_id]["role"]
        statuses: tuple[str, ...] = ("certified",)
        if include_quarantined and caller.principal is None and role == "owner":
            statuses = ("certified", "quarantined", "refused")
        rows = store.current_rows(s, workspace_id, kind=kind, q=q, statuses=statuses)
        return [store.summary(r) for r in rows if r.status != "certified" or _may_use(s, caller, workspace_id, r)]


@router.get("/tools/{tool_id}")
def get_tool(tool_id: str, workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Manifest view: the version a call would run (or the newest, if none may run), owner, versions, health."""
    require_role(session, user, workspace_id, "viewer")
    base, _ = split_ref(tool_id)
    rows = store.versions(session, workspace_id, base)
    if not rows:
        raise NotFound(f"tool {tool_id} not found")
    try:
        row = store.resolve(session, workspace_id, tool_id)
    except Exception:  # quarantined, deprecated-only, retired: show the newest with its status
        row = rows[-1]
    return {**store.manifest_out(row), "health": store.health(session, row),
            "versions": [{"version": r.version, "status": r.status, "status_reason": r.status_reason,
                          "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}


@router.get("/tools/{tool_id}/versions")
def tool_versions(tool_id: str, workspace_id: str, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return [store.manifest_out(r) for r in store.versions(session, workspace_id, split_ref(tool_id)[0])]


@router.get("/tools/{tool_id}/usage")
def tool_usage(tool_id: str, workspace_id: str, days: int = Query(default=7, ge=1, le=90), user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "analyst")
    return store.usage(session, workspace_id, split_ref(tool_id)[0], days=days)


# ------------------------------------------------------------------------------------ write
@router.post("/tools:publish", status_code=201)
def publish_tool(body: ToolPublishIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    row, created = pub.publish(session, user, body.workspace_id, body.source_type, body.ref, body.options)
    out = {**store.manifest_out(row), "created": created}
    return out if created else JSONResponse(status_code=200, content=out)


@router.post("/tools/{tool_id}:invoke")
def invoke_tool(tool_id: str, body: ToolInvokeIn, authorization: str | None = Header(default=None)):
    out = invoke(_caller(authorization), body.workspace_id, tool_id, body.arguments, approval_id=body.approval_id)
    return JSONResponse(status_code=202, content=out) if out.get("status") == "approval_required" else out


@router.post("/tools/{tool_id}:deprecate")
def deprecate_tool(tool_id: str, body: ToolDeprecateIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    require_role(session, user, body.workspace_id, pub.PUBLISH_ROLE)
    base, pin = split_ref(tool_id)
    version = body.version or pin
    rows = [r for r in store.versions(session, body.workspace_id, base) if not version or r.version == version]
    if not rows:
        raise NotFound(f"tool {tool_id} not found")
    to = "retired" if body.retire else "deprecated"
    changed = []
    for r in rows:
        if r.status in ("certified", "deprecated") and r.status != to:
            store.set_status(session, r, to, actor=f"user:{user.id}", reason=body.reason)
            changed.append(r.version)
    return {"tool_id": base, "status": to, "versions": changed}


@router.post("/tools/{tool_id}:classify")
def classify_tool(tool_id: str, body: ToolClassifyIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    return store.manifest_out(external.classify(session, user, body.workspace_id, tool_id, body.side_effect))


@router.post("/mcp-servers/{server_id}:admit")
def admit_server(server_id: str, body: McpAdmitIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, McpServer, server_id, body.workspace_id, user=user, label="MCP server")
    return external.admit(session, user, body.workspace_id, server_id)


# ------------------------------------------------------------------------------------ signing keys (S10-T5)
@router.get("/tool-keys")
def list_keys(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The public keys that verify this workspace's tenant manifests (for external verifiers too)."""
    require_role(session, user, workspace_id, "viewer")
    return signing.list_keys(session, signing.tenant_of(session, workspace_id))


@router.post("/tool-keys:rotate")
def rotate_keys(body: ToolKeyRotateIn, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    if body.revoke_previous and not body.resign:
        raise InvalidInput("revoking the previous key without re-signing would refuse every manifest it signed")
    tenant = signing.tenant_of(session, body.workspace_id)
    key = signing.rotate(session, tenant, actor=f"user:{admin.id}", revoke_previous=body.revoke_previous)
    resigned = store.resign_all(session, tenant, actor=f"user:{admin.id}") if body.resign else 0
    return {"key_id": key.key_id, "public_key": key.public_key, "resigned": resigned}

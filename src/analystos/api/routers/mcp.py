"""MCP administration (P4-X05 client side, P4-X06 server side). The MCP protocol endpoint itself is
mounted at `/mcp` by `analystos.mcp.server.mount`."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import row
from analystos.db.base import session_scope
from analystos.db.models import McpServer, User
from analystos.governance.policy import load_in_workspace, require_role
from analystos.mcp import client as mcp_client
from analystos.mcp import grants as mcp_grants

router = APIRouter(prefix="/api", tags=["mcp"])


class ServerIn(BaseModel):
    name: str
    url: str
    secret_ref: str | None = None
    transport: str = "streamable_http"
    config: dict[str, Any] = Field(default_factory=dict)


class AllowIn(BaseModel):
    allowed: bool


class ClassifyIn(BaseModel):
    side_effect: str


class InvokeIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    approval_id: str | None = None


class ClientIn(BaseModel):
    name: str


class GrantIn(BaseModel):
    role: str = "viewer"
    tools: list[str] = Field(default_factory=lambda: ["get_finding_evidence"])
    quotas: dict[str, int] = Field(default_factory=dict)
    # S10-T3: registry tools by pattern (metric.*), calls per minute across the grant, expiry
    tool_patterns: list[str] = Field(default_factory=list)
    rate_per_min: int | None = None
    expires_at: datetime | None = None
    # P2-8: limit the grant to a slice of the workspace (context tools only); empty = the whole workspace
    domains: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)


def _server_out(srv: McpServer) -> dict[str, Any]:
    out = row(srv, exclude={"secret_ref"})
    out["has_secret"] = bool(srv.secret_ref)
    out["capabilities"] = [m.model_dump() for m in mcp_client.manifests_for(srv)] if srv.allowed else []
    return out


# ------------------------------------------------------------------------------------ client side (P4-X05)
@router.post("/workspaces/{workspace_id}/mcp/servers", status_code=201)
def register_server(workspace_id: str, body: ServerIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    srv = mcp_client.register_server(session, user, workspace_id, name=body.name, url=body.url, secret_ref=body.secret_ref,
                                     transport=body.transport, config=body.config)
    return _server_out(srv)


@router.get("/workspaces/{workspace_id}/mcp/servers")
def list_servers(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return [_server_out(s) for s in session.scalars(select(McpServer).where(McpServer.workspace_id == workspace_id)
                                                    .order_by(McpServer.name))]


@router.post("/workspaces/{workspace_id}/mcp/servers/{server_id}/allow")
def allow_server(workspace_id: str, server_id: str, body: AllowIn, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    load_in_workspace(session, McpServer, server_id, workspace_id, user=user, label="MCP server")
    return _server_out(mcp_client.set_allowed(session, user, workspace_id, server_id, body.allowed))


@router.post("/workspaces/{workspace_id}/mcp/servers/{server_id}/refresh")
def refresh_server(workspace_id: str, server_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, McpServer, server_id, workspace_id, user=user, label="MCP server")
    return _server_out(mcp_client.refresh_tools(session, user, workspace_id, server_id))


@router.post("/workspaces/{workspace_id}/mcp/servers/{server_id}/tools/{tool_name}/classify")
def classify_tool(workspace_id: str, server_id: str, tool_name: str, body: ClassifyIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    load_in_workspace(session, McpServer, server_id, workspace_id, user=user, label="MCP server")
    return _server_out(mcp_client.classify_tool(session, user, workspace_id, server_id, tool_name, body.side_effect))


@router.get("/workspaces/{workspace_id}/mcp/capabilities")
def workspace_capabilities(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return [m.model_dump() for m in mcp_client.workspace_manifests(session, workspace_id)]


@router.post("/workspaces/{workspace_id}/mcp/servers/{server_name}/tools/{tool_name}/invoke")
def invoke_tool(workspace_id: str, server_name: str, tool_name: str, body: InvokeIn, user: User = Depends(current_user)):
    out = mcp_client.invoke_tool(session_scope, user, workspace_id, server_name, tool_name, body.arguments,
                                 run_id=body.run_id, approval_id=body.approval_id)
    return JSONResponse(status_code=202, content=out) if out["status"] == "approval_required" else out


# ------------------------------------------------------------------------------------ server side (P4-X06)
@router.post("/admin/mcp/clients", status_code=201)
def create_client(body: ClientIn, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    client, token = mcp_grants.create_client(session, admin, body.name)
    return {"client_id": client.id, "name": client.name, "token": token,
            "note": "the token is shown once; store it in the client's secret store"}


@router.get("/admin/mcp/clients")
def list_clients(admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    return mcp_grants.list_clients(session)


@router.post("/admin/mcp/clients/{client_id}/revoke")
def revoke_client(client_id: str, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    c = mcp_grants.revoke_client(session, admin, client_id)
    return {"client_id": c.id, "status": c.status}


@router.put("/admin/mcp/clients/{client_id}/grants/{workspace_id}")
def set_grant(client_id: str, workspace_id: str, body: GrantIn, admin: User = Depends(admin_user),
              session: Session = Depends(db, scope="function")):
    g = mcp_grants.set_grant(session, admin, client_id, workspace_id, role=body.role, tools=body.tools, quotas=body.quotas,
                             tool_patterns=body.tool_patterns, rate_per_min=body.rate_per_min, expires_at=body.expires_at,
                             domains=body.domains, assets=body.assets)
    return {"client_id": g.client_id, "workspace_id": g.workspace_id, "role": g.role, "tools": g.tools, "quotas": g.quotas,
            "tool_patterns": g.tool_patterns, "rate_per_min": g.rate_per_min, "domains": g.domains, "assets": g.assets,
            "expires_at": g.expires_at.isoformat() if g.expires_at else None}


@router.delete("/admin/mcp/clients/{client_id}/grants/{workspace_id}", status_code=204)
def delete_grant(client_id: str, workspace_id: str, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    mcp_grants.delete_grant(session, admin, client_id, workspace_id)

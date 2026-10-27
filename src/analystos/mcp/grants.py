"""MCP server clients, grants and quotas (P4-X06, the DataPilot pattern).

A client authenticates with the bearer `client_id.secret`. Only a SHA-256 of the secret is stored
(the secret is 256 random bits, so a fast hash is sufficient; there is nothing to brute-force) and
it is shown once, at creation. Each client is backed by a *service user* that holds a
`workspace_member` row per grant, so everything the client does goes through the normal scope
resolution, role checks and gateway. Grants are capped at `analyst`: an MCP client can never
publish, approve or administer.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.core.errors import BudgetExceeded, Forbidden, InvalidInput, NotFound, Unauthenticated
from analystos.core.ids import new_id, utcnow
from analystos.db.models import McpClient, McpGrant, McpUsage, User, WorkspaceMember
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace

TOOLS = ("ask", "investigate", "get_finding_evidence", "validate_sql", "request.create", "request.status")
# P7-11: a workspace's published query tools and tested/certified read-only HTTP tools, granted by name
# (`query.<key>`, `http.<capability id>`) or by family (`query.*`, `http.*`)
WORKSPACE_TOOL_PREFIXES = ("query.", "http.")
# S10: registry tools (data products, playbooks, admitted external MCP tools), granted by id or pattern
REGISTRY_TOOL_PREFIXES = ("metric.", "verified_query.", "dataset.", "playbook.", "mcp.")
PATTERN = re.compile(r"^[a-z][a-z0-9_.*-]{0,199}$")
RATE_KEY = "__rate__"  # mcp_usage row counting a grant's calls in one UTC minute
RESOURCE_READ = "resources/read"  # quota key for resource reads
GRANT_ROLES = ("viewer", "analyst")
TOOL_MIN_ROLE = {"ask": "analyst", "investigate": "analyst", "validate_sql": "analyst",
                 "get_finding_evidence": "viewer", RESOURCE_READ: "viewer",
                 # Request Desk (S13): a request may run Ask or start an investigation, so it needs the grant those need
                 "request.create": "analyst", "request.status": "viewer"}
DEFAULT_DAILY_QUOTA = 1000


class QuotaExceeded(BudgetExceeded):
    code = "quota_exceeded"


def is_workspace_tool(name: str) -> bool:
    return any(name.startswith(p) and len(name) > len(p) for p in WORKSPACE_TOOL_PREFIXES)


def is_registry_tool(name: str) -> bool:
    return any(name.startswith(p) and len(name) > len(p) for p in REGISTRY_TOOL_PREFIXES)


def _patternable(name: str) -> bool:
    return is_workspace_tool(name) or is_registry_tool(name)


def min_role(tool: str) -> str:
    return "analyst" if _patternable(tool) else TOOL_MIN_ROLE[tool]


def grants_tool(tools: list[str], tool: str, patterns: list[str] | tuple[str, ...] = ()) -> bool:
    """An exact grant, the family wildcard, or a tool pattern; patterns never cover the built-in tools."""
    if tool in tools:
        return True
    if not _patternable(tool):
        return False
    return f"{tool.split('.', 1)[0]}.*" in tools or any(fnmatch.fnmatchcase(tool, p) for p in patterns)


def _check_patterns(patterns: list[str]) -> list[str]:
    out = list(dict.fromkeys(patterns))
    families = (*WORKSPACE_TOOL_PREFIXES, *REGISTRY_TOOL_PREFIXES)
    bad = [p for p in out if not PATTERN.match(p) or not any(p.startswith(f) for f in families)]
    if bad:
        raise InvalidInput(f"tool patterns {', '.join(bad)} must start with a tool family ({', '.join(families)})")
    return out


def expired(grant: dict[str, Any]) -> bool:
    exp = grant.get("expires_at")
    if exp is None:
        return False
    if exp.tzinfo is None:  # SQLite returns naive datetimes; the column is UTC
        exp = exp.replace(tzinfo=UTC)
    return exp <= utcnow()


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class ClientPrincipal:
    """The authenticated caller of one MCP request."""

    client_id: str
    user_id: str
    grants: dict[str, dict[str, Any]] = field(default_factory=dict)  # workspace_id -> {role, tools, quotas}
    tenant_id: str | None = None  # the service user's home tenant (ADR-009)


# ------------------------------------------------------------------------------------ administration
def create_client(session: Session, admin: User, name: str) -> tuple[McpClient, str]:
    """Returns the client and its bearer token. The token is not recoverable afterwards."""
    if not (name or "").strip():
        raise InvalidInput("client name is required")
    client_id = new_id("mcpc")
    secret = secrets.token_urlsafe(32)
    user = User(id=new_id("usr"), email=f"{client_id}@mcp-clients.analystos.local", name=f"MCP client: {name.strip()[:150]}",
                password_hash="!", is_admin=False, active=True,  # "!" never verifies: no interactive login
                attributes={"service_account": "mcp_client", "client_id": client_id})
    session.add(user)
    session.flush()
    client = McpClient(id=client_id, name=name.strip()[:200], secret_hash=hash_secret(secret), service_user_id=user.id,
                       status="active", created_by=admin.id)
    session.add(client)
    session.flush()
    audit(f"user:{admin.id}", "mcp.client_created", target=client_id, decision="allow",
          details={"name": client.name, "service_user": user.id}, session=session)
    return client, f"{client_id}.{secret}"


def _client(session: Session, client_id: str) -> McpClient:
    client = session.get(McpClient, client_id)
    if client is None:
        raise NotFound(f"MCP client {client_id} not found")
    return client


def revoke_client(session: Session, admin: User, client_id: str) -> McpClient:
    client = _client(session, client_id)
    client.status, client.revoked_at = "revoked", utcnow()
    user = session.get(User, client.service_user_id)
    if user is not None:
        user.active = False
    session.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == client.service_user_id))
    audit(f"user:{admin.id}", "mcp.client_revoked", target=client_id, decision="deny", session=session)
    return client


def set_grant(session: Session, admin: User, client_id: str, workspace_id: str, *, role: str = "viewer",
              tools: list[str] | None = None, quotas: dict[str, int] | None = None,
              tool_patterns: list[str] | None = None, rate_per_min: int | None = None,
              expires_at: datetime | None = None) -> McpGrant:
    client = _client(session, client_id)
    if client.status != "active":
        raise Forbidden("the client is revoked")
    get_workspace(session, workspace_id)
    if role not in GRANT_ROLES:
        raise InvalidInput(f"an MCP grant role must be one of {', '.join(GRANT_ROLES)}")
    tools = list(dict.fromkeys(tools if tools is not None else ["get_finding_evidence"]))
    unknown = [t for t in tools if t not in TOOLS and not _patternable(t)]
    if unknown:
        raise InvalidInput(f"unknown MCP tools: {', '.join(unknown)} (known: {', '.join(TOOLS)}, query.<key>, http.<id>, "
                           "query.*, http.*, or a registry tool id such as metric.<name>)")
    patterns = _check_patterns(tool_patterns or [])
    too_low = [t for t in tools if min_role(t) == "analyst" and role != "analyst"]
    if (too_low or patterns) and role != "analyst":  # every pattern family needs an analyst grant
        raise InvalidInput(f"tools {', '.join([*too_low, *patterns])} need an analyst grant")
    if rate_per_min is not None and (not isinstance(rate_per_min, int) or not 1 <= rate_per_min <= 100_000):
        raise InvalidInput("rate_per_min must be a positive integer (calls per minute across the grant)")
    if expires_at is not None and expires_at.tzinfo is None:
        raise InvalidInput("expires_at must carry a time zone")
    quotas = dict(quotas or {})
    for k, v in quotas.items():
        if (k not in (*TOOLS, RESOURCE_READ) and not _patternable(k)) or not isinstance(v, int) or v < 1:
            raise InvalidInput(f"quota {k!r} must name a tool (or {RESOURCE_READ}) and be a positive integer")
    grant = session.scalar(select(McpGrant).where(McpGrant.client_id == client_id, McpGrant.workspace_id == workspace_id))
    if grant is None:
        grant = McpGrant(client_id=client_id, workspace_id=workspace_id, created_by=admin.id)
        session.add(grant)
    grant.role, grant.tools, grant.quotas = role, tools, quotas
    grant.tool_patterns, grant.rate_per_min, grant.expires_at = patterns, rate_per_min, expires_at
    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                                          WorkspaceMember.user_id == client.service_user_id))
    if member is None:
        session.add(WorkspaceMember(workspace_id=workspace_id, user_id=client.service_user_id, role=role))
    else:
        member.role = role
    session.flush()
    audit(f"user:{admin.id}", "mcp.grant_set", workspace_id=workspace_id, target=client_id, decision="allow",
          details={"role": role, "tools": tools, "quotas": quotas, "tool_patterns": patterns, "rate_per_min": rate_per_min,
                   "expires_at": expires_at.isoformat() if expires_at else None}, session=session)
    return grant


def delete_grant(session: Session, admin: User, client_id: str, workspace_id: str) -> None:
    client = _client(session, client_id)
    session.execute(delete(McpGrant).where(McpGrant.client_id == client_id, McpGrant.workspace_id == workspace_id))
    session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                                  WorkspaceMember.user_id == client.service_user_id))
    audit(f"user:{admin.id}", "mcp.grant_revoked", workspace_id=workspace_id, target=client_id, decision="deny",
          session=session)


# ------------------------------------------------------------------------------------ request time
def authenticate(session: Session, token: str | None) -> ClientPrincipal:
    """Bearer `client_id.secret` -> principal. Constant-time comparison; one message for every failure
    (the caller audits it in its own transaction, since raising rolls this one back)."""
    client_id, _, secret = (token or "").partition(".")
    client = session.get(McpClient, client_id) if client_id and secret else None
    ok = client is not None and hmac.compare_digest(hash_secret(secret), client.secret_hash) and client.status == "active"
    user = session.get(User, client.service_user_id) if ok else None
    if not ok or user is None or not user.active:
        raise Unauthenticated("invalid MCP client credentials")
    client.last_used_at = utcnow()
    grants = {g.workspace_id: {"role": g.role, "tools": list(g.tools or []), "quotas": dict(g.quotas or {}),
                               "tool_patterns": list(g.tool_patterns or []), "rate_per_min": g.rate_per_min,
                               "expires_at": g.expires_at}
              for g in session.scalars(select(McpGrant).where(McpGrant.client_id == client.id))}
    return ClientPrincipal(client_id=client.id, user_id=user.id, grants=grants, tenant_id=user.tenant_id)


def resolve_workspace(principal: ClientPrincipal, workspace_id: str | None) -> str | None:
    """An omitted workspace means the only granted one; ambiguity is the caller's to resolve."""
    if workspace_id:
        return str(workspace_id)
    return next(iter(principal.grants)) if len(principal.grants) == 1 else None


def check_grant(principal: ClientPrincipal, workspace_id: str | None, tool: str) -> dict[str, Any]:
    """Raise Forbidden unless the client holds a grant covering this workspace and tool."""
    if not workspace_id:
        raise Forbidden("workspace_id is required (the client holds grants for several workspaces)"
                        if principal.grants else "the client holds no workspace grant")
    grant = principal.grants.get(workspace_id)
    if grant is None:
        raise Forbidden(f"no grant for workspace {workspace_id}")
    if expired(grant):
        raise Forbidden(f"the grant for workspace {workspace_id} expired")
    if tool != RESOURCE_READ and not grants_tool(grant["tools"], tool, grant.get("tool_patterns") or ()):
        raise Forbidden(f"the grant for workspace {workspace_id} does not include tool {tool}")
    return grant


def _count(session: Session, principal: ClientPrincipal, workspace_id: str, tool: str, day: str) -> int:
    stmt = insert(McpUsage).values(client_id=principal.client_id, workspace_id=workspace_id, tool=tool[:200], day=day, count=1)
    stmt = stmt.on_conflict_do_update(index_elements=["client_id", "workspace_id", "tool", "day"],
                                      set_={"count": McpUsage.count + 1}).returning(McpUsage.count)
    return int(session.execute(stmt).scalar_one())


def consume_quota(session: Session, principal: ClientPrincipal, workspace_id: str, tool: str) -> int:
    """Atomically count this call against the grant's per-minute rate and today's quota; raise QuotaExceeded
    (429) past either."""
    grant = principal.grants[workspace_id]
    now = utcnow()
    rate = grant.get("rate_per_min")
    if rate:
        minute = now.strftime("%Y-%m-%dT%H:%M")
        if _count(session, principal, workspace_id, RATE_KEY, minute) > int(rate):
            raise QuotaExceeded(f"rate of {rate} calls per minute exhausted for this client's grant",
                                details={"limit": int(rate), "minute": minute})
    limit = int(grant["quotas"].get(tool, DEFAULT_DAILY_QUOTA))
    day = now.strftime("%Y-%m-%d")
    used = _count(session, principal, workspace_id, tool, day)
    if used > limit:
        raise QuotaExceeded(f"daily quota of {limit} calls to {tool} exhausted for this client",
                            details={"tool": tool, "limit": limit, "day": day})
    return used


def list_clients(session: Session) -> list[dict[str, Any]]:
    out = []
    for c in session.scalars(select(McpClient).order_by(McpClient.created_at)):
        grants = session.scalars(select(McpGrant).where(McpGrant.client_id == c.id))
        out.append({"client_id": c.id, "name": c.name, "status": c.status, "service_user_id": c.service_user_id,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
                    "grants": [{"workspace_id": g.workspace_id, "role": g.role, "tools": g.tools, "quotas": g.quotas,
                                "tool_patterns": g.tool_patterns or [], "rate_per_min": g.rate_per_min,
                                "expires_at": g.expires_at.isoformat() if g.expires_at else None}
                               for g in grants]})
    return out

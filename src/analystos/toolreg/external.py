"""External MCP admission (S10-T4, FR-047; finishes P4-X05): admitted tools enter the registry quarantined.

* **Allowlisted host only.** A server is registered and allowed through `mcp/client.py`, which checks the
  platform host allowlist at registration and pins every connection to the vetted address (egress goes only
  to the admitted host).
* **Screened, hashed descriptions.** Admission runs `tools/list`; each tool's name, description and schema
  text are injection-screened and the raw definition hashed. A tool that fails screening is registered as
  `refused` (never invocable, never classifiable). The description is data: it is shown, never obeyed.
* **Quarantine.** Every newly admitted tool, and every tool whose definition hash changed since the last
  admission (a re-screen), is a new version with `side_effect: write_external` and status `quarantined`;
  older versions of a changed tool are retired at once, because the server now serves something else.
* **Classification.** An owner classifies a quarantined version (side effect none / write_internal /
  write_external): it is re-signed with that side effect and becomes certified. The MCP client's own
  classification is bound to the same definition hash, so the two cannot drift.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.contracts.tool_manifest import ToolExecutionSpec, ToolExposure, ToolManifestV2
from analystos.core.errors import Conflict, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import utcnow
from analystos.db.models import McpServer, RegisteredTool, User
from analystos.governance.policy import require_role
from analystos.mcp import client as mcp_client
from analystos.toolreg import store

ADMIN_ROLE = "owner"
CLASSIFIABLE = ("none", "write_internal", "write_external")


def tool_id_for(server: str, tool: dict[str, Any]) -> str:
    return f"mcp.{server}.{tool['cap_name']}"


def _manifest(srv: McpServer, tool: dict[str, Any], version: str, side_effect: str) -> ToolManifestV2:
    tool_id = tool_id_for(srv.name, tool)
    schema = dict(tool.get("input_schema") or {"type": "object"})
    if schema.get("type") != "object":
        schema = {"type": "object"}
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
    except Exception:  # an unusable third-party schema: arguments are passed through unchecked, still screened
        schema = {"type": "object"}
    return ToolManifestV2(
        tool_id=tool_id, version=version, kind="external_mcp", owner=f"mcp:{srv.name}", title=tool["name"][:300],
        description=str(tool.get("description") or "")[:2000], input_schema=schema, side_effect=side_effect,  # type: ignore[arg-type]
        permissions=["external.call"], data_classification_max="internal",
        execution=ToolExecutionSpec(via="mcp", ref={"server": srv.name, "server_id": srv.id, "tool": tool["name"],
                                                    "tool_hash": tool["hash"]}),
        exposure=ToolExposure(rest=True, mcp=False, grants_required=[tool_id, f"mcp.{srv.name}.*"]))


def sync_server(session: Session, srv: McpServer, *, actor: str) -> dict[str, list[str]]:
    """Mirror the server's screened tool list into the registry. Returns what was admitted, re-screened,
    refused and retired."""
    out: dict[str, list[str]] = {"quarantined": [], "rescreened": [], "refused": [], "retired": [], "unchanged": []}
    offered: set[str] = set()
    for tool in srv.tools or []:
        tool_id = tool_id_for(srv.name, tool)
        offered.add(tool_id)
        rows = store.versions(session, srv.workspace_id, tool_id)
        latest = rows[-1] if rows else None
        if latest is not None and latest.description_hash == tool["hash"] and latest.status != "retired":
            out["unchanged"].append(tool_id)
            continue
        changed = latest is not None
        for old in rows:
            if old.status not in ("retired", "refused"):
                store.set_status(session, old, "retired", actor=actor,
                                 reason="the server changed this tool's definition; the new definition was re-screened")
        major = (latest.major + 1) if latest is not None else 1
        status = "refused" if tool.get("refused") else "quarantined"
        m = _manifest(srv, tool, f"{major}.0.0", "write_external")
        store.register(session, srv.workspace_id, m, status=status, actor=actor, supersede=False,
                       source={"type": "mcp_server", "id": srv.id, "name": srv.name, "tool": tool["name"]},
                       description_hash=tool["hash"],
                       screening={"flags": list(tool.get("flags") or []), "screened_at": utcnow().isoformat(),
                                  "read_only_hint": tool.get("read_only_hint"), "rescreen": changed})
        out["refused" if status == "refused" else ("rescreened" if changed else "quarantined")].append(tool_id)
    from sqlalchemy import select

    for row in session.scalars(select(RegisteredTool).where(RegisteredTool.workspace_id == srv.workspace_id,
                                                            RegisteredTool.kind == "external_mcp",
                                                            RegisteredTool.tool_id.like(f"mcp.{srv.name}.%"))):
        if row.tool_id not in offered and row.status not in ("retired",):
            store.set_status(session, row, "retired", actor=actor, reason="no longer offered by the server")
            out["retired"].append(row.tool_id)
    return out


def admit(session: Session, user: User, workspace_id: str, server_id: str) -> dict[str, Any]:
    """`tools/list` (screened) and registry sync. The server must already be allowed by an owner and its host
    be on the platform MCP host allowlist (checked again on the connection)."""
    require_role(session, user, workspace_id, ADMIN_ROLE)
    srv = mcp_client.refresh_tools(session, user, workspace_id, server_id)
    result = sync_server(session, srv, actor=f"user:{user.id}")
    return {"server": srv.name, "server_id": srv.id, **result}


def classify(session: Session, user: User, workspace_id: str, tool_ref: str, side_effect: str) -> RegisteredTool:
    """An owner's verdict on a quarantined (or re-classified certified) version: re-signed with the side effect."""
    require_role(session, user, workspace_id, ADMIN_ROLE)
    if side_effect not in CLASSIFIABLE:
        raise InvalidInput(f"side_effect must be one of {', '.join(CLASSIFIABLE)}")
    tool_id, _, pin = tool_ref.partition("@")
    rows = [r for r in store.versions(session, workspace_id, tool_id) if not pin or r.version == pin]
    if not rows:
        raise NotFound(f"tool {tool_ref} not found")
    row = rows[-1]
    if row.kind != "external_mcp":
        raise InvalidInput(f"{tool_id} is not an external MCP tool")
    if row.status == "refused":
        raise PolicyDenied(f"tool {tool_id} was refused at screening ({', '.join(row.screening.get('flags') or [])}) "
                           "and cannot be classified")
    if row.status not in ("quarantined", "certified"):
        raise PolicyDenied(f"tool {row.tool_id}@{row.version} is {row.status}")
    m = ToolManifestV2.model_validate(row.manifest)
    srv = mcp_client.server_by_name(session, workspace_id, m.execution.ref["server"])
    current = next((t for t in srv.tools or [] if t["name"] == m.execution.ref["tool"]), None)
    if current is None or current["hash"] != row.description_hash:
        raise Conflict(f"tool {tool_id} changed on its server since it was screened: admit the server again first")
    mcp_client.classify_tool(session, user, workspace_id, srv.id, current["name"], side_effect)
    major, minor, _ = (int(x) for x in row.version.split("."))
    new = _manifest(srv, current, f"{major}.{minor + 1}.0", side_effect)
    actor = f"user:{user.id}"
    store.set_status(session, row, "retired", actor=actor, reason=f"classified as {side_effect} in {new.version}")
    return store.register(session, workspace_id, new, status="certified", actor=actor, supersede=True,
                          source=dict(row.source), description_hash=row.description_hash,
                          screening={**(row.screening or {}), "classified_by": user.id, "classified_at": utcnow().isoformat(),
                                     "side_effect": side_effect})

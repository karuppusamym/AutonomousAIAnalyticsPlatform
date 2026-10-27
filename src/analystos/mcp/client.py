"""MCP client (P4-X05): workspace-registered MCP servers become governed Tool capabilities.

Security model, in order of what can go wrong:

* **Platform host allowlist (SSRF).** A workspace can register, and connect to, only a server whose
  host matches the administrator's allowlist (`ANALYSTOS_MCP_HOST_ALLOWLIST` + platform setting
  `outbound.mcp_host_allowlist`); the host is resolved and every address checked at registration and
  again on each connection, which is pinned to the vetted address (`_pin`, `tools/http.py`).
* **Allowlist.** A registered server is inert until a workspace owner (or platform admin) sets
  `allowed`. Nothing — not even `tools/list` — is sent to a server that is not allowed.
* **Credentials by reference.** `secret_ref` is `env:NAME` / `file:/path`, resolved at call time
  and sent only as the bearer token to that server. Values never reach a row, a log or a prompt.
* **Untrusted descriptions.** Tool names, descriptions and input-schema text come from a third
  party and are screened; a tool whose text carries an instruction-to-the-model pattern is
  refused (recorded, never registered as a capability).
* **Unknown side effect = `write_external`.** An imported tool needs an approval until an owner
  classifies it. MCP `readOnlyHint` is recorded as advisory only. A classification is bound to the
  tool's definition hash, so a server that silently changes a tool loses the classification.
* **One gate per call.** Workspace policy (role, `tool_denylist`, autonomy), a per-run call budget,
  a hash-bound, single-use approval for `write_external` (`verify_for_execution` immediately before
  the call), a `tool_execution` row and an audit event. Results are size-capped and screened
  before they are returned, so nothing raw from the server can reach a prompt.
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.capabilities import registry as cap_registry
from analystos.contracts.capability import SIDE_EFFECT_ORDER, CapabilityManifest
from analystos.contracts.policy import ExecutionIdentity
from analystos.contracts.registry import ToolSpec
from analystos.core.errors import (
    AnalystOSError,
    ApprovalRequired,
    BudgetExceeded,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    PolicyDenied,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import AnalysisRun, Approval, McpServer, ToolExecution, User
from analystos.events.bus import emit
from analystos.governance.approvals import consume, request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import evaluate, get_workspace, load_policy, member_role, require_role
from analystos.skills.catalog import has_injection, screen_text
from analystos.tools import http as outbound

SERVER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SIDE_EFFECTS = tuple(SIDE_EFFECT_ORDER)
TRANSPORTS = ("streamable_http",)
APPROVAL_ACTION = "mcp_tool_call"
MAX_TOOLS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_SCHEMA_CHARS = 20_000
RESULT_MAX_CHARS = 8_000
RESULT_MAX_CHARS_CEILING = 48_000  # a server's `result_max_chars` config may raise the default up to this (knowledge tools)
DEFAULT_MAX_CALLS_PER_RUN = 50
DEFAULT_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


# ------------------------------------------------------------------------------------ transport
def _run_async(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine from sync code; if this thread already has a loop, use a worker thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _private_hosts() -> list[str]:
    from analystos.core.config import get_settings

    s = get_settings()
    return outbound.split_hosts(s.outbound_private_hosts) + outbound.split_hosts(s.mcp_private_hosts)


def _operator_private_hosts() -> list[str]:
    from analystos.core.config import get_settings

    return outbound.split_hosts(get_settings().outbound_private_hosts)


def _platform_allowlist() -> list[str]:
    """The platform MCP host allowlist: deployment env plus the admin's runtime setting."""
    from analystos.core.config import get_settings
    from analystos.services import platform_settings

    return outbound.split_hosts(get_settings().mcp_host_allowlist) + list(platform_settings.get().outbound.mcp_host_allowlist)


def _air_gapped() -> bool:
    from analystos.core.config import get_settings

    return bool(get_settings().air_gapped)


def _pin(url: str, resolver: outbound.Resolver | None = None) -> outbound.PinnedTarget:
    """P4-X05: the one check behind registration and every connection. The host must match the platform
    allowlist; a private address is reachable only for an exact (non-wildcard) entry, within the MCP
    private ranges or a listed network (or the operator's outbound_private_hosts); link-local/metadata
    never; air-gapped installs reach internal addresses only. Resolved here, connected to that address."""
    _, host, port = outbound.parse_url(url)
    entries = _platform_allowlist()
    match = outbound.match_host(host, port, entries)
    if match is None:
        raise outbound.OutboundRefused(f"host {host} (port {port}) is not on the platform MCP host allowlist; a platform "
                                       "administrator must add it (outbound.mcp_host_allowlist)")
    private = _operator_private_hosts()
    if match == "explicit":
        networks = [e for e in entries if _is_network_entry(e)]
        private += [h for h in _private_hosts() if h not in private] + networks
    return outbound.pin(url, allowlist=None, private_hosts=private, resolver=resolver, internal_only=_air_gapped())


def _is_network_entry(entry: str) -> bool:
    try:
        return outbound.parse_entry(entry).kind == "network"
    except InvalidInput:
        return False


def _bearer(server: McpServer) -> dict[str, str]:
    from analystos.connectors.secrets import resolve_secret

    token = resolve_secret(server.secret_ref, purpose="mcp")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _with_client(server: McpServer, fn: Callable[[Any], Awaitable[T]]) -> T:
    import httpx

    timeout = float((server.config or {}).get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    # P7-11 / P4-X05: the owner's `allowed` flag says the workspace uses this server; the platform host
    # allowlist and the address checks are re-applied on every connection (an admin's removal takes effect
    # at once), and every request goes to the address vetted here (no rebinding, no redirects, no proxies).
    target = _pin(server.url)
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with (httpx.AsyncClient(headers=_bearer(server), timeout=httpx.Timeout(timeout), follow_redirects=False,
                                  trust_env=False, transport=outbound.pinned_async_transport(target, httpx)) as http,
                Client(streamable_http_client(server.url, http_client=http), read_timeout_seconds=timeout,
                       cache=None) as client):
        return await fn(client)


def _call_remote(server: McpServer, fn: Callable[[Any], Awaitable[T]]) -> T:
    try:
        return _run_async(lambda: _with_client(server, fn))
    except AnalystOSError:
        raise
    except BaseException as exc:  # transport, protocol and task-group errors all mean "server unavailable"
        detail = exc
        while isinstance(detail, BaseExceptionGroup) and detail.exceptions:
            detail = detail.exceptions[0]
        raise UpstreamUnavailable(f"MCP server {server.name} unavailable: {type(detail).__name__}: {str(detail)[:200]}") from None


# ------------------------------------------------------------------------------------ screening
def cap_id(server_name: str, tool_name: str) -> str:
    return f"tool.mcp_{server_name}_{normalize_tool_name(tool_name)}"


def normalize_tool_name(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", name.lower())).strip("_")[:60] or "tool"


def _schema_texts(node: Any, depth: int = 0) -> list[str]:
    if depth > 12:
        return []
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k in ("description", "title", "default", "examples", "pattern"):
                out.append(v)
            else:
                out.extend(_schema_texts(v, depth + 1))
            if isinstance(k, str):
                out.append(k)
    elif isinstance(node, list):
        for v in node:
            out.extend(_schema_texts(v, depth + 1) if not isinstance(v, str) else [v])
    return out


def _screen_schema(node: Any, depth: int = 0) -> Any:
    if depth > 12:
        return {}
    if isinstance(node, dict):
        return {k: (screen_text(v, max_chars=300) if k in ("description", "title") and isinstance(v, str)
                    else _screen_schema(v, depth + 1)) for k, v in node.items()}
    if isinstance(node, list):
        return [_screen_schema(v, depth + 1) for v in node]
    return node


def screen_tool(tool: Any) -> dict[str, Any]:
    """One `tools/list` entry -> the stored, screened record. Refused tools are kept for review only."""
    import json

    name = str(tool.name)
    description = tool.description or ""
    schema = dict(tool.input_schema or {})
    annotations = getattr(tool, "annotations", None)
    flags: list[str] = []
    texts = [name, description, getattr(tool, "title", None) or "", *_schema_texts(schema)]
    if any(has_injection(t) for t in texts):
        flags.append("injection_pattern")
    if len(json.dumps(schema, default=str)) > MAX_SCHEMA_CHARS:
        flags.append("schema_too_large")
        schema = {"type": "object"}
    if len(description) > 4 * MAX_DESCRIPTION_CHARS:
        flags.append("description_too_long")
    return {"name": name, "cap_name": normalize_tool_name(name),
            "description": screen_text(description, max_chars=MAX_DESCRIPTION_CHARS),
            "input_schema": _screen_schema(schema),
            "read_only_hint": getattr(annotations, "read_only_hint", None) if annotations else None,
            "destructive_hint": getattr(annotations, "destructive_hint", None) if annotations else None,
            # The hash covers the raw definition: any change on the server voids a classification.
            "hash": stable_hash({"name": name, "description": description, "input_schema": tool.input_schema or {}}),
            "refused": bool(flags), "flags": flags}


def _screen_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[nested value omitted]"
    if isinstance(value, str):
        return screen_text(value, max_chars=1000)
    if isinstance(value, dict):
        return {screen_text(str(k), max_chars=100): _screen_value(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [_screen_value(v, depth + 1) for v in value[:200]]
    return value


def screen_result(result: Any, max_chars: int = RESULT_MAX_CHARS, *, lift_json: bool = False) -> dict[str, Any]:
    """CallToolResult -> capped, screened dict. Only text and structured content survive.

    `lift_json` (server config `lift_json_blocks`): a server that returns its structured answer as a
    fenced ```json text block (Atlas's knowledge tools do) has that block parsed into structured
    content *before* screening, so it is screened value by value instead of being flattened, and
    removed from the text."""
    import json

    texts: list[str] = []
    omitted: list[str] = []
    for block in result.content or []:
        if getattr(block, "type", None) == "text":
            texts.append(block.text)
        else:
            omitted.append(str(getattr(block, "type", "unknown")))
    raw = "\n".join(texts)
    structured = getattr(result, "structured_content", None)
    if lift_json and structured is None:
        blocks = list(re.finditer(r"```json\s*\n(.*?)\n```", raw, re.S))
        if blocks:
            try:
                lifted = json.loads(blocks[-1].group(1))
            except ValueError:
                lifted = None
            if isinstance(lifted, dict):
                structured = lifted
                raw = (raw[:blocks[-1].start()] + raw[blocks[-1].end():]).strip()
    flags = ["injection_removed"] if has_injection(raw) else []
    text = screen_text(raw, max_chars=max_chars)
    truncated = len(raw) > max_chars
    if structured is not None:
        if has_injection(json.dumps(structured, default=str)) and "injection_removed" not in flags:
            flags.append("injection_removed")
        structured = _screen_value(structured)
        if len(json.dumps(structured, default=str)) > max_chars:
            structured, truncated = None, True
    return {"is_error": bool(getattr(result, "is_error", False)), "text": text, "structured": structured,
            "omitted_content": omitted, "truncated": truncated, "flags": flags}


# ------------------------------------------------------------------------------------ registration
def _server(session: Session, workspace_id: str, server_id: str) -> McpServer:
    srv = session.get(McpServer, server_id)
    if srv is None or srv.workspace_id != workspace_id:
        raise NotFound(f"MCP server {server_id} not found")
    return srv


def server_by_name(session: Session, workspace_id: str, name: str) -> McpServer:
    srv = session.scalar(select(McpServer).where(McpServer.workspace_id == workspace_id, McpServer.name == name))
    if srv is None:
        raise NotFound(f"MCP server {name} not found")
    return srv


def _validate_url(url: str, resolver: outbound.Resolver | None = None) -> str:
    """Shape errors are the caller's input (400); host and address refusals are policy (403). The host
    is resolved now as well as on every connection, so a server that could never be reached is not stored."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidInput("MCP server url must be an http(s) URL")
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidInput("put credentials in secret_ref (env:NAME or file:/path), never in the url")
    try:
        outbound.parse_url(url)
    except outbound.OutboundRefused as exc:
        raise InvalidInput(f"MCP server url: {exc.message}") from None
    _pin(url, resolver)
    return url.strip()


def _validate_secret_ref(ref: str | None) -> str | None:
    if ref is None or not ref.strip():
        return None
    if not re.match(r"^(env:[A-Za-z_][A-Za-z0-9_]*|file:/\S+)$", ref.strip()):
        raise InvalidInput("secret_ref must look like 'env:NAME' or 'file:/path' (a reference, never a value)")
    from analystos.connectors.secrets import check_secret_ref

    check_secret_ref(ref.strip())  # only process secrets the operator allows workspaces to name (S02)
    return ref.strip()


def register_server(session: Session, user: User, workspace_id: str, *, name: str, url: str,
                    secret_ref: str | None = None, transport: str = "streamable_http",
                    config: dict[str, Any] | None = None) -> McpServer:
    require_role(session, user, workspace_id, "owner")
    if not SERVER_NAME.match(name or ""):
        raise InvalidInput("server name must be lower snake case (letters, digits, underscore; max 40)")
    if transport not in TRANSPORTS:
        raise InvalidInput(f"transport {transport!r} is not supported (use one of {', '.join(TRANSPORTS)})")
    if session.scalar(select(McpServer.id).where(McpServer.workspace_id == workspace_id, McpServer.name == name)):
        raise Conflict(f"an MCP server named {name} is already registered in this workspace")
    cfg = {k: v for k, v in (config or {}).items() if k in ("max_calls_per_run", "timeout_seconds", "result_max_chars", "lift_json_blocks")}
    try:
        url = _validate_url(url)
    except outbound.OutboundRefused as exc:
        audit(f"user:{user.id}", "mcp.server_refused", workspace_id=workspace_id, target=name, decision="deny",
              reasons=["mcp_host_not_allowed"], details={"url": (url or "")[:300], "error": exc.message[:300]}, session=session)
        session.commit()  # the refusal is recorded although the caller sees the failure
        raise
    srv = McpServer(id=new_id("mcps"), workspace_id=workspace_id, name=name, url=url, transport=transport,
                    secret_ref=_validate_secret_ref(secret_ref), status="registered", allowed=False, config=cfg,
                    tools=[], classifications={}, created_by=user.id)
    session.add(srv)
    session.flush()
    audit(f"user:{user.id}", "mcp.server_registered", workspace_id=workspace_id, target=srv.id,
          details={"name": name, "url": srv.url, "transport": transport}, session=session)
    return srv


def set_allowed(session: Session, user: User, workspace_id: str, server_id: str, allowed: bool) -> McpServer:
    """The allowlist decision. Revoking also stops every invocation immediately."""
    require_role(session, user, workspace_id, "owner")
    srv = _server(session, workspace_id, server_id)
    if allowed:  # re-enabling needs the host on the platform allowlist today, not only when it was registered
        _, host, port = outbound.parse_url(srv.url)
        if outbound.match_host(host, port, _platform_allowlist()) is None:
            raise PolicyDenied(f"MCP host {host} is not on the platform MCP host allowlist")
    srv.allowed, srv.allowed_by = bool(allowed), user.id
    audit(f"user:{user.id}", "mcp.server_allowed" if allowed else "mcp.server_disallowed", workspace_id=workspace_id,
          target=srv.id, decision="allow" if allowed else "deny", details={"name": srv.name, "url": srv.url}, session=session)
    return srv


def refresh_tools(session: Session, user: User, workspace_id: str, server_id: str) -> McpServer:
    """`tools/list` -> screened snapshot. Classifications whose tool changed are dropped."""
    require_role(session, user, workspace_id, "owner")
    srv = _server(session, workspace_id, server_id)
    if not srv.allowed:
        raise PolicyDenied(f"MCP server {srv.name} is not on the workspace allowlist")

    async def list_all(client: Any) -> list[Any]:
        tools: list[Any] = []
        cursor = None
        for _ in range(20):
            page = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
            tools.extend(page.tools)
            cursor = getattr(page, "next_cursor", None)
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    try:
        raw = _call_remote(srv, list_all)
    except AnalystOSError as exc:
        srv.status, srv.last_error = "error", exc.message[:1000]
        audit(f"user:{user.id}", "mcp.server_refresh_failed", workspace_id=workspace_id, target=srv.id,
              decision="deny", reasons=[exc.code], details={"error": exc.message[:300]}, session=session)
        session.commit()  # keep the error state and the audit row although the caller sees the failure
        raise
    seen: set[str] = set()
    tools = []
    for t in raw:
        rec = screen_tool(t)
        if rec["cap_name"] in seen:
            rec["refused"], rec["flags"] = True, [*rec["flags"], "name_collision"]
        seen.add(rec["cap_name"])
        tools.append(rec)
    by_name = {t["name"]: t for t in tools}
    srv.classifications = {k: v for k, v in (srv.classifications or {}).items()
                           if k in by_name and by_name[k]["hash"] == v.get("hash")}
    srv.tools, srv.status, srv.last_error, srv.last_refreshed_at = tools, "ready", None, utcnow()
    refused = [t["name"] for t in tools if t["refused"]]
    audit(f"user:{user.id}", "mcp.server_refreshed", workspace_id=workspace_id, target=srv.id,
          decision="allow", details={"tools": len(tools), "refused": refused}, session=session)
    emit(workspace_id, "mcp.server.refreshed", {"server": srv.name, "tools": len(tools), "refused": refused},
         actor=f"user:{user.id}", session=session)
    return srv


def _tool(srv: McpServer, tool_name: str) -> dict[str, Any]:
    for t in srv.tools or []:
        if t["name"] == tool_name:
            return t
    raise NotFound(f"tool {tool_name} is not offered by MCP server {srv.name} (refresh the server?)")


def classify_tool(session: Session, user: User, workspace_id: str, server_id: str, tool_name: str,
                  side_effect: str) -> McpServer:
    require_role(session, user, workspace_id, "owner")
    if side_effect not in SIDE_EFFECTS:
        raise InvalidInput(f"side_effect must be one of {', '.join(SIDE_EFFECTS)}")
    srv = _server(session, workspace_id, server_id)
    tool = _tool(srv, tool_name)
    if tool["refused"]:
        raise PolicyDenied(f"tool {tool_name} was refused at import ({', '.join(tool['flags'])}) and cannot be classified")
    classes = dict(srv.classifications or {})
    classes[tool_name] = {"side_effect": side_effect, "hash": tool["hash"], "by": user.id, "at": utcnow().isoformat(),
                          "read_only_hint": tool.get("read_only_hint")}
    srv.classifications = classes
    audit(f"user:{user.id}", "mcp.tool_classified", workspace_id=workspace_id, target=cap_id(srv.name, tool_name),
          decision="allow", details={"server": srv.name, "tool": tool_name, "side_effect": side_effect,
                                     "read_only_hint": tool.get("read_only_hint")}, session=session)
    return srv


def effective_side_effect(srv: McpServer, tool: dict[str, Any]) -> str:
    c = (srv.classifications or {}).get(tool["name"])
    return c["side_effect"] if c and c.get("hash") == tool["hash"] else "write_external"


# ------------------------------------------------------------------------------------ capabilities
def manifests_for(srv: McpServer) -> list[CapabilityManifest]:
    out = []
    for t in srv.tools or []:
        if t["refused"]:
            continue
        side = effective_side_effect(srv, t)
        classified = _classified(srv, t)
        out.append(CapabilityManifest(
            kind="Tool", id=cap_id(srv.name, t["name"]), version="1.0.0",
            summary=(t["description"] or t["name"])[:200], entry=f"mcp://{srv.name}/{t['name']}",
            input_schema=t["input_schema"], determinism="model", side_effect=side, cost_class="query",
            permissions=["role:editor" if side == "write_external" else "role:analyst"],
            certification={"status": "draft"}, tags=["mcp"], source=f"mcp:{srv.name}",
            spec={"server": srv.name, "tool": t["name"], "hash": t["hash"], "classified": classified,
                  "read_only_hint": t.get("read_only_hint"), "workspace_id": srv.workspace_id}))
    return out


def workspace_manifests(session: Session, workspace_id: str) -> list[CapabilityManifest]:
    servers = session.scalars(select(McpServer).where(McpServer.workspace_id == workspace_id, McpServer.allowed.is_(True),
                                                      McpServer.status == "ready").order_by(McpServer.name))
    return [m for s in servers for m in manifests_for(s)]


def workspace_snapshot(session: Session, workspace_id: str) -> cap_registry.Snapshot:
    """The process registry plus this workspace's MCP tools (registered for this workspace only)."""
    return cap_registry.overlay(cap_registry.current(), workspace_manifests(session, workspace_id))


# ------------------------------------------------------------------------------------ invocation
def _tool_spec(cap: str, tool: dict[str, Any], side: str) -> ToolSpec:
    external = side == "write_external"
    return ToolSpec(tool_id=cap, name=tool["name"], description=tool["description"] or tool["name"], category="mcp",
                    risk="high" if external else "low", approval_policy="always" if external else "none",
                    side_effects="external_write" if external else ("internal_write" if side == "write_internal" else "none"),
                    runtime="remote_api", min_role="editor" if external else "analyst")


def _denylisted(denylist: list[str], cap: str, server_name: str) -> bool:
    return any(fnmatch.fnmatchcase(cap, p) or p == f"mcp:{server_name}" for p in denylist)


def _record(session: Session, base: dict[str, Any], *, status: str, output: dict[str, Any], decision: dict[str, Any],
            latency: int = 0, error: str | None = None) -> None:
    """One `tool_execution` row and one audit event per gate outcome (denied, approval_required, ok, error)."""
    session.add(ToolExecution(workspace_id=base["workspace_id"], run_id=base["run_id"], agent_id=base["agent_id"],
                              tool_id=base["cap"], status=status, input=_screen_value(base["inputs"]), output=output,
                              decision=decision, latency_ms=latency, error=error))
    audit(base["actor"], "mcp.tool_call", workspace_id=base["workspace_id"], run_id=base["run_id"], target=base["cap"],
          decision=decision.get("decision"), reasons=decision.get("reasons") or [],
          details={"server": base["server"], "tool": base["tool"], "status": status, "latency_ms": latency,
                   "error": (error or "")[:300] or None}, session=session)


def _classified(srv: McpServer, tool: dict[str, Any]) -> bool:
    return (srv.classifications or {}).get(tool["name"], {}).get("hash") == tool["hash"]


def approval_payload(srv: McpServer, tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """What an approval binds: this server URL, this exact tool definition, these arguments."""
    return {"server": srv.name, "server_id": srv.id, "url": srv.url, "tool": tool["name"], "tool_hash": tool["hash"],
            "arguments": arguments}


def invoke_tool(session_factory: Callable[[], Any], user: User, workspace_id: str, server_name: str, tool_name: str,
                arguments: dict[str, Any] | None = None, *, run_id: str | None = None, approval_id: str | None = None,
                agent_id: str | None = None) -> dict[str, Any]:
    """Gate, (approve), call, screen, record. `session_factory` is `session_scope`; decisions commit
    in their own transactions so a denial or an approval request is recorded even though we raise."""
    arguments = dict(arguments or {})
    actor = f"agent:{agent_id}" if agent_id else f"user:{user.id}"
    started = time.perf_counter()
    base = {"workspace_id": workspace_id, "run_id": run_id, "agent_id": agent_id, "actor": actor,
            "server": server_name, "tool": tool_name, "inputs": arguments}

    # 1) gate: registration, allowlist, refusal, policy, budget
    with session_factory() as s:
        require_role(s, s.merge(user), workspace_id, "viewer")
        srv = server_by_name(s, workspace_id, server_name)
        cap = cap_id(srv.name, tool_name)
        base["cap"] = cap
        reasons: list[str] = []
        tool: dict[str, Any] | None = None
        if not srv.allowed:
            reasons.append("mcp_server_not_allowlisted")
        elif srv.status != "ready":
            reasons.append(f"mcp_server_{srv.status}")
        else:
            tool = next((t for t in srv.tools or [] if t["name"] == tool_name), None)
            if tool is None:
                reasons.append("mcp_tool_unknown")
            elif tool["refused"]:
                reasons.append("mcp_tool_refused_at_import")
        side = effective_side_effect(srv, tool) if tool else "write_external"
        decision = None
        if not reasons:
            ws = get_workspace(s, workspace_id)
            policy = load_policy(s, ws)
            if _denylisted(policy.tool_denylist, cap, srv.name):
                reasons.append("tool_denied_by_workspace_policy")
            identity = ExecutionIdentity(user_id=user.id, workspace_id=workspace_id, run_id=run_id, agent_id=agent_id,
                                         tool_id=cap, purpose="mcp_tool")
            decision = evaluate(s, s.merge(user), identity, "tool", tool=_tool_spec(cap, tool, side))
            if decision.decision == "deny":
                reasons += decision.reasons
        if not reasons and run_id:
            run = s.get(AnalysisRun, run_id)
            if run is None or run.workspace_id != workspace_id:
                reasons.append("run_not_in_workspace")
            else:
                cap_calls = int((srv.config or {}).get("max_calls_per_run") or DEFAULT_MAX_CALLS_PER_RUN)
                used = s.scalar(select(func.count(ToolExecution.id)).where(
                    ToolExecution.run_id == run_id, ToolExecution.tool_id.like("tool.mcp\\_%"),
                    ToolExecution.status.in_(["ok", "error"]))) or 0
                if used >= cap_calls:
                    _record(s, base, status="denied", output={}, latency=0, error="budget_exceeded", decision={"decision": "deny", "reasons": ["mcp_calls_per_run_budget_exceeded"]})
                    s.commit()
                    raise BudgetExceeded(f"run {run_id} used its budget of {cap_calls} MCP tool calls")
        if reasons:
            _record(s, base, status="denied", output={}, latency=0, error=None, decision={"decision": "deny", "reasons": reasons})
            emit(workspace_id, "policy.denied", {"tool": cap, "reasons": reasons}, run_id=run_id, actor=actor, session=s)
            s.commit()
            raise PolicyDenied(f"MCP tool {server_name}/{tool_name} denied: {', '.join(reasons)}")
        assert tool is not None and decision is not None
        payload = approval_payload(srv, tool, arguments)
        needs_approval = decision.decision == "approval_required"
        decision_doc = {"decision": decision.decision, "reasons": decision.reasons, "side_effect": side}

        # 2) approval: request one, or verify and consume the one presented — immediately before the call
        if needs_approval and not approval_id:
            apr = request_approval(s, workspace_id=workspace_id, run_id=run_id, action=APPROVAL_ACTION, payload=payload,
                                   plan_hash=None, policy_version=ws.policy_version, requested_by=user.id,
                                   risk_tier="high", destination=f"mcp:{srv.name}", affected_assets=[cap],
                                   evidence={"side_effect": side, "read_only_hint": tool.get("read_only_hint"),
                                             "classified": _classified(srv, tool)})
            _record(s, base, status="approval_required", output={"approval_id": apr.id}, latency=0, error=None, decision=decision_doc)
            return {"status": "approval_required", "approval_id": apr.id, "capability": cap, "side_effect": side,
                    "payload_hash": apr.payload_hash}
        if needs_approval:
            apr = s.get(Approval, approval_id, with_for_update=True)
            if apr is None or apr.workspace_id != workspace_id or apr.action != APPROVAL_ACTION:
                raise ApprovalRequired("the approval does not cover an MCP tool call in this workspace")
            verify_for_execution(s, approval_id, payload=payload, plan_hash=None)
            if member_role(s, s.merge(user), workspace_id) is None:
                raise Forbidden("caller is no longer a workspace member")
            consume(s, apr)  # single use (compare-and-set): consumed before the side effect, never replayable
            decision_doc["approval_id"] = approval_id
        server_snapshot = McpServer(id=srv.id, workspace_id=srv.workspace_id, name=srv.name, url=srv.url,
                                    transport=srv.transport, secret_ref=srv.secret_ref, config=dict(srv.config or {}))

    # 3) call (outside any DB transaction), screen, record
    async def call(client: Any) -> Any:
        return await client.call_tool(tool_name, arguments)

    status, out, error = "ok", None, None
    try:
        result = _call_remote(server_snapshot, call)
        result_chars = int((server_snapshot.config or {}).get("result_max_chars") or RESULT_MAX_CHARS)
        out = screen_result(result, max(1_000, min(result_chars, RESULT_MAX_CHARS_CEILING)),
                            lift_json=bool((server_snapshot.config or {}).get("lift_json_blocks")))
        if out["is_error"]:
            status, error = "error", "mcp_tool_error: " + out["text"][:300]
        return {"status": status, "capability": cap, "side_effect": side, "result": out}
    except AnalystOSError as exc:
        status, error = "error", f"{exc.code}: {exc.message}"
        raise
    finally:
        latency = round((time.perf_counter() - started) * 1000)
        with session_factory() as s:
            _record(s, base, status=status, output=out or {}, latency=latency, error=error, decision=decision_doc)
            emit(workspace_id, "mcp.tool_called", {"capability": cap, "status": status, "latency_ms": latency},
                 run_id=run_id, actor=actor, session=s)

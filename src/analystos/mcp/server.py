"""AnalystOS as an MCP server (P4-X06, spec v3 §3.7), streamable HTTP, mounted at `/mcp`.

Request path, outermost first:

1. `McpGate` (ASGI): bearer `client_id.secret` -> 401; no grant for the workspace or tool -> 403;
   per-tool daily quota -> 429. Every request, allowed or not, writes an audit row. The decision
   is made on the raw JSON-RPC body before the SDK sees it, so a refused call never runs.
2. The SDK's stateless streamable-HTTP transport (both the `initialize` handshake and the
   2026-07-28 per-request era).
3. Handlers: re-check the grant (defence in depth), then act as the client's service user through
   the ordinary paths — `resolve_scope`, the Ask agent (which executes only via the gateway),
   `create_run`, and the gateway validator (never executes). No handler opens a connection.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import anyio
from sqlalchemy import select

from analystos.core.errors import AnalystOSError, Forbidden, InvalidInput, NotFound, Unauthenticated
from analystos.db.base import session_scope
from analystos.db.models import Artifact, Insight, QueryExecution, User
from analystos.governance.audit import audit
from analystos.governance.policy import require_role, resolve_scope
from analystos.mcp import grants as G

MCP_PATH = "/mcp"
MAX_BODY = 1_000_000
MAX_ROWS_OUT = 200
MAX_RESOURCES_PER_KIND = 200
URI_SCHEME = "analystos://"
RESOURCE_KINDS = ("knowledge", "metrics", "findings", "datasets")
SCOPE_KEY = "analystos.mcp.principal"

INSTRUCTIONS = ("AnalystOS governed analytics. Every tool takes `workspace_id` (optional when the client holds exactly "
                "one grant). Results come from the governed gateway; findings are published only when verified.")


def _ws_prop() -> dict[str, Any]:
    return {"type": "string", "description": "Workspace id (optional when the client holds exactly one grant)."}


TOOL_DEFS: dict[str, dict[str, Any]] = {
    "ask": {"description": "Answer a data question with governed SQL (read-only, within the workspace scope).",
            "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(),
                                                        "question": {"type": "string", "minLength": 3}},
                       "required": ["question"]}, "read_only": True},
    "investigate": {"description": "Start an autonomous analysis run for a business objective; returns a run handle.",
                    "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(),
                                                                "objective": {"type": "string", "minLength": 10},
                                                                "source_ids": {"type": "array", "items": {"type": "string"}},
                                                                # a published playbook version (P7-03); drafts only in dev
                                                                "definition": {"type": "object", "properties": {
                                                                    "key": {"type": "string"}, "version": {"type": "integer"}},
                                                                    "required": ["key"]}},
                               "required": ["objective"]}, "read_only": False},
    "get_finding_evidence": {"description": "A finding with its evidence queries, statistics and independent verification.",
                             "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(),
                                                                         "insight_id": {"type": "string"}},
                                        "required": ["insight_id"]}, "read_only": True},
    "validate_sql": {"description": "Check a SQL statement against the gateway validator for this workspace scope. "
                                    "Never executes it.",
                     "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(), "sql": {"type": "string"}},
                                "required": ["sql"]}, "read_only": True},
    "request.create": {"description": "Ask the analyst Request Desk for something (a number, an explanation, a definition, "
                                      "a report). It is triaged R1-R9 and answered by the governed paths, or escalated to an "
                                      "analyst; returns the request with its state and, when answered, the answer.",
                       "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(),
                                                                   "text": {"type": "string", "minLength": 3, "maxLength": 4000},
                                                                   "priority": {"type": "string",
                                                                                "enum": ["low", "normal", "high", "urgent"]}},
                                  "required": ["text"]}, "read_only": False},
    "request.status": {"description": "A Request Desk request created by this client: state, class and answer.",
                       "schema": {"type": "object", "properties": {"workspace_id": _ws_prop(),
                                                                   "request_id": {"type": "string"}},
                                  "required": ["request_id"]}, "read_only": True},
}


# ------------------------------------------------------------------------------------ tool implementations
def _service_user(session, principal: G.ClientPrincipal) -> User:
    user = session.get(User, principal.user_id)
    if user is None or not user.active:
        raise Unauthenticated("the client's service identity is inactive")
    return user


def _tool_ask(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.agents.sql_agent import ask as sql_ask
    from analystos.api.routers.analysis import _adhoc  # the Ask box's own context: same scope, agent, gateway

    question = str(args.get("question") or "").strip()
    if len(question) < 3:
        raise InvalidInput("question is required")
    with session_scope() as s:
        ctx = _adhoc(s, _service_user(s, principal), ws)
    out = sql_ask(ctx, question)
    result = dict(out.get("result") or {})
    rows = result.get("rows") or []
    result["rows"], result["rows_returned"] = rows[:MAX_ROWS_OUT], min(len(rows), MAX_ROWS_OUT)
    # A verified-query decline (P4-T05) comes back as status needs_input with the missing parameters.
    return {"sql": out.get("sql"), "explanation": out.get("explanation"), "result": result, "status": out.get("status"),
            "answered_by": out.get("answered_by"), "missing": out.get("missing")}


def _tool_investigate(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.services.runs import create_run

    with session_scope() as s:
        user = _service_user(s, principal)
        s.expunge(user)
    source_ids = args.get("source_ids") or None
    if source_ids is not None and not (isinstance(source_ids, list) and all(isinstance(x, str) for x in source_ids)):
        raise InvalidInput("source_ids must be a list of strings")
    definition = args.get("definition")
    if definition is not None and not (isinstance(definition, dict) and isinstance(definition.get("key"), str)):
        raise InvalidInput("definition must be {key, version}")
    run = create_run(user, ws, objective=str(args.get("objective") or ""), source_ids=source_ids,
                     definition={k: definition[k] for k in ("key", "version") if k in definition} if definition else None,
                     origin={"type": "mcp", "client_id": principal.client_id})
    return {"run_id": run.id, "status": run.status, "workspace_id": ws, "workflow_id": run.workflow_id}


def _tool_finding_evidence(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    insight_id = str(args.get("insight_id") or "")
    with session_scope() as s:
        require_role(s, _service_user(s, principal), ws, "viewer")
        ins = s.get(Insight, insight_id)
        if ins is None or ins.workspace_id != ws:
            raise NotFound(f"finding {insight_id} not found")
        query_ids = [e.get("id") for e in ins.evidence or [] if isinstance(e, dict) and e.get("type") == "query"]
        queries = [{"id": q.id, "sql": q.sql, "status": q.status, "row_count": q.row_count, "fingerprint": q.fingerprint,
                    "result_hash": q.result_hash, "referenced_assets": q.referenced_assets}
                   for q in s.scalars(select(QueryExecution).where(QueryExecution.id.in_(query_ids),
                                                                  QueryExecution.workspace_id == ws))] if query_ids else []
        return {"id": ins.id, "run_id": ins.run_id, "code": ins.code, "title": ins.title, "finding": ins.finding,
                "status": ins.status, "verified": ins.verified, "confidence": ins.confidence,
                "population_size": ins.population_size, "caveats": ins.caveats, "evidence": ins.evidence,
                "verification": ins.verification, "queries": queries,
                # P4-03: discovery/confirmed state, staleness and the typed evidence bundle
                "validation": ins.validation, "stale": ins.stale_since is not None, "evidence_bundle": ins.evidence_bundle,
                # P7-01: the verdict's record state; a VOID one carries its cause
                "verification_state": _verification_state(s, ins)}


def _verification_state(session: Any, ins: Any) -> dict[str, Any]:
    from analystos.evidence.verification import insight_states

    state = insight_states(session, [ins.id])[ins.id]
    return {k: state.get(k) for k in ("state", "badge", "record_id", "fingerprint", "void")}


def _tool_validate_sql(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.gateway.validator import validate_sql

    sql = str(args.get("sql") or "")
    with session_scope() as s:
        user = _service_user(s, principal)
        scope = resolve_scope(s, user, ws)
        try:
            v = validate_sql(scope, sql, max_rows=scope.max_rows)
            return {"accepted": True, "dialect": v.dialect, "referenced_assets": v.referenced_assets,
                    "referenced_columns": v.referenced_columns, "executed": False}
        except AnalystOSError as exc:
            # A rejected probe is audited like the query console's explain: not a silent scope oracle.
            audit(f"mcp_client:{principal.client_id}", "query.validate_rejected", workspace_id=ws, decision="deny",
                  details={"code": exc.code, "reason": exc.message[:300], "sql": sql[:2000]}, session=s)
            return {"accepted": False, "code": exc.code, "reason": exc.message, "executed": False}


_REQUEST_FIELDS = ("id", "workspace_id", "state", "class", "priority", "channel", "duplicate_of", "conversation_id",
                   "run_ids", "answer", "sla_due_at", "created_at", "first_answer_at", "closed_at")


def _tool_request_create(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.desk.service import submit

    with session_scope() as s:
        user = _service_user(s, principal)
        s.expunge(user)
    out = submit(user, ws, str(args.get("text") or ""), channel="mcp", priority=str(args.get("priority") or "normal"))
    return {k: out.get(k) for k in _REQUEST_FIELDS}


def _tool_request_status(principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.desk.service import get_request

    with session_scope() as s:
        out = get_request(s, _service_user(s, principal), str(args.get("request_id") or ""))
        if out["workspace_id"] != ws:
            raise NotFound("request not found")
        return {k: out.get(k) for k in _REQUEST_FIELDS}


TOOL_IMPLS: dict[str, Callable[[G.ClientPrincipal, str, dict[str, Any]], dict[str, Any]]] = {
    "ask": _tool_ask, "investigate": _tool_investigate, "get_finding_evidence": _tool_finding_evidence,
    "validate_sql": _tool_validate_sql, "request.create": _tool_request_create, "request.status": _tool_request_status,
}


# ------------------------------------------------------------------------------------ workspace tools (P7-11)
MCP_HTTP_STATUSES = ("tested", "certified")  # a draft HTTP tool is never offered or run over MCP


def _http_callable(m: Any) -> str | None:
    """None when this HTTP tool may be called over MCP, else why not."""
    from analystos.capabilities.invoke import READ_ONLY, is_http_tool

    if not is_http_tool(m):
        return f"{m.id} is not an HTTP tool"
    if m.certification.status not in MCP_HTTP_STATUSES:
        return (f"HTTP tool {m.ref} is {m.certification.status}: only tested or certified tools are callable over MCP")
    if m.side_effect not in READ_ONLY:
        return f"HTTP tool {m.ref} has side effect {m.side_effect}: it needs an approval, which an MCP client cannot give"
    return None


def workspace_tools(session: Any, ws: str) -> dict[str, dict[str, Any]]:
    """The workspace tools an MCP client can call now: published query tools and tested/certified read-only
    HTTP tools that are enabled here. Drafts are absent from the list and refused when called by name."""
    from analystos.capabilities import enablement
    from analystos.capabilities.invoke import _snapshot
    from analystos.contracts.definition import RUNNABLE_STATUSES
    from analystos.db.models import Definition

    out: dict[str, dict[str, Any]] = {}
    newest: dict[str, Any] = {}
    for row in session.scalars(select(Definition).where(Definition.workspace_id == ws, Definition.kind == "query_tool",
                                                        Definition.status.in_(RUNNABLE_STATUSES))
                               .order_by(Definition.version)):
        newest[row.key] = row
    for key, row in sorted(newest.items()):
        params = dict(row.spec.get("parameters") or {"type": "object", "properties": {}})
        schema = {**params, "properties": {"workspace_id": _ws_prop(), **(params.get("properties") or {})}}
        out[f"query.{key}"] = {"description": row.spec.get("description") or row.title or key, "schema": schema,
                               "read_only": True, "family": "query", "ref": f"{key}@{row.version}"}
    snap = _snapshot(session, ws)
    explicit = enablement.overrides(session, ws)
    for m in snap.list("Tool"):
        if _http_callable(m) is None and enablement.usable(m, snap, explicit, autonomous_run=False) is None:
            schema = dict(m.input_schema or {"type": "object", "properties": {}})
            schema = {**schema, "properties": {"workspace_id": _ws_prop(), **(schema.get("properties") or {})}}
            out[f"http.{m.id}"] = {"description": m.summary or m.id, "schema": schema, "read_only": True,
                                   "family": "http", "ref": m.ref}
    return out


def _tool_query(principal: G.ClientPrincipal, ws: str, key: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.contracts.definition import RUNNABLE_STATUSES
    from analystos.core.errors import PolicyDenied
    from analystos.db.models import Definition
    from analystos.services.definitions import latest_published
    from analystos.tools import query_tools

    with session_scope() as s:
        user = _service_user(s, principal)
        s.expunge(user)
        row = latest_published(s, ws, "query_tool", key)
        if row is None:
            other = s.scalar(select(Definition.status).where(Definition.workspace_id == ws, Definition.kind == "query_tool",
                                                             Definition.key == key).order_by(Definition.version.desc()))
            if other is not None and other not in RUNNABLE_STATUSES:
                raise PolicyDenied(f"query tool {key} has no published version ({other}): drafts are not callable over MCP")
            raise NotFound(f"query tool {key} not found")
        from analystos.services.definitions import apply_bindings

        spec, version = apply_bindings(dict(row.spec), (row.bindings or {}).get("sources") or {}), row.version
    out = query_tools.run(user, ws, key, spec, {k: v for k, v in args.items() if k != "workspace_id"},
                          actor=f"mcp_client:{principal.client_id}", purpose=f"mcp.query_tool:{key}")
    out["rows"] = out["rows"][:MAX_ROWS_OUT]
    return {**out, "version": version}


def _tool_http(principal: G.ClientPrincipal, ws: str, capability_id: str, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.capabilities.invoke import _snapshot, invoke
    from analystos.core.errors import PolicyDenied

    with session_scope() as s:
        user = _service_user(s, principal)
        s.expunge(user)
        m = _snapshot(s, ws).get(capability_id)
    if (why := _http_callable(m)) is not None:
        raise PolicyDenied(why)
    return invoke(user, ws, capability_id, {k: v for k, v in args.items() if k != "workspace_id"})


def call_workspace_tool(principal: G.ClientPrincipal, ws: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    family, _, ident = name.partition(".")
    return _tool_query(principal, ws, ident, args) if family == "query" else _tool_http(principal, ws, ident, args)


def _call_named(name: str, principal: G.ClientPrincipal, ws: str, args: dict[str, Any]) -> dict[str, Any]:
    return call_workspace_tool(principal, ws, name, args)


def granted_workspace_tools(principal: G.ClientPrincipal) -> dict[str, dict[str, Any]]:
    """Workspace tools the client's grants cover, across its workspaces (a name listed once)."""
    out: dict[str, dict[str, Any]] = {}
    with session_scope() as s:
        for ws, grant in sorted(principal.grants.items()):
            if not any(G.is_workspace_tool(t) for t in grant["tools"]):
                continue
            for name, d in workspace_tools(s, ws).items():
                if G.grants_tool(grant["tools"], name):
                    out.setdefault(name, d)
    return out


# ------------------------------------------------------------------------------------ resources
def parse_uri(uri: str | None) -> tuple[str | None, str | None, str | None]:
    """`analystos://<workspace>/<kind>/<id>` -> (workspace, kind, id); anything else -> Nones."""
    if not uri or not str(uri).startswith(URI_SCHEME):
        return None, None, None
    parts = str(uri)[len(URI_SCHEME):].split("/")
    if len(parts) != 3 or parts[1] not in RESOURCE_KINDS or not parts[0] or not parts[2]:
        return None, None, None
    return parts[0], parts[1], parts[2]


def _context_entries(s: Any, ws: str, metric: bool) -> list[Any]:
    """Trusted knowledge the workspace sees: its own entries and its packs' documents (P4-K01)."""
    from analystos.knowledge.entries import visible_entries

    rows = (visible_entries(s, ws, kinds=["metric"], trusted_only=True) if metric
            else visible_entries(s, ws, exclude_kinds=["metric", "episode"], trusted_only=True))
    return sorted(rows, key=lambda e: (e.name, e.id))[:MAX_RESOURCES_PER_KIND]


def list_resource_rows(principal: G.ClientPrincipal) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with session_scope() as s:
        user = _service_user(s, principal)
        for ws in sorted(principal.grants):
            require_role(s, user, ws, "viewer")
            for kind, metric in (("knowledge", False), ("metrics", True)):
                for e in _context_entries(s, ws, metric):
                    out.append({"uri": f"{URI_SCHEME}{ws}/{kind}/{e.id}", "name": e.name, "description": f"{e.kind}: {e.body[:160]}"})
            for m in s.scalars(select(Artifact).where(Artifact.workspace_id == ws, Artifact.type == "metric",
                                                      Artifact.status == "published").limit(MAX_RESOURCES_PER_KIND)):
                out.append({"uri": f"{URI_SCHEME}{ws}/metrics/{m.id}", "name": m.name, "description": "published metric"})
            for i in s.scalars(select(Insight).where(Insight.workspace_id == ws, Insight.verified.is_(True),
                                                     Insight.status == "verified")
                               .order_by(Insight.created_at.desc()).limit(MAX_RESOURCES_PER_KIND)):
                out.append({"uri": f"{URI_SCHEME}{ws}/findings/{i.id}", "name": i.title,
                            "description": f"verified finding (confidence {i.confidence:.2f})"})
            for d in s.scalars(select(Artifact).where(Artifact.workspace_id == ws, Artifact.type == "dataset",
                                                      Artifact.status == "published").limit(MAX_RESOURCES_PER_KIND)):
                out.append({"uri": f"{URI_SCHEME}{ws}/datasets/{d.id}", "name": d.name, "description": "published dataset"})
    return out


def read_resource_doc(principal: G.ClientPrincipal, uri: str) -> dict[str, Any]:
    ws, kind, rid = parse_uri(uri)
    G.check_grant(principal, ws, G.RESOURCE_READ)
    with session_scope() as s:
        require_role(s, _service_user(s, principal), ws, "viewer")
        if kind in ("knowledge", "metrics"):
            from analystos.knowledge.entries import get_entry

            e = get_entry(s, ws, rid)
            if e is not None and e.trusted and e.kind != "episode" and (e.kind == "metric") == (kind == "metrics"):
                return {"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": list(e.synonyms),
                        "mapped_columns": list(e.mapped_columns), "origin": e.origin,
                        **({"path": e.path, "sha256": e.sha256} if e.path else {})}
            if kind == "metrics":
                a = s.get(Artifact, rid)
                if a is not None and a.workspace_id == ws and a.type == "metric" and a.status == "published":
                    return {"id": a.id, "name": a.name, "version": a.version, "definition": a.content}
        elif kind == "findings":
            i = s.get(Insight, rid)
            if i is not None and i.workspace_id == ws and i.verified and i.status == "verified":
                return {"id": i.id, "run_id": i.run_id, "title": i.title, "finding": i.finding, "confidence": i.confidence,
                        "population_size": i.population_size, "business_impact": i.business_impact, "caveats": i.caveats,
                        "evidence": i.evidence, "verification": i.verification}
        elif kind == "datasets":
            a = s.get(Artifact, rid)
            if a is not None and a.workspace_id == ws and a.type == "dataset" and a.status == "published":
                return {"id": a.id, "name": a.name, "version": a.version, "platform": a.platform,
                        "external_id": a.external_id, "definition": a.content}
    raise NotFound(f"resource {uri} not found")


# ------------------------------------------------------------------------------------ SDK handlers
def _principal(ctx: Any) -> G.ClientPrincipal:
    request = getattr(ctx, "request", None)
    principal = request.scope.get(SCOPE_KEY) if request is not None else None
    if principal is None:  # only reachable if the gate were bypassed: fail closed
        raise Unauthenticated("unauthenticated MCP request")
    from analystos.tenancy import set_current_tenant

    set_current_tenant(principal.tenant_id)  # handler tasks do not inherit the gate's context
    return principal


def _json_text(value: Any) -> str:
    from analystos.db.base import json_dumps

    return json_dumps(value)


def build_server():
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        principal = _principal(ctx)
        granted = {t for g in principal.grants.values() for t in g["tools"]}
        defs = {name: d for name, d in TOOL_DEFS.items() if name in granted}
        defs.update(await anyio.to_thread.run_sync(granted_workspace_tools, principal))
        return types.ListToolsResult(tools=[
            types.Tool(name=name, description=d["description"], input_schema=d["schema"],
                       annotations=types.ToolAnnotations(read_only_hint=d["read_only"], destructive_hint=False))
            for name, d in defs.items()])

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        principal = _principal(ctx)
        name, args = params.name, dict(params.arguments or {})
        if name not in TOOL_IMPLS and not G.is_workspace_tool(name):
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool {name}")
        ws = G.resolve_workspace(principal, args.get("workspace_id"))
        G.check_grant(principal, ws, name)  # the gate already did; a handler never trusts that alone
        actor = f"mcp_client:{principal.client_id}"
        impl = TOOL_IMPLS.get(name) or partial(_call_named, name)
        try:
            out = await anyio.to_thread.run_sync(impl, principal, ws, args)
        except AnalystOSError as exc:
            await anyio.to_thread.run_sync(partial(
                audit, actor, "mcp.tool_result", workspace_id=ws, target=name,
                decision="deny" if exc.http_status in (401, 403) else "error", reasons=[exc.code],
                details={"message": exc.message[:300]}))
            return types.CallToolResult(content=[types.TextContent(text=f"{exc.code}: {exc.message}")], is_error=True,
                                        structured_content={"error": exc.to_dict()})
        await anyio.to_thread.run_sync(partial(audit, actor, "mcp.tool_result", workspace_id=ws, target=name,
                                               decision="allow", details={"status": "ok"}))
        structured = json.loads(_json_text(out))
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(structured, default=str))],
                                    structured_content=structured)

    async def list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        principal = _principal(ctx)
        rows = await anyio.to_thread.run_sync(list_resource_rows, principal)
        return types.ListResourcesResult(resources=[types.Resource(mime_type="application/json", **r) for r in rows])

    async def read_resource(ctx: Any, params: Any) -> types.ReadResourceResult:
        principal = _principal(ctx)
        try:
            doc = await anyio.to_thread.run_sync(read_resource_doc, principal, str(params.uri))
        except AnalystOSError as exc:
            raise MCPError(code=-32002, message=f"{exc.code}: {exc.message}") from None
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=str(params.uri), mime_type="application/json",
                                                                             text=_json_text(doc))])

    return Server("analystos", version=_version(), instructions=INSTRUCTIONS, on_list_tools=list_tools,
                  on_call_tool=call_tool, on_list_resources=list_resources, on_read_resource=read_resource)


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("analystos")
    except PackageNotFoundError:
        return "0.0.0"


# ------------------------------------------------------------------------------------ ASGI gate
def _authenticate(token: str | None) -> G.ClientPrincipal:
    from analystos.tenancy import system_scope

    try:
        with system_scope(), session_scope() as s:  # credentials are checked before any tenant is known
            return G.authenticate(s, token)
    except Unauthenticated:
        client_id = (token or "").partition(".")[0][:40] or "unknown"
        audit(f"mcp_client:{client_id}", "mcp.auth_failed", decision="deny", reasons=["invalid_client_credentials"])
        raise


def _authorize(principal: G.ClientPrincipal, method: str, tool: str | None, ws: str | None) -> AnalystOSError | None:
    """Grant and quota decision for one JSON-RPC request; audited either way."""
    actor = f"mcp_client:{principal.client_id}"
    with session_scope() as s:
        try:
            if tool is not None:
                G.check_grant(principal, ws, tool)
                G.consume_quota(s, principal, ws, tool)
            elif not principal.grants:
                raise Forbidden("the client holds no workspace grant")
        except AnalystOSError as exc:
            s.rollback()  # an over-quota call does not count
            audit(actor, "mcp.request", workspace_id=ws if ws in principal.grants else None, target=tool or method,
                  decision="deny", reasons=[exc.code], details={"method": method, "message": exc.message[:300],
                                                                "requested_workspace": ws}, session=s)
            return exc
        audit(actor, "mcp.request", workspace_id=ws, target=tool or method, decision="allow",
              details={"method": method}, session=s)
    return None


async def _send_json(send: Any, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": payload})


def _rpc_error(rpc_id: Any, exc: AnalystOSError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000 - (exc.http_status % 100), "message": exc.message,
                                                      "data": exc.to_dict()}}


class McpGate:
    """Authentication, grants, quotas and audit in front of the SDK transport."""

    def __init__(self) -> None:
        self.inner: Any = None  # set while the app's lifespan is running

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        if self.inner is None:
            await _send_json(send, 503, {"error": {"code": "upstream_unavailable", "message": "MCP server not started"}})
            return
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        try:
            principal = await anyio.to_thread.run_sync(_authenticate, token)
            from analystos.tenancy import set_current_tenant

            set_current_tenant(principal.tenant_id)  # this request's task acts for the client's tenant
        except Unauthenticated as exc:
            await _send_json(send, 401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": exc.message,
                                                                                 "data": exc.to_dict()}},
                             [(b"www-authenticate", b'Bearer realm="analystos-mcp"')])
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY:
                await _send_json(send, 413, {"error": {"code": "invalid_input", "message": "request body too large"}})
                return
        rpc_id, method, tool, ws = None, "", None, None
        if scope.get("method") == "POST" and body:
            try:
                msg = json.loads(body)
            except ValueError:
                msg = None
            if isinstance(msg, list):
                await _send_json(send, 400, {"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32600, "message": "batch requests are not supported"}})
                return
            if isinstance(msg, dict):
                rpc_id, method = msg.get("id"), str(msg.get("method") or "")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                if method == "tools/call":
                    tool = str(params.get("name") or "")
                    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                    ws = G.resolve_workspace(principal, args.get("workspace_id"))
                elif method == "resources/read":
                    tool, ws = G.RESOURCE_READ, parse_uri(params.get("uri"))[0]
        if method:  # notifications and responses carry no decision worth a row; requests all do
            denied = await anyio.to_thread.run_sync(_authorize, principal, method, tool, ws)
            if denied is not None:
                await _send_json(send, denied.http_status, _rpc_error(rpc_id, denied))
                return
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.inner({**scope, SCOPE_KEY: principal}, replay, send)


def mount(app: Any, path: str = MCP_PATH) -> McpGate | None:
    """Serve MCP at `path` on a FastAPI/Starlette app. The SDK session manager runs inside the app's
    lifespan (a fresh one per lifespan, since a manager can only be started once). Without the
    optional `mcp` extra the endpoint is simply absent."""
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        from analystos.core.logging import get_logger

        get_logger(__name__).warning("mcp extra not installed: the /mcp endpoint is disabled")
        return None

    gate = McpGate()
    server = build_server()
    app.router.add_route(path, gate, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        # Bearer auth is mandatory, so DNS-rebinding protection (for unauthenticated local servers) is off.
        manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True,
                                               security_settings=TransportSecuritySettings(
                                                   enable_dns_rebinding_protection=False))
        async with manager.run():
            gate.inner = StreamableHTTPASGIApp(manager)
            try:
                async with previous(a) as state:
                    yield state
            finally:
                gate.inner = None

    app.router.lifespan_context = lifespan
    return gate

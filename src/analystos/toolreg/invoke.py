"""One invoke path for every registry tool (S10-T3): REST `POST /v2/tools/{id}:invoke`, MCP `tools/call` and agents.

Order of checks, each fail-closed, all before anything executes:

1. **Caller.** A user needs the workspace role the manifest's permissions imply; an MCP/API client needs a
   grant covering the tool id or a pattern (not expired), and its per-minute rate and daily quota; an agent
   needs a run in this workspace whose bound playbook declares the tool.
2. **Version.** `tool_id@4` pins a major (or minor/exact) version; unpinned gets the latest certified one.
   Quarantined, refused and retired versions never run; deprecated ones only when pinned.
3. **Signature.** The manifest must verify against the tenant key (`signing.verify`): unsigned or tampered
   manifests are refused (v1 threat "tool supply-chain").
4. **Exposure and limits.** REST/MCP exposure flags, the manifest's per-minute rate, JSON-Schema arguments.
5. **Approval.** `side_effect != none` needs a hash-bound approval of (tool, version, manifest hash,
   arguments), verified and consumed immediately before acting (CLAUDE.md rule 6). An external MCP tool goes
   through the MCP client's own gate, which binds its approval to the server's tool definition. A playbook
   tool only starts a run, exactly like MCP `investigate`: the run carries its own gates (plan approval at
   autonomy <= 2, hash-bound approvals for every publication), so starting it needs no extra approval.
6. **Execution.** Exactly one deterministic path per `execution.via`: the semantic compiler, a verified
   query template with typed literals, a SELECT over a published dataset built as a syntax tree, `create_run`
   for a playbook, or the MCP client. Every statement runs through `QueryGateway.execute` as the caller,
   under their scope (rule 4); no path accepts SQL from the caller.

Every outcome writes a `tool_invocation` row and an audit event.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from analystos.contracts.tool_manifest import ToolManifestV2, split_ref
from analystos.core.errors import (
    AnalystOSError,
    ApprovalRequired,
    BudgetExceeded,
    Conflict,
    Forbidden,
    InvalidInput,
    PolicyDenied,
)
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, McpServer, RegisteredTool, ToolInvocation, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.toolreg import store

APPROVAL_ACTION = "tool_invoke"
CHANNELS = ("rest", "mcp", "agent")
PERMISSION_ROLE = {"dataset.read": "analyst", "run.start": "analyst", "external.call": "analyst"}
MAX_OUT_ROWS = 5000


class RateLimited(BudgetExceeded):
    code = "rate_limited"


@dataclass
class Caller:
    """Who is invoking: a user, an MCP/API client (acting as its service user) or an agent in a run."""

    user: User  # detached
    actor: str  # user:<id> | mcp_client:<id> | agent:<id>
    channel: str  # rest | mcp | agent
    principal: Any = None  # mcp.grants.ClientPrincipal for a client
    run_id: str | None = None
    agent_id: str | None = None
    quota_consumed: bool = False  # the MCP gate already counted this call

    @property
    def client_id(self) -> str | None:
        return getattr(self.principal, "client_id", None)


def min_role(m: ToolManifestV2) -> str:
    from analystos.security.auth import ROLE_RANK

    roles = [PERMISSION_ROLE.get(p, "editor") for p in m.permissions] or ["analyst"]
    return max(roles, key=lambda r: ROLE_RANK[r])


# ------------------------------------------------------------------------------------ checks
def _authorize_caller(session: Any, caller: Caller, ws: str, m: ToolManifestV2) -> None:
    from analystos.governance.policy import require_role
    from analystos.mcp import grants as G

    if caller.principal is not None:
        G.check_grant(caller.principal, ws, m.tool_id)
        grant = caller.principal.grants[ws]
        if grant["role"] != "analyst" and min_role(m) == "analyst":
            raise Forbidden(f"tool {m.tool_id} needs an analyst grant")
        if not caller.quota_consumed:
            G.consume_quota(session, caller.principal, ws, m.tool_id)
    require_role(session, session.merge(caller.user), ws, min_role(m))
    if caller.channel == "agent":
        _require_declared(session, caller, ws, m.tool_id)


def _require_declared(session: Any, caller: Caller, ws: str, tool_id: str) -> None:
    """An agent may call a registry tool only inside a run whose bound playbook declares it (`spec.tools`)."""
    run = session.get(AnalysisRun, caller.run_id) if caller.run_id else None
    if run is None or run.workspace_id != ws:
        raise PolicyDenied("an agent invokes registry tools only inside a run of this workspace")
    caps = run.capabilities or {}
    playbook = (caps.get("manifests") or {}).get(caps.get("playbook") or "") or {}
    declared = [str(p) for p in ((playbook.get("spec") or {}).get("tools") or [])]
    if not any(fnmatch.fnmatchcase(tool_id, p) for p in declared):
        raise PolicyDenied(f"the run's playbook {caps.get('playbook') or '(unbound)'} does not declare tool {tool_id}",
                           details={"declared": declared})


def _check_exposure(m: ToolManifestV2, channel: str) -> None:
    if channel == "rest" and not m.exposure.rest:
        raise PolicyDenied(f"tool {m.tool_id} is not exposed over REST")
    if channel == "mcp" and not m.exposure.mcp:
        raise PolicyDenied(f"tool {m.tool_id} is not exposed over MCP")


def _check_rate(session: Any, caller: Caller, ws: str, m: ToolManifestV2) -> None:
    since = utcnow() - timedelta(seconds=60)
    used = session.scalar(select(func.count(ToolInvocation.id)).where(
        ToolInvocation.workspace_id == ws, ToolInvocation.tool_id == m.tool_id, ToolInvocation.caller == caller.actor,
        ToolInvocation.status.in_(["ok", "error"]), ToolInvocation.created_at >= since)) or 0
    if used >= m.limits.rate_per_min:
        raise RateLimited(f"tool {m.tool_id} allows {m.limits.rate_per_min} calls per minute per caller",
                          details={"limit": m.limits.rate_per_min})


def validate_arguments(m: ToolManifestV2, arguments: dict[str, Any]) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    errors = sorted(Draft202012Validator(m.input_schema, format_checker=FormatChecker()).iter_errors(arguments),
                    key=lambda e: list(e.path))
    if errors:
        raise InvalidInput(f"arguments do not match {m.tool_id}'s input schema: {errors[0].message}",
                           details={"errors": [{"loc": ["arguments", *map(str, e.path)], "msg": e.message}
                                               for e in errors[:20]]})


def approval_payload(ws: str, row: RegisteredTool, arguments: dict[str, Any]) -> dict[str, Any]:
    """What an approval binds: this workspace, this exact tool version and content, these arguments."""
    return {"workspace_id": ws, "tool_id": row.tool_id, "version": row.version, "manifest_hash": row.manifest_hash,
            "arguments": arguments}


def _approval(session: Any, caller: Caller, ws: str, row: RegisteredTool, m: ToolManifestV2,
              arguments: dict[str, Any], approval_id: str | None) -> dict[str, Any] | None:
    """None when the approval presented was verified and consumed; else the approval request to return."""
    from analystos.governance.approvals import consume, request_approval, verify_for_execution
    from analystos.governance.policy import get_workspace, member_role

    payload = approval_payload(ws, row, arguments)
    if not approval_id:
        apr = request_approval(session, workspace_id=ws, run_id=caller.run_id, action=APPROVAL_ACTION, payload=payload,
                               plan_hash=None, policy_version=get_workspace(session, ws).policy_version,
                               requested_by=caller.user.id, risk_tier="high", destination=f"tool:{m.tool_id}",
                               affected_assets=[m.ref], evidence={"side_effect": m.side_effect, "kind": m.kind,
                                                                  "channel": caller.channel, "caller": caller.actor})
        return {"status": "approval_required", "approval_id": apr.id, "payload_hash": apr.payload_hash,
                "side_effect": m.side_effect}
    apr = session.get(Approval, approval_id, with_for_update=True)
    if apr is None or apr.workspace_id != ws or apr.action != APPROVAL_ACTION:
        raise ApprovalRequired("the approval does not cover a tool invocation in this workspace")
    verify_for_execution(session, approval_id, payload=payload, plan_hash=None)
    if member_role(session, session.merge(caller.user), ws) is None:
        raise Forbidden("caller is no longer a workspace member")
    consume(session, apr)  # single use, before the side effect
    return None


def _external_current(session: Any, ws: str, row: RegisteredTool) -> None:
    """An admitted tool runs only while the server still offers exactly the definition that was screened."""
    ref = ToolManifestV2.model_validate(row.manifest).execution.ref
    srv = session.scalar(select(McpServer).where(McpServer.workspace_id == ws, McpServer.name == ref.get("server")))
    tool = next((t for t in (srv.tools or []) if t.get("name") == ref.get("tool")), None) if srv else None
    if tool is None or tool.get("hash") != row.description_hash:
        raise store.ToolQuarantined(f"tool {row.tool_id} changed on its server since admission: re-admit it (re-screen)")


# ------------------------------------------------------------------------------------ execution paths
def _scope(caller: Caller, ws: str) -> Any:
    from analystos.governance.policy import resolve_scope

    with session_scope() as s:
        return resolve_scope(s, s.merge(caller.user), ws, minimum_role="analyst")


def _rows_out(result: Any, max_rows: int) -> dict[str, Any]:
    rows = result.rows[:max_rows]
    return {"columns": result.columns, "rows": rows, "row_count": result.row_count,
            "truncated": bool(result.truncated or len(result.rows) > len(rows)), "governed": True,
            "query_id": result.query_id, "result_hash": result.result_hash, "sql_hash": stable_hash(result.sql or ""),
            "freshness": {"computed_at": utcnow().isoformat(), "cache_hit": bool(result.cache_hit)}}


def _execute_sql(caller: Caller, scope: Any, sql: str, m: ToolManifestV2, source_id: str | None = None) -> Any:
    from analystos.runtime.context import default_gateway

    gw = default_gateway()
    purpose = f"tool:{m.tool_id}"[:120]
    if source_id:
        return gw.run_sql_for(scope, actor=caller.actor, run_id=caller.run_id, source_id=source_id)(
            sql, purpose=purpose, max_rows=m.limits.max_rows)
    return gw.execute(scope, sql, actor=caller.actor, purpose=purpose, run_id=caller.run_id, max_rows=m.limits.max_rows,
                      timeout_seconds=m.limits.timeout_s)


def run_metric(caller: Caller, ws: str, m: ToolManifestV2, args: dict[str, Any]) -> dict[str, Any]:
    """The semantic compiler path: names and literal values in, SQL only from the approved definition."""
    from analystos.contracts.semantic import SemanticQuery
    from analystos.semantic.compiler import compile_query, load_catalog

    ref = m.execution.ref
    scope = _scope(caller, ws)
    with session_scope() as s:
        catalog = load_catalog(s, ws)
    entry = ((catalog or {}).get("metrics") or {}).get(ref["metric"])
    if entry is None:
        raise Conflict(f"metric {ref['metric']} is no longer approved: the tool {m.ref} is stale")
    if entry["version"] != ref["metric_version"]:
        raise Conflict(f"tool {m.ref} computes metric {ref['metric']} v{ref['metric_version']}, but v{entry['version']} "
                       "is approved now: republish the tool", details={"reason": "stale_source"})
    time_q = None
    if args.get("grain") or args.get("period"):
        times = ref.get("time_dimensions") or []
        dim = args.get("time_dimension") or (times[0] if len(times) == 1 else None)
        if not dim:
            raise InvalidInput("time_dimension is required when the metric has several time dimensions")
        period = args.get("period") or {}
        time_q = {"dimension": dim, "grain": args.get("grain"), "start": period.get("start"), "end": period.get("end")}
    try:
        query = SemanticQuery(metrics=[ref["metric"]], dimensions=list(args.get("dimensions") or []),
                              filters=list(args.get("filters") or []), time=time_q,
                              limit=min(int(args.get("limit") or m.limits.max_rows), m.limits.max_rows, 5000))
    except ValueError as exc:
        raise InvalidInput(f"invalid metric query: {str(exc)[:300]}") from None
    compiled = compile_query(query, catalog, scope)
    result = _execute_sql(caller, scope, compiled.sql, m)
    out = _rows_out(result, m.limits.max_rows)
    out.update(metric=ref["metric"], metric_version=ref["metric_version"], sql_hash=stable_hash(compiled.sql),
               provenance=compiled.provenance)
    return out


def run_verified_query(caller: Caller, ws: str, m: ToolManifestV2, args: dict[str, Any]) -> dict[str, Any]:
    """The verified-query path: the stored template, its typed parameters bound as SQL literals."""
    from analystos.db.models import VerifiedQuery
    from analystos.registries.verified_queries import render

    ref = m.execution.ref
    with session_scope() as s:
        vq = s.get(VerifiedQuery, ref.get("verified_query_id"))
        if vq is None or vq.workspace_id != ws or vq.status != "active":
            raise Conflict(f"the verified query behind {m.ref} is no longer active")
        if stable_hash(vq.sql_template) != ref.get("template_hash"):
            raise Conflict(f"the verified query behind {m.ref} changed after publishing: republish the tool",
                           details={"reason": "stale_source"})
        template, params, dialect, source_id = vq.sql_template, list(vq.parameters or []), vq.dialect, vq.source_id
    scope = _scope(caller, ws)
    sql = render(template, params, args, scope.source_dialects.get(source_id, dialect) if source_id else dialect)
    return _rows_out(_execute_sql(caller, scope, sql, m, source_id), m.limits.max_rows)


def _dataset_predicate(column: Any, op: str, value: Any) -> Any:
    from sqlglot import exp

    if op == "is_null":
        return exp.Is(this=column, expression=exp.null())
    if op == "is_not_null":
        return exp.not_(exp.Is(this=column, expression=exp.null()))
    if op == "in":
        if not isinstance(value, list) or not value:
            raise InvalidInput("op 'in' needs a non-empty list value")
        return exp.In(this=column, expressions=[exp.convert(v) for v in value])
    if isinstance(value, (list, dict)) or value is None:
        raise InvalidInput(f"op {op!r} needs one scalar value")
    cls = {"=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}[op]
    return cls(this=column, expression=exp.convert(value))


def dataset_sql(dataset_sql_text: str, columns: list[str], args: dict[str, Any], dialect: str, max_rows: int) -> str:
    """A SELECT over the published dataset's own statement, built as a syntax tree from declared column names
    and literal values (never caller text)."""
    import sqlglot
    from sqlglot import exp

    inner = sqlglot.parse_one(dataset_sql_text, read=dialect)
    wanted = list(args.get("columns") or columns)
    q = exp.select(*[exp.column(c, table="d", quoted=True) for c in wanted]).from_(inner.subquery("d"))
    for f in args.get("filters") or []:
        q = q.where(_dataset_predicate(exp.column(f["column"], table="d", quoted=True), f["op"], f.get("value")))
    for o in args.get("order_by") or []:
        q = q.order_by(exp.Ordered(this=exp.column(o["column"], table="d", quoted=True), desc=o.get("direction") == "desc"))
    q = q.limit(min(int(args.get("limit") or max_rows), max_rows))
    return q.sql(dialect=dialect)


def run_dataset(caller: Caller, ws: str, m: ToolManifestV2, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.db.models import Artifact

    ref = m.execution.ref
    with session_scope() as s:
        a = s.get(Artifact, ref.get("artifact_id"))
        if a is None or a.workspace_id != ws or a.type != "dataset" or a.status != "published":
            raise Conflict(f"the dataset behind {m.ref} is no longer published")
        if a.content_hash != ref.get("content_hash"):
            raise Conflict(f"the dataset behind {m.ref} changed after publishing: republish the tool",
                           details={"reason": "stale_source"})
        text = str((a.content or {}).get("sql") or "")
    scope = _scope(caller, ws)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    sql = dataset_sql(text, list(ref.get("columns") or []), args, dialect, m.limits.max_rows)
    return _rows_out(_execute_sql(caller, scope, sql, m), m.limits.max_rows)


def run_playbook(caller: Caller, ws: str, m: ToolManifestV2, args: dict[str, Any]) -> dict[str, Any]:
    from analystos.services.runs import create_run

    ref = m.execution.ref
    origin = {"type": "tool", "tool_id": m.tool_id, "version": m.version, "channel": caller.channel,
              **({"client_id": caller.client_id} if caller.client_id else {})}
    if ref.get("definition_key"):
        run = create_run(caller.user, ws, objective=str(args["objective"]), source_ids=args.get("source_ids") or None,
                         definition={"key": ref["definition_key"], "version": ref["definition_version"]}, origin=origin)
    else:
        run = create_run(caller.user, ws, objective=str(args["objective"]), source_ids=args.get("source_ids") or None,
                         playbook=ref["capability"], origin=origin)
    return {"run_id": run.id, "status": run.status, "workflow_id": run.workflow_id}


def run_external(caller: Caller, ws: str, m: ToolManifestV2, args: dict[str, Any], approval_id: str | None) -> dict[str, Any]:
    from analystos.mcp import client as mcp_client

    ref = m.execution.ref
    return mcp_client.invoke_tool(session_scope, caller.user, ws, ref["server"], ref["tool"], args, run_id=caller.run_id,
                                  approval_id=approval_id, agent_id=caller.agent_id)


PATHS = {"semantic_compiler": run_metric, "verified_query": run_verified_query, "dataset": run_dataset,
         "playbook": run_playbook}


# ------------------------------------------------------------------------------------ entry point
def _record(ws: str, caller: Caller, rec: dict[str, Any], *, status: str, started: float, reason: str | None = None,
            out: dict[str, Any] | None = None, error: str | None = None, approval_id: str | None = None) -> None:
    latency = round((time.perf_counter() - started) * 1000)
    result = (out or {}).get("result") if isinstance((out or {}).get("result"), dict) else (out or {})
    with session_scope() as s:
        s.add(ToolInvocation(id=new_id("tinv"), workspace_id=ws, tool_id=rec["tool_id"][:200], version=rec.get("version"),
                             kind=rec.get("kind"), caller=caller.actor[:120], client_id=caller.client_id,
                             channel=caller.channel, run_id=caller.run_id, status=status, reason=(reason or None),
                             latency_ms=latency, row_count=result.get("row_count") if isinstance(result, dict) else None,
                             cost_usd=0.0, query_id=result.get("query_id") if isinstance(result, dict) else None,
                             approval_id=approval_id, error=(error or "")[:2000] or None))
        audit(caller.actor, "tool.invoked", workspace_id=ws, run_id=caller.run_id,
              target=f"{rec['tool_id']}@{rec.get('version') or '?'}", decision="allow" if status == "ok" else
              ("deny" if status in ("denied", "approval_required") else "error"), reasons=[reason] if reason else [],
              details={"channel": caller.channel, "client_id": caller.client_id, "status": status, "latency_ms": latency,
                       "error": (error or "")[:300] or None}, session=s)
        emit(ws, "tool.invoked", {"tool_id": rec["tool_id"], "version": rec.get("version"), "status": status,
                                  "channel": caller.channel, "latency_ms": latency}, run_id=caller.run_id,
             actor=caller.actor, session=s)


def _has_access(caller: Caller, ws: str) -> bool:
    """Whether a refusal may be recorded against the workspace (a caller with no access learns nothing)."""
    if caller.principal is not None:
        return ws in caller.principal.grants
    from analystos.governance.policy import member_role

    try:
        with session_scope() as s:
            return member_role(s, s.merge(caller.user), ws) is not None
    except AnalystOSError:
        return False


def invoke(caller: Caller, workspace_id: str, ref: str, arguments: dict[str, Any] | None = None, *,
           approval_id: str | None = None) -> dict[str, Any]:
    if caller.channel not in CHANNELS:
        raise InvalidInput(f"channel must be one of {', '.join(CHANNELS)}")
    arguments = dict(arguments or {})
    started = time.perf_counter()
    try:
        tool_id, _ = split_ref(ref)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from None
    rec: dict[str, Any] = {"tool_id": tool_id, "version": None, "kind": None}
    try:
        with session_scope() as s:
            if caller.principal is not None:  # a client without a grant for the tool learns nothing about it
                from analystos.mcp import grants as G

                G.check_grant(caller.principal, workspace_id, tool_id)
            else:  # nor does a non-member learn which tool ids exist
                from analystos.governance.policy import require_role

                require_role(s, s.merge(caller.user), workspace_id, "viewer")
            row = store.resolve(s, workspace_id, ref)
            rec.update(version=row.version, kind=row.kind)
            m = store.verified(s, row)
            _check_exposure(m, caller.channel)
            _authorize_caller(s, caller, workspace_id, m)
            _check_rate(s, caller, workspace_id, m)
            validate_arguments(m, arguments)
            deprecated = row.status == "deprecated"
            pending = None
            if m.kind == "external_mcp":
                _external_current(s, workspace_id, row)
            elif m.execution.via not in PATHS:
                raise PolicyDenied(f"tool {m.ref} has no registry execution path ({m.execution.via})")
            elif m.side_effect != "none" and m.kind != "playbook":
                pending = _approval(s, caller, workspace_id, row, m, arguments, approval_id)
        if pending is not None:
            _record(workspace_id, caller, rec, status="approval_required", reason="approval_required", started=started,
                    approval_id=pending["approval_id"])
            return {"tool_id": m.tool_id, "version": m.version, "kind": m.kind, **pending}
        if m.kind == "external_mcp":
            out = run_external(caller, workspace_id, m, arguments, approval_id)
            status = "approval_required" if out.get("status") == "approval_required" else (
                "error" if out.get("status") == "error" else "ok")
            _record(workspace_id, caller, rec, status=status, started=started, out=out,
                    approval_id=out.get("approval_id") or approval_id)
            return {"tool_id": m.tool_id, "version": m.version, "kind": m.kind, "deprecated": deprecated, **out}
        result = PATHS[m.execution.via](caller, workspace_id, m, arguments)
    except AnalystOSError as exc:
        if _has_access(caller, workspace_id):
            denied = exc.http_status in (401, 403, 404, 409, 429) and not isinstance(exc, Conflict)
            _record(workspace_id, caller, rec, status="denied" if denied else "error", reason=exc.code[:80],
                    started=started, error=exc.message, approval_id=approval_id)
        raise
    _record(workspace_id, caller, rec, status="ok", started=started, out=result, approval_id=approval_id)
    return {"tool_id": m.tool_id, "version": m.version, "kind": m.kind, "deprecated": deprecated, "status": "ok",
            "result": result}

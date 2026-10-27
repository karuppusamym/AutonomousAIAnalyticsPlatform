"""Invoke one capability from the registry by id (P4-U07's generated forms; the backend gap U06/U07 reported).

Order, all before anything runs: the manifest (with the workspace's MCP overlay), enablement and
certification, the input against the manifest's `input_schema`, that the generic runtime can execute
it (a python `spec.call: context` entry, as for declarative agents), the entry and its tool gate, then
the side effect. A capability that is not read-only (`write_internal`, `write_external`) needs an
editor and never executes directly: the first call creates an approval request bound to the payload
hash *and the requesting user*; only that user, presenting the approved request, runs it, verified
(`verify_for_execution`) and consumed immediately before the entry runs. MCP tools go through the MCP client,
which applies the same rule with its own allowlist and classification.

The entry runs with an `InvokeContext`: the caller's resolved scope, SQL only through the gateway
(tool gate `sql.execute`, a small per-call statement budget), the workspace's PII policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from analystos.capabilities import enablement, registry
from analystos.capabilities.validation import EXECUTABLE_KINDS
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import ApprovalRequired, BudgetExceeded, InvalidInput, PolicyDenied
from analystos.db.base import session_scope

READ_ONLY = ("none", "read_source")
APPROVAL_ACTION = "capability.invoke"
MAX_QUERIES = 5


@dataclass
class InvokeContext:
    """What a `call: context` entry needs outside a run: scope, policy, a governed SQL runner."""

    user: Any
    workspace: Any
    scope: Any
    policy: Any
    agent: Any
    services: Any
    capability: str
    run: Any = None
    task: Any = None
    queries: int = field(default=0)

    def tools(self) -> Any:
        from analystos.contracts.policy import ExecutionIdentity
        from analystos.tools.registry import ToolRuntime

        identity = ExecutionIdentity(user_id=self.user.id, workspace_id=self.workspace.id, agent_id=self.agent.id,
                                     purpose="capability.invoke")
        return ToolRuntime(user=self.user, identity=identity, agent=self.agent, scope=getattr(self, "scope", None))

    def run_sql(self, source_id: str | None = None) -> Any:
        """Tool gate (`sql.execute`, the workspace policy applies), budget, then the gateway as the user."""
        self.tools().authorize("sql.execute", {"capability": self.capability}, bound=False)
        inner = self.services.gateway.run_sql_for(self.scope, actor=f"user:{self.user.id}",
                                                  **({"source_id": source_id} if source_id else {}))
        ctx = self

        class _Budgeted:
            dialect = inner.dialect

            def __call__(self, sql: str, *, purpose: str = "capability", max_rows: int | None = None, use_cache: bool = True):
                if ctx.queries >= MAX_QUERIES:
                    raise BudgetExceeded(f"{ctx.capability} used its {MAX_QUERIES} statements for this call")
                ctx.queries += 1
                return inner(sql, purpose=f"capability:{ctx.capability}"[:120], max_rows=max_rows)

        return _Budgeted()


def _snapshot(session: Any, workspace_id: str) -> registry.Snapshot:
    try:
        from analystos.mcp.client import workspace_snapshot
    except ImportError:  # the MCP extra is optional
        return registry.current()
    return workspace_snapshot(session, workspace_id)


def schema_errors(m: CapabilityManifest, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Every input error, in the shape the UI's forms read (`loc`, `msg`)."""
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(m.input_schema or {"type": "object"}).iter_errors(arguments), key=lambda e: list(e.path))
    return [{"loc": ["arguments", *e.path], "msg": e.message} for e in errors[:20]]


def executable(m: CapabilityManifest) -> bool:
    if is_http_tool(m):
        return True
    return m.kind in EXECUTABLE_KINDS and m.spec.get("call") == "context" and (m.entry or "").startswith("python:")


def is_http_tool(m: CapabilityManifest) -> bool:
    """P7-11: a Tool whose entry is ``http:<name>`` and whose ``spec.http`` names the URL and method."""
    return m.kind == "Tool" and (m.entry or "").startswith("http:")


def _http_entry(m: CapabilityManifest) -> Any:
    from analystos.tools import http as outbound

    outbound.http_spec(m)  # refuses a malformed spec before any approval is requested or consumed

    def run(ctx: InvokeContext, **arguments: Any) -> dict[str, Any]:
        return outbound.invoke_manifest(m, arguments)

    return run


def payload_for(m: CapabilityManifest, arguments: dict[str, Any], requested_by: str) -> dict[str, Any]:
    """What an approval covers: the exact capability version and arguments, for the user who asked.
    The entry runs under the executing user's scope, so an approval is not transferable."""
    return {"capability": m.ref, "arguments": arguments, "requested_by": requested_by}


def invoke(user: Any, workspace_id: str, capability_id: str, arguments: dict[str, Any] | None = None, *,
           approval_id: str | None = None) -> dict[str, Any]:
    from analystos.contracts.registry import AgentPolicies, AgentSpec
    from analystos.db.models import Approval
    from analystos.events.bus import emit
    from analystos.governance.approvals import consume, request_approval, verify_for_execution
    from analystos.governance.audit import audit
    from analystos.governance.policy import get_workspace, load_policy, require_role, resolve_scope
    from analystos.runtime.context import default_services

    arguments = dict(arguments or {})
    with session_scope() as s:
        require_role(s, s.merge(user), workspace_id, "viewer")
        snap = _snapshot(s, workspace_id)
        m = snap.get(capability_id)
        if m.source.startswith("mcp:") or (m.entry or "").startswith("mcp://"):
            server, tool = m.spec.get("server"), m.spec.get("tool")
            if not (server and tool) and (m.entry or "").startswith("mcp://"):
                server, _, tool = m.entry[len("mcp://"):].partition("/")
            if not (server and tool):
                raise InvalidInput(f"{m.id} names no MCP server and tool")
        else:
            server = tool = None
            problem = enablement.usable(m, snap, enablement.overrides(s, workspace_id), autonomous_run=False)
            if problem:
                raise PolicyDenied(problem)
    if server is not None:
        from analystos.mcp import client as mcp_client

        return mcp_client.invoke_tool(session_scope, user, workspace_id, server, tool, arguments, approval_id=approval_id)
    if not executable(m):
        raise InvalidInput(f"{m.ref} cannot be run on its own: it runs inside investigations and playbooks",
                           details={"reason": "not_invocable", "kind": m.kind})
    errors = schema_errors(m, arguments)
    if errors:
        raise InvalidInput(f"input does not match {m.id} input_schema: {errors[0]['msg']}", details={"errors": errors})
    payload = payload_for(m, arguments, user.id)
    writes = m.side_effect not in READ_ONLY
    with session_scope() as s:
        me = s.merge(user)
        require_role(s, me, workspace_id, "editor" if writes else "analyst")  # a write needs publish rights to request
        ws = get_workspace(s, workspace_id)
        policy = load_policy(s, ws)
        scope = resolve_scope(s, me, workspace_id)
        s.expunge_all()
    agent = AgentSpec(id="capability_console", name="Capability console", description="A capability run by a signed-in user",
                      policies=AgentPolicies(pii_access=policy.pii_access))
    ctx = InvokeContext(user=user, workspace=ws, scope=scope, policy=policy, agent=agent, services=default_services(),
                        capability=m.ref)
    # Everything that can refuse runs before an approval is requested or consumed: a gate denial or an
    # unresolvable entry must not burn a single-use approval.
    fn = _http_entry(m) if is_http_tool(m) else registry.resolve_entry(m)
    gate = m.spec.get("tool")
    if gate:
        ctx.tools().authorize(gate, {"capability": m.ref, **arguments}, bound=False)
    if writes:
        with session_scope() as s:
            if not approval_id:
                apr = request_approval(s, workspace_id=workspace_id, run_id=None, action=APPROVAL_ACTION, payload=payload,
                                       plan_hash=None, policy_version=ws.policy_version, requested_by=user.id,
                                       risk_tier="high" if m.side_effect == "write_external" else "medium",
                                       destination=m.id, affected_assets=[m.id],
                                       evidence={"side_effect": m.side_effect, "certification": m.certification.status})
                return {"status": "approval_required", "approval_id": apr.id, "payload_hash": apr.payload_hash,
                        "capability": m.ref, "side_effect": m.side_effect}
            apr = s.get(Approval, approval_id, with_for_update=True)
            if apr is None or apr.workspace_id != workspace_id or apr.action != APPROVAL_ACTION:
                raise ApprovalRequired("the approval does not cover a capability invocation in this workspace")
            if apr.requested_by != user.id:
                audit(f"user:{user.id}", "capability.invoke_refused", workspace_id=workspace_id, target=m.ref,
                      decision="deny", reasons=["approval_belongs_to_another_requester"],
                      details={"approval_id": approval_id})  # own transaction: survives the refusal
                raise PolicyDenied("this approval was requested by another user; request your own")
            verify_for_execution(s, approval_id, payload=payload, plan_hash=None)
            consume(s, apr)  # single use (compare-and-set): consumed immediately before the side effect
    result = fn(ctx, **arguments)
    with session_scope() as s:
        audit(f"user:{user.id}", "capability.invoked", workspace_id=workspace_id, target=m.ref,
              details={"side_effect": m.side_effect, "approval_id": approval_id, "queries": ctx.queries}, session=s)
        emit(workspace_id, "capability.invoked", {"capability": m.ref, "side_effect": m.side_effect}, actor=f"user:{user.id}",
             session=s)
    return {"status": "ok", "capability": m.ref, "side_effect": m.side_effect, "result": result}

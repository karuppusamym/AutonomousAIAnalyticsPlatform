"""P4-X05 MCP client against a real MCP server (official SDK, streamable HTTP, local port).

The server is a TEST DOUBLE standing in for Superset/dbt MCP (see tests/mcp_double.py): these tests
prove the AnalystOS side — allowlist, screening, classification, the tool gate, approvals, budget
and audit — not vendor certification.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.mcp_double import double_server

from analystos.core.errors import ApprovalRequired, BudgetExceeded, Forbidden, InvalidInput, PolicyDenied
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, AuditEvent, McpServer, ToolExecution, User
from analystos.governance import approvals
from analystos.mcp import client as mc
from analystos.security.auth import hash_password
from analystos.services.workspaces import add_member, create_workspace, set_policy

pytestmark = pytest.mark.integration
pytest.importorskip("mcp", reason="needs the optional `mcp` extra")

TOKEN = "double-token-" + "x" * 16


def _user(s, email):
    u = User(id=new_id("usr"), email=email, name=email, password_hash=hash_password("x"))
    s.add(u)
    s.flush()
    return u


@pytest.fixture()
def double(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_MCP_DOUBLE_TOKEN", TOKEN)
    with double_server(TOKEN) as d:
        yield d


@pytest.fixture()
def world(control_db, double):
    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        analyst = _user(s, f"analyst-{new_id('x')}@t")
        approver = _user(s, f"approver-{new_id('x')}@t")
        ws = create_workspace(s, owner, name="MCP client test", objective="Use BI tools over MCP", autonomy_level=3)
        s.flush()
        add_member(s, owner, ws.id, analyst.email, "analyst")
        add_member(s, owner, ws.id, approver.email, "approver")
        ids = dict(ws=ws.id, owner=owner.id, analyst=analyst.id, approver=approver.id)
    return ids


def U(uid):
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


def _registered(world, double, *, allow=True, refresh=True, config=None) -> str:
    with session_scope() as s:
        srv = mc.register_server(s, U(world["owner"]), world["ws"], name="bi_double", url=double.url,
                                 secret_ref="env:ANALYSTOS_SOURCE_SECRET_MCP_DOUBLE_TOKEN", config=config)
        sid = srv.id
    if allow:
        with session_scope() as s:
            mc.set_allowed(s, U(world["owner"]), world["ws"], sid, True)
    if refresh:
        with session_scope() as s:
            mc.refresh_tools(s, U(world["owner"]), world["ws"], sid)
    return sid


def _invoke(world, who, tool, args, **kw):
    return mc.invoke_tool(session_scope, U(world[who]), world["ws"], "bi_double", tool, args, **kw)


def test_registration_is_owner_only_and_never_stores_a_secret_value(world, double):
    with session_scope() as s, pytest.raises(Forbidden):
        mc.register_server(s, U(world["analyst"]), world["ws"], name="x", url=double.url)
    with session_scope() as s, pytest.raises(InvalidInput):
        mc.register_server(s, U(world["owner"]), world["ws"], name="x", url=double.url, secret_ref=TOKEN)
    with session_scope() as s, pytest.raises(InvalidInput):
        mc.register_server(s, U(world["owner"]), world["ws"], name="x", url="http://user:pw@127.0.0.1/mcp")


def test_nothing_is_sent_before_the_allowlist(world, double):
    sid = _registered(world, double, allow=False, refresh=False)
    with session_scope() as s, pytest.raises(PolicyDenied):
        mc.refresh_tools(s, U(world["owner"]), world["ws"], sid)
    assert double.calls == [] and double.auth_failures == []


def test_platform_host_allowlist_gates_registration_and_connection(world, double, monkeypatch):
    """P4-X05: only an administrator's allowlist entry makes a host registrable; removing it stops calls."""
    from analystos.core.config import get_settings
    from analystos.services import platform_settings

    monkeypatch.setattr(get_settings(), "mcp_host_allowlist", "")
    platform_settings.invalidate()
    with session_scope() as s:
        admin = User(id=new_id("usr"), email=f"admin-{new_id('x')}@t", name="a", password_hash=hash_password("x"),
                     is_admin=True)
        s.add(admin)
        s.flush()
        admin_id = admin.id
    try:
        with session_scope() as s, pytest.raises(PolicyDenied, match="platform MCP host allowlist"):
            mc.register_server(s, U(world["owner"]), world["ws"], name="bi_double", url=double.url)
        with session_scope() as s:
            refused = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == world["ws"],
                                                         AuditEvent.action == "mcp.server_refused")).all()
            assert len(refused) == 1 and refused[0].decision == "deny"
            assert s.scalar(select(McpServer.id).where(McpServer.workspace_id == world["ws"])) is None
        with session_scope() as s, pytest.raises(Forbidden):  # a workspace owner cannot extend it
            platform_settings.update(s, U(world["owner"]), {"outbound": {"mcp_host_allowlist": ["127.0.0.1"]}})
        with session_scope() as s:
            platform_settings.update(s, s.get(User, admin_id), {"outbound": {"mcp_host_allowlist": ["127.0.0.1"]}})
        sid = _registered(world, double)
        assert double.calls  # registered, allowed and refreshed against the listed host
        with session_scope() as s:
            platform_settings.update(s, s.get(User, admin_id), {"outbound": {"mcp_host_allowlist": []}})
        calls = len(double.calls)
        with session_scope() as s, pytest.raises(PolicyDenied, match="allowlist"):
            mc.refresh_tools(s, U(world["owner"]), world["ws"], sid)
        assert len(double.calls) == calls  # nothing sent once the host left the allowlist
    finally:
        with session_scope() as s:
            platform_settings.update(s, s.get(User, admin_id), {"outbound": {"mcp_host_allowlist": []}})
        platform_settings.invalidate()


def test_tools_import_as_unclassified_write_external_capabilities_with_screening(world, double):
    sid = _registered(world, double)
    with session_scope() as s:
        srv = s.get(McpServer, sid)
        tools = {t["name"]: t for t in srv.tools}
        manifests = {m.id: m for m in mc.workspace_manifests(s, world["ws"])}
        snapshot = mc.workspace_snapshot(s, world["ws"])
    assert srv.status == "ready" and double.auth_failures == []  # the bearer came from secret_ref
    assert tools["poisoned"]["refused"] and "injection_pattern" in tools["poisoned"]["flags"]
    assert "tool.mcp_bi_double_poisoned" not in manifests  # refused tools never become capabilities
    rows = manifests["tool.mcp_bi_double_count_rows"]
    assert rows.entry == "mcp://bi_double/count_rows" and rows.source == "mcp:bi_double"
    assert rows.side_effect == "write_external" and rows.needs_approval  # readOnlyHint is advisory only
    assert rows.spec["read_only_hint"] is True and rows.spec["classified"] is False
    assert rows.certification.status == "draft" and not rows.autonomous_ok
    assert manifests["tool.mcp_bi_double_create_chart"].side_effect == "write_external"
    assert snapshot.get("tool.mcp_bi_double_count_rows").ref == "tool.mcp_bi_double_count_rows@1.0.0"
    with session_scope() as s:  # registered for this workspace only
        other = create_workspace(s, s.get(User, world["owner"]), name="other", objective="x" * 12)
        s.flush()
        assert mc.workspace_manifests(s, other.id) == []


def test_unclassified_tool_requires_a_hash_bound_single_use_approval(world, double):
    _registered(world, double)
    args = {"name": "SLA by group", "dataset": "incidents"}
    # analysts cannot drive an external side effect at all
    with pytest.raises(PolicyDenied):
        _invoke(world, "analyst", "create_chart", args)
    out = _invoke(world, "owner", "create_chart", args)
    assert out["status"] == "approval_required" and out["side_effect"] == "write_external"
    assert double.calls == []  # nothing ran without the approval
    apr_id = out["approval_id"]
    with pytest.raises(ApprovalRequired):  # pending is not approved
        _invoke(world, "owner", "create_chart", args, approval_id=apr_id)
    with session_scope() as s:
        approvals.decide(s, apr_id, s.get(User, world["approver"]), approve=True)
    with pytest.raises(ApprovalRequired):  # the approval binds the exact arguments
        _invoke(world, "owner", "create_chart", {**args, "dataset": "changes"}, approval_id=apr_id)

    assert double.calls == []
    out = _invoke(world, "owner", "create_chart", args, approval_id=apr_id)
    assert out["status"] == "ok" and not out["result"]["is_error"] and '"chart_id": 7' in out["result"]["text"]
    assert double.calls == [("create_chart", args)]
    with session_scope() as s:
        executed = s.scalars(select(Approval).where(Approval.workspace_id == world["ws"], Approval.status == "executed")).all()
    assert len(executed) == 1


def _fresh(world, args) -> str:
    out = _invoke(world, "owner", "create_chart", args)
    with session_scope() as s:
        approvals.decide(s, out["approval_id"], s.get(User, world["approver"]), approve=True)
    return out["approval_id"]


def test_approval_cannot_be_replayed(world, double):
    _registered(world, double)
    args = {"name": "c", "dataset": "d"}
    apr = _fresh(world, args)
    assert _invoke(world, "owner", "create_chart", args, approval_id=apr)["status"] == "ok"
    with pytest.raises(ApprovalRequired):
        _invoke(world, "owner", "create_chart", args, approval_id=apr)
    assert len(double.calls) == 1


def test_classified_read_tool_runs_for_analysts_and_results_are_screened(world, double):
    sid = _registered(world, double)
    with session_scope() as s:
        mc.classify_tool(s, U(world["owner"]), world["ws"], sid, "count_rows", "read_source")
        mc.classify_tool(s, U(world["owner"]), world["ws"], sid, "sneaky", "read_source")
    out = _invoke(world, "analyst", "count_rows", {"table": "incident"})
    assert out["status"] == "ok" and out["side_effect"] == "read_source"
    assert "42 rows" in out["result"]["text"]
    sneaky = _invoke(world, "analyst", "sneaky", {"q": "sla"})["result"]
    assert "Ignore previous instructions" not in sneaky["text"] and "injection_removed" in sneaky["flags"]
    assert sneaky["truncated"] and len(sneaky["text"]) <= mc.RESULT_MAX_CHARS + 3
    with session_scope() as s, pytest.raises(PolicyDenied):  # refused tools cannot be classified into use
        mc.classify_tool(s, U(world["owner"]), world["ws"], sid, "poisoned", "none")


def test_changed_tool_definition_voids_its_classification(world, double):
    sid = _registered(world, double)
    with session_scope() as s:
        mc.classify_tool(s, U(world["owner"]), world["ws"], sid, "count_rows", "read_source")
    double.server.remove_tool("count_rows")

    @double.server.tool()
    def count_rows(table: str, drop: bool = False) -> str:
        """Count the rows of a dataset (and optionally drop it)."""
        return "0"

    with session_scope() as s:
        mc.refresh_tools(s, U(world["owner"]), world["ws"], sid)
        manifests = {m.id: m for m in mc.workspace_manifests(s, world["ws"])}
    assert manifests["tool.mcp_bi_double_count_rows"].side_effect == "write_external"


def test_policy_denylist_disallow_and_run_budget(world, double):
    sid = _registered(world, double, config={"max_calls_per_run": 1})
    with session_scope() as s:
        mc.classify_tool(s, U(world["owner"]), world["ws"], sid, "count_rows", "read_source")
        run = AnalysisRun(id=new_id("run"), workspace_id=world["ws"], objective="budget test objective",
                          requested_by=world["owner"], status="RUNNING")
        s.add(run)
        run_id = run.id
    assert _invoke(world, "analyst", "count_rows", {"table": "a"}, run_id=run_id)["status"] == "ok"
    with pytest.raises(BudgetExceeded):
        _invoke(world, "analyst", "count_rows", {"table": "b"}, run_id=run_id)

    with session_scope() as s:
        set_policy(s, s.get(User, world["owner"]), world["ws"], {"tool_denylist": ["tool.mcp_bi_double_*"]})
    with pytest.raises(PolicyDenied, match="tool_denied_by_workspace_policy"):
        _invoke(world, "analyst", "count_rows", {"table": "c"})
    with session_scope() as s:
        set_policy(s, s.get(User, world["owner"]), world["ws"], {"tool_denylist": []})
        mc.set_allowed(s, U(world["owner"]), world["ws"], sid, False)
    with pytest.raises(PolicyDenied, match="not_allowlisted"):
        _invoke(world, "analyst", "count_rows", {"table": "d"})
    assert [c[1]["table"] for c in double.calls] == ["a"]


def test_every_gate_outcome_is_audited(world, double):
    _registered(world, double)
    _invoke(world, "owner", "create_chart", {"name": "a", "dataset": "b"})
    with pytest.raises(PolicyDenied):
        _invoke(world, "analyst", "create_chart", {"name": "a", "dataset": "b"})
    with session_scope() as s:
        execs = s.scalars(select(ToolExecution).where(ToolExecution.workspace_id == world["ws"])).all()
        audits = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == world["ws"],
                                                    AuditEvent.action.like("mcp.%"))).all()
    assert {e.status for e in execs} >= {"approval_required", "denied"}
    assert all(e.tool_id == "tool.mcp_bi_double_create_chart" for e in execs)
    actions = {(a.action, a.decision) for a in audits}
    assert ("mcp.tool_call", "approval_required") in actions and ("mcp.tool_call", "deny") in actions
    assert {"mcp.server_registered", "mcp.server_allowed", "mcp.server_refreshed"} <= {a.action for a in audits}


def test_http_api_register_refresh_invoke(world, double):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.security.auth import issue_token

    owner = U(world["owner"])
    h = {"Authorization": f"Bearer {issue_token(owner.id, owner.email)}"}
    c = TestClient(app)
    ws = world["ws"]
    r = c.post(f"/api/workspaces/{ws}/mcp/servers", json={"name": "bi_double", "url": double.url,
                                                           "secret_ref": "env:ANALYSTOS_SOURCE_SECRET_MCP_DOUBLE_TOKEN"}, headers=h)
    assert r.status_code == 201 and "secret_ref" not in r.json() and r.json()["has_secret"]
    sid = r.json()["id"]
    assert c.post(f"/api/workspaces/{ws}/mcp/servers/{sid}/refresh", headers=h).status_code == 403
    assert c.post(f"/api/workspaces/{ws}/mcp/servers/{sid}/allow", json={"allowed": True}, headers=h).status_code == 200
    r = c.post(f"/api/workspaces/{ws}/mcp/servers/{sid}/refresh", headers=h)
    assert r.status_code == 200 and {m["id"] for m in r.json()["capabilities"]} >= {"tool.mcp_bi_double_create_chart"}
    r = c.post(f"/api/workspaces/{ws}/mcp/servers/bi_double/tools/create_chart/invoke",
               json={"arguments": {"name": "n", "dataset": "d"}}, headers=h)
    assert r.status_code == 202 and r.json()["status"] == "approval_required"
    r = c.post(f"/api/workspaces/{ws}/mcp/servers/{sid}/tools/count_rows/classify", json={"side_effect": "none"}, headers=h)
    assert r.status_code == 200
    caps = c.get(f"/api/workspaces/{ws}/mcp/capabilities", headers=h).json()
    assert {m["id"]: m["side_effect"] for m in caps}["tool.mcp_bi_double_count_rows"] == "none"
    r = c.post(f"/api/workspaces/{ws}/mcp/servers/bi_double/tools/count_rows/invoke", json={"arguments": {"table": "t"}},
               headers=h)
    assert r.status_code == 200 and "42 rows" in r.json()["result"]["text"]

"""S10 on Postgres under strict tenancy: the registry migration, tenant isolation of registry rows and signing
keys, publish-as-tool and invoke over the v2 REST API through the real QueryGateway against staged ServiceNow
data, MCP/API client grants (patterns, per-minute rate, daily quota, expiry) with usage records, hash-bound
approvals for side effects, the MCP protocol exposure, and external MCP admission with quarantine and
re-screening against the SDK-built test double (`tests/mcp_double.py`: a double, not a certification)."""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.ids import new_id, stable_hash, utcnow  # noqa: E402
from analystos.db.base import session_scope  # noqa: E402
from analystos.db.models import (  # noqa: E402
    Artifact,
    McpUsage,
    QueryExecution,
    RegisteredTool,
    SemanticMetric,
    ToolInvocation,
    ToolSigningKey,
    User,
    VerifiedQuery,
)
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration


def _user(s, email: str, *, admin: bool = False) -> User:
    from analystos.security.auth import hash_password

    u = User(id=new_id("usr"), email=email, name=email.split("@")[0], password_hash=hash_password("x"), is_admin=admin)
    s.add(u)
    s.flush()
    return u


def _auth(user_id: str, email: str) -> dict[str, str]:
    from analystos.security.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(user_id, email)}"}


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        admin = _user(s, f"admin-{new_id('x')}@t", admin=True)
        people = {r: _user(s, f"{r}-{new_id('x')}@t") for r in ("analyst", "approver", "editor", "viewer")}
        ws = create_workspace(s, owner, name="Tool registry", objective="Publish governed data products", autonomy_level=3)
        s.flush()
        for role, u in people.items():
            add_member(s, owner, ws.id, u.email, role)
        ids = {"ws": ws.id, "owner": owner.id, "admin": admin.id, **{r: u.id for r, u in people.items()}}
        ids["headers"] = {k: _auth(v.id, v.email) for k, v in {"owner": owner, "admin": admin, **people}.items()}
    return ids


@pytest.fixture(scope="module")
def api(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def U(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


@pytest.fixture()
def live_gateway(monkeypatch, staged_servicenow, servicenow_scope, dp_settings, dp_session_factory):
    """The real QueryGateway over the staged ServiceNow schema (its reader role belongs to the data-plane
    fixture's workspace, so the scope comes from that fixture; roles are still checked in the registry)."""
    from analystos.gateway.service import QueryGateway
    from analystos.toolreg import invoke as inv

    gw = QueryGateway(dp_settings, session_factory=dp_session_factory)
    monkeypatch.setattr("analystos.runtime.context.default_gateway", lambda: gw)
    monkeypatch.setattr(inv, "_scope", lambda caller, ws: servicenow_scope)
    return {"gw": gw, "scope": servicenow_scope, "schema": staged_servicenow["schema"],
            "source_id": staged_servicenow["source_id"], "dp": dp_session_factory}


def _sources(world, live, suffix: str) -> dict[str, str]:
    vq, art = new_id("vq"), new_id("art")
    with session_scope() as s:
        s.add(VerifiedQuery(id=vq, workspace_id=world["ws"], name=f"Incidents by priority {suffix}",
                            description="Incident count for one priority.",
                            sql_template="SELECT priority, COUNT(*) AS n FROM incident WHERE priority = {{priority}} "
                                         "GROUP BY priority",
                            parameters=[{"name": "priority", "type": "number"}], source_id=live["source_id"],
                            dialect="postgres", status="active", created_by=world["owner"]))
        content = {"name": f"incidents_{suffix}", "sql": "SELECT number, priority, assignment_group_name FROM incident",
                   "columns": [{"name": "number"}, {"name": "priority"}, {"name": "assignment_group_name"}]}
        s.add(Artifact(id=art, workspace_id=world["ws"], type="dataset", name=f"Incidents {suffix}", status="published",
                       content=content, content_hash=stable_hash(content)))
    return {"vq": vq, "art": art}


# ------------------------------------------------------------------------------------ T1 migration
def test_migration_0061_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from analystos.core.config import REPO_ROOT
    from analystos.db import models, vectors
    from analystos.tenancy.ddl import rls_status

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig61"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    tables = ("registered_tool", "tool_invocation", "tool_signing_key")
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0065_tool_registry")
        insp = inspect(engine)
        for table in tables:
            assert {c["name"] for c in insp.get_columns(table)} == set(models.Base.metadata.tables[table].columns.keys()) | {
                "tenant_id"}, table
        assert {"tool_patterns", "rate_per_min", "expires_at"} <= {c["name"] for c in insp.get_columns("mcp_grant")}
        with engine.connect() as c:
            status = rls_status(c, list(tables))
        assert all(all(v.values()) for v in status.values()), status
        command.downgrade(cfg, "0060_request_desk")
        insp = inspect(engine)
        assert not set(tables) & set(insp.get_table_names())
        assert "tool_patterns" not in {c["name"] for c in insp.get_columns("mcp_grant")}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db} WITH (FORCE)"))
        admin.dispose()


# ------------------------------------------------------------------------------------ T2 + T3 over REST
def test_rest_publish_and_invoke_through_the_real_gateway(api, world, live_gateway):
    h, ws = world["headers"], world["ws"]
    src = _sources(world, live_gateway, "rest")
    r = api.post("/v2/tools:publish", headers=h["owner"], json={"workspace_id": ws, "source_type": "verified_query",
                                                               "ref": src["vq"]})
    assert r.status_code == 201, r.text
    vq_tool = r.json()["tool_id"]
    assert vq_tool == "verified_query.incidents_by_priority_rest" and r.json()["manifest"]["signature"]["alg"] == "ed25519"
    again = api.post("/v2/tools:publish", headers=h["owner"], json={"workspace_id": ws, "source_type": "verified_query",
                                                                   "ref": src["vq"]})
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["version"] == "1.0.0"
    assert api.post("/v2/tools:publish", headers=h["analyst"], json={"workspace_id": ws, "source_type": "dataset",
                                                                    "ref": src["art"]}).status_code == 403
    ds_tool = api.post("/v2/tools:publish", headers=h["editor"], json={
        "workspace_id": ws, "source_type": "dataset", "ref": src["art"],
        "options": {"columns": ["number", "priority"], "limits": {"max_rows": 20}}}).json()["tool_id"]

    out = api.post(f"/v2/tools/{vq_tool}:invoke", headers=h["analyst"], json={"workspace_id": ws, "arguments": {"priority": 1}})
    assert out.status_code == 200, out.text
    body = out.json()
    direct = live_gateway["gw"].execute(live_gateway["scope"], "SELECT priority, COUNT(*) AS n FROM incident "
                                        "WHERE priority = 1 GROUP BY priority", actor="user:test", use_cache=False)
    assert body["version"] == "1.0.0" and body["result"]["rows"] == direct.rows and body["result"]["governed"] is True
    with live_gateway["dp"]() as s:  # the gateway audited it as the caller, for this tool
        q = s.get(QueryExecution, body["result"]["query_id"])
        assert q.actor == f"user:{world['analyst']}" and q.purpose == f"tool:{vq_tool}" and q.status == "ok"
    rows = api.post(f"/v2/tools/{ds_tool}:invoke", headers=h["analyst"], json={"workspace_id": ws, "arguments": {
        "filters": [{"column": "priority", "op": "=", "value": 1}], "order_by": [{"column": "number"}], "limit": 5}}).json()
    assert rows["result"]["columns"] == ["number", "priority"] and 0 < len(rows["result"]["rows"]) <= 5
    assert {r[1] for r in rows["result"]["rows"]} == {1}
    # no raw SQL path, an undeclared column is refused, a viewer may not invoke
    assert api.post(f"/v2/tools/{ds_tool}:invoke", headers=h["analyst"], json={
        "workspace_id": ws, "arguments": {"sql": "SELECT caller_id FROM incident"}}).status_code == 422
    assert api.post(f"/v2/tools/{ds_tool}:invoke", headers=h["analyst"], json={
        "workspace_id": ws, "arguments": {"columns": ["assignment_group_name"]}}).status_code == 422
    assert api.post(f"/v2/tools/{vq_tool}:invoke", headers=h["viewer"], json={
        "workspace_id": ws, "arguments": {"priority": 1}}).status_code == 403

    listed = {t["tool_id"] for t in api.get("/v2/tools", headers=h["analyst"], params={"workspace_id": ws}).json()}
    assert {vq_tool, ds_tool} <= listed
    assert api.get("/v2/tools", headers=h["viewer"], params={"workspace_id": ws}).json() == []  # nothing a viewer may use
    assert {t["tool_id"] for t in api.get("/v2/tools", headers=h["analyst"],
                                          params={"workspace_id": ws, "kind": "data_product", "q": "priority"}).json()} >= {vq_tool}
    view = api.get(f"/v2/tools/{vq_tool}", headers=h["viewer"], params={"workspace_id": ws}).json()
    assert view["health"]["signature"] == "valid" and view["health"]["invocable"] and view["versions"][0]["version"] == "1.0.0"
    usage = api.get(f"/v2/tools/{vq_tool}/usage", headers=h["analyst"], params={"workspace_id": ws}).json()
    assert usage["by_status"] == {"ok": 1, "denied": 1} and usage["recent"][0]["caller"] == f"user:{world['viewer']}"

    # a manifest changed in the database behind the registry's back is refused at once
    with session_scope() as s:
        s.execute(text("UPDATE registered_tool SET manifest = jsonb_set(manifest, '{limits,max_rows}', '50000') "
                       "WHERE workspace_id = :ws AND tool_id = :t"), {"ws": ws, "t": ds_tool})
    refused = api.post(f"/v2/tools/{ds_tool}:invoke", headers=h["analyst"], json={"workspace_id": ws, "arguments": {}})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "manifest_refused"
    assert api.get(f"/v2/tools/{ds_tool}", headers=h["analyst"], params={"workspace_id": ws}).json()["health"]["signature"] == "tampered"
    # deprecate: unpinned calls stop, pinned calls keep working; retire stops both
    dep = api.post(f"/v2/tools/{vq_tool}:deprecate", headers=h["editor"], json={"workspace_id": ws, "reason": "use v2"})
    assert dep.json()["versions"] == ["1.0.0"]
    assert api.post(f"/v2/tools/{vq_tool}:invoke", headers=h["analyst"], json={
        "workspace_id": ws, "arguments": {"priority": 1}}).status_code == 403
    assert api.post(f"/v2/tools/{vq_tool}@1:invoke", headers=h["analyst"], json={
        "workspace_id": ws, "arguments": {"priority": 1}}).json()["deprecated"] is True


METRIC = {"name": "mean_reassignments", "display_name": "Mean reassignments", "dataset": "incidents",
          "description": "Mean reassignments per incident.",
          "expressions": [{"expression": "AVG(reassignment_count)"}], "dimensions": ["assignment_group_name", "opened_at"]}


def _approved_metric(world, live, monkeypatch) -> None:
    """An approved metric over the staged incident table; the catalog the compiler reads is the approved model's."""
    schema = live["schema"]
    catalog = {"id": "m", "version": 1, "hash": "mh", "datasets": [{
        "name": "incidents", "source": f"SELECT assignment_group_name, reassignment_count, opened_at FROM {schema}.incident",
        "fields": [{"name": "assignment_group_name", "expressions": [{"expression": "assignment_group_name"}], "dimension": {}},
                   {"name": "reassignment_count", "expressions": [{"expression": "reassignment_count"}]},
                   {"name": "opened_at", "expressions": [{"expression": "opened_at"}], "dimension": {"is_time": True}}]}],
        "metrics": {"mean_reassignments": {"id": "met_live", "version": 1, "hash": "dh", "definition": METRIC}}}
    monkeypatch.setattr("analystos.semantic.compiler.load_catalog", lambda s, w: catalog)
    with session_scope() as s:
        if s.scalar(select(SemanticMetric).where(SemanticMetric.workspace_id == world["ws"],
                                                 SemanticMetric.name == "mean_reassignments")) is None:
            s.add(SemanticMetric(id=new_id("met"), workspace_id=world["ws"], name="mean_reassignments", version=1,
                                 status="approved", definition=METRIC, expression="AVG(reassignment_count)",
                                 normalized_expression="avg(reassignment_count)", proposed_by=world["owner"],
                                 proposed_via="user", content_hash="dh"))


def test_metric_tool_compiles_on_postgres(api, world, live_gateway, monkeypatch):
    h, ws = world["headers"], world["ws"]
    _approved_metric(world, live_gateway, monkeypatch)
    tool = api.post("/v2/tools:publish", headers=h["owner"], json={"workspace_id": ws, "source_type": "metric",
                                                                  "ref": "mean_reassignments"}).json()
    assert tool["tool_id"] == "metric.mean_reassignments" and tool["version"] == "1.0.0"
    out = api.post("/v2/tools/metric.mean_reassignments@1:invoke", headers=h["analyst"], json={
        "workspace_id": ws, "arguments": {"dimensions": ["assignment_group_name"],
                                          "filters": [{"field": "assignment_group_name", "op": "is_not_null"}]}}).json()
    res = out["result"]
    assert res["governed"] is True and res["metric_version"] == 1 and len(res["rows"]) == 12
    assert "Network Operations" in {r[0] for r in res["rows"]}
    with live_gateway["dp"]() as s:  # the statement the gateway ran is the compiler's, byte for byte
        q = s.get(QueryExecution, res["query_id"])
        assert stable_hash(q.sql) == res["sql_hash"] and res["provenance"]["metrics"][0]["name"] == "mean_reassignments"


# ------------------------------------------------------------------------------------ T3 grants, usage, approvals
def test_grant_scope_enforced(api, world, live_gateway):
    from analystos.mcp import grants as G

    h, ws = world["headers"], world["ws"]
    src = _sources(world, live_gateway, "grant")
    tool = api.post("/v2/tools:publish", headers=h["owner"], json={"workspace_id": ws, "source_type": "verified_query",
                                                                  "ref": src["vq"]}).json()["tool_id"]
    other = api.post("/v2/tools:publish", headers=h["owner"], json={"workspace_id": ws, "source_type": "dataset",
                                                                   "ref": src["art"]}).json()["tool_id"]
    with session_scope() as s:
        client, token = G.create_client(s, s.get(User, world["admin"]), "partner-agent")
        G.set_grant(s, s.get(User, world["admin"]), client.id, ws, role="analyst", tools=[tool], rate_per_min=2,
                    quotas={tool: 50})
        cid = client.id
    bearer = {"Authorization": f"Bearer {token}"}
    call = {"workspace_id": ws, "arguments": {"priority": 1}}
    assert api.post(f"/v2/tools/{tool}:invoke", headers=bearer, json=call).status_code == 200
    assert api.post(f"/v2/tools/{other}:invoke", headers=bearer, json={"workspace_id": ws}).status_code == 403
    assert [t["tool_id"] for t in api.get("/v2/tools", headers=bearer, params={"workspace_id": ws}).json()] == [tool]
    assert api.post(f"/v2/tools/{tool}:invoke", headers=bearer, json=call).status_code == 200
    limited = api.post(f"/v2/tools/{tool}:invoke", headers=bearer, json=call)
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "quota_exceeded"
    with session_scope() as s:
        counters = {(u.tool, len(u.day)): u.count for u in s.scalars(select(McpUsage).where(McpUsage.client_id == cid))}
        assert counters[(G.RATE_KEY, 16)] == 2 and counters[(tool, 10)] == 2  # the refused call counts nowhere
        rows = list(s.scalars(select(ToolInvocation).where(ToolInvocation.client_id == cid)))
        # the ungranted tool and the over-rate call are recorded as denials; nothing ran for them
        assert sorted(r.status for r in rows) == ["denied", "denied", "ok", "ok"]
        assert {r.caller for r in rows} == {f"mcp_client:{cid}"} and {r.reason for r in rows if r.status == "denied"} == {
            "forbidden", "quota_exceeded"}
        assert {r.channel for r in rows} == {"rest"} and all(r.cost_usd == 0 for r in rows)
        # a pattern grant reaches the family; an expired grant reaches nothing
        G.set_grant(s, s.get(User, world["admin"]), cid, ws, role="analyst", tools=[], tool_patterns=["dataset.*"])
    assert api.post(f"/v2/tools/{other}:invoke", headers=bearer, json={"workspace_id": ws, "arguments": {"limit": 1}}).status_code == 200
    assert api.post(f"/v2/tools/{tool}:invoke", headers=bearer, json=call).status_code == 403
    with session_scope() as s:
        G.set_grant(s, s.get(User, world["admin"]), cid, ws, role="analyst", tools=[], tool_patterns=["dataset.*"],
                    expires_at=utcnow() - timedelta(seconds=1))
    expired = api.post(f"/v2/tools/{other}:invoke", headers=bearer, json={"workspace_id": ws, "arguments": {"limit": 1}})
    assert expired.status_code == 403 and "expired" in expired.json()["error"]["message"]
    assert api.post(f"/v2/tools/{tool}:invoke", headers={"Authorization": "Bearer mcpc_nope.secret"}, json=call).status_code == 401


def test_side_effect_tool_needs_a_hash_bound_single_use_approval(world, monkeypatch):
    from analystos.contracts.tool_manifest import ToolExecutionSpec, ToolManifestV2
    from analystos.core.errors import ApprovalRequired
    from analystos.governance.approvals import decide
    from analystos.toolreg import invoke as inv
    from analystos.toolreg import store

    ran: list[dict] = []
    monkeypatch.setitem(inv.PATHS, "builtin", lambda caller, ws, m, args: ran.append(args) or {"sent": True})
    tool_id = f"builtin.notify_{new_id('x')[-6:]}"
    m = ToolManifestV2(tool_id=tool_id, version="1.0.0", kind="builtin", owner="platform",
                       input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                       side_effect="write_external", permissions=["dataset.read"], execution=ToolExecutionSpec(via="builtin"))
    with session_scope() as s:
        store.register(s, world["ws"], m, status="certified", source={}, actor=f"user:{world['owner']}")
    editor = inv.Caller(user=U(world["editor"]), actor=f"user:{world['editor']}", channel="rest")
    first = inv.invoke(editor, world["ws"], tool_id, {"text": "hi"})
    assert first["status"] == "approval_required" and ran == []
    with session_scope() as s:
        decide(s, first["approval_id"], s.get(User, world["approver"]), approve=True)
    with pytest.raises(ApprovalRequired, match="payload changed"):  # bound to the arguments it approved
        inv.invoke(editor, world["ws"], tool_id, {"text": "something else"}, approval_id=first["approval_id"])
    assert ran == []
    assert inv.invoke(editor, world["ws"], tool_id, {"text": "hi"}, approval_id=first["approval_id"])["status"] == "ok"
    assert ran == [{"text": "hi"}]
    with pytest.raises(ApprovalRequired):  # consumed before the side effect: never replayable
        inv.invoke(editor, world["ws"], tool_id, {"text": "hi"}, approval_id=first["approval_id"])
    with pytest.raises(ApprovalRequired):  # an approval of another action does not cover a tool call
        from analystos.governance.approvals import request_approval

        with session_scope() as s:
            other = request_approval(s, workspace_id=world["ws"], run_id=None, action="publish", payload={"x": 1},
                                     plan_hash=None, policy_version=1, requested_by=world["editor"], risk_tier="high",
                                     destination=None, affected_assets=[]).id
        inv.invoke(editor, world["ws"], tool_id, {"text": "hi"}, approval_id=other)
    with session_scope() as s:
        statuses = [r.status for r in s.scalars(select(ToolInvocation).where(ToolInvocation.tool_id == tool_id)
                                                 .order_by(ToolInvocation.created_at))]
    assert statuses[0] == "approval_required" and statuses.count("ok") == 1


# ------------------------------------------------------------------------------------ T5 tenancy of keys and rows
def test_registry_rows_and_signing_keys_are_tenant_isolated(world):
    from analystos.contracts.tool_manifest import ToolExecutionSpec, ToolManifestV2
    from analystos.db.models import Tenant, Workspace
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope
    from analystos.toolreg import signing, store

    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"reg-{tb[-6:]}", name="Registry tenant B"))
    with tenant_scope(tb), session_scope() as s:
        ub = _user(s, f"b-{tb}@t")
        ws_b = Workspace(id=new_id("ws"), name="B", created_by=ub.id)
        s.add(ws_b)
        s.flush()
        m = ToolManifestV2(tool_id="metric.b_only", version="1.0.0", kind="data_product", owner="b",
                           input_schema={"type": "object"}, side_effect="none",
                           execution=ToolExecutionSpec(via="semantic_compiler"))
        row_b = store.register(s, ws_b.id, m, status="certified", source={}, actor=f"user:{ub.id}")
        manifest_b, ws_b_id = row_b.manifest, ws_b.id
        assert [k.key_id for k in s.scalars(select(ToolSigningKey))] == ["tenant-signing-1"]
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        assert s.scalar(select(RegisteredTool).where(RegisteredTool.workspace_id == ws_b_id)) is None
        assert all(k.tenant_id == DEFAULT_TENANT_ID for k in s.scalars(select(ToolSigningKey)))
        with pytest.raises(signing.ManifestRefused):  # B's manifest does not verify under A's keys
            signing.verify(s, DEFAULT_TENANT_ID, manifest_b)
    with tenant_scope(tb), session_scope() as s:
        signing.verify(s, tb, manifest_b)
        assert s.scalar(select(RegisteredTool).where(RegisteredTool.workspace_id == world["ws"])) is None


# ------------------------------------------------------------------------------------ MCP exposure (needs the extra)
def test_at22_certified_metric_callable_over_mcp_with_a_dimension_filter(world, live_gateway, monkeypatch):
    """AT-22: a certified metric published as a tool is listed to a granted MCP client and callable over the MCP
    protocol with a dimension filter; the response carries governed, the metric version and freshness."""
    pytest.importorskip("mcp", reason="needs the optional `mcp` extra")
    from tests.mcp_double import serve

    from analystos.api.app import app
    from analystos.mcp import grants as G
    from analystos.toolreg import publish as pub

    _approved_metric(world, live_gateway, monkeypatch)
    src = _sources(world, live_gateway, "mcp")
    with session_scope() as s:
        tool = pub.publish(s, s.get(User, world["owner"]), world["ws"], "metric", "mean_reassignments")[0].tool_id
        hidden = pub.publish(s, s.get(User, world["owner"]), world["ws"], "verified_query", src["vq"])[0].tool_id
        client, token = G.create_client(s, s.get(User, world["admin"]), "mcp-agent")
        G.set_grant(s, s.get(User, world["admin"]), client.id, world["ws"], role="analyst", tools=[],
                    tool_patterns=["metric.*"])
        cid = client.id

    async def go(url):
        import httpx
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with (httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                    Client(streamable_http_client(f"{url}/mcp", http_client=http), cache=None) as c):
            tools = {t.name: t for t in (await c.list_tools()).tools}
            ok = await c.call_tool(tool, {"version": "1", "dimensions": ["assignment_group_name"], "filters": [
                {"field": "assignment_group_name", "op": "=", "value": "Network Operations"}]})
            bad = await c.call_tool(tool, {"dimensions": ["caller_id"]})
            return tools, ok, bad

    import httpx

    with serve(app) as url:
        tools, ok, bad = asyncio.run(go(url))
        ungranted = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                   "params": {"name": hidden, "arguments": {"priority": 1}}},
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                        "Accept": "application/json, text/event-stream",
                                        "MCP-Protocol-Version": "2025-06-18"}, timeout=20)
    assert set(tools) == {tool} and tools[tool].annotations.read_only_hint is True
    assert tools[tool].description == "Mean reassignments per incident."
    assert {"dimensions", "filters", "version", "workspace_id"} <= set(tools[tool].input_schema["properties"])
    body = ok.structured_content
    assert body["status"] == "ok" and body["version"] == "1.0.0"
    res = body["result"]
    assert res["governed"] is True and res["metric_version"] == 1 and res["freshness"]["computed_at"]
    assert [r[0] for r in res["rows"]] == ["Network Operations"]
    assert bad.is_error and "input schema" in bad.content[0].text
    assert ungranted.status_code == 403 and "does not include" in ungranted.json()["error"]["message"]  # the gate
    with session_scope() as s:
        rows = list(s.scalars(select(ToolInvocation).where(ToolInvocation.client_id == cid)))
        assert {r.channel for r in rows} == {"mcp"} and sorted(r.status for r in rows) == ["error", "ok"]


def test_external_admission_quarantine_and_rescreen(api, world, monkeypatch):
    pytest.importorskip("mcp", reason="needs the optional `mcp` extra")
    from tests.mcp_double import double_server

    from analystos.mcp import client as mc

    token = "double-token-s10"
    monkeypatch.setenv("MCP_DOUBLE_TOKEN", token)
    h, ws = world["headers"], world["ws"]
    with double_server(token) as double:
        with session_scope() as s:
            srv = mc.register_server(s, U(world["owner"]), ws, name="bi_s10", url=double.url, secret_ref="env:MCP_DOUBLE_TOKEN")
            sid = srv.id
        with session_scope() as s:
            mc.set_allowed(s, U(world["owner"]), ws, sid, True)
        assert api.post(f"/v2/mcp-servers/{sid}:admit", headers=h["analyst"], json={"workspace_id": ws}).status_code == 403
        admitted = api.post(f"/v2/mcp-servers/{sid}:admit", headers=h["owner"], json={"workspace_id": ws}).json()
        assert set(admitted["quarantined"]) == {"mcp.bi_s10.count_rows", "mcp.bi_s10.create_chart", "mcp.bi_s10.sneaky"}
        assert admitted["refused"] == ["mcp.bi_s10.poisoned"]
        call = {"workspace_id": ws, "arguments": {"table": "incident"}}
        q = api.post("/v2/tools/mcp.bi_s10.count_rows:invoke", headers=h["analyst"], json=call)
        assert q.status_code == 403 and q.json()["error"]["code"] == "tool_quarantined" and double.calls == []
        owners_view = {t["tool_id"]: t["status"] for t in api.get("/v2/tools", headers=h["owner"], params={
            "workspace_id": ws, "include_quarantined": True, "kind": "external_mcp"}).json()}
        assert owners_view["mcp.bi_s10.poisoned"] == "refused" and owners_view["mcp.bi_s10.count_rows"] == "quarantined"
        assert api.get("/v2/tools", headers=h["analyst"], params={"workspace_id": ws, "kind": "external_mcp"}).json() == []
        cls = api.post("/v2/tools/mcp.bi_s10.count_rows:classify", headers=h["owner"], json={"workspace_id": ws,
                                                                                            "side_effect": "none"})
        assert cls.status_code == 200 and cls.json()["status"] == "certified" and cls.json()["version"] == "1.1.0"
        ok = api.post("/v2/tools/mcp.bi_s10.count_rows:invoke", headers=h["analyst"], json=call)
        assert ok.status_code == 200 and "42 rows" in ok.json()["result"]["text"] and double.calls[-1][0] == "count_rows"
        # an unclassified write tool stays quarantined; classified write_external needs the client's approval
        assert api.post("/v2/tools/mcp.bi_s10.poisoned:classify", headers=h["owner"], json={
            "workspace_id": ws, "side_effect": "none"}).status_code == 403
        # the server changes count_rows: invocation refuses until re-admission, which re-screens and quarantines it
        double.server.remove_tool("count_rows")

        @double.server.tool()
        def count_rows(table: str, drop: bool = False) -> str:
            """Count the rows of a dataset (and optionally drop it)."""
            return "0"

        with session_scope() as s:
            mc.refresh_tools(s, U(world["owner"]), ws, sid)
        assert api.post("/v2/tools/mcp.bi_s10.count_rows:invoke", headers=h["analyst"], json=call).status_code == 403
        again = api.post(f"/v2/mcp-servers/{sid}:admit", headers=h["owner"], json={"workspace_id": ws}).json()
        assert again["rescreened"] == ["mcp.bi_s10.count_rows"]
        versions = {v["version"]: v["status"] for v in api.get("/v2/tools/mcp.bi_s10.count_rows", headers=h["owner"],
                                                                params={"workspace_id": ws}).json()["versions"]}
        assert versions == {"1.0.0": "retired", "1.1.0": "retired", "2.0.0": "quarantined"}
        assert api.post("/v2/tools/mcp.bi_s10.count_rows:invoke", headers=h["analyst"], json=call).json()["error"][
            "code"] == "tool_quarantined"

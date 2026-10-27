"""P4-X06 AnalystOS as an MCP server: conformance-style tests with the official SDK client against
the real API app (uvicorn on a free port, MCP mounted at /mcp), plus grants, quotas and audit."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from tests.mcp_double import serve

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AuditEvent, Insight, McpUsage, QueryExecution, Source, SourceAsset, SourceColumn, User
from analystos.mcp import grants as G
from analystos.security.auth import hash_password
from analystos.services.workspaces import create_workspace

pytestmark = pytest.mark.integration
pytest.importorskip("mcp", reason="needs the optional `mcp` extra")


@pytest.fixture(scope="module")
def api(control_db):
    from analystos.api.app import app

    with serve(app) as base:
        yield base


def _admin(s) -> User:
    u = User(id=new_id("usr"), email=f"adm-{new_id('x')}@t", name="adm", password_hash=hash_password("x"), is_admin=True)
    s.add(u)
    s.flush()
    return u


@pytest.fixture()
def world(api):
    with session_scope() as s:
        admin = _admin(s)
        ws = create_workspace(s, admin, name="MCP server test", objective="Explain SLA breaches", autonomy_level=3)
        other = create_workspace(s, admin, name="Other", objective="Something else entirely")
        s.flush()
        src = Source(id=new_id("src"), workspace_id=ws.id, kind="servicenow", name="SN", config={}, status="ready",
                     execution_mode="staged")
        s.add(src)
        s.flush()
        a = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name="src_t", name="incident",
                        source_name="incident", selected=True)
        s.add(a)
        s.flush()
        for i, (n, tags) in enumerate([("number", []), ("made_sla", []), ("salary", ["restricted"])]):
            s.add(SourceColumn(asset_id=a.id, name=n, ordinal=i, data_type="text", tags=tags))
        q = QueryExecution(id=new_id("qry"), workspace_id=ws.id, actor="agent:x", sql="select count(*) from src_t.incident",
                           status="ok", row_count=1)
        s.add(q)
        ins = Insight(id=new_id("ins"), workspace_id=ws.id, run_id=new_id("run"), code="I1", title="P1 breaches SLA",
                      finding="P1 incidents breach SLA 3x more often", confidence=0.9, verified=True, status="verified",
                      evidence=[{"type": "query", "id": q.id, "label": "breach rate"}], verification={"method": "chi2"})
        s.add(ins)
        client, token = G.create_client(s, admin, "claude-desktop")
        G.set_grant(s, admin, client.id, ws.id, role="analyst", tools=["validate_sql", "get_finding_evidence", "ask"],
                    quotas={"validate_sql": 3})
        bare, bare_token = G.create_client(s, admin, "no-grants")
        ids = dict(ws=ws.id, other=other.id, admin=admin.id, client=client.id, token=token, insight=ins.id, query=q.id,
                   bare=bare.id, bare_token=bare_token, service_user=client.service_user_id)
    return ids


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _with_client(url: str, token: str, fn, mode: str = "auto") -> Any:
    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with (httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                Client(streamable_http_client(f"{url}/mcp", http_client=http), mode=mode, cache=None) as c):
        return await fn(c)


def _rpc(api: str, token: str | None, method: str, params: dict | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(f"{api}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                      headers=headers, timeout=20)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_conformance_initialize_list_read_call(api, world, mode):
    async def go(c):
        info = c.server_info
        tools = (await c.list_tools()).tools
        resources = (await c.list_resources()).resources
        finding_uri = f"analystos://{world['ws']}/findings/{world['insight']}"
        doc = await c.read_resource(finding_uri)
        ok = await c.call_tool("validate_sql", {"workspace_id": world["ws"], "sql": "select number from src_t.incident"})
        denied = await c.call_tool("validate_sql", {"sql": "select salary from src_t.incident"})
        ev = await c.call_tool("get_finding_evidence", {"insight_id": world["insight"]})
        return info, tools, resources, doc, ok, denied, ev, c.protocol_version

    info, tools, resources, doc, ok, denied, ev, version = _run(lambda: _with_client(api, world["token"], go, mode))
    assert info.name == "analystos"
    assert version == ("2026-07-28" if mode == "auto" else version) and version
    assert {t.name for t in tools} == {"validate_sql", "get_finding_evidence", "ask"}  # only granted tools
    assert all(t.input_schema["type"] == "object" for t in tools)
    uris = {r.uri for r in resources}
    assert f"analystos://{world['ws']}/findings/{world['insight']}" in uris
    assert not any(world["other"] in u for u in uris)
    assert '"P1 breaches SLA"' in doc.contents[0].text
    assert ok.structured_content["accepted"] is True and ok.structured_content["executed"] is False
    assert denied.structured_content["accepted"] is False  # the service identity's scope denies restricted columns
    assert ev.structured_content["queries"][0]["id"] == world["query"]


def test_ask_runs_through_the_ask_path_as_the_service_identity(api, world, monkeypatch):
    """No model call: the SQL agent's model step is faked; the scope and gateway call are real code paths."""
    from analystos.agents import sql_agent
    from analystos.gateway.validator import validate_sql
    from analystos.runtime import context

    seen: dict[str, Any] = {}

    class Gateway:
        def execute(self, scope, sql, **kw):
            seen.update(scope=scope, sql=sql, actor=kw.get("actor"))
            validate_sql(scope, sql, max_rows=scope.max_rows)

            class R:
                query_id, columns, rows, row_count, truncated = "qry_fake", ["number"], [["INC1"]], 1, False
            return R()

    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: ({"sql": "select number from src_t.incident"}, "fake"))
    monkeypatch.setattr(context, "default_services", lambda: context.Services(router=None, gateway=Gateway()))

    async def go(c):
        return await c.call_tool("ask", {"question": "how many incidents?"})

    out = _run(lambda: _with_client(api, world["token"], go))
    assert not out.is_error, out.content
    assert out.structured_content["result"]["rows"] == [["INC1"]]
    assert seen["scope"].user_id == world["service_user"] and seen["scope"].role == "analyst"
    # Ask is audited as the calling user (P4-C02 per-user Ask budget); over MCP that is the client's service identity.
    assert "src_t.incident.salary" in seen["scope"].denied_columns and seen["actor"] == f"user:{world['service_user']}"


def test_unauthenticated_and_revoked_clients_get_401(api, world):
    assert _rpc(api, None, "tools/list").status_code == 401
    assert _rpc(api, f"{world['client']}.wrong-secret", "tools/list").status_code == 401
    with session_scope() as s:
        G.revoke_client(s, s.get(User, world["admin"]), world["client"])
    r = _rpc(api, world["token"], "tools/list")
    assert r.status_code == 401
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:  # a failed credential has no tenant: platform-private rows
        fails = s.scalars(select(AuditEvent).where(AuditEvent.action == "mcp.auth_failed",
                                                   AuditEvent.actor == f"mcp_client:{world['client']}")).all()
    assert len(fails) >= 2


def test_client_without_a_grant_is_refused(api, world):
    # no grant at all: even the handshake is refused
    assert _rpc(api, world["bare_token"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "t", "version": "1"}}).status_code == 403
    # a grant for another workspace does not cover this one
    r = _rpc(api, world["token"], "tools/call", {"name": "validate_sql",
                                                 "arguments": {"workspace_id": world["other"], "sql": "select 1"}})
    assert r.status_code == 403 and r.json()["error"]["data"]["code"] == "forbidden"
    # a tool outside the grant
    r = _rpc(api, world["token"], "tools/call", {"name": "investigate", "arguments": {"objective": "x" * 20}})
    assert r.status_code == 403
    r = _rpc(api, world["token"], "resources/read", {"uri": f"analystos://{world['other']}/findings/x"})
    assert r.status_code == 403

    async def go(c):
        return await c.call_tool("investigate", {"objective": "x" * 20})

    from mcp.shared.exceptions import MCPError

    with pytest.raises(BaseExceptionGroup) as err:  # the SDK surfaces the refusal as an MCP error, not a result
        _run(lambda: _with_client(api, world["token"], go))
    leaf = err.value
    while isinstance(leaf, BaseExceptionGroup):
        leaf = leaf.exceptions[0]
    assert isinstance(leaf, MCPError) and "does not include tool investigate" in leaf.message
    with session_scope() as s:
        denials = s.scalars(select(AuditEvent).where(AuditEvent.action == "mcp.request", AuditEvent.decision == "deny",
                                                     AuditEvent.actor.in_([f"mcp_client:{world['client']}",
                                                                           f"mcp_client:{world['bare']}"]))).all()
    assert len(denials) >= 4


def test_quota_exceeded_returns_429(api, world):
    async def go(c):
        return [await c.call_tool("validate_sql", {"sql": "select number from src_t.incident"}) for _ in range(3)]

    results = _run(lambda: _with_client(api, world["token"], go))
    assert all(r.structured_content["accepted"] for r in results)
    r = _rpc(api, world["token"], "tools/call", {"name": "validate_sql", "arguments": {"sql": "select 1"}})
    assert r.status_code == 429
    assert r.json()["error"]["data"]["code"] == "quota_exceeded"
    # other tools keep their own quota
    r2 = _rpc(api, world["token"], "tools/call", {"name": "get_finding_evidence", "arguments": {"insight_id": world["insight"]}})
    assert r2.status_code == 200
    with session_scope() as s:
        usage = s.scalar(select(McpUsage).where(McpUsage.client_id == world["client"], McpUsage.tool == "validate_sql"))
        assert usage.count == 3  # the refused call is not counted
        audits = s.scalars(select(AuditEvent).where(AuditEvent.actor == f"mcp_client:{world['client']}")).all()
    actions = {(a.action, a.decision) for a in audits}
    assert ("mcp.request", "allow") in actions and ("mcp.request", "deny") in actions
    assert ("mcp.tool_result", "allow") in actions
    assert any(a.reasons == ["quota_exceeded"] for a in audits)


def test_grants_are_capped_at_analyst_and_secrets_are_hashed(world):
    from analystos.core.errors import InvalidInput

    with session_scope() as s:
        admin = s.get(User, world["admin"])
        with pytest.raises(InvalidInput):
            G.set_grant(s, admin, world["bare"], world["ws"], role="editor")
        with pytest.raises(InvalidInput):
            G.set_grant(s, admin, world["bare"], world["ws"], role="viewer", tools=["ask"])
        client = s.get(G.McpClient, world["client"])
        secret = world["token"].split(".", 1)[1]
        assert client.secret_hash == G.hash_secret(secret) and secret not in client.secret_hash
        svc = s.get(User, client.service_user_id)
        assert not svc.is_admin and svc.password_hash == "!"


def test_admin_endpoints_create_grant_and_revoke(api, world):
    from analystos.security.auth import issue_token

    with session_scope() as s:
        admin = s.get(User, world["admin"])
        h = {"Authorization": f"Bearer {issue_token(admin.id, admin.email)}"}
    r = httpx.post(f"{api}/api/admin/mcp/clients", json={"name": "ci-bot"}, headers=h)
    assert r.status_code == 201
    cid, token = r.json()["client_id"], r.json()["token"]
    assert token.startswith(cid + ".")
    r = httpx.put(f"{api}/api/admin/mcp/clients/{cid}/grants/{world['ws']}",
                  json={"role": "viewer", "tools": ["get_finding_evidence"], "quotas": {"get_finding_evidence": 5}}, headers=h)
    assert r.status_code == 200
    listed = {c["client_id"]: c for c in httpx.get(f"{api}/api/admin/mcp/clients", headers=h).json()}
    assert listed[cid]["grants"][0]["tools"] == ["get_finding_evidence"] and "token" not in listed[cid]
    assert _rpc(api, token, "tools/call", {"name": "get_finding_evidence",
                                           "arguments": {"insight_id": world["insight"]}}).status_code == 200
    assert httpx.delete(f"{api}/api/admin/mcp/clients/{cid}/grants/{world['ws']}", headers=h).status_code == 204
    assert _rpc(api, token, "tools/list").status_code == 403
    assert httpx.post(f"{api}/api/admin/mcp/clients/{cid}/revoke", headers=h).status_code == 200
    assert _rpc(api, token, "tools/list").status_code == 401

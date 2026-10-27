"""Spec S04 on Postgres with row-level security (strict tenancy): the v2 conversation API over real Ask turns
(staged ServiceNow data, governed gateway, fake model transport), branches restoring state, AT-18 (a team
correction applies to another user only after the owner approves it in the knowledge review), summaries whose
numbers cite turns, deletion cascade and legal hold, retention, MCP conversation tools, and tenant isolation."""
from __future__ import annotations

import os
import socket
import threading
import time
from datetime import timedelta

import pytest
import uvicorn
from sqlalchemy import func, select, text

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servicenow_url():
    from analystos.connectors.servicenow_mock import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


class Transport:
    """sql_generation answers with the SQL mapped to a question found in the prompt; decisions abstain."""

    def __init__(self):
        self.sql_for: dict[str, str] = {}
        self.prompts: list[str] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        from tests.fakes import chat_json

        prompt = " ".join(str(m.get("content")) for m in payload.get("messages") or [])
        self.prompts.append(prompt)
        for question in sorted(self.sql_for, key=len, reverse=True):
            if question in prompt:
                return chat_json({"sql": self.sql_for[question], "explanation": "Reference query.", "chart": {"type": "table"}})
        return chat_json({"sql": "SELECT 1 AS n", "explanation": "?"})

    def decide(self, *, base_url, api_key, payload, timeout):
        return {"answers": {}, "usage": {}}


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.runtime.context import default_router
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "SERVICENOW_PASSWORD")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["SERVICENOW_PASSWORD"] = "admin"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="conversations", objective="Answer questions about IT incidents")
        s.flush()
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        add_member(s, admin, ws.id, "approver@analystos.local", "analyst")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:SERVICENOW_PASSWORD")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws_id, SourceAsset.name == "incident"))
        table = f"{asset.schema_name}.{asset.name}"
    yield {"ws": ws_id, "table": table, "admin": admin}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


@pytest.fixture(scope="module")
def transport(world):
    t = Transport()
    table = world["table"]
    t.sql_for = {
        "How many incidents are there in total?": f"SELECT COUNT(*) AS n FROM {table}",
        "Count of incidents by priority": f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY priority",
        "Number of incidents by state": f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state",
        "Total count of distinct incident states": f"SELECT COUNT(DISTINCT state) AS n FROM {table}",
        "Average priority across all incidents": f"SELECT AVG(priority) AS p FROM {table}",
    }
    return t


@pytest.fixture(scope="module")
def api(world, transport):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.llm.router import ModelRouter
    from analystos.runtime import context as runtime_context
    from analystos.runtime.context import Services, default_gateway
    from analystos.runtime.usage import DbUsageSink

    settings = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    router = ModelRouter(transport=transport, api_key_lookup=lambda env: "k", sink=DbUsageSink(), settings_provider=lambda: settings)
    services = Services(router=router, gateway=default_gateway())
    mp = pytest.MonkeyPatch()
    mp.setattr(runtime_context, "default_services", lambda: services)
    with TestClient(app) as client:
        yield client
    mp.undo()


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _new(api, h, ws, title="t") -> dict:
    r = api.post("/api/v2/conversations", headers=h, json={"workspace_id": ws, "title": title})
    assert r.status_code == 201, r.text
    return r.json()


def _say(api, h, cid, text_, **kw) -> dict:
    r = api.post(f"/api/v2/conversations/{cid}/messages", headers=h, json={"text": text_, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _reference(sql: str):
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"])
    try:
        with engine.connect() as c:
            return c.execute(text(sql)).all()
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------------ T1, T2
def test_v2_conversation_messages_state_and_branch(api, world):
    a, b = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    conv = _new(api, a, world["ws"], "incidents")
    assert conv["channel"] == "api" and conv["tenant_id"] and conv["state_version"] == 0
    m1 = _say(api, a, conv["id"], "How many incidents are there in total?")["message"]
    assert m1["status"] == "answered" and m1["seq"] == 1, m1
    total = _reference(f"SELECT COUNT(*) FROM {world['table']}")[0][0]
    assert m1["answer"]["result"]["rows"][0][0] == total
    m2 = _say(api, a, conv["id"], "Count of incidents by priority")["message"]
    assert m2["status"] == "answered" and m2["seq"] == 2
    r = api.post(f"/api/v2/conversations/{conv['id']}/corrections", headers=a,
                 json={"term": "open", "meaning": "state is not Closed", "scope": "session"})
    assert r.status_code == 201, r.text
    state = api.get(f"/api/v2/conversations/{conv['id']}/state", headers=a)
    assert state.headers["ETag"] == '"3"' and [h["seq"] for h in state.json()["state"]["result_handles"]] == [1, 2]
    at_m1 = api.get(f"/api/v2/conversations/{conv['id']}/state?version=1", headers=a).json()

    r = api.post(f"/api/v2/conversations/{conv['id']}/branch", headers=a, json={"from_message_id": m1["id"]})
    assert r.status_code == 201, r.text
    branch = r.json()
    assert branch["restored_state"]["version"] == 1
    restored = api.get(f"/api/v2/conversations/{branch['id']}/state", headers=a).json()
    assert restored["state"] == at_m1["state"] and restored["cause"] == "branch"
    m3 = _say(api, a, branch["id"], "Number of incidents by state")["message"]
    assert m3["seq"] == 2 and m3["conversation_id"] == branch["id"]
    detail = api.get(f"/api/v2/conversations/{branch['id']}", headers=a).json()
    assert [(m["id"], m["inherited"]) for m in detail["messages"]] == [(m1["id"], True), (m3["id"], False)]
    edited = _say(api, a, conv["id"], "Total count of distinct incident states", mode="edit", edit_message_id=m2["id"])
    assert edited["conversation"]["parent_conversation_id"] == conv["id"] and edited["message"]["seq"] == 2

    mine = api.get(f"/api/v2/conversations?mine=true&workspace_id={world['ws']}", headers=a).json()
    assert {conv["id"], branch["id"], edited["conversation"]["id"]} <= {c["id"] for c in mine}
    assert api.get("/api/v2/conversations?mine=false", headers=a).status_code == 422
    for path in ("", "/state", "/summary", "/memory"):  # another member of the same workspace: 404, never 403
        assert api.get(f"/api/v2/conversations/{conv['id']}{path}", headers=b).status_code == 404
    assert api.post(f"/api/v2/conversations/{conv['id']}/messages", headers=b, json={"text": "x"}).status_code == 404


# ------------------------------------------------------------------------------------ AT-18
def test_at18_team_correction_applies_to_others_only_after_owner_approval(api, world, transport):
    a, b = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    owner = _login(api, "admin@analystos.local")
    meaning = "an incident whose state is not Closed"
    a1 = _new(api, a, world["ws"], "A one")
    r = api.post(f"/api/v2/conversations/{a1['id']}/corrections", headers=a,
                 json={"term": "active incident", "meaning": meaning, "scope": "workspace_proposed"})
    assert r.status_code == 201, r.text
    corr = r.json()["correction"]
    assert corr["status"] == "proposed" and corr["suggestion_id"]

    def stages(msg):
        return [s for s in msg["answer"]["stages"] if s["key"] == "memory"]

    transport.prompts.clear()
    mine = _say(api, a, a1["id"], "Average priority across all incidents")["message"]
    assert stages(mine), "the author's own session applies the correction at once"
    generated = [p for p in transport.prompts if "Average priority across all incidents" in p]
    assert all(meaning in p for p in generated)  # when the model writes the SQL, it sees the clarification as data
    a2 = _new(api, a, world["ws"], "A two")  # the author's next conversation: not yet (session scope only)
    assert api.get(f"/api/v2/conversations/{a2['id']}/memory", headers=a).json()["items"] == []
    b1 = _new(api, b, world["ws"], "B one")
    transport.prompts.clear()
    before = _say(api, b, b1["id"], "Total count of distinct incident states")["message"]
    assert not stages(before) and not any(meaning in p for p in transport.prompts)

    queue = api.get(f"/api/workspaces/{world['ws']}/knowledge/suggestions?origin=conversation.correction",
                    headers=owner).json()
    assert [q["id"] for q in queue] == [corr["suggestion_id"]]
    assert api.post(f"/api/workspaces/{world['ws']}/knowledge/suggestions/review", headers=b,
                    json={"decisions": [{"id": corr["suggestion_id"], "action": "approve"}]}).status_code == 403
    r = api.post(f"/api/workspaces/{world['ws']}/knowledge/suggestions/review", headers=owner,
                 json={"decisions": [{"id": corr["suggestion_id"], "action": "approve"}]})
    assert r.status_code == 200 and r.json()["approved"], r.text

    mem = api.get(f"/api/v2/conversations/{b1['id']}/memory", headers=b).json()
    assert [(i["term"], i["meaning"], i["scope"]) for i in mem["items"]] == [("active incident", meaning, "workspace")]
    transport.prompts.clear()
    after = _say(api, b, b1["id"], "Number of incidents by state")["message"]
    assert stages(after) and "team definition" in stages(after)[0]["text"]
    assert all(meaning in p for p in transport.prompts if "Number of incidents by state" in p)


# ------------------------------------------------------------------------------------ T3
def test_summary_numbers_cite_turns_and_land_in_the_ledger(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import AskTurn
    from analystos.tenancy import DEFAULT_TENANT_ID

    a = _login(api, "analyst@analystos.local")
    conv = _new(api, a, world["ws"], "summary")
    _say(api, a, conv["id"], "How many incidents are there in total?")
    _say(api, a, conv["id"], "Average priority across all incidents")
    assert api.get(f"/api/v2/conversations/{conv['id']}/summary", headers=a).status_code == 404
    r = api.post(f"/api/v2/conversations/{conv['id']}/close", headers=a)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["trigger"] == "close" and out["trust"] == "personal" and len(out["summary"]["answers"]) == 2
    from analystos.conversations.summaries import check_claim

    with session_scope() as s:
        for ans in out["summary"]["answers"]:
            turn = s.get(AskTurn, ans["evidence_ref"]["message_id"])
            assert turn.thread_id == conv["id"] and check_claim(ans["claim"], turn)["ok"], ans
        row = s.execute(text("SELECT tenant_id, trust, owner_id, kind FROM context_ledger_entry "
                             "WHERE ref_table = 'conversation_summary' AND ref_id = :r AND valid_to IS NULL"),
                        {"r": out["id"]}).one()
    assert row[0] == DEFAULT_TENANT_ID and row[1] == "personal" and row[3] == "conversation_summary"
    got = api.get(f"/api/v2/conversations/{conv['id']}/summary", headers=a).json()
    assert got["version"] == 1 and got["stale"] is False


# ------------------------------------------------------------------------------------ T5
def test_delete_cascade_legal_hold_and_retention(api, world):
    from analystos.conversations import retention
    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import AskThread, AskTurn, AuditEvent

    a, admin = _login(api, "analyst@analystos.local"), _login(api, "admin@analystos.local")
    conv = _new(api, a, world["ws"], "to delete")
    _say(api, a, conv["id"], "How many incidents are there in total?")
    api.post(f"/api/v2/conversations/{conv['id']}/corrections", headers=a,
             json={"term": "region", "meaning": "billing region", "scope": "personal"})
    api.post(f"/api/v2/conversations/{conv['id']}/summary", headers=a)
    assert api.put(f"/api/v2/conversations/{conv['id']}/legal-hold", headers=a,
                   json={"hold": True, "reason": "x"}).status_code == 403
    assert api.put(f"/api/v2/conversations/{conv['id']}/legal-hold", headers=admin,
                   json={"hold": True, "reason": "matter 7"}).json()["legal_hold"] is True
    assert api.delete(f"/api/v2/conversations/{conv['id']}", headers=a).status_code == 409
    api.put(f"/api/v2/conversations/{conv['id']}/legal-hold", headers=admin, json={"hold": False})
    with session_scope() as s:  # the conversation's projections in the S03 ledger (every version)
        projected = list(s.execute(text(
            "SELECT e.id FROM context_ledger_entry e WHERE (e.ref_table = 'conversation_summary' AND e.ref_id IN "
            "(SELECT id FROM conversation_summary WHERE conversation_id = :c)) OR (e.ref_table = 'conversation_correction' "
            "AND e.ref_id IN (SELECT id FROM conversation_correction WHERE conversation_id = :c))"),
            {"c": conv["id"]}).scalars())
    assert len(projected) == 2
    r = api.delete(f"/api/v2/conversations/{conv['id']}?reason=user%20request", headers=a)
    assert r.status_code == 200, r.text
    assert r.json()["messages"] == 1 and r.json()["embeddings"] == 2
    with session_scope() as s:
        for table in ("conversation_state", "conversation_summary", "conversation_correction"):
            n = s.execute(text(f"SELECT COUNT(*) FROM {table} WHERE conversation_id = :c"), {"c": conv["id"]}).scalar()
            assert n == 0, table
        for table in ("context_ledger_entry", "context_ledger_change"):  # projection, embedding and feed rows erased
            col = "id" if table == "context_ledger_entry" else "entry_id"
            n = s.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {col} = ANY(:ids)"), {"ids": projected}).scalar()
            assert n == 0, table
        assert s.get(AskThread, conv["id"]) is None
        tomb = s.scalar(select(AuditEvent).where(AuditEvent.action == "conversation.deleted",
                                                 AuditEvent.target == conv["id"]))
        assert tomb is not None and tomb.details["tombstone"] and "incidents" not in str(tomb.details)

    held, free = _new(api, a, world["ws"], "held"), _new(api, a, world["ws"], "free")
    hm = _say(api, a, held["id"], "How many incidents are there in total?")["message"]
    fm = _say(api, a, free["id"], "How many incidents are there in total?")["message"]
    api.put(f"/api/v2/conversations/{held['id']}/legal-hold", headers=admin, json={"hold": True, "reason": "matter 8"})
    with session_scope() as s:
        for mid in (hm["id"], fm["id"]):
            s.get(AskTurn, mid).created_at = utcnow() - timedelta(days=400)
    retention.apply_retention()  # the platform loop, each tenant in its own binding
    with session_scope() as s:
        assert s.get(AskTurn, fm["id"]) is None and s.get(AskTurn, hm["id"]) is not None

    me = api.get("/api/tenants/me", headers=a).json()["current"]["id"]
    assert api.put(f"/api/v2/tenants/{me}/conversation-retention", headers=a, json={"messages_days": 90}).status_code == 403
    r = api.put(f"/api/v2/tenants/{me}/conversation-retention", headers=admin, json={"messages_days": 90})
    assert r.status_code == 200 and r.json()["messages_days"] == 90
    assert api.get(f"/api/v2/tenants/{me}/conversation-retention", headers=a).json()["messages_days"] == 90
    assert api.get("/api/v2/tenants/tnt_other/conversation-retention", headers=a).status_code == 404


# ------------------------------------------------------------------------------------ T6
def test_mcp_conversation_tools_create_desk_requests_for_the_participant(api, world):
    from analystos.core.errors import InvalidInput, NotFound
    from analystos.db.base import session_scope
    from analystos.db.models import AskThread, User
    from analystos.mcp import grants as G
    from analystos.mcp.server import TOOL_IMPLS
    from analystos.tenancy import system_scope

    tools = ["conversation.start", "conversation.say", "conversation.summary"]
    with session_scope() as s:
        admin = s.get(User, world["admin"].id)
        client, token = G.create_client(s, admin, "conversation-bot")
        G.set_grant(s, admin, client.id, world["ws"], role="analyst", tools=tools)
        other, other_token = G.create_client(s, admin, "other-bot")
        G.set_grant(s, admin, other.id, world["ws"], role="analyst", tools=tools)
        viewer, _ = G.create_client(s, admin, "viewer-bot")
        with pytest.raises(InvalidInput):
            G.set_grant(s, admin, viewer.id, world["ws"], role="viewer", tools=["conversation.start"])
    with system_scope(), session_scope() as s:
        principal, intruder = G.authenticate(s, token), G.authenticate(s, other_token)
    G.check_grant(principal, world["ws"], "conversation.start")
    started = TOOL_IMPLS["conversation.start"](principal, world["ws"], {"text": "How many incidents are there in total?"})
    cid = started["conversation_id"]
    assert started["request"]["state"] == "auto_answered" and started["request"]["conversation_id"] == cid
    assert started["message"]["status"] == "answered" and started["message"]["seq"] == 1
    said = TOOL_IMPLS["conversation.say"](principal, world["ws"], {"conversation_id": cid,
                                                                     "text": "Count of incidents by priority"})
    assert said["message"]["seq"] == 2 and said["request"]["conversation_id"] == cid
    with session_scope() as s:
        conv = s.get(AskThread, cid)
        assert conv.channel == "mcp" and conv.request_id == started["request"]["id"]
    summary = TOOL_IMPLS["conversation.summary"](principal, world["ws"], {"conversation_id": cid})
    assert [a["evidence_ref"]["seq"] for a in summary["summary"]["answers"]] == [1, 2]
    for tool, args in (("conversation.say", {"conversation_id": cid, "text": "And by state?"}),
                       ("conversation.summary", {"conversation_id": cid})):
        with pytest.raises(NotFound):  # another client of the same workspace is not a participant
            TOOL_IMPLS[tool](intruder, world["ws"], args)


# ------------------------------------------------------------------------------------ tenancy
def test_conversations_are_isolated_across_tenants(api, world):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, User, Workspace, WorkspaceMember
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope, tenant_scope

    a = _login(api, "analyst@analystos.local")
    conv = _new(api, a, world["ws"], "tenant A only")
    _say(api, a, conv["id"], "How many incidents are there in total?")
    api.post(f"/api/v2/conversations/{conv['id']}/corrections", headers=a,
             json={"term": "open", "meaning": "not Closed", "scope": "personal"})
    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"conv-{tb[-6:]}", name="Tenant B"))
    with tenant_scope(tb), session_scope() as s:
        user = User(id=new_id("usr"), email=f"admin-{tb}@b.test", name="B admin", password_hash=hash_password(PASSWORD),
                    is_admin=True)  # even a platform admin of tenant B: RLS alone keeps tenant A's rows out
        s.add(user)
        s.flush()
        ws_b = Workspace(id=new_id("ws"), name="B", created_by=user.id)
        s.add(ws_b)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws_b.id, user_id=user.id, role="owner"))
        email = user.email
    b = _login(api, email)
    for method, path in (("get", ""), ("get", "/state"), ("get", "/memory"), ("delete", ""),
                         ("put", "/legal-hold")):
        kw = {"json": {"hold": True, "reason": "x"}} if method == "put" else {}
        assert getattr(api, method)(f"/api/v2/conversations/{conv['id']}{path}", headers=b, **kw).status_code == 404
    assert api.get("/api/v2/conversations?mine=true", headers=b).json() == []
    with tenant_scope(tb), session_scope() as s:
        for table in ("ask_thread", "conversation_state", "conversation_correction", "conversation_summary"):
            assert s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == 0, table
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Workspace)) >= 1
        assert s.execute(text("SELECT COUNT(*) FROM conversation_state WHERE conversation_id = :c"),
                         {"c": conv["id"]}).scalar() >= 1


# ------------------------------------------------------------------------------------ S03 ledger
def test_summary_is_searchable_through_the_ledger_only_by_allowed_callers(api, world):
    """A conversation summary is an S03 ledger kind: search, get and MCP `context.search` find it for its owner (and,
    once the workspace shares summaries, for other members), never for another workspace or tenant."""
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, User, Workspace, WorkspaceMember
    from analystos.governance.policy import get_workspace, load_policy, save_policy
    from analystos.mcp import grants as G
    from analystos.mcp.server import TOOL_IMPLS
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope, tenant_scope

    a, b = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    admin = _login(api, "admin@analystos.local")
    title = "Ledger drill on incident volume"
    conv = _new(api, a, world["ws"], title)
    _say(api, a, conv["id"], "How many incidents are there in total?")
    first = api.post(f"/api/v2/conversations/{conv['id']}/summary", headers=a).json()
    assert first["trust"] == "personal"

    def search(h, ws=world["ws"]):
        return api.get("/api/v2/context/search", headers=h,
                       params={"workspace": ws, "q": title, "kinds": "conversation_summary"})

    def titles(r):
        return [h["title"] for h in r.json()]

    hits = [h for h in search(a).json() if h["title"] == title]  # the owner may have other summaries too
    assert len(hits) == 1 and hits[0]["trust"] == "personal"
    entry = api.get(f"/api/v2/context/entries/{hits[0]['id']}", headers=a).json()
    assert entry["ref"] == {"table": "conversation_summary", "id": first["id"]}
    assert [x["evidence_ref"]["seq"] for x in entry["body"]["answers"]] == [1]
    assert title not in titles(search(b))  # same workspace, not the owner: personal stays private
    assert api.get(f"/api/v2/context/entries/{hits[0]['id']}", headers=b).status_code == 404

    with session_scope() as s:  # another workspace of the same tenant
        me = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        other = Workspace(id=new_id("ws"), name="other ledger ws", created_by=me.id)
        s.add(other)
        s.flush()
        s.add(WorkspaceMember(workspace_id=other.id, user_id=me.id, role="owner"))
        other_id = other.id
    assert title not in titles(search(admin, other_id))
    assert search(a, other_id).status_code == 404  # not a member there

    tb = new_id("tnt")  # another tenant: even its platform admin finds nothing and cannot open the entry
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"ledger-{tb[-6:]}", name="Tenant C"))
    with tenant_scope(tb), session_scope() as s:
        u = User(id=new_id("usr"), email=f"admin-{tb}@c.test", name="C admin", password_hash=hash_password(PASSWORD),
                 is_admin=True)
        s.add(u)
        email = u.email
    c = _login(api, email)
    assert search(c).status_code == 404 and api.get(f"/api/v2/context/entries/{hits[0]['id']}", headers=c).status_code == 404

    with session_scope() as s:  # an MCP client of the workspace: a personal summary is not its to see
        me = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        client, token = G.create_client(s, me, "ledger-reader")
        G.set_grant(s, me, client.id, world["ws"], role="viewer", tools=["context.search"])
    with system_scope(), session_scope() as s:
        principal = G.authenticate(s, token)

    def mcp_titles():
        out = TOOL_IMPLS["context.search"](principal, world["ws"], {"q": title, "kinds": ["conversation_summary"]})
        return [r["title"] for r in out["results"]]

    assert title not in mcp_titles()
    with session_scope() as s:  # the workspace now shares conversation summaries
        ws = get_workspace(s, world["ws"])
        doc = load_policy(s, ws).model_dump()
        before = WorkspacePolicyDoc.model_validate(doc)
        save_policy(s, ws, WorkspacePolicyDoc.model_validate({**doc, "share_conversation_summaries": True}), me.id)
    try:
        shared = api.post(f"/api/v2/conversations/{conv['id']}/summary", headers=a).json()
        assert shared["trust"] == "workspace"
        assert (title, "workspace") in [(h["title"], h["trust"]) for h in search(b).json()]
        assert title in mcp_titles()
        assert title not in titles(search(admin, other_id)) and search(c).status_code == 404
        reopened = api.get(f"/api/v2/context/entries/{hits[0]['id']}", headers=a).json()
        assert reopened["current"] is False  # the first summary's projection was retired by the newer one
    finally:
        with session_scope() as s:
            save_policy(s, get_workspace(s, world["ws"]), before, me.id)

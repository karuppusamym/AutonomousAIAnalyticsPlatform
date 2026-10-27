"""Spec S13 on Postgres with row-level security: AT-19 (20 seeded R1/R2 requests over staged ServiceNow data: at
least 18 auto-answered with the numbers of an independent reference query, the rest escalated, none answered
wrongly), dedupe through the API, the escalation path (data pack, answer, accept, value metrics) and a 404 for
another tenant. The model transport is a fake that writes the SQL; the gateway, scope and data are real."""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import create_engine, select, text

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
BAD_SQL = "SELECT COUNT(*) AS n FROM secret.payroll"


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
    """sql_generation / sql_repair answer with the SQL mapped to the question in the prompt; decisions abstain."""

    def __init__(self):
        self.sql_for: dict[str, str] = {}
        self.chat_calls: list[dict] = []
        self.decide_calls: list[dict] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        from tests.fakes import chat_json

        self.chat_calls.append(payload)
        prompt = " ".join(str(m.get("content")) for m in payload.get("messages") or [])
        for question in sorted(self.sql_for, key=len, reverse=True):
            if question in prompt:
                return chat_json({"sql": self.sql_for[question], "explanation": "Reference query.", "chart": {"type": "table"}})
        return chat_json({"sql": BAD_SQL, "explanation": "?"})

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls.append(payload)
        return {"answers": {}, "usage": {}}


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.runtime.context import default_router
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANALYSTOS_SOURCE_SECRET_SERVICENOW")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="request desk", objective="Answer the IT operations team's data requests")
        s.flush()
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        add_member(s, admin, ws.id, "approver@analystos.local", "viewer")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws_id, SourceAsset.name == "incident"))
        table = f"{asset.schema_name}.{asset.name}"
    yield {"ws": ws_id, "table": table}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


@pytest.fixture(scope="module")
def transport():
    return Transport()


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


def _reference(sql: str) -> list[tuple]:
    """The reference numbers, read straight from the analytics plane (not through the Ask path)."""
    engine = create_engine(os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"])
    try:
        with engine.connect() as c:
            return sorted(tuple(_num(v) for v in r) for r in c.execute(text(sql)).all())
    finally:
        engine.dispose()


def _num(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v


def _seed_questions(table: str) -> tuple[dict[str, str], list[str]]:
    """18 answerable R1/R2 requests with reference SQL, and 2 the data in scope cannot answer."""
    prios = [r[0] for r in _reference(f"SELECT DISTINCT priority FROM {table}")][:5]
    qa = {
        "How many incidents are there in total?": f"SELECT COUNT(*) AS n FROM {table}",
        "Count of incidents by priority": f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY priority",
        "Number of incidents by state": f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state",
        "Number of incidents per priority and state": f"SELECT priority, state, COUNT(*) AS n FROM {table} GROUP BY priority, state",
        "Highest priority value among incidents": f"SELECT MAX(priority) AS p FROM {table}",
        "Lowest priority value among incidents": f"SELECT MIN(priority) AS p FROM {table}",
        "Average priority of incidents by state": f"SELECT state, AVG(priority) AS p FROM {table} GROUP BY state",
        "Total count of distinct incident states": f"SELECT COUNT(DISTINCT state) AS n FROM {table}",
        "Total count of distinct incident priorities": f"SELECT COUNT(DISTINCT priority) AS n FROM {table}",
        "Average priority across all incidents": f"SELECT AVG(priority) AS p FROM {table}",
    }
    for p in prios:
        qa[f"How many incidents have priority {int(p)} exactly?"] = f"SELECT COUNT(*) AS n FROM {table} WHERE priority = {int(p)}"
        qa[f"Number of incidents above priority {int(p)} by state"] = \
            f"SELECT state, COUNT(*) AS n FROM {table} WHERE priority > {int(p)} GROUP BY state"
    answerable = dict(list(qa.items())[:18])
    assert len(answerable) == 18, "the staged data needs at least four priorities"
    return answerable, ["How many payroll runs by department this year?", "Total budget lines by cost centre last month"]


def test_at19_desk_auto_resolves_seeded_r1_r2_requests(api, world, transport):
    from analystos.db.base import session_scope
    from analystos.db.models import AskTurn

    headers = _login(api, "analyst@analystos.local")
    answerable, unanswerable = _seed_questions(world["table"])
    transport.sql_for = dict(answerable)
    outcomes = {}
    for question in [*answerable, *unanswerable]:
        r = api.post("/api/requests?wait=true", headers=headers, json={"workspace_id": world["ws"], "text": question, "channel": "api"})
        assert r.status_code == 201, r.text
        req = r.json()["request"]
        assert req["class"] in ("R1", "R2") and req["triage"]["answered_by"] == "rules", (question, req["triage"])
        outcomes[question] = req
    auto = {q: r for q, r in outcomes.items() if r["state"] == "auto_answered"}
    escalated = {q: r for q, r in outcomes.items() if r["state"] == "escalated"}
    assert len(auto) >= 18 and len(auto) + len(escalated) == 20, {q: r["state"] for q, r in outcomes.items()}
    wrong = []
    with session_scope() as s:
        for question, req in auto.items():
            assert question in answerable, f"answered a question the data cannot answer: {question}"
            turn = s.get(AskTurn, req["answer"]["turn_id"])
            got = sorted(tuple(_num(v) for v in (row.values() if isinstance(row, dict) else row))
                         for row in turn.result["rows"])
            if got != _reference(answerable[question]):
                wrong.append(question)
    assert wrong == []  # none answered wrongly
    for question in unanswerable:
        req = escalated[question]
        detail = api.get(f"/api/requests/{req['id']}", headers=headers).json()
        pack = detail["data_pack"]
        assert pack["refusal"]["kind"] and detail["assignee_id"] and detail["sla_due_at"], pack
        turn = pack["evidence"][0]
        assert turn["kind"] == "ask_turn" and turn["inspector"].endswith(f"/{turn['id']}/inspector")
        inspected = api.get(turn["inspector"], headers=headers).json()  # the analyst sees what was tried
        assert any(c["purpose"] == "sql_generation" for c in inspected["model_calls"]), inspected
    assert transport.decide_calls == [] or all("request_triage" not in str(c) for c in transport.decide_calls)
    value = api.get(f"/api/value?workspace={world['ws']}", headers=headers).json()
    assert value["by_class"] and value["n"] >= 20 and value["tta_p50_seconds"] is not None


def test_dedupe_links(api, world):
    headers = _login(api, "approver@analystos.local")
    other = _login(api, "analyst@analystos.local")
    text_ = "Should we move the service desk to a follow-the-sun model next year?"
    first = api.post("/api/requests?wait=true", headers=headers, json={"workspace_id": world["ws"], "text": text_}).json()["request"]
    assert first["state"] == "escalated" and first["class"] == "R9"
    dup = api.post("/api/requests?wait=true", headers=other,
                   json={"workspace_id": world["ws"], "text": text_.lower().rstrip("?")}).json()["request"]
    assert dup["state"] == "declined" and dup["resolution"] == "duplicate" and dup["duplicate_of"] == first["id"]
    detail = api.get(f"/api/requests/{first['id']}", headers=other).json()
    assert len(detail["followers"]) == 1  # the second requester follows the original
    assert detail["timeline"][-1]["type"] == "follower_added"


def test_escalated_request_is_answered_accepted_and_measured(api, world):
    requester, analyst = _login(api, "approver@analystos.local"), _login(api, "analyst@analystos.local")
    admin = _login(api, "admin@analystos.local")
    r = api.post("/api/requests?wait=true", headers=requester,
                 json={"workspace_id": world["ws"], "text": "Send me the raw rows for every P1 incident", "priority": "high"})
    req = r.json()["request"]
    assert req["class"] == "R8" and req["state"] == "escalated" and req["answer"] is None and "data_pack" not in req
    queue = api.get(f"/api/requests?view=queue&workspace_id={world['ws']}", headers=analyst).json()
    assert req["id"] in [q["id"] for q in queue]
    assert api.get(f"/api/requests?view=queue&workspace_id={world['ws']}", headers=requester).status_code == 403
    assert api.post(f"/api/requests/{req['id']}/actions", headers=requester,
                    json={"type": "answer", "payload": {"text": "x", "human_minutes": 1}}).status_code == 403
    ans = api.post(f"/api/requests/{req['id']}/actions", headers=analyst,
                   json={"type": "answer", "payload": {"text": "Shared through the data owner's approved extract.",
                                                       "human_minutes": 15}})
    assert ans.status_code == 200 and ans.json()["state"] == "answered"
    acc = api.post(f"/api/requests/{req['id']}/actions", headers=requester, json={"type": "accept"}).json()
    assert acc["state"] == "accepted" and acc["resolution"] == "human"
    put = api.put("/api/value/baselines", headers=admin,
                  json={"workspace_id": world["ws"], "class": "R8", "baseline_minutes": 45})
    assert put.status_code == 200, put.text
    value = api.get(f"/api/value?workspace={world['ws']}", headers=analyst).json()
    assert value["by_class"]["R8"]["est_minutes_saved"] == 30.0 and value["by_class"]["R8"]["human_minutes"] == 15


def test_another_tenant_gets_404(api, world):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, User
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope, tenant_scope

    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"desk-{tb[-6:]}", name="Desk tenant B"))
    email = f"admin-{tb}@b.test"
    with tenant_scope(tb), session_scope() as s:
        s.add(User(id=new_id("usr"), email=email, name="B admin", password_hash=hash_password(PASSWORD), is_admin=True))
    mine = _login(api, "analyst@analystos.local")
    req = api.post("/api/requests?wait=true", headers=mine,
                   json={"workspace_id": world["ws"], "text": "Should we outsource the network team next year?"}).json()["request"]
    other = _login(api, email)
    assert api.get(f"/api/requests/{req['id']}", headers=other).status_code == 404
    assert api.post(f"/api/requests/{req['id']}/actions", headers=other, json={"type": "accept"}).status_code == 404
    assert api.post(f"/api/requests/{req['id']}/actions", headers=other,
                    json={"type": "decline", "payload": {"reason": "x"}}).status_code == 404
    assert req["id"] not in [r["id"] for r in api.get("/api/requests?view=all", headers=other).json()]
    assert api.get(f"/api/value?workspace={world['ws']}", headers=other).status_code == 404
    assert api.post("/api/requests", headers=other, json={"workspace_id": world["ws"], "text": "How many incidents?"}).status_code == 404

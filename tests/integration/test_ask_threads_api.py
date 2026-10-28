"""P4-U02 on the local stack: Ask threads over HTTP with streamed stages, the route / clarify
decisions recorded against the turn, the inspector, refusals, promotions (verified query, metric
proposal, monitor, dashboard behind an approval, "Investigate why"), paste-SQL explain with the
gateway plan, the generic capability invoke (read-only runs; a write needs an approval) and spend
by model. The model transport is a fake that writes the SQL; staged ServiceNow data is real."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

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
    """Chat answers with the SQL the test sets; decision calls answer nothing (rules decide)."""

    def __init__(self):
        self.sql = "SELECT 1"
        self.chat_calls: list[dict] = []
        self.decide_calls: list[dict] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        from tests.fakes import chat_json

        self.chat_calls.append(payload)
        return chat_json({"sql": self.sql, "explanation": "Incidents by priority.", "chart": {"type": "bar"}})

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls.append(payload)
        return {"answers": {}, "usage": {}}


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    import os

    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Artifact, SourceAsset, User
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
        ws = create_workspace(s, admin, name="ask threads", objective="Find the drivers of SLA breaches in IT incidents")
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
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
        # A minimal analytical dataset (what an investigation builds) for metric and monitor promotion.
        s.add(Artifact(id=new_id("art"), workspace_id=ws_id, run_id=None, type="dataset", name="aos_incidents",
                       content={"sql": f"SELECT priority, state, opened_at FROM {table}", "raw_time_column": "opened_at"}, content_hash="h"))
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


def _frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    return out


def test_example_followup_uses_prior_filtered_population_without_a_model(api, world, transport):
    headers = _login(api, "admin@analystos.local")
    thread = api.post(f"/api/workspaces/{world['ws']}/ask/threads", headers=headers, json={}).json()
    transport.sql = f"SELECT COUNT(*) AS n FROM {world['table']} WHERE priority = 1"
    endpoint = f"/api/ask/threads/{thread['id']}/turns"
    previous = api.post(endpoint, headers=headers, json={"question":"Count incidents with priority exactly one"}).json()
    assert previous["status"] == "answered"
    calls = len(transport.chat_calls)
    response = api.post(endpoint, headers=headers, json={"question":"list down few same incident"})
    assert response.status_code == 200
    sample = response.json()
    assert sample["status"] == "answered" and sample["answered_by"] == "rules"
    assert sample["result"]["row_count"] == 5
    assert sample["provenance"]["parent_turn_id"] == previous["id"]
    assert "priority = 1" in sample["sql"]
    assert len(transport.chat_calls) == calls
    assert "not a random or representative sample" in sample["explanation"]


def test_ask_thread_streams_stages_records_decisions_and_promotes(api, world, transport):
    admin, approver = _login(api, "admin@analystos.local"), _login(api, "approver@analystos.local")
    ws, table = world["ws"], world["table"]
    transport.sql = f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY 1 ORDER BY 2 DESC"

    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=admin, json={}).json()
    question = "How many incidents per priority, busiest first?"  # not a rules shape: the model writes it
    with api.stream("POST", f"/api/ask/threads/{thread['id']}/turns", headers={**admin, "Accept": "text/event-stream"},
                    json={"question": question}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        frames = _frames("".join(r.iter_text()))
    kinds = [k for k, _ in frames]
    assert kinds[-2:] == ["turn", "end"] and kinds.count("stage") >= 5
    texts = [d["text"] for k, d in frames if k == "stage"]
    assert "Finding the tables that answer this" in texts and "Writing the SQL" in texts
    turn = frames[-2][1]
    assert turn["status"] == "answered" and turn["answered_by"] == "model" and turn["route"] == "generate"
    assert turn["result"]["row_count"] > 0 and turn["provenance"]["assets"][0]["asset"] == table
    assert turn["provenance"]["assets"][0]["source_kind"] == "servicenow" and turn["staleness"]["state"] != "changed"

    detail = api.get(f"/api/ask/threads/{thread['id']}", headers=admin).json()
    assert detail["title"] == question and [t["id"] for t in detail["turns"]] == [turn["id"]]
    found = api.get(f"/api/workspaces/{ws}/ask/threads?q=priority", headers=admin).json()
    # the module's workspace is shared: other tests' threads may also be about priority
    assert thread["id"] in [t["id"] for t in found] and all("priority" in (t.get("title") or "").lower() for t in found)
    assert api.get(f"/api/workspaces/{ws}/ask/threads?q=nothing-like-this", headers=admin).json() == []
    assert api.get(f"/api/ask/threads/{thread['id']}", headers=approver).status_code == 404  # threads are private

    inspected = api.get(f"/api/ask/turns/{turn['id']}/inspector", headers=admin).json()
    purposes = [d["purpose"] for d in inspected["decisions"]]
    assert purposes == ["ask_route", "clarify_needed"]
    route = inspected["decisions"][0]
    assert route["backend"] == "rules" and route["answer"] == "generate" and route["subject"] == f"ask:{turn['id']}"
    assert "sql_generation" in [c["purpose"] for c in inspected["model_calls"]]
    assert inspected["query"]["id"] == turn["result"]["query_id"] and inspected["query"]["status"] == "ok"

    # One refusal kind per failure, each with its remedy; nothing runs for an ambiguous question.
    chats = len(transport.chat_calls)
    vague = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin, json={"question": "what about it?"}).json()
    assert vague["status"] == "clarify" and vague["refusal"]["kind"] == "clarify" and vague["refusal"]["remedy"]
    assert len(transport.chat_calls) == chats

    # Promote: verified query -> the same question now answers from the registry with no model call.
    vq = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "verified_query"})
    assert vq.status_code == 200, vq.text
    again = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin, json={"question": question}).json()
    assert again["answered_by"] == "registry" and again["route"] == "verified_query" and len(transport.chat_calls) == chats

    # Metric: proposed to the semantic layer (approved there); monitor: created on the validated expression.
    metric = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "metric", "name": "incident_count"})
    assert metric.status_code == 200, metric.text
    assert metric.json()["status"] == "proposed" and metric.json()["approval_id"] and metric.json()["value"] > 0
    monitor = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "monitor", "kind": "metric_drift"})
    assert monitor.status_code == 200, monitor.text
    mons = api.get(f"/api/workspaces/{ws}/monitors", headers=admin).json()
    assert any(m["id"] == monitor.json()["id"] and m["config"]["sql_expression"] == "COUNT(*)" for m in mons)
    bad = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin,
                   json={"target": "monitor", "sql_expression": "SUM(no_such_column)"})
    assert bad.status_code == 422

    # Dashboard: an approval over the exact payload first; published only with the approved request, through
    # the BI publisher (a preview publisher stands in for Superset here), and the pending entry is completed.
    from analystos.publishing import base as publishing
    from analystos.publishing.preview import PreviewPublisher

    published: list = []

    class Recording(PreviewPublisher):
        def publish(self, bundle, **kw):
            published.append(bundle)
            return super().publish(bundle, **kw)

    mp = pytest.MonkeyPatch()
    mp.setattr(publishing, "get_publisher", lambda destination, settings=None: Recording())
    first = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "dashboard", "destination": "superset"})
    assert first.status_code == 202, first.text
    apr = first.json()["approval_id"]
    pending = api.get(f"/api/ask/threads/{thread['id']}", headers=admin).json()["turns"][0]["promotions"][-1]
    assert pending["status"] == "approval_required" and pending["approval_status"] == "pending"
    early = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "dashboard", "approval_id": apr})
    assert early.status_code == 409  # still pending
    assert api.post(f"/api/approvals/{apr}/approve", headers=approver, json={}).status_code == 200
    approved = api.get(f"/api/ask/threads/{thread['id']}", headers=admin).json()["turns"][0]["promotions"][-1]
    assert approved["approval_status"] == "approved"  # the thread offers "Complete" after a reload
    added = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "dashboard", "approval_id": apr})
    assert added.status_code == 200 and added.json()["status"] == "published", added.text
    assert [c.chart_type for c in published[0].charts] == ["table"] and published[0].datasets[0].sql == turn["sql"]
    chart = api.get(f"/api/artifacts/{added.json()['id']}", headers=admin).json()
    assert chart["status"] == "published" and chart["platform"] == added.json()["destination"] and chart["external_id"]
    replay = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "dashboard", "approval_id": apr})
    assert replay.status_code == 409  # single use

    promotions = api.get(f"/api/ask/threads/{thread['id']}", headers=admin).json()["turns"][0]["promotions"]
    assert [p["target"] for p in promotions] == ["verified_query", "metric", "monitor", "dashboard"]
    assert promotions[-1]["status"] == "published" and promotions[-1]["requested_at"]

    # A publication is rolled back once.
    pub = added.json()["publication_id"]
    assert api.post(f"/api/publications/{pub}/rollback", headers=admin).status_code == 200
    assert api.get(f"/api/artifacts/{added.json()['id']}", headers=admin).json()["status"] == "rolled_back"
    assert api.post(f"/api/publications/{pub}/rollback", headers=admin).status_code == 409
    mp.undo()

    # Save as report: an HTML document of the stored answer, listed as a report and downloadable.
    report = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "report"})
    assert report.status_code == 200 and report.json()["status"] == "created", report.text
    listed = api.get(f"/api/workspaces/{ws}/artifacts?type=report", headers=admin).json()
    assert report.json()["id"] in [a["id"] for a in listed]
    html = api.get(f"/api/artifacts/{report.json()['id']}/download?format=html", headers=admin)
    assert html.status_code == 200 and turn["result"]["query_id"] in html.text and "text/html" in html.headers["content-type"]

    # Schedule: the request is kept on the turn and a retry reuses it (no duplicate approval).
    body = {"name": question[:200], "cron": "0 9 * * *", "timezone": "UTC"}
    one = api.post(f"/api/ask/turns/{turn['id']}/schedule", headers=admin, json=body).json()
    two = api.post(f"/api/ask/turns/{turn['id']}/schedule", headers=admin, json=body).json()
    assert one["status"] == "approval_required" and two["approval_id"] == one["approval_id"]
    kept = api.get(f"/api/ask/threads/{thread['id']}", headers=admin).json()["turns"][0]["promotions"]
    assert [p["approval_id"] for p in kept if p["target"] == "schedule"] == [one["approval_id"]]
    assert api.post(f"/api/ask/turns/{vague['id']}/promote", headers=admin, json={"target": "monitor"}).status_code == 422


def test_simple_shapes_are_answered_by_the_rules_without_a_model(api, world, transport):
    """The owner's report: "distribution of incident" was refused as "no model route". It is a
    distribution of the incident table: the rules group it by the dimension the ITSM pack declares,
    build the SQL from the catalog and run it through the gateway; no model call is made."""
    admin = _login(api, "admin@analystos.local")
    chats = len(transport.chat_calls)
    thread = api.post(f"/api/workspaces/{world['ws']}/ask/threads", headers=admin, json={}).json()
    turn = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin, json={"question": "distribution of incident"}).json()
    assert turn["status"] == "answered", turn["refusal"]
    assert turn["answered_by"] == "rules" and turn["route"] == "tool" and turn["model"] is None
    assert '"category"' in turn["sql"] and "COUNT(*)" in turn["sql"] and turn["result"]["row_count"] > 1
    assert turn["provenance"]["suggestions"] and all(q.startswith("distribution of incident by ") for q in
                                                      turn["provenance"]["suggestions"])
    assert "category" in turn["explanation"].lower() and len(transport.chat_calls) == chats
    inspected = api.get(f"/api/ask/turns/{turn['id']}/inspector", headers=admin).json()
    assert inspected["query"]["status"] == "ok" and inspected["decisions"][0]["answer"] == "tool"
    skipped = [c for c in inspected["model_calls"] if c["status"] == "skipped" and c["purpose"] == "sql_generation"]
    assert skipped and skipped[0]["answered_by"] == "rules" and skipped[0]["purpose"] == "sql_generation"
    # Each follow-up resolves to its own grouping, still without a model.
    follow = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin,
                      json={"question": turn["provenance"]["suggestions"][0]}).json()
    assert follow["answered_by"] == "rules" and follow["sql"] != turn["sql"] and len(transport.chat_calls) == chats
    monthly = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin,
                       json={"question": "How many incidents were opened each month?"}).json()
    assert monthly["answered_by"] == "rules" and "opened_at" in monthly["sql"] and monthly["chart"]["type"] == "line"


def test_a_question_the_rules_cannot_answer_says_why_no_model_could(api, world, transport):
    """No provider key in the API process: the refusal names the key, where compose reads it, and
    that the api and worker containers must be restarted — not "no model route"."""
    from analystos.runtime import context as runtime_context

    admin = _login(api, "admin@analystos.local")
    router = runtime_context.default_services().router
    saved = router.api_key_lookup
    router.api_key_lookup = lambda env: None
    try:
        thread = api.post(f"/api/workspaces/{world['ws']}/ask/threads", headers=admin, json={}).json()
        turn = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin,
                        json={"question": "Which configuration items had incidents in two consecutive weeks?"}).json()
    finally:
        router.api_key_lookup = saved
    r = turn["refusal"]
    assert turn["status"] == "refused" and r["kind"] == "no_api_key" and r["details"]["env"] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY" in r["remedy"] and "api and worker" in r["remedy"] and "restart" in r["remedy"]


def test_investigate_why_starts_a_run_from_the_answer(api, world, transport):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, LineageEdge

    admin = _login(api, "admin@analystos.local")
    transport.sql = f"SELECT state, COUNT(*) AS n FROM {world['table']} GROUP BY 1"
    thread = api.post(f"/api/workspaces/{world['ws']}/ask/threads", headers=admin, json={"title": "why"}).json()
    turn = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin,
                    json={"question": "How many incidents are in each state?"}).json()
    assert turn["status"] == "answered", turn
    r = api.post(f"/api/ask/turns/{turn['id']}/promote", headers=admin, json={"target": "investigate"})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.origin["type"] == "ask" and run.origin["turn_id"] == turn["id"]
        assert run.objective == "Investigate why: How many incidents are in each state?"
        assert s.scalar(select(LineageEdge).where(LineageEdge.from_id == turn["id"], LineageEdge.to_id == run_id)) is not None
        run.control = "cancel"  # the run itself is not under test here
    deadline = time.time() + 120
    while time.time() < deadline:
        with session_scope() as s:
            if s.get(AnalysisRun, run_id).status in ("COMPLETED", "FAILED", "CANCELLED", "REJECTED"):
                break
        time.sleep(0.5)


def test_explain_asks_the_gateway_for_a_plan_without_running_anything(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution

    admin = _login(api, "admin@analystos.local")
    ok = api.post(f"/api/workspaces/{world['ws']}/query/explain", headers=admin,
                  json={"sql": f"SELECT priority, COUNT(*) FROM {world['table']} GROUP BY 1"})
    body = ok.json()
    assert body["gateway"]["accepted"] is True and body["plan"]["available"] is True, body
    assert world["table"].split(".")[1] in " ".join(body["plan"]["relations"]) and body["plan"]["estimated_rows"] is not None
    with session_scope() as s:
        row = s.scalar(select(QueryExecution).where(QueryExecution.workspace_id == world["ws"], QueryExecution.purpose == "console.explain")
                       .order_by(QueryExecution.created_at.desc()))
        assert row.status == "explained" and row.row_count == 0
    bad = api.post(f"/api/workspaces/{world['ws']}/query/explain", headers=admin, json={"sql": f"DELETE FROM {world['table']}"}).json()
    assert bad["gateway"]["accepted"] is False and "plan" not in bad


def test_capability_invoke_runs_read_only_and_holds_writes_for_approval(api, world, monkeypatch):
    from analystos.capabilities import invoke as inv
    from analystos.capabilities import registry

    admin, approver = _login(api, "admin@analystos.local"), _login(api, "approver@analystos.local")
    ws, table = world["ws"], world["table"]
    listed = api.get(f"/api/capabilities?workspace_id={ws}", headers=admin).json()["capabilities"]
    row_count = next(c for c in listed if c["id"] == "skill.row_count")
    assert row_count["input_schema"]["required"] == ["asset"] and "ui" in row_count

    off = api.post(f"/api/workspaces/{ws}/capabilities/skill.row_count/invoke", headers=admin, json={"arguments": {"asset": table}})
    if off.status_code == 403:  # not enabled by default here: enablement is checked before anything runs
        assert "disabled" in off.json()["error"]["message"]
        assert api.put(f"/api/workspaces/{ws}/capabilities/skill.row_count", headers=admin, json={"enabled": True}).status_code == 200
    ok = api.post(f"/api/workspaces/{ws}/capabilities/skill.row_count/invoke", headers=admin, json={"arguments": {"asset": table}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "ok" and ok.json()["result"]["row_count"] > 0
    bad = api.post(f"/api/workspaces/{ws}/capabilities/skill.row_count/invoke", headers=admin, json={"arguments": {"table": table}})
    assert bad.status_code == 422 and bad.json()["error"]["details"]["errors"]
    assert api.put(f"/api/workspaces/{ws}/capabilities/method.trend", headers=admin, json={"enabled": True}).status_code == 200
    method = api.post(f"/api/workspaces/{ws}/capabilities/method.trend/invoke", headers=admin, json={"arguments": {}})
    assert method.status_code == 422 and method.json()["error"]["details"]["reason"] == "not_invocable"

    writer = {"apiVersion": "analystos/v1", "kind": "Skill", "id": "skill.test_writer", "summary": "writes (test)",
              "entry": "python:analystos.skills.lookup:row_count", "side_effect": "write_internal", "spec": {"call": "context"},
              "certification": {"status": "certified"},
              "input_schema": {"type": "object", "required": ["asset"], "properties": {"asset": {"type": "string"}}}}
    snap = registry.load(extra=[("builtin", writer)])
    monkeypatch.setattr(inv, "_snapshot", lambda s, ws_id: snap)
    monkeypatch.setattr(inv.enablement, "usable", lambda *a, **k: None)
    held = api.post(f"/api/workspaces/{ws}/capabilities/skill.test_writer/invoke", headers=admin, json={"arguments": {"asset": table}})
    assert held.status_code == 202 and held.json()["status"] == "approval_required", held.text
    apr = held.json()["approval_id"]
    changed = api.post(f"/api/workspaces/{ws}/capabilities/skill.test_writer/invoke", headers=admin,
                       json={"arguments": {"asset": "other.table"}, "approval_id": apr})
    assert changed.status_code == 409  # pending, and another payload in any case
    assert api.post(f"/api/approvals/{apr}/approve", headers=approver, json={}).status_code == 200
    # the approval is bound to its requester: another editor holding the id is refused, and it is not consumed
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security.auth import hash_password
    from analystos.services.workspaces import add_member

    other_email = f"editor-{new_id('u')[-8:]}@analystos.local"
    with session_scope() as s:
        s.add(User(id=new_id("usr"), email=other_email, name="Other editor", password_hash=hash_password(PASSWORD),
                   is_admin=False, attributes={}))
        s.flush()
        add_member(s, s.scalar(select(User).where(User.email == "admin@analystos.local")), ws, other_email, "editor")
    stolen = api.post(f"/api/workspaces/{ws}/capabilities/skill.test_writer/invoke", headers=_login(api, other_email),
                      json={"arguments": {"asset": table}, "approval_id": apr})
    assert stolen.status_code == 403 and "another user" in stolen.json()["error"]["message"], stolen.text
    ran =api.post(f"/api/workspaces/{ws}/capabilities/skill.test_writer/invoke", headers=admin,
                   json={"arguments": {"asset": table}, "approval_id": apr})
    assert ran.status_code == 200 and ran.json()["status"] == "ok", ran.text
    again = api.post(f"/api/workspaces/{ws}/capabilities/skill.test_writer/invoke", headers=admin,
                     json={"arguments": {"asset": table}, "approval_id": apr})
    assert again.status_code == 409  # consumed


def test_an_approved_ai_suggestion_is_a_receipt_in_the_ask_inspector(api, world, transport):
    """P4-U04 journey: review an AI suggestion -> approve (publish into the workspace pack) -> ask a
    question -> the approved document is a context receipt on the answer's Evidence tab."""
    from analystos.db.base import session_scope
    from analystos.knowledge import suggestions

    admin = _login(api, "admin@analystos.local")
    ws, table = world["ws"], world["table"]
    with session_scope() as s:
        draft = suggestions.propose(s, ws, kind="term", subject="term:reopen rate", title="Reopen rate",
                                    fields={"body": suggestions.field("The reopen rate is the share of resolved incidents "
                                                                      "that were reopened within seven days.", 0.55,
                                                                      source="model", model="m1"),
                                            "synonyms": suggestions.field(["reopen ratio"], 0.8, source="rule")},
                                    origin="crawler.enrichment", proposed_by="model:m1")
        sid, path = draft.id, draft.path
    queue = api.get(f"/api/workspaces/{ws}/knowledge/suggestions", headers=admin).json()
    shown = next(q for q in queue if q["id"] == sid)
    assert shown["fields"]["body"]["confidence"] == 0.55 and shown["fields"]["body"]["provenance"]["model"] == "m1"
    r = api.post(f"/api/workspaces/{ws}/knowledge/suggestions/review", headers=admin,
                 json={"decisions": [{"id": sid, "action": "approve"}]})
    assert r.status_code == 200 and r.json()["approved"] == [{"id": sid, "path": path}], r.text

    transport.sql = f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY 1"
    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=admin, json={}).json()
    turn = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin,
                    json={"question": "What is the reopen rate by priority?"}).json()
    assert turn["status"] == "answered" and turn["answered_by"] == "model", turn
    receipts = api.get(f"/api/ask/turns/{turn['id']}/inspector", headers=admin).json()["receipts"]
    mine = [x for x in receipts if x.get("path") == path]
    assert mine and mine[0]["section"] == "glossary" and mine[0]["source"] == "review:crawler.enrichment"
    located = api.get(f"/api/workspaces/{ws}/knowledge/locate", headers=admin,
                      params={"document_id": mine[0]["document_id"]}).json()
    assert located["path"] == path  # the receipt opens its document in the studio


def test_spend_by_model_is_reported(api, world):
    admin = _login(api, "admin@analystos.local")
    body = api.get("/api/admin/token-savings", headers=admin).json()
    assert isinstance(body["by_model"], dict) and isinstance(body["by_rung"], dict)

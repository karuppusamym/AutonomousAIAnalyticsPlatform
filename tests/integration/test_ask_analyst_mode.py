"""Ask analyst mode on the local stack.

* the service with a fake single-question Ask: a partial failure keeps the failed step, the turn is
  persisted with `analysis` and mirrors the headline step; a step that needs detail makes the turn
  `clarify` with an assumption; a quick turn has no analysis;
* over HTTP with staged ServiceNow data and no model: "incidents by priority and state" is planned into a
  total and two breakdowns that the Ask rules answer through the gateway, stages stream per step, the
  synthesis cites its steps; a step rerun with edited SQL recomputes that step and marks the synthesis
  stale, re-synthesis clears it; "why this number" works for the headline and for another step;
* migration 0045 upgrades and downgrades.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select, text

from analystos.db import vectors

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
    """No chat model should be needed; any chat call is recorded (and answers nothing usable)."""

    def __init__(self):
        self.chat_calls: list[dict] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        from tests.fakes import chat_json

        self.chat_calls.append(payload)
        return chat_json({"sql": "SELECT 1"})

    def decide(self, *, base_url, api_key, payload, timeout):
        return {"answers": {}, "usage": {}}


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    import os

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.runtime.context import default_router
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANALYSTOS_SOURCE_SECRET_SERVICENOW")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="ask analyst mode", objective="Understand incident volumes")
        s.flush()
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN analyst",
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
    yield {"ws": ws_id, "table": table, "admin": admin}
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


def _frames(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    return out


# ------------------------------------------------------------------------------------ service, fake Ask
def _answer(table, columns, rows):
    return {"status": "answered", "answered_by": "rules", "sql": f"SELECT 1 FROM {table}", "chart": {"type": "bar"},
            "model": None, "route": "tool", "decisions": [], "attempts": [],
            "result": {"query_id": None, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False,
                       "referenced_assets": [table], "result_hash": "h"}}


def test_the_service_persists_the_analysis_mirrors_the_headline_and_keeps_a_failed_step(world, api):
    from analystos.core.errors import SQLRejected
    from analystos.services import ask as ask_svc

    admin, table = world["admin"], world["table"]
    from analystos.db.base import session_scope

    with session_scope() as s:
        thread = ask_svc.create_thread(s, s.merge(admin), world["ws"])

    def fake(ctx, q, parameters=None):
        if "by priority" in q:
            raise SQLRejected("no such column")
        if "by state" in q:
            return _answer(table, ["state", "n"], [["New", 4], ["Closed", 9]])
        return _answer(table, ["n"], [[13]])

    turn = ask_svc.ask_in_thread(admin, thread["id"], "incidents by priority and state", mode="analyst", ask_fn=fake)
    a = turn["analysis"]
    assert turn["status"] == "answered" and turn["route"] == "analyst" and a["mode"] == "analyst"
    assert [s["status"] for s in a["steps"]] == ["answered", "refused", "answered"]
    assert turn["result"]["rows"] == [[13]] and turn["sql"] == f"SELECT 1 FROM {table}" and a["headline_step"] == 1
    assert turn["explanation"] == a["synthesis"]["text"] and a["synthesis"]["citations"] == [1, 2, 3]
    assert turn["provenance"]["assets"][0]["asset"] == table and a["steps"][2]["provenance"]["assets"][0]["asset"] == table
    assert any("Step 2 of 3" in st["text"] for st in turn["stages"]) and turn["stages"][-1]["key"] == "done"

    def clarify(ctx, q, parameters=None):
        return {"status": "clarify", "explanation": "Which time column?", "missing": [{"name": "time"}], "suggestions": []}

    asked = ask_svc.ask_in_thread(admin, thread["id"], "incidents over time", mode="analyst", ask_fn=clarify)
    assert asked["status"] == "clarify" and asked["refusal"]["details"]["assumption"] == "in the last 12 months"
    assert asked["analysis"]["steps"][0]["status"] == "clarify"

    quick = ask_svc.ask_in_thread(admin, thread["id"], "how many incidents", ask_fn=lambda c, q, parameters=None: _answer(
        table, ["n"], [[13]]))
    assert quick["analysis"] is None
    with session_scope() as s:
        detail = ask_svc.thread_detail(s, s.merge(admin), thread["id"])
    assert [t["analysis"] is not None for t in detail["turns"]] == [True, True, False]

    # Answered turns are recorded as Data Thread steps as they happen (no "record" click): each answered step of
    # the analyst turn (steps 1 and 3; step 2 was refused) and the quick answer; the clarify turn adds nothing.
    from analystos.db.models import AnalysisStep

    with session_scope() as s:
        steps = list(s.scalars(select(AnalysisStep).where(AnalysisStep.container_type == "ask_thread",
                                                           AnalysisStep.container_id == thread["id"])))
    origins = sorted((st.origin or {}).get("id") for st in steps)
    assert origins == sorted([f"{turn['id']}:step1", f"{turn['id']}:step3", quick["id"]])
    assert all(st.kind == "query" for st in steps)
    # recording again is a no-op
    from analystos.services.steps import ingest_ask_thread

    again = ingest_ask_thread(admin, world["ws"], thread["id"])
    assert len(again["steps"]) == 3


# ------------------------------------------------------------------------------------ HTTP, real stack
def test_analyst_mode_over_http_plans_runs_checks_synthesizes_reruns_and_explains(api, world, transport):
    admin = _login(api, "admin@analystos.local")
    ws, table = world["ws"], world["table"]
    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=admin, json={}).json()
    chats = len(transport.chat_calls)
    with api.stream("POST", f"/api/ask/threads/{thread['id']}/turns", headers={**admin, "Accept": "text/event-stream"},
                    json={"question": "incidents by priority and state", "mode": "analyst"}) as r:
        assert r.status_code == 200
        frames = _frames("".join(r.iter_text()))
    kinds = [k for k, _ in frames]
    assert kinds[-2:] == ["turn", "end"]
    texts = [d["text"] for k, d in frames if k == "stage"]
    assert texts[0].startswith("Planned 3 steps") and "Step 2 of 3: Break incidents down by priority" in texts
    assert any(t.startswith("Step 3 of 3: Running it through the query gateway") for t in texts)
    turn = frames[-2][1]
    a = turn["analysis"]
    assert turn["status"] == "answered", turn.get("refusal")
    assert [s["status"] for s in a["steps"]] == ["answered"] * 3, [s.get("refusal") for s in a["steps"]]
    assert all(s["answered_by"] == "rules" for s in a["steps"]) and len(transport.chat_calls) == chats  # no model
    by_priority = a["steps"][1]
    assert by_priority["result"]["row_count"] > 1 and by_priority["facts"]["measures"][0]["total"] > 0
    assert [m["column"] for m in by_priority["facts"]["measures"]] == ["incident_count"]  # priority is the grouping
    assert by_priority["facts"]["measures"][0]["max_label"].startswith("priority ")
    from analystos.agents.analyst import bound_inputs
    from analystos.skills.result_facts import numbers_bound

    values, labels = bound_inputs(a["steps"], a["plan"], {})
    assert numbers_bound(a["synthesis"]["text"], values, labels=labels, steps=[1, 2, 3])["ok"]
    assert {c["code"] for c in by_priority["checks"]} >= {"empty_result", "truncation", "grouping", "single_row_breakdown"}
    assert a["synthesis"]["origin"] == "template" and a["synthesis"]["citations"] == [1, 2, 3]

    # the answer was recorded as three Data Thread steps; their stored results stay out of the Outputs list
    steps = api.get(f"/api/workspaces/{ws}/threads/ask_thread/{thread['id']}", headers=admin).json()["steps"]
    assert len(steps) == 3 and all(s["kind"] == "query" for s in steps)
    listed = api.get(f"/api/workspaces/{ws}/artifacts", headers=admin).json()
    assert not any(x["type"] == "step_result" for x in listed)
    assert len(api.get(f"/api/workspaces/{ws}/artifacts", headers=admin, params={"type": "step_result"}).json()) >= 3
    assert a["plan"]["origin"] == "rules" and len(a["follow_ups"]) <= 3
    assert turn["result"]["query_id"] == a["steps"][0]["result"]["query_id"]

    # "Why this number" for the headline (the turn) and for another step.
    why = api.get(f"/api/ask/turns/{turn['id']}/why", headers=admin)
    assert why.status_code == 200 and why.json()["numbers"], why.text
    step_why = api.get(f"/api/ask/turns/{turn['id']}/why?step=2", headers=admin)
    assert step_why.status_code == 200, step_why.text
    assert step_why.json()["subject"]["step"] == 2 and step_why.json()["numbers"]

    # Rerun step 2 with edited SQL through the gateway: facts recomputed, synthesis stale; then re-synthesize.
    edited = f'SELECT "priority", COUNT(*) AS n FROM {table} WHERE "priority" IS NOT NULL GROUP BY "priority"'
    rerun = api.post(f"/api/ask/turns/{turn['id']}/steps/2/rerun", headers=admin, json={"sql": edited})
    assert rerun.status_code == 200, rerun.text
    after = rerun.json()["analysis"]
    assert after["steps"][1]["sql"] == edited and after["steps"][1]["rerun"]["edited"] is True
    assert after["steps"][1]["governance"] == "ad_hoc" and after["synthesis"]["stale"] is True
    assert after["steps"][1]["facts"]["row_count"] == after["steps"][1]["result"]["row_count"]
    bad = api.post(f"/api/ask/turns/{turn['id']}/steps/2/rerun", headers=admin, json={"sql": f"DELETE FROM {table}"})
    assert bad.status_code in (400, 403, 422), bad.text
    assert api.post(f"/api/ask/turns/{turn['id']}/steps/9/rerun", headers=admin, json={}).status_code == 404
    fresh = api.post(f"/api/ask/turns/{turn['id']}/synthesize", headers=admin)
    assert fresh.status_code == 200 and not fresh.json()["analysis"]["synthesis"].get("stale")

    # Quick mode is unchanged and has no analysis; step endpoints refuse a quick turn.
    quick = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=admin, json={"question": "distribution of incident"}).json()
    assert quick["status"] == "answered" and quick["analysis"] is None and quick["route"] == "tool"
    assert api.post(f"/api/ask/turns/{quick['id']}/steps/1/rerun", headers=admin, json={}).status_code == 422


# ------------------------------------------------------------------------------------ migration
def test_migration_0045_upgrades_and_downgrades(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from analystos.core.config import REPO_ROOT

    url = make_url(control_db)
    name = f"{url.database}_m45"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {name}"))
    target = url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(target)

    def columns() -> set[str]:
        with engine.connect() as c:
            return {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns "
                                                 "WHERE table_name = 'ask_turn'"))}
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", target)
        command.upgrade(cfg, "head")
        assert "analysis" in columns()
        command.downgrade(cfg, "0044")
        assert "analysis" not in columns()
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()

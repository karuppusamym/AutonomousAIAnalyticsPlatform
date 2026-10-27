"""P4-06 and P7-03 through the HTTP API on the local stack (no model, runs are not executed unless a
test drives the engine):

* idempotency keys on run creation, schedule creation, work orders and Ask turns: replay, 409 on a
  different body, concurrent duplicates converge on one run;
* the dispatch outbox: orchestrator down (crash before dispatch) then relayed, cancellation before
  dispatch, one outbox row per run;
* revision checks (ETag / If-Match: 412 stale, 428 missing where required) and cursor pages that stay
  consistent while rows are inserted, with the old array responses kept for existing clients;
* typed work orders: placeholders persist but refuse to start (`unsupported_capability`);
* definitions: a draft is refused as a trigger outside `environment: dev`, publish/retire through the
  API; a real pack upgrade (admin reload) shows "upgrade available" on a pinned schedule and its next
  fire still binds the pinned versions.
"""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn
import yaml
from sqlalchemy import func, select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"

AGENT = {
    "apiVersion": "analystos/v1", "kind": "Agent", "id": "agent.pin_notes", "version": "0.1.0",
    "summary": "Pinned notes agent", "entry": "builtin:generic", "determinism": "model", "side_effect": "write_internal",
    "cost_class": "llm_small", "certification": {"status": "certified", "evidence": "tests/integration/test_workbench_contracts_api.py"},
    "spec": {"role": "Notes agent", "goal": "Note the size of every selected table",
             "capabilities": ["skill.row_count"], "tools": ["sql.execute"], "model_purpose": "agent_actions",
             "budget": {"llm_calls": 1, "queries": 1, "max_steps": 1}, "policies": {"pii_access": "none"},
             "output": {"artifact_type": "agent_output", "artifact_name": "Pinned notes"}}}
PLAYBOOK = {
    "apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.pin_notes", "version": "0.1.0",
    "summary": "Collect metadata, then note every table", "determinism": "model", "side_effect": "write_internal",
    "certification": {"status": "certified", "evidence": "tests/integration/test_workbench_contracts_api.py"},
    "spec": {"framing": False, "steps": [
        {"key": "metadata", "use": "agent.metadata", "title": "Collect metadata"},
        {"key": "notes", "use": "agent.pin_notes", "title": "Write table notes", "after": ["metadata"]}]}}


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


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(api, servicenow_url):
    import os

    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import discover_source, register_source, select_assets

    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    admin, analyst, viewer = (_login(api, f"{u}@analystos.local") for u in ("admin", "analyst", "approver"))
    r = api.post("/api/workspaces", headers=analyst, json={"name": "workbench", "objective": "Find the drivers of SLA breaches"})
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
    r = api.post("/api/workspaces", headers=analyst, json={"name": "workbench other", "objective": "Another workspace here"})
    other = r.json()["id"]
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, owner, ws, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        src_id = src.id
        s.expunge(owner)
    discover_source(owner, src_id)
    select_assets(owner, src_id, ["incident"])
    return {"ws": ws, "other": other, "admin": admin, "analyst": analyst, "viewer": viewer, "owner": owner}


@pytest.fixture
def started(monkeypatch):
    """Record dispatches instead of running the engine (the tests drive what they need)."""
    from analystos.services import runs

    calls: list[str] = []
    monkeypatch.setattr(runs, "start_run", lambda run_id: calls.append(run_id) or f"analysis-{run_id}")
    return calls


def _outbox(run_id: str) -> list:
    from analystos.db.base import session_scope
    from analystos.db.models import DispatchOutbox

    with session_scope() as s:
        rows = list(s.scalars(select(DispatchOutbox).where(DispatchOutbox.run_id == run_id)))
        s.expunge_all()
    return rows


# ------------------------------------------------------------------------------------ idempotency + outbox
def test_duplicate_run_requests_create_one_run_and_a_changed_body_is_409(api, world, started):
    ws, analyst = world["ws"], world["analyst"]
    body = {"objective": "Why do priority 1 incidents breach their SLA?"}
    headers = {**analyst, "Idempotency-Key": "run-key-1"}
    first = api.post(f"/api/workspaces/{ws}/analysis", headers=headers, json=body)
    assert first.status_code == 200, first.text
    again = api.post(f"/api/workspaces/{ws}/analysis", headers=headers, json=body)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers
    assert started == [first.json()["id"]]  # dispatched once
    rows = _outbox(first.json()["id"])
    assert len(rows) == 1 and rows[0].status == "dispatched" and rows[0].workflow_id == f"analysis-{first.json()['id']}"
    changed = api.post(f"/api/workspaces/{ws}/analysis", headers=headers, json={"objective": "Something else entirely here"})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
    # The same key from another principal is a different key.
    admin_first = api.post(f"/api/workspaces/{ws}/analysis", headers={**world["admin"], "Idempotency-Key": "run-key-1"}, json=body)
    assert admin_first.status_code == 200 and admin_first.json()["id"] != first.json()["id"]


def test_concurrent_duplicates_converge_on_one_run(api, world, started):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    ws, analyst = world["ws"], world["analyst"]
    body = {"objective": "Which assignment groups resolve incidents fastest?"}

    def post(_):
        return api.post(f"/api/workspaces/{ws}/analysis", headers={**analyst, "Idempotency-Key": "concurrent-1"}, json=body)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(post, range(4)))
    assert {r.status_code for r in results} == {200}
    assert len({r.json()["id"] for r in results}) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.objective == body["objective"])) == 1


def test_orchestrator_down_then_relayed_and_cancel_before_dispatch(api, world, monkeypatch):
    from datetime import timedelta

    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun
    from analystos.services import dispatch, runs

    ws, analyst = world["ws"], world["analyst"]

    def down(run_id):
        raise ConnectionError("orchestrator unreachable")
    monkeypatch.setattr(runs, "start_run", down)
    a = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={"objective": "Crash before dispatch: relay me later"})
    b = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={"objective": "Crash before dispatch: cancel me first"})
    assert a.status_code == b.status_code == 200 and a.json()["status"] == "NEW" and a.json()["workflow_id"] is None
    assert _outbox(a.json()["id"])[0].status == "pending" and _outbox(a.json()["id"])[0].attempts == 1
    assert api.post(f"/api/workspaces/{ws}/analysis/{b.json()['id']}/cancel", headers=analyst).status_code == 200

    started: list[str] = []
    monkeypatch.setattr(runs, "start_run", lambda run_id: started.append(run_id) or f"analysis-{run_id}")
    out = dispatch.relay(now=utcnow() + timedelta(minutes=10))
    assert out.get("dispatched", 0) >= 1 and out.get("cancelled", 0) >= 1
    assert a.json()["id"] in started and b.json()["id"] not in started
    with session_scope() as s:
        assert s.get(AnalysisRun, a.json()["id"]).workflow_id == f"analysis-{a.json()['id']}"
        assert s.get(AnalysisRun, b.json()["id"]).status == "CANCELLED"
    assert _outbox(b.json()["id"])[0].status == "cancelled"


# ------------------------------------------------------------------------------------ pages and revisions
def test_cursor_pages_stay_consistent_during_inserts_and_old_clients_get_arrays(api, world, started):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, User

    ws, analyst = world["ws"], world["analyst"]
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        for i in range(7):
            s.add(AnalysisRun(id=new_id("run"), workspace_id=ws, objective=f"page seed {i}", status="COMPLETED", plan={},
                              plan_version=1, scope={}, instructions=[], constraints={}, requested_by=owner.id, summary={},
                              origin={"type": "user"}))
    legacy = api.get(f"/api/workspaces/{ws}/analysis", headers=analyst)
    assert legacy.status_code == 200 and isinstance(legacy.json(), list)  # existing clients: unchanged shape
    before = [r["id"] for r in legacy.json()]
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        r = api.get(f"/api/workspaces/{ws}/analysis", headers=analyst, params=params)
        assert r.status_code == 200, r.text
        seen += [x["id"] for x in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        pages += 1
        if pages == 1:  # rows inserted while paging land in front of the cursor: never duplicated or skipped
            api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={"objective": "Inserted while a client pages"})
        if not cursor:
            break
    # Rows written in one transaction share created_at; pages break the tie by id, so compare as sets.
    assert set(seen) == set(before) and len(seen) == len(set(seen)) == len(before) and pages >= 3
    assert api.get(f"/api/workspaces/{ws}/analysis", headers=analyst, params={"limit": 0}).status_code == 422
    wrong = api.get(f"/api/workspaces/{world['other']}/analysis", headers=analyst, params={"cursor": r.json()["next_cursor"] or
                    api.get(f"/api/workspaces/{ws}/analysis", headers=analyst, params={"limit": 1}).json()["next_cursor"]})
    assert wrong.status_code == 422  # a cursor is bound to the workspace and query it was issued for
    assert api.get(f"/api/workspaces/{ws}/analysis", headers=world["viewer"], params={"limit": 2}).status_code == 200


def test_schedule_create_is_idempotent_and_edits_are_revision_checked(api, world):
    ws, analyst = world["ws"], world["analyst"]
    body = {"name": "nightly refresh", "kind": "dataset_refresh", "cron": "0 2 * * *"}
    headers = {**analyst, "Idempotency-Key": "sch-1"}
    first = api.post(f"/api/workspaces/{ws}/schedules", headers=headers, json=body)
    assert first.status_code == 200 and first.headers["ETag"] == '"1"'
    again = api.post(f"/api/workspaces/{ws}/schedules", headers=headers, json=body)
    assert again.json()["id"] == first.json()["id"] and again.headers["Idempotent-Replayed"] == "true"
    assert api.post(f"/api/workspaces/{ws}/schedules", headers=headers, json={**body, "cron": "0 3 * * *"}).status_code == 409
    sid = first.json()["id"]
    detail = api.get(f"/api/workspaces/{ws}/schedules/{sid}", headers=analyst)
    assert detail.status_code == 200 and detail.headers["ETag"] == '"1"' and detail.json()["pin_status"]["state"] == "unpinned"
    ok = api.patch(f"/api/schedules/{sid}", headers={**analyst, "If-Match": '"1"'}, json={"cron": "0 4 * * *"})
    assert ok.status_code == 200 and ok.headers["ETag"] == '"2"' and ok.json()["revision"] == 2
    stale = api.patch(f"/api/schedules/{sid}", headers={**analyst, "If-Match": '"1"'}, json={"cron": "0 5 * * *"})
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "precondition_failed"
    legacy = api.patch(f"/api/schedules/{sid}", headers=analyst, json={"enabled": False})  # old clients send no If-Match
    assert legacy.status_code == 200 and legacy.json()["revision"] == 3
    page = api.get(f"/api/workspaces/{ws}/schedules", headers=analyst, params={"limit": 1})
    assert page.status_code == 200 and set(page.json()) == {"items", "next_cursor"}
    assert isinstance(api.get(f"/api/workspaces/{ws}/schedules", headers=analyst).json(), list)
    assert api.get(f"/api/workspaces/{world['other']}/schedules/{sid}", headers=analyst).status_code == 404


# ------------------------------------------------------------------------------------ work orders
def test_work_orders_are_typed_revisioned_and_placeholders_do_not_start(api, world, started):
    ws, analyst = world["ws"], world["analyst"]
    ml = {"kind": "forecast", "objective": "Forecast weekly incident volume for the next four weeks",
          "spec": {"type": "ml", "task": "forecast", "target_metric": "incident_count", "horizon": 4}}
    r = api.post(f"/api/workspaces/{ws}/work-orders", headers={**analyst, "Idempotency-Key": "wo-1"}, json=ml)
    assert r.status_code == 201 and r.headers["ETag"] == '"1"' and r.json()["executable"] is False
    wo = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/work-orders", headers={**analyst, "Idempotency-Key": "wo-1"}, json=ml).json()["id"] == wo
    assert api.post(f"/api/workspaces/{ws}/work-orders", headers=analyst,
                    json={**ml, "spec": {"type": "ml", "task": "divine"}}).status_code == 422
    assert api.patch(f"/api/workspaces/{ws}/work-orders/{wo}", headers=analyst, json=ml).status_code == 428
    assert api.patch(f"/api/workspaces/{ws}/work-orders/{wo}", headers={**analyst, "If-Match": '"9"'}, json=ml).status_code == 412
    r = api.patch(f"/api/workspaces/{ws}/work-orders/{wo}", headers={**analyst, "If-Match": '"1"'}, json={**ml, "objective":
                  "Forecast weekly incident volume for the next eight weeks"})
    assert r.status_code == 200 and r.headers["ETag"] == '"2"'
    refused = api.post(f"/api/workspaces/{ws}/work-orders/{wo}/runs", headers={**analyst, "If-Match": '"2"'})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "unsupported_capability"

    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset

    with session_scope() as s:  # the staged table as the gateway sees it (P4-04 readiness checks the inputs)
        a = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.name == "incident"))
        table = f"{a.schema_name}.{a.name}"

    def analysis_on(asset: str) -> dict:
        return {"kind": "diagnose", "objective": "Does priority drive SLA breaches on incidents?",
                "spec": {"type": "analysis", "statements": ["Priority drives SLA breaches"],
                         "analyses": [{"method": "rate_by_segment", "asset": asset,
                                       "outcome": {"type": "is_true", "column": "made_sla"},
                                       "segment": {"type": "column", "column": "priority"}}]}}

    outside = api.post(f"/api/workspaces/{ws}/work-orders", headers=analyst, json=analysis_on("sn.nowhere")).json()["id"]
    blocked = api.post(f"/api/workspaces/{ws}/work-orders/{outside}/runs", headers={**analyst, "If-Match": '"1"'})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "readiness_blocked"
    wo2 = api.post(f"/api/workspaces/{ws}/work-orders", headers=analyst, json=analysis_on(table)).json()["id"]
    assert api.post(f"/api/workspaces/{ws}/work-orders/{wo2}/runs", headers=analyst).status_code == 428
    start = {**analyst, "If-Match": '"1"', "Idempotency-Key": "wo2-start"}
    run = api.post(f"/api/workspaces/{ws}/work-orders/{wo2}/runs", headers=start)
    assert run.status_code == 202 and run.headers["Location"].endswith(run.json()["id"])
    assert api.post(f"/api/workspaces/{ws}/work-orders/{wo2}/runs", headers=start).json()["id"] == run.json()["id"]
    assert run.json()["origin"] == {"type": "work_order", "work_order_id": wo2, "revision": 1, "publish": "skip"}
    assert run.json()["capabilities"]["pinned"]["analyses"][0]["spec"]["method"] == "rate_by_segment"
    listed = api.get(f"/api/workspaces/{ws}/work-orders", headers=analyst, params={"limit": 1}).json()
    assert len(listed["items"]) == 1 and listed["next_cursor"]
    assert api.get(f"/api/workspaces/{ws}/work-orders/{wo2}", headers=analyst).json()["run_ids"] == [run.json()["id"]]
    assert api.get(f"/api/workspaces/{world['other']}/work-orders/{wo2}", headers=analyst).status_code == 404


# ------------------------------------------------------------------------------------ Ask turn idempotency
def test_ask_turn_retry_returns_the_first_turn(api, world, monkeypatch):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AskTurn
    from analystos.services import ask as ask_svc

    ws, analyst = world["ws"], world["analyst"]
    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=analyst, json={"title": "idem"}).json()["id"]
    calls = []

    def fake(user, thread_id, question, parameters=None, **kw):
        calls.append(question)
        with session_scope() as s:
            turn = AskTurn(id=new_id("askt"), thread_id=thread_id, workspace_id=ws, user_id=user.id, seq=len(calls),
                           question=question, parameters={}, status="refused", refusal={"kind": "test"}, attempts=[], stages=[],
                           decisions=[], provenance={}, promotions=[])
            s.add(turn)
            s.flush()
            return ask_svc.turn_out(s, turn)
    monkeypatch.setattr(ask_svc, "ask_in_thread", fake)
    headers = {**analyst, "Idempotency-Key": "ask-1"}
    first = api.post(f"/api/ask/threads/{thread}/turns", headers=headers, json={"question": "How many incidents?"})
    again = api.post(f"/api/ask/threads/{thread}/turns", headers=headers, json={"question": "How many incidents?"})
    assert first.status_code == again.status_code == 200 and first.json()["id"] == again.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true" and calls == ["How many incidents?"]
    other = api.post(f"/api/ask/threads/{thread}/turns", headers=headers, json={"question": "How many problems?"})
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"


# ------------------------------------------------------------------------------------ definitions + pack upgrade
def _playbook_spec(key: str) -> dict:
    from analystos.capabilities import registry

    base = registry.current().get("playbook.investigate").model_dump(mode="json", exclude={"source"})
    return {**base, "id": key, "summary": "Workspace-authored investigation"}


def test_draft_definitions_are_refused_as_triggers_outside_dev(api, world, started):
    ws, analyst = world["ws"], world["analyst"]
    body = {"kind": "playbook", "key": "playbook.ws_investigate", "spec": _playbook_spec("playbook.ws_investigate")}
    r = api.post(f"/api/workspaces/{ws}/definitions", headers=analyst, json=body)
    assert r.status_code == 201 and r.json()["status"] == "draft" and r.headers["ETag"] == '"1"'
    did = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/definitions", headers=world["viewer"], json=body).status_code == 403
    run_body = {"objective": "Investigate SLA breaches with the draft", "definition": {"id": did}}
    denied = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json=run_body)
    assert denied.status_code == 403 and "draft" in denied.json()["error"]["message"]
    sch = api.post(f"/api/workspaces/{ws}/schedules", headers=analyst,
                   json={"name": "draft sched", "kind": "reanalysis", "cron": "0 7 * * 1", "config": {"definition": {"id": did}}})
    assert sch.status_code == 403
    assert api.patch(f"/api/workspaces/{ws}", headers=analyst, json={"settings": {"environment": "dev"}}).status_code == 200
    dev = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json=run_body)
    assert dev.status_code == 200 and dev.json()["capabilities"]["definition"]["status"] == "draft"
    assert api.patch(f"/api/workspaces/{ws}", headers=analyst, json={"settings": {"environment": "prod"}}).status_code == 200
    assert api.post(f"/api/workspaces/{ws}/definitions/{did}/publish", headers=analyst).status_code == 428
    pub = api.post(f"/api/workspaces/{ws}/definitions/{did}/publish", headers={**analyst, "If-Match": '"1"'})
    assert pub.status_code == 200 and pub.json()["status"] == "published" and pub.json()["published_by"]
    ok = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst,
                  json={"objective": "Investigate SLA breaches, published", "playbook": "playbook.ws_investigate"})
    assert ok.status_code == 200 and ok.json()["capabilities"]["definition"]["version"] == 1
    listed = api.get(f"/api/workspaces/{ws}/definitions", headers=analyst, params={"include_builtin": True}).json()
    assert listed["items"][0]["id"] == did and any(b["key"] == "playbook.investigate" for b in listed["builtin"])
    rev = pub.headers["ETag"]
    assert api.post(f"/api/workspaces/{ws}/definitions/{did}/retire", headers={**analyst, "If-Match": rev},
                    json={"reason": "superseded"}).status_code == 200
    gone = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst,
                    json={"objective": "Investigate SLA breaches, retired", "definition": {"id": did}})
    assert gone.status_code == 403 and "retired" in gone.json()["error"]["message"]


@pytest.fixture
def pin_pack(tmp_path, monkeypatch):
    import shutil

    from analystos.capabilities import packs as domain_packs
    from analystos.capabilities import registry

    root = tmp_path / "packs"
    shutil.copytree(registry.PACKS_DIR, root)
    (root / "pin_notes").mkdir()
    (root / "pin_notes" / "agent.yaml").write_text(yaml.safe_dump(AGENT), encoding="utf-8")
    (root / "pin_notes" / "playbook.yaml").write_text(yaml.safe_dump(PLAYBOOK), encoding="utf-8")
    registry.current()
    monkeypatch.setattr(registry, "PACKS_DIR", root)
    yield root
    monkeypatch.undo()
    registry.reload()
    domain_packs.reset()


def test_a_pack_upgrade_shows_upgrade_available_and_the_next_fire_keeps_the_pinned_versions(api, world, pin_pack, started):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Notification, ScheduleRun
    from analystos.runtime import engine
    from analystos.services import schedules as sch_svc

    ws, admin, analyst = world["ws"], world["admin"], world["analyst"]
    assert api.post("/api/admin/capabilities/reload", headers=admin).status_code == 200
    assert api.put(f"/api/workspaces/{ws}/capabilities/playbook.pin_notes", headers=analyst, json={"enabled": True}).status_code == 200
    r = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst,
                 json={"objective": "Note the incident table", "playbook": "playbook.pin_notes", "autonomy_level": 3})
    baseline = r.json()["id"]
    engine.plan_run(baseline)  # binds agent.pin_notes@0.1.0; the test does not need its steps executed
    with session_scope() as s:
        run = s.get(AnalysisRun, baseline)
        assert "agent.pin_notes@0.1.0" in run.capabilities["refs"], run.error
        assert run.capabilities["definition"]["key"] == "playbook.pin_notes" and run.capabilities["definition"]["content_hash"]
        run.status = "COMPLETED"
    sch = api.post(f"/api/workspaces/{ws}/schedules", headers=analyst,
                   json={"name": "pinned notes", "kind": "reanalysis", "cron": "0 7 * * 1",
                         "config": {"baseline_run_id": baseline, "refresh_first": False}})
    assert sch.status_code == 200, sch.text
    sid = sch.json()["id"]
    assert sch.json()["pins"]["revision"] == 1 and "agent.pin_notes@0.1.0" in sch.json()["pins"]["refs"]

    bumped = {**AGENT, "version": "0.2.0", "spec": {**AGENT["spec"], "budget": {"llm_calls": 0}}}
    (pin_pack / "pin_notes" / "agent.yaml").write_text(yaml.safe_dump(bumped), encoding="utf-8")
    reload = api.post("/api/admin/capabilities/reload", headers=admin)
    assert reload.status_code == 200 and reload.json()["pinned_schedules"].get("upgrade_available", 0) >= 1
    detail = api.get(f"/api/workspaces/{ws}/schedules/{sid}", headers=analyst).json()
    status = detail["pin_status"]
    assert status["state"] == "upgrade_available" and status["upgrade_hash"]
    item = next(i for i in status["items"] if i["id"] == "agent.pin_notes")
    assert (item["pinned"], item["current"]) == ("agent.pin_notes@0.1.0", "agent.pin_notes@0.2.0")

    srun = sch_svc.run_now(world["owner"], sid)  # the next fire: a new run on the pinned versions
    with session_scope() as s:
        fire = s.get(ScheduleRun, srun).result
        assert fire["pins"]["upgrade_available"] is True and fire["pin_revision"] == 1
        fire_run = fire["run_id"]
    engine.plan_run(fire_run)
    with session_scope() as s:
        caps = s.get(AnalysisRun, fire_run).capabilities
        assert "agent.pin_notes@0.1.0" in caps["refs"] and "agent.pin_notes@0.2.0" not in caps["refs"]
        assert caps["manifests"]["agent.pin_notes"]["spec"]["budget"]["llm_calls"] == 1  # the pinned manifest, not the upgrade
        assert any(n.title == "Upgrade available: pinned notes" for n in s.scalars(select(Notification).where(
            Notification.workspace_id == ws)))

    # The owner accepts: new schedule revision; its pins are the upgraded versions.
    no_match = api.post(f"/api/workspaces/{ws}/schedules/{sid}/upgrade", headers=analyst, json={})
    assert no_match.status_code == 428
    etag = api.get(f"/api/workspaces/{ws}/schedules/{sid}", headers=analyst).headers["ETag"]
    acc = api.post(f"/api/workspaces/{ws}/schedules/{sid}/upgrade", headers={**analyst, "If-Match": etag},
                   json={"upgrade_hash": status["upgrade_hash"]})
    assert acc.status_code == 200, acc.text
    assert acc.json()["pin_revision"] == 2 and "agent.pin_notes@0.2.0" in acc.json()["added"]
    assert api.get(f"/api/workspaces/{ws}/schedules/{sid}", headers=analyst).json()["pin_status"]["state"] == "current"

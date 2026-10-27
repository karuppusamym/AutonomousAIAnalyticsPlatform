"""Goal API v2 on Postgres under strict tenancy (S06-T1..T4): idempotent goal creation, the SSE contract,
budget exhaustion -> COMPLETED_WITH_LIMITATIONS (AT-04), typed replans with approval invalidation, and
the run, evidence and plan-version views. No model is called: runs execute their deterministic paths."""
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
DQ_QUESTION = "Run a data quality review: nulls, duplicates and freshness of the incident table"


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


@pytest.fixture
def world(api, servicenow_url, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services import runs
    from analystos.services.sources import discover_source, register_source, select_assets

    monkeypatch.setenv("SERVICENOW_PASSWORD", "admin")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # the preview destination, never a live Superset
    get_settings.cache_clear()
    monkeypatch.setattr(runs, "start_run", lambda run_id: f"manual-{run_id}")  # the test drives the engine
    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "goals v2", "objective": "Find the drivers of SLA breaches"})
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, owner, ws, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:SERVICENOW_PASSWORD")
        s.flush()
        src_id = src.id
        s.expunge(owner)
    discover_source(owner, src_id)
    select_assets(owner, src_id, ["incident"])
    yield {"ws": ws, "src": src_id, "analyst": analyst, "approver": approver}
    monkeypatch.undo()
    get_settings.cache_clear()  # later tests read the real settings again


def _enable(api, world, *ids: str) -> None:
    for cid in ids:
        r = api.put(f"/api/workspaces/{world['ws']}/capabilities/{cid}", headers=world["analyst"], json={"enabled": True})
        assert r.status_code == 200, r.text


def _goal(api, world, key: str, **body) -> object:
    payload = {"workspace_id": world["ws"], "question": DQ_QUESTION, "mode": "goal_supervised", **body}
    return api.post("/v2/goals", headers={**world["analyst"], "Idempotency-Key": key}, json=payload)


def _drive(run_id: str) -> str:
    from analystos.workflows.orchestrator import local_runtime

    return local_runtime().drive(run_id, park=True, max_seconds=900)


def _sse(api, headers: dict, run_id: str) -> list[dict]:
    frames, current = [], {}
    with api.stream("GET", f"/v2/runs/{run_id}/events", headers=headers) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line:
                if current:
                    frames.append(current)
                current = {}
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(": ")
            current[field] = value
    return frames


def test_goal_creation_is_idempotent_and_keyed(api, world):
    _enable(api, world, "playbook.dq_review")
    body = {"workspace_id": world["ws"], "question": DQ_QUESTION, "mode": "goal_supervised"}
    r = api.post("/v2/goals", headers=world["analyst"], json=body)
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["error"]["message"]
    first = _goal(api, world, "goal-key-1")
    assert first.status_code == 202, first.text
    out = first.json()
    assert out["goal_id"].startswith("goal") and out["run_id"].startswith("run")
    assert out["events_url"] == f"/v2/runs/{out['run_id']}/events" and out["state"] == "NEW"
    assert out["playbook"] == "playbook.dq_review" and out["limits"]["max_steps"] == 10
    assert out["tier"] == "medium"  # the incident table has masked/sensitive columns: the policy floor is medium
    again = _goal(api, world, "goal-key-1")
    assert again.status_code == 202 and again.headers.get("Idempotent-Replayed") == "true"
    assert (again.json()["goal_id"], again.json()["run_id"]) == (out["goal_id"], out["run_id"])
    clash = _goal(api, world, "goal-key-1", question="Run a data quality review of the change table instead")
    assert clash.status_code == 409
    from analystos.autonomy.models import Goal
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    with session_scope() as s:
        assert len(list(s.scalars(select(Goal).where(Goal.workspace_id == world["ws"])))) == 1
        run = s.get(AnalysisRun, out["run_id"])
        assert run.origin["type"] == "goal" and run.origin["goal_id"] == out["goal_id"] and run.autonomy_level == 2
        assert run.limits == out["limits"]
    waived = _goal(api, world, "goal-key-2", approval_policy={"publish": "not_required"})
    assert waived.status_code == 422  # approvals cannot be waived through the API


def test_goal_runs_a_template_and_streams_the_sse_contract(api, world):
    _enable(api, world, "playbook.dq_review")
    out = _goal(api, world, "goal-sse").json()
    run_id = out["run_id"]
    assert _drive(run_id) == "COMPLETED"
    view = api.get(f"/v2/runs/{run_id}", headers=world["analyst"]).json()
    assert view["state"] == "COMPLETED" and view["goal_id"] == out["goal_id"] and view["playbook"] == "playbook.dq_review"
    assert [s["key"] for s in view["steps"]] == ["metadata", "relationships", "profile", "quality", "finalize"]
    assert view["budget"]["used"]["steps"] == 5 and view["budget"]["remaining"]["max_steps"] == 5
    assert view["complexity"]["floor"] == "medium" and view["complexity"]["features"]["table_count"] == 1
    assert any("masked or sensitive" in r for r in view["complexity"]["reasons"])
    frames = _sse(api, world["analyst"], run_id)
    events = [f for f in frames if f.get("event") not in ("end",)]
    assert frames[-1]["event"] == "end" and json.loads(frames[-1]["data"])["status"] == "COMPLETED"
    ids = [int(f["id"]) for f in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    data = [json.loads(f["data"]) for f in events]
    assert all(d["v"] == 2 and d["run_id"] == run_id and d["type"] == f["event"] for d, f in zip(data, events, strict=True))
    types = [d["type"] for d in data]
    assert "goal.created" in types and "run.replanned" in types and types.count("run.budget") == 5
    last_budget = [d for d in data if d["type"] == "run.budget"][-1]["payload"]
    assert last_budget["remaining"]["max_steps"] == 5 and last_budget["exhausted"] == []
    resumed = [json.loads(f["data"]) for f in _sse(api, {**world["analyst"], "Last-Event-ID": str(ids[-3])}, run_id)
               if f.get("event") != "end"]
    assert [d["id"] for d in resumed] == ids[-2:]  # resume after the last id seen
    evidence = api.get(f"/v2/runs/{run_id}/evidence", headers=world["analyst"]).json()
    assert evidence["state"] == "COMPLETED" and evidence["queries"]
    plan = api.get(f"/v2/runs/{run_id}/plan", headers=world["analyst"]).json()
    assert plan["current"] and plan["version"] == 1
    other = _login(api, "admin@analystos.local")
    assert api.get(f"/v2/runs/{run_id}", headers=other).status_code in (200, 403, 404)


def test_budget_exhaustion_completes_with_limitations(api, world):
    """AT-04: a goal whose step budget runs out ends COMPLETED_WITH_LIMITATIONS with what it validated."""
    _enable(api, world, "playbook.dq_review")
    out = _goal(api, world, "goal-budget", limits={"max_steps": 3}).json()
    assert out["limits"]["max_steps"] == 3  # the client tightened the template default
    assert _drive(out["run_id"]) == "COMPLETED_WITH_LIMITATIONS"
    view = api.get(f"/v2/runs/{out['run_id']}", headers=world["analyst"]).json()
    assert view["state"] == "COMPLETED_WITH_LIMITATIONS"
    lim = view["limitations"]
    assert lim["reason"].startswith("max_steps (3 of 3)") and len(lim["completed_steps"]) == 3
    assert "finalize" in lim["not_run"] and lim["unverified_findings"] == 0
    assert {s["key"]: s["status"] for s in view["steps"]}["finalize"] == "SKIPPED"
    assert view["budget"]["exhausted"] == ["max_steps (3 of 3)"]
    frames = _sse(api, world["analyst"], out["run_id"])
    limited = [json.loads(f["data"]) for f in frames if f.get("event") == "run.limited"]
    assert limited and limited[0]["payload"]["outcome"] == "COMPLETED_WITH_LIMITATIONS"
    assert json.loads(frames[-1]["data"])["status"] == "COMPLETED_WITH_LIMITATIONS"


def test_replan_invalidates_the_plan_approval_and_signals_drive_the_run(api, world):
    """A supervised goal waits for plan approval; a typed replan invalidates it and asks again for the new
    hash; approve_step opens the gate; the superseded plan stays readable."""
    from analystos.autonomy.replan import replan
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Approval, RunTask

    q = "What drives SLA breaches by assignment group and priority over the last year?"
    out = _goal(api, world, "goal-replan", question=q, limits={"max_steps": 4}).json()
    assert out["playbook"] == "playbook.investigate"
    run_id = out["run_id"]
    assert _drive(run_id) == "WAITING_USER"
    with session_scope() as s:
        first = s.scalar(select(Approval).where(Approval.run_id == run_id, Approval.action == "execute_plan"))
        old_hash, first_id = first.plan_hash, first.id
    assert replan(run_id, "user_redirect", "only priority 1 incidents", actor="user:test")["approvals_invalidated"] == 1
    with session_scope() as s:
        assert s.get(Approval, first_id).status == "invalidated"
        run = s.get(AnalysisRun, run_id)
        gate = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "plan_approval"))
        second = s.get(Approval, gate.input["approval_id"])
        assert second.status == "pending" and second.plan_hash == run.plan_hash != old_hash
    assert _drive(run_id) == "WAITING_USER"  # the invalidated approval did not open the gate
    old = api.get(f"/v2/runs/{run_id}/plan?version=1", headers=world["analyst"]).json()
    assert old["current"] is False and old["plan_hash"] == old_hash and old["superseded_by"]["reason_code"] == "user_redirect"
    assert api.get(f"/v2/runs/{run_id}/plan?version=7", headers=world["analyst"]).status_code == 404
    r = api.post(f"/v2/runs/{run_id}/signals", headers=world["approver"], json={"type": "pause", "payload": {}})
    assert r.status_code == 403  # an approver decides approvals; operating the run needs analyst rights
    r = api.post(f"/v2/runs/{run_id}/signals", headers=world["approver"],
                 json={"type": "approve_step", "payload": {"step": "plan_approval"}})
    assert r.status_code == 200 and r.json()["result"] == {"approval_id": second.id, "status": "approved"}, r.text
    assert _drive(run_id) == "COMPLETED_WITH_LIMITATIONS"  # 4 steps of investigate.v1, then the step budget stops it
    with session_scope() as s:
        lim = s.get(AnalysisRun, run_id).summary["limitations"]
        assert lim["reason"].startswith("max_steps (4 of 4)") and "plan_approval" in lim["completed_steps"]


def test_signals_pause_and_cancel_a_goal_run(api, world):
    _enable(api, world, "playbook.dq_review")
    run_id = _goal(api, world, "goal-signals").json()["run_id"]
    r = api.post(f"/v2/runs/{run_id}/signals", headers=world["analyst"], json={"type": "pause", "payload": {}})
    assert r.status_code == 200 and r.json()["result"]["control"] == "pause"
    r = api.post(f"/v2/runs/{run_id}/signals", headers=world["analyst"], json={"type": "cancel", "payload": {}})
    assert r.status_code == 200 and r.json()["result"]["control"] == "cancel"
    assert _drive(run_id) == "CANCELLED"
    bad = api.post(f"/v2/runs/{run_id}/signals", headers=world["analyst"], json={"type": "redirect", "payload": {}})
    assert bad.status_code == 422
    r = api.post(f"/v2/runs/{run_id}/signals", headers=world["analyst"], json={"type": "resume", "payload": {}})
    assert r.status_code == 409


def test_goals_are_tenant_isolated(api, world):
    """Rows of a goal carry the workspace's tenant; another tenant sees neither the goal nor its run."""
    from sqlalchemy import text

    from analystos.db.base import session_scope

    _enable(api, world, "playbook.dq_review")
    out = _goal(api, world, "goal-tenant").json()
    with session_scope() as s:
        tenants = s.execute(text("SELECT tenant_id FROM goal WHERE id = :g UNION SELECT tenant_id FROM analysis_run WHERE id = :r"),
                            {"g": out["goal_id"], "r": out["run_id"]}).scalars().all()
        assert len(tenants) == 1 and tenants[0] is not None
        policies = s.execute(text("SELECT tablename FROM pg_policies WHERE tablename IN ('goal', 'run_plan_version')")).scalars().all()
        assert set(policies) == {"goal", "run_plan_version"}

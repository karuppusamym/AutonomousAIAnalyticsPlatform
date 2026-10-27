"""P4-X01/X02/X03 on the full stack: a new agent and playbook defined only in a directory pack run
through the HTTP API with no code change; per-workspace enablement; hot reload through the admin
endpoint while a run in flight keeps the versions it bound; disabled agents skip optional steps and
fail required ones. Model proposals come from FakeTransport (no real LLM)."""
from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"

AGENT = {
    "apiVersion": "analystos/v1", "kind": "Agent", "id": "agent.table_notes", "version": "0.1.0",
    "summary": "Table notes agent", "entry": "builtin:generic", "determinism": "model", "side_effect": "write_internal",
    "cost_class": "llm_small", "certification": {"status": "certified", "evidence": "tests/integration/test_playbook_runs.py"},
    "spec": {"role": "Table notes agent", "goal": "Note the size and columns of every selected table",
             "capabilities": ["skill.catalog_lookup", "skill.row_count"], "tools": ["metadata.read", "sql.execute"],
             "model_purpose": "agent_actions", "budget": {"llm_calls": 2, "queries": 1, "max_steps": 2},
             "policies": {"pii_access": "none"},
             "default_actions": [{"capability": "skill.catalog_lookup", "input": {"assets": "$scope.assets"}}],
             "output": {"artifact_type": "agent_output", "artifact_name": "Table notes"}}}
PLAYBOOK = {
    "apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.table_notes", "version": "0.1.0",
    "summary": "Collect metadata, then note every table", "determinism": "model", "side_effect": "write_internal",
    "certification": {"status": "certified", "evidence": "tests/integration/test_playbook_runs.py"},
    "spec": {"framing": False, "steps": [
        {"key": "metadata", "use": "agent.metadata", "title": "Collect metadata"},
        {"key": "notes", "use": "agent.table_notes", "title": "Write table notes", "after": ["metadata"]}]}}


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
def packs(tmp_path, monkeypatch):
    """A packs directory: the repository packs plus a config-only pack with the new agent and playbook."""
    from analystos.capabilities import packs as domain_packs
    from analystos.capabilities import registry

    root = tmp_path / "packs"
    shutil.copytree(registry.PACKS_DIR, root)
    (root / "table_notes").mkdir()
    (root / "table_notes" / "agent.yaml").write_text(yaml.safe_dump(AGENT), encoding="utf-8")
    (root / "table_notes" / "playbook.yaml").write_text(yaml.safe_dump(PLAYBOOK), encoding="utf-8")
    registry.current()  # the process registry as it was before the pack was dropped in
    monkeypatch.setattr(registry, "PACKS_DIR", root)
    yield root
    monkeypatch.undo()
    registry.reload()
    domain_packs.reset()


@pytest.fixture
def world(api, servicenow_url, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services import runs
    from analystos.services.sources import discover_source, register_source, select_assets

    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setattr(runs, "start_run", lambda run_id: f"manual-{run_id}")  # the test drives the engine
    admin, analyst, viewer = (_login(api, f"{u}@analystos.local") for u in ("admin", "analyst", "approver"))
    r = api.post("/api/workspaces", headers=analyst, json={"name": "playbooks", "objective": "Document the incident table"})
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
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
    return {"ws": ws, "admin": admin, "analyst": analyst, "viewer": viewer}


def _services(proposals=None, *, key: bool = True):
    from tests.fakes import FakeTransport, chat_json

    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.llm.router import ModelRouter
    from analystos.runtime.context import Services, default_gateway
    from analystos.runtime.usage import DbUsageSink

    def chat(payload):
        # The prompt is a cached stable prefix plus a volatile message (token economy); the agent's state is
        # every JSON object in the user messages, merged (scope in the prefix, history in the last message).
        state: dict = {}
        decoder = json.JSONDecoder()
        for message in payload["messages"]:
            text = message.get("content") if message.get("role") == "user" else None
            i = 0
            while isinstance(text, str) and (i := text.find("{", i)) != -1:
                try:
                    obj, end = decoder.raw_decode(text, i)
                except ValueError:
                    i += 1
                    continue
                if isinstance(obj, dict):
                    state.update(obj)
                i = end
        return chat_json(proposals(state) if proposals else {})

    transport = FakeTransport(chat=chat)
    settings = PlatformSettings(llm=LLMSettings(purpose_modes={"agent_actions": "always"}, cache_enabled=False))
    router = ModelRouter(transport=transport, api_key_lookup=lambda env: "k" if key else None, sink=DbUsageSink(),
                         settings_provider=lambda: settings)
    return Services(router=router, gateway=default_gateway()), transport


def _capabilities(api, headers, ws, kind):
    r = api.get(f"/api/capabilities?kind={kind}&workspace_id={ws}", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["capabilities"]}


def test_a_yaml_only_agent_runs_in_a_playbook_and_a_run_keeps_its_versions(api, world, packs, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Artifact, QueryExecution, RunEvent, RunTask
    from analystos.runtime import engine
    from analystos.runtime.plan import plan_hash
    from analystos.workflows.orchestrator import run_local

    ws, admin, analyst = world["ws"], world["admin"], world["analyst"]
    assert api.post("/api/admin/capabilities/reload", headers=analyst).status_code == 403
    r = api.post("/api/admin/capabilities/reload", headers=admin)  # hot reload: the pack appears, no restart
    assert r.status_code == 200 and not r.json()["problems"] and r.json()["digest"] != r.json()["previous_digest"]
    agents = _capabilities(api, analyst, ws, "Agent")
    assert agents["agent.table_notes"]["enabled"] is False and agents["agent.investigator"]["enabled"] is True
    assert api.put(f"/api/workspaces/{ws}/capabilities/playbook.table_notes", headers=world["viewer"],
                   json={"enabled": True}).status_code == 403
    r = api.put(f"/api/workspaces/{ws}/capabilities/playbook.table_notes", headers=analyst, json={"enabled": True})
    assert r.status_code == 200 and r.json()["ref"] == "playbook.table_notes@0.1.0"
    assert _capabilities(api, analyst, ws, "Agent")["agent.table_notes"]["enabled"] is True

    def proposals(state):
        asset = state["scope"]["assets"][0]
        if not state["history"]:
            return {"actions": [{"capability": "skill.row_count", "input": {"asset": asset}},
                                {"capability": "skill.catalog_lookup", "input": {"assets": [asset]}},
                                {"capability": "skill.row_count", "input": {"asset": "hr.salaries"}},
                                {"capability": "tool.superset_publish", "input": {}}], "done": False}
        return {"actions": [{"capability": "skill.row_count", "input": {"asset": asset}}], "done": True,
                "summary": "The selected table is documented with its columns and size."}

    services, transport = _services(proposals)
    monkeypatch.setattr(engine, "default_services", lambda: services)
    r = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst,
                 json={"objective": "Document the incident table", "playbook": "playbook.table_notes", "autonomy_level": 3})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == "READY", run.error
        refs = run.capabilities["refs"]
        assert {"playbook.table_notes@0.1.0", "agent.table_notes@0.1.0", "skill.row_count@1.0.0"} <= set(refs)
        assert run.plan_hash == plan_hash(run.plan, constraints={}, scope_hash=run.scope["hash"], plan_version=1, capabilities=refs)
        assert [t["key"] for t in run.plan["steps"]] == ["metadata", "notes"]

    # A new version arrives while the run is in flight: the run keeps 0.1.0.
    bumped = {**AGENT, "version": "0.2.0", "spec": {**AGENT["spec"], "budget": {"llm_calls": 0}}}
    (packs / "table_notes" / "agent.yaml").write_text(yaml.safe_dump(bumped), encoding="utf-8")
    assert api.post("/api/admin/capabilities/reload", headers=admin).status_code == 200
    assert _capabilities(api, analyst, ws, "Agent")["agent.table_notes"]["version"] == "0.2.0"

    assert run_local(run_id) == "COMPLETED"
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "notes"))
        out = task.output
        assert task.status == "COMPLETED" and out["agent"] == "agent.table_notes@0.1.0" and out["mode"] == "model"
        statuses = [(a["capability"], a["status"], a["reason"]) for a in out["actions"]]
        assert statuses[0][:2] == ("skill.row_count", "executed") and statuses[1][:2] == ("skill.catalog_lookup", "executed")
        assert statuses[2] == ("skill.row_count", "rejected", "hr.salaries outside the authorized scope of this run")
        assert statuses[3] == ("tool.superset_publish", "rejected", "tool.superset_publish is not bound to agent.table_notes")
        assert statuses[4] == ("skill.row_count", "rejected", "agent.table_notes query budget (1) exhausted for this step")
        assert out["summary_source"] == "model" and len(transport.chat_calls) == 2
        count = s.scalar(select(QueryExecution).where(QueryExecution.run_id == run_id, QueryExecution.task_id == task.id))
        assert count.status == "ok" and count.actor == "agent:table_notes"  # through the one gateway
        assert out["results"][0]["result"]["row_count"] > 0
        art = s.get(Artifact, out["artifact_id"])
        assert art.type == "agent_output" and art.content["agent"] == "agent.table_notes@0.1.0"
        actions = s.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "agent.action")).all()
        assert len(actions) == 5


def test_disabled_agents_skip_optional_steps_and_fail_required_ones(api, world, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunTask
    from analystos.runtime import engine

    ws, analyst = world["ws"], world["analyst"]
    services, _ = _services(key=False)
    monkeypatch.setattr(engine, "default_services", lambda: services)
    assert api.put(f"/api/workspaces/{ws}/capabilities/agent.data_quality", headers=analyst,
                   json={"enabled": False}).status_code == 200
    run_id = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={}).json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:
        assert s.get(AnalysisRun, run_id).status == "READY"
        quality = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "quality"))
        assert (quality.status, quality.error) == ("SKIPPED", "capability agent.data_quality@1.0.0 is disabled for this workspace")
    assert engine.get_state(run_id) == {"ready": ["context", "metadata"]}
    engine.finish_run(run_id, "CANCELLED")

    assert api.put(f"/api/workspaces/{ws}/capabilities/agent.sql", headers=analyst, json={"enabled": False}).status_code == 200
    run_id = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={}).json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == "FAILED" and "step dataset: capability agent.sql@1.0.0 is disabled" in run.error
    assert engine.get_state(run_id) == {"terminal": True, "status": "FAILED"}


def test_a_failed_reload_keeps_the_registry(api, world, packs):
    admin = world["admin"]
    good = api.get("/api/capabilities", headers=admin).json()["digest"]
    bad = {**AGENT, "id": "agent.broken", "spec": {**AGENT["spec"], "capabilities": ["skill.no_such_skill"]}}
    (Path(packs) / "table_notes" / "broken.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    r = api.post("/api/admin/capabilities/reload", headers=admin)
    assert r.status_code == 422 and "agent.broken: unknown capability skill.no_such_skill" in r.json()["error"]["message"]
    assert api.get("/api/capabilities", headers=admin).json()["digest"] == good


def test_the_builtin_data_dictionary_playbook_runs_deterministically_below_autonomy_3(api, world, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Artifact, RunTask
    from analystos.runtime import engine
    from analystos.workflows.orchestrator import run_local

    ws, analyst = world["ws"], world["analyst"]
    services, transport = _services(key=False)  # no model available: the manifest's default actions answer
    monkeypatch.setattr(engine, "default_services", lambda: services)
    body = {"objective": "Document the incident table", "playbook": "playbook.data_dictionary"}
    run_id = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json=body).json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:  # disabled by default in every workspace
        assert "playbook.data_dictionary@1.0.0 is disabled for this workspace" in s.get(AnalysisRun, run_id).error
    assert api.put(f"/api/workspaces/{ws}/capabilities/playbook.data_dictionary", headers=analyst,
                   json={"enabled": True}).status_code == 200
    run_id = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json=body).json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:  # `tested`, not certified: refused at autonomy 3
        assert "is tested, not certified" in s.get(AnalysisRun, run_id).error
    run_id = api.post(f"/api/workspaces/{ws}/analysis", headers=analyst, json={**body, "autonomy_level": 2}).json()["id"]
    assert run_local(run_id) == "COMPLETED"
    with session_scope() as s:
        out = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "dictionary")).output
        assert out["mode"] == "deterministic" and [a["status"] for a in out["actions"]] == ["executed"]
        tables = out["results"][0]["result"]["tables"]
        assert tables and all(t["row_count"] for t in tables) and transport.chat_calls == []
        assert s.get(Artifact, out["artifact_id"]).name == "Data dictionary"

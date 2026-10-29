"""P7-19 on the full stack: a workspace owner creates a declarative agent from the form API, the server
refuses every grant beyond the workspace's (form, raw definition, role), the owner publishes it, a
workspace playbook uses it, and a manual run of that exact playbook version executes the agent through
the generic runtime and the one gateway. No model key: the agent takes its deterministic path."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.integration.playbook_signoff import publish_playbook
from tests.integration.test_playbook_runs import _services, api, servicenow_url, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration

FORM = {"key": "agent.table_notes_form", "title": "Table notes", "purpose": "Note the size and columns of every selected table",
        "capabilities": ["skill.catalog_lookup", "skill.row_count"], "default_actions": ["skill.catalog_lookup"],
        "knowledge": {"sections": ["glossary"]}, "output": {"name": "Table notes"},
        "budget": {"llm_calls": 2, "usd": 0.05, "queries": 1, "max_steps": 2},
        "autonomy": {"mode": "propose", "pii_access": "none", "max_rows": 1000}}


def _playbook(agent: str) -> dict:
    return {"apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.form_notes", "version": "1.0.0",
            "summary": "Collect metadata, then note every table", "determinism": "model", "side_effect": "write_internal",
            "cost_class": "llm_large", "certification": {"status": "draft"}, "tags": ["workspace_workflow"],
            "spec": {"framing": False, "steps": [
                {"key": "metadata", "title": "Collect metadata", "use": "agent.metadata", "after": [], "optional": False},
                {"key": "notes", "title": "Write table notes", "use": agent, "after": ["metadata"], "optional": False}]}}


def test_an_owner_creates_publishes_and_runs_a_form_agent_in_a_workspace_playbook(api, world, monkeypatch):  # noqa: F811
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Artifact, QueryExecution, RunTask
    from analystos.runtime import engine
    from analystos.workflows.orchestrator import run_local

    ws, owner, viewer = world["ws"], world["analyst"], world["viewer"]
    base = f"/api/workspaces/{ws}"

    options = api.get(f"{base}/agent-form", headers=owner).json()
    lookup = next(c for c in options["capabilities"] if c["id"] == "skill.catalog_lookup")
    assert lookup["grantable"] is False and "not enabled" in lookup["reason"]
    assert options["limits"]["pii_access"] == ["none", "restricted"]  # the workspace policy's ceiling

    r = api.post(f"{base}/agent-form", headers=owner, json=FORM)
    assert r.status_code == 403 and "not enabled in this workspace" in r.json()["error"]["message"]
    for cap in ("skill.catalog_lookup", "skill.row_count"):
        assert api.put(f"{base}/capabilities/{cap}", headers=owner, json={"enabled": True}).status_code == 200
    assert api.post(f"{base}/agent-form", headers=viewer, json=FORM).status_code == 403
    r = api.post(f"{base}/agent-form", headers=owner, json={**FORM, "autonomy": {**FORM["autonomy"], "pii_access": "allowed"}})
    assert r.status_code == 403 and "pii_access allowed exceeds" in r.json()["error"]["message"]
    r = api.post(f"{base}/agent-form", headers=owner, json={**FORM, "tools": ["superset.publish"]})
    assert r.status_code == 422  # the form has no tools field: tools are derived from the capabilities

    r = api.post(f"{base}/agent-form", headers=owner, json=FORM)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["kind"] == "agent" and draft["status"] == "draft" and draft["form"]["capabilities"] == FORM["capabilities"]
    assert draft["spec"]["spec"]["tools"] == ["metadata.read", "sql.execute"]
    raw = {**draft["spec"], "entry": "python:analystos.agents.metadata:run", "version": "9.0.0"}
    r = api.patch(f"{base}/definitions/{draft['id']}", headers={**owner, "If-Match": f'"{draft["revision"]}"'}, json={"spec": raw})
    assert r.status_code == 403 and "builtin:generic only" in r.json()["error"]["message"]

    r = api.post(f"{base}/definitions/{draft['id']}/publish", headers={**owner, "If-Match": f'"{draft["revision"]}"'})
    assert r.status_code == 200 and r.json()["status"] == "published", r.text

    r = api.post(f"{base}/definitions", headers=owner,
                 json={"kind": "playbook", "key": "playbook.form_notes", "title": "Form notes", "spec": _playbook(FORM["key"])})
    assert r.status_code == 201, r.text
    pb = r.json()
    r = publish_playbook(api, ws, pb, owner, world["admin"])  # S06-T6: simulated and signed off by someone else
    assert r.status_code == 200 and r.json()["signature"]["state"] == "signed", r.text

    services, transport = _services(key=False)
    monkeypatch.setattr(engine, "default_services", lambda: services)
    r = api.post(f"{base}/analysis", headers=owner,
                 json={"objective": "Document the incident table", "definition": pb["id"], "autonomy_level": 2})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == "READY", run.error
        assert f"{FORM['key']}@1.0.0" in run.capabilities["refs"]
        assert run.capabilities["manifests"][FORM["key"]]["source"] == f"definition:{draft['id']}"
    assert run_local(run_id) == "COMPLETED"
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "notes"))
        out = task.output
        assert task.status == "COMPLETED" and out["agent"] == f"{FORM['key']}@1.0.0" and out["mode"] == "deterministic"
        assert [(a["capability"], a["status"]) for a in out["actions"]] == [("skill.catalog_lookup", "executed")]
        art = s.get(Artifact, out["artifact_id"])
        assert art.type == "agent_output" and art.name == "Table notes"
        assert not transport.chat_calls
        assert s.scalar(select(QueryExecution).where(QueryExecution.run_id == run_id, QueryExecution.task_id == task.id)) is None

    # A capability disabled after publication stops the next run's step: grants are re-checked at binding.
    assert api.put(f"{base}/capabilities/skill.row_count", headers=owner, json={"enabled": False}).status_code == 200
    r = api.post(f"{base}/analysis", headers=owner,
                 json={"objective": "Document the incident table", "definition": pb["id"], "autonomy_level": 2})
    run_id = r.json()["id"]
    engine.plan_run(run_id)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == "FAILED" and "exceeds this workspace's grants" in run.error

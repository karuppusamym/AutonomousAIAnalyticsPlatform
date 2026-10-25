"""New mappings must affect prompts and evaluation, not merely exist in an editor."""
import pytest
from fastapi.testclient import TestClient

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn

pytestmark = pytest.mark.integration


def test_owner_review_lifecycle_resolution_and_golden_evaluation(control_db, monkeypatch):
    from analystos.api.app import app
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
        user = login["user"]
        headers = {"Authorization": "Bearer " + login["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Grounding integration"}).json()["id"]
        sid, aid = new_id("src"), new_id("ast")
        with session_scope() as s:
            s.add(Source(id=sid, workspace_id=wid, name="Incidents", kind="csv", config={}, status="ready", staging_schema="s"))
            s.add(SourceAsset(id=aid, workspace_id=wid, source_id=sid, schema_name="s", name="incident", source_name="incident", kind="table", selected=True))
            s.add(SourceColumn(asset_id=aid, name="repeat_flag", ordinal=1, data_type="boolean", semantic_type="boolean"))
        base = f"/api/workspaces/{wid}/grounding"
        body = {"term": "Repeat", "definition": "A repeat_flag is true when the reviewed repeat policy holds",
                "columns": ["s.incident.repeat_flag"], "source_ids": [sid], "owner_id": user["id"],
                "evidence": "Owner policy reference", "examples": "Known repeat=true and first=false records", "state": "draft"}
        created = client.post(base + "/bindings", headers=headers, json=body)
        assert created.status_code == 200, created.text
        binding = created.json()
        unresolved = client.post(base + "/resolve", headers=headers, json={"question": "Why did repeat incidents increase?", "use_model": False})
        assert unresolved.status_code == 200, unresolved.text
        assert unresolved.json()["needs_clarification"]
        approved = client.put(base + f"/bindings/{binding['id']}", headers={**headers, "If-Match": '"1"'}, json={**body, "state": "approved"})
        assert approved.status_code == 200, approved.text
        result = client.post(base + "/resolve", headers=headers, json={"question": "Why did repeat incidents increase?", "use_model": False})
        assert result.status_code == 200, result.text
        assert not result.json()["needs_clarification"], result.text
        assert any(r["columns"] == ["s.incident.repeat_flag"] for r in result.json()["resolutions"])
        from types import SimpleNamespace
        queries = []
        def execute(scope, sql, **kwargs):
            queries.append((scope, sql, kwargs))
            return SimpleNamespace(query_id="q_grounding_test", rows=[[25, 24, 2]])
        monkeypatch.setattr("analystos.runtime.context.default_gateway", lambda: SimpleNamespace(execute=execute))
        checked = client.post(base + f"/bindings/{binding['id']}/check", headers=headers)
        assert checked.status_code == 200, checked.text
        assert checked.json()["checks"][0]["nonnull_rows"] == 24
        assert "LIMIT 1000" in queries[0][1] and queries[0][2]["retain_rows"] is False
        golden = client.post(base + "/golden", headers=headers, json={"question": "Why did repeat incidents increase?", "expected_columns": ["s.incident.repeat_flag"]})
        assert golden.status_code == 200, golden.text
        evaluation = client.post(base + "/golden/evaluate", headers=headers, json={"use_model": False})
        assert evaluation.status_code == 200, evaluation.text
        assert evaluation.json()["result"]["passed"] == evaluation.json()["result"]["total"] == 1
        missing_revision = client.put(base + f"/bindings/{binding['id']}", headers=headers, json=body)
        assert missing_revision.status_code == 428
        stale_revision = client.put(base + f"/bindings/{binding['id']}", headers={**headers, "If-Match": '"1"'}, json=body)
        assert stale_revision.status_code == 412
        expired = client.put(base + f"/bindings/{binding['id']}", headers={**headers, "If-Match": '"2"'}, json={**body, "state": "approved", "expires_on": "2020-01-01"})
        assert expired.status_code == 200
        assert client.get(base, headers=headers).json()["mappings"][0]["mapping_state"] == "outdated"
        failed = client.post(base + "/golden/evaluate", headers=headers, json={"use_model": False})
        assert failed.json()["result"]["passed"] == 0
        assert client.get(base + "/golden", headers=headers).json()["evaluations"][0]["result"]["passed"] == 0
        foreign = client.post(base + "/bindings", headers=headers, json={**body, "columns": ["foreign.secret.value"]})
        assert foreign.status_code == 422
        assert client.get("/api/workspaces/foreign/grounding", headers=headers).status_code == 404


def test_unresolved_meaning_waits_without_approving_any_plan_and_can_be_rechecked(control_db, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select

    from analystos.api.app import app
    from analystos.core.errors import MeaningRequired
    from analystos.db.models import AnalysisRun, RunTask
    from analystos.runtime import engine

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
        headers = {"Authorization": "Bearer " + login["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Meaning wait"}).json()["id"]
        rid = new_id("run")
        with session_scope() as s:
            s.add(AnalysisRun(id=rid, workspace_id=wid, objective="Explain repeats", requested_by=login["user"]["id"],
                              status="RUNNING", control="run", plan={"steps": []}, plan_version=1, scope={}, origin={}))
            s.flush()
            s.add(RunTask(id=new_id("task"), run_id=rid, key="context", agent_id="context", title="Context", status="COMPLETED", depends_on=[], input={}, output={}, plan_version=1, seq=1))
            s.add(RunTask(id=new_id("task"), run_id=rid, key="hypotheses", agent_id="investigator", title="Hypotheses", status="NEW", depends_on=["context"], input={}, output={}, plan_version=1, seq=2))
        monkeypatch.setattr(engine.RunContext, "load", lambda *a: SimpleNamespace())
        def unresolved(*args):
            raise MeaningRequired("Resolve the remaining meaning", details={"questions": ["Which repeat policy applies?"], "grounding": {"needs_clarification": True}})
        monkeypatch.setattr("analystos.agents.dispatch.dispatch", unresolved)
        result = engine.execute_task(rid, "hypotheses", services=SimpleNamespace())
        assert result["status"] == "WAITING_USER"
        assert engine.get_state(rid).get("waiting_user") == ["hypotheses"]
        resumed = client.post(f"/api/workspaces/{wid}/grounding/runs/{rid}/recheck", headers=headers)
        assert resumed.status_code == 200, resumed.text
        with session_scope() as s:
            tasks = list(s.scalars(select(RunTask).where(RunTask.run_id == rid)))
            assert all(t.status == "NEW" for t in tasks)

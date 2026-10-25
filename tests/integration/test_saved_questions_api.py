"""Saved-evidence questions persist without changing a run or crossing workspace boundaries."""
import pytest
from sqlalchemy import func, select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Insight, ModelCall, QueryExecution

pytestmark = pytest.mark.integration


def test_saved_question_history_and_access_boundaries(control_db, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    def no_model():
        raise AssertionError("saved-evidence questions must not construct a model router")

    monkeypatch.setattr("analystos.services.runs.default_router", no_model)
    with TestClient(app) as client:
        def login(name):
            response = client.post("/api/auth/login", json={"email": f"{name}@analystos.local", "password": "ChangeMe123!"})
            assert response.status_code == 200
            return response.json(), {"Authorization": f"Bearer {response.json()['access_token']}"}

        owner, headers = login("analyst")
        _, viewer = login("approver")
        workspace = client.post("/api/workspaces", headers=headers, json={"name": "Saved question demo"}).json()["id"]
        other = client.post("/api/workspaces", headers=headers, json={"name": "Other workspace"}).json()["id"]
        assert client.post(f"/api/workspaces/{workspace}/members", headers=headers,
                           json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
        run_id, finding_id = new_id("run"), new_id("ins")
        with session_scope() as session:
            session.add(AnalysisRun(id=run_id, workspace_id=workspace, objective="Why did resolution times change?",
                                    status="COMPLETED", requested_by=owner["user"]["id"], scope={}, plan_version=1,
                                    policy_version=1, plan_hash="unchanged"))
            session.add(Insight(id=finding_id, workspace_id=workspace, run_id=run_id, code="F1", title="Time difference",
                                finding="Recorded median is 52.6 hours.", verified=False, caveats=["Synthetic data only"]))
            session.flush()
            before_queries = session.scalar(select(func.count()).select_from(QueryExecution))
            before_models = session.scalar(select(func.count()).select_from(ModelCall))

        path = f"/api/workspaces/{workspace}/analysis/{run_id}"
        response = client.post(path + "/feedback", headers=headers,
                               json={"text": "How was F1 checked?", "kind": "question"})
        assert response.status_code == 200, response.text
        answer = response.json()["answer"]
        assert answer["mode"] == "saved_evidence"
        assert answer["references"][0]["id"] == finding_id
        assert "needs checking" in " ".join(answer["paragraphs"])
        detail = client.get(path, headers=headers).json()
        assert detail["instructions"][-1]["answer"] == answer
        assert (detail["status"], detail["plan_hash"], detail["plan_version"]) == ("COMPLETED", "unchanged", 1)
        for bad_path, bad_headers, body, status in [
            (path, viewer, {"text": "Summarize the findings", "kind": "question"}, 403),
            (f"/api/workspaces/{other}/analysis/{run_id}", headers, {"text": "Summarize the findings", "kind": "question"}, 404),
            (path, headers, {"text": "Explain F1", "kind": "question", "target_id": "foreign_finding"}, 404),
            (path, headers, {"text": " ", "kind": "question"}, 422),
        ]:
            result = client.post(bad_path + "/feedback", headers=bad_headers, json=body)
            assert result.status_code == status, result.text
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(QueryExecution)) == before_queries
            assert session.scalar(select(func.count()).select_from(ModelCall)) == before_models
            assert len(session.get(AnalysisRun, run_id).instructions) == 1

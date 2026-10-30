"""Guided templates, reviewed context, actions and pilot measurements through the API."""
import pytest
from fastapi.testclient import TestClient

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Insight, Source, SourceAsset, SourceColumn, VerificationRecord

pytestmark = pytest.mark.integration


def test_guided_context_actions_and_pilot_are_scoped_and_persisted(control_db, monkeypatch):
    from analystos.api.app import app

    monkeypatch.setattr("analystos.services.dispatch.dispatch", lambda *args, **kwargs: None)
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"})
        assert login.status_code == 200
        user = login.json()["user"]
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Four priorities API check"}).json()["id"]
        base = f"/api/workspaces/{wid}"
        sid, aid, rid, iid = (new_id(prefix) for prefix in ("src", "ast", "run", "ins"))
        with session_scope() as s:
            s.add(Source(id=sid, workspace_id=wid, name="Test incidents", kind="csv", config={}, status="ready", staging_schema="s"))
            s.add(SourceAsset(id=aid, workspace_id=wid, source_id=sid, schema_name="s", name="incident", source_name="incident", kind="table", selected=True))
            s.add(SourceColumn(asset_id=aid, name="made_sla", ordinal=1, data_type="boolean"))
            s.add(SourceColumn(asset_id=aid, name="opened_at", ordinal=2, data_type="timestamp", semantic_type="datetime"))  # the period applies to it
            s.add(AnalysisRun(id=rid, workspace_id=wid, objective="SLA evidence", status="COMPLETED", requested_by=user["id"]))
            s.add(Insight(id=iid, workspace_id=wid, run_id=rid, code="F1", title="SLA association", finding="Recorded test evidence", verified=True))
            s.add(VerificationRecord(id=new_id("vr"), workspace_id=wid, run_id=rid, subject_type="insight", subject_id=iid,
                                     verdict="verified", verifier="test", fingerprint="test", state="ACTIVE"))
        brief = client.get(base + "/brief", headers=headers).json()
        saved = client.patch(base + "/brief", headers={**headers, "If-Match": f'"{brief["version"]}"'}, json={"ops": [
            {"op": "set", "assertion": {"group": "domain", "field": "term", "subject": "SLA", "value": "A breach is made_sla=false"}}]})
        assert saved.status_code == 200, saved.text
        context = client.get(base + "/reviewed-context", headers=headers).json()
        assert context["facts"][0]["subject"] == "SLA"
        templates = client.get(base + "/investigation-templates", headers=headers)
        assert templates.status_code == 200 and len(templates.json()) == 4
        started = client.post(base + "/guided-investigations", headers=headers, json={"template_id": "sla_change",
                "period": {"start": "2026-07-01", "end": "2026-09-30", "label": "Q3 2026"}, "comparison_mode": "custom",
                "baseline": {"start": "2026-04-01", "end": "2026-06-30"}, "decision": "Choose a team to review", "source_ids": [sid]})
        assert started.status_code == 200, started.text
        assert started.json()["origin"]["guided_template"]["version"] == 1
        assert started.json()["origin"]["reviewed_context"]["version"] == saved.json()["version"]
        # Later business edits cannot mutate the run's snapshot.
        edited = client.patch(base + "/brief", headers={**headers, "If-Match": f'"{saved.json()["version"]}"'}, json={"ops": [
            {"op": "set", "assertion": {"group": "domain", "field": "term", "subject": "SLA", "value": "Updated reviewed wording"}}]})
        assert edited.status_code == 200
        pinned = client.get(base + "/analysis/" + started.json()["id"], headers=headers).json()["origin"]["reviewed_context"]
        assert "facts" not in pinned and pinned["fact_count"] == 1
        with session_scope() as session:
            internal = session.get(AnalysisRun, started.json()["id"]).origin["reviewed_context"]
            assert internal["facts"][0]["value"] == "A breach is made_sla=false"
        action_body = {"title": "Review SLA routing", "next_step": "Review the routing evidence with the team", "owner_id": user["id"],
                       "success_measure": "Compare the next breach-rate period"}
        created = client.post(base + f"/findings/{iid}/actions", headers=headers, json=action_body)
        assert created.status_code == 200, created.text
        action = created.json()
        assert client.get(base + "/investigation-actions", headers=headers).json()[0]["verification_state"]["badge"] == "verified"
        done = client.put(base + f"/findings/{iid}/actions/{action['id']}", headers={**headers, "If-Match": '"1"'},
                          json={**action_body, "status": "done", "resolution_note": "Evidence reviewed and follow-up completed"})
        assert done.status_code == 200 and done.json()["revision"] == 2
        stale = client.put(base + f"/findings/{iid}/actions/{action['id']}", headers={**headers, "If-Match": '"1"'}, json=action_body)
        assert stale.status_code == 412
        pilot = {"run_id": rid, "evidence_kind": "rehearsal", "task": "API contract check with illustrative timings",
                 "baseline_minutes": 60, "assisted_minutes": 20, "accuracy": "not_reviewed", "usefulness": 3,
                 "would_use_again": False, "notes": "Synthetic automated test, not a real pilot observation"}
        response = client.post(base + "/pilot-reviews", headers=headers, json=pilot)
        assert response.status_code == 200, response.text
        assert response.json()["reviewer_id"] == user["id"]
        result = client.get(base + "/pilot-reviews", headers=headers).json()
        assert result["summary"]["real_pilot"]["assessments"] == 0
        assert result["summary"]["rehearsal"]["minutes_saved"] == 40
        assert result["summary"]["rehearsal"]["correct_fraction"] is None
        duplicate = client.post(base + "/pilot-reviews", headers=headers, json=pilot)
        assert duplicate.status_code == 409
        foreign = client.get("/api/workspaces/foreign/investigation-actions", headers=headers)
        assert foreign.status_code == 404

"""A review batch is scored against golden questions in a savepoint before it is approved (review P0-1)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import (
    ContextEntry,
    GoldenQuestion,
    KnowledgeSuggestion,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    VerifiedQuery,
)

pytestmark = pytest.mark.integration

QUESTION = "Show recurrence incidents by priority"


def test_preview_scores_the_batch_without_applying_it_then_review_reports_the_effect(control_db):
    from analystos.api.app import app
    from analystos.contracts.grounding import ResolveIn
    from analystos.knowledge import suggestions
    from analystos.registries import verified_queries
    from analystos.services import context_grounding

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
        headers = {"Authorization": "Bearer " + login["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Golden effect"}).json()["id"]
        sid, aid, vid = new_id("src"), new_id("ast"), new_id("vq")
        with session_scope() as s:
            s.add(Source(id=sid, workspace_id=wid, name="Incidents", kind="csv", config={}, status="ready", staging_schema="s"))
            s.add(SourceAsset(id=aid, workspace_id=wid, source_id=sid, schema_name="s", name="incident", source_name="incident",
                              kind="table", selected=True))
            s.add(SourceColumn(asset_id=aid, name="priority", ordinal=1, data_type="text", semantic_type="category"))
            s.add(SourceColumn(asset_id=aid, name="repeat_flag", ordinal=2, data_type="boolean", semantic_type="boolean"))
            s.add(VerifiedQuery(id=vid, workspace_id=wid, name="Repeat incidents by priority", patterns=["repeat incidents by priority"],
                                sql_template="SELECT priority, COUNT(*) AS incidents FROM s.incident WHERE repeat_flag GROUP BY priority",
                                parameters=[], source_id=sid, created_by=login["user"]["id"]))
        with session_scope() as s:
            user = s.get(User, login["user"]["id"])
            report = context_grounding.resolve(s, user, wid, ResolveIn(question=QUESTION, use_model=False))
            # The grounding part is pinned to what resolves today (the domain pack maps "priority"); only the
            # verified-query part depends on the batch.
            grounded = sorted({c for r in report["resolutions"] if r["state"] == "resolved" and r.get("by") != "catalog"
                               for c in r["columns"]})
            s.add(GoldenQuestion(id=new_id("gold"), workspace_id=wid, created_by=user.id,
                                 definition={"question": QUESTION, "expected_columns": grounded, "expected_query": vid,
                                             "expect_clarification": bool(report["needs_clarification"]), "notes": ""}))
            draft = suggestions.propose(s, wid, kind="glossary_term", subject="term:repeat", title="Repeat",
                                        fields={"name": suggestions.field("Repeat", 0.9, source="test"),
                                                "body": suggestions.field("An incident reopened within the repeat window", 0.9, source="test"),
                                                "synonyms": suggestions.field(["recurrence"], 0.9, source="test"),
                                                "mapped_columns": suggestions.field([], 0.9, source="test")},
                                        origin="crawler", proposed_by="test")
            draft_id = draft.id
        decisions = [{"id": draft_id, "action": "approve"}]

        preview = client.post(f"/api/workspaces/{wid}/knowledge/suggestions/preview", headers=headers, json={"decisions": decisions})
        assert preview.status_code == 200, preview.text
        effect = preview.json()
        assert effect["evaluated"] is True, effect
        assert effect["counts"]["fixed"] == 1 and effect["counts"]["newly_failing"] == 0, effect["cases"]
        assert effect["cases"][0]["served_before"] is None and effect["cases"][0]["served_after"] == vid
        assert effect["delta_points"] == 100.0

        with session_scope() as s:  # the preview changed nothing, and left no unapproved synonym cached for Ask
            assert s.get(KnowledgeSuggestion, draft_id).status == "pending"
            assert not s.scalars(select(ContextEntry).where(ContextEntry.workspace_id == wid, ContextEntry.kind == "term")).all()
            assert verified_queries.match(s, wid, QUESTION) is None

        reviewed = client.post(f"/api/workspaces/{wid}/knowledge/suggestions/review", headers=headers, json={"decisions": decisions})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["effect"]["counts"]["fixed"] == 1
        assert [a["id"] for a in reviewed.json()["approved"]] == [draft_id]
        with session_scope() as s:
            assert s.get(KnowledgeSuggestion, draft_id).status == "approved"
            hit = verified_queries.match(s, wid, QUESTION)
            assert hit is not None and hit.entry.id == vid

        evaluation = client.post(f"/api/workspaces/{wid}/grounding/golden/evaluate", headers=headers, json={"use_model": False})
        assert evaluation.status_code == 200, evaluation.text
        assert evaluation.json()["result"]["passed"] == evaluation.json()["result"]["total"] == 1
        assert evaluation.json()["result"]["cases"][0]["served_query"] == vid


def test_golden_question_must_name_a_verified_query_of_its_own_workspace(control_db):
    from analystos.api.app import app

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
        headers = {"Authorization": "Bearer " + login["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Golden query ref"}).json()["id"]
        bad = client.post(f"/api/workspaces/{wid}/grounding/golden", headers=headers,
                          json={"question": "Show recurrence incidents by priority", "expected_query": "vq_missing"})
        assert bad.status_code == 422, bad.text

"""PILOT-FIX-04 on Postgres: the meaning review asks once, the owner answers, the recheck records the round on the
run, and round 2 asks nothing new; absent data is a limitation the run's findings carry (C11, 2026-10-30)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn

pytestmark = pytest.mark.integration

QUESTION = ("Why are some orders returned, and are late deliveries part of the reason? Compare categories, sales channels "
            "and regions; say what the data cannot show.")
TABLES = {"orders": [("order_id", "text", "id"), ("order_date", "date", "datetime"), ("promised_date", "date", "datetime"),
                     ("delivered_date", "date", "datetime"), ("sales_channel", "text", "categorical"),
                     ("returned", "text", "categorical"), ("customer_id", "text", "id"), ("product_id", "text", "id")],
          "customers": [("customer_id", "text", "id"), ("region", "text", "categorical")],
          "products": [("product_id", "text", "id"), ("category", "text", "categorical")]}


def test_meaning_review_converges_in_one_owner_round_and_records_it_on_the_run(control_db, monkeypatch):
    from analystos.agents import common
    from analystos.api.app import app
    from analystos.context.compiler import CompiledContext
    from analystos.contracts.grounding import ResolveIn
    from analystos.core.errors import MeaningRequired
    from analystos.db.models import AnalysisRun, Artifact, RunTask, User
    from analystos.runtime import engine
    from analystos.services import context_grounding as svc
    from analystos.services import meaning_review as mr

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
        user_id = login["user"]["id"]
        headers = {"Authorization": "Bearer " + login["access_token"]}
        wid = client.post("/api/workspaces", headers=headers, json={"name": "Retail meaning review"}).json()["id"]
        sid, rid = new_id("src"), new_id("run")
        with session_scope() as s:
            s.add(Source(id=sid, workspace_id=wid, name="Retail", kind="csv", config={}, status="ready", staging_schema="r"))
            for table, cols in TABLES.items():
                aid = new_id("ast")
                s.add(SourceAsset(id=aid, workspace_id=wid, source_id=sid, schema_name="r", name=table, source_name=table,
                                  kind="table", selected=True))
                for n, (name, dtype, sem) in enumerate(cols):
                    s.add(SourceColumn(asset_id=aid, name=name, ordinal=n, data_type=dtype, semantic_type=sem,
                                       business_name=name.replace("_", " ").title()))
            s.add(AnalysisRun(id=rid, workspace_id=wid, objective=QUESTION, requested_by=user_id, status="RUNNING", control="run",
                              plan={"steps": [], "questions": ["Do return rates differ by sales channel?"]}, plan_version=1,
                              scope={}, origin={}))
            s.flush()
            s.add(RunTask(id=new_id("task"), run_id=rid, key="context", agent_id="context", title="Context", status="COMPLETED",
                          depends_on=[], input={}, output={}, plan_version=1, seq=1))
            s.add(RunTask(id=new_id("task"), run_id=rid, key="hypotheses", agent_id="investigator", title="Hypotheses",
                          status="NEW", depends_on=["context"], input={}, output={}, plan_version=1, seq=2))

        answers = iter([
            {"open_terms": [{"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable",
                             "columns": ["r.orders.promised_date", "r.orders.delivered_date"]},
                            {"term": "return reason", "question": "Which field holds the return reason?", "availability": "absent"},
                            {"term": "sales channel", "question": "What is a sales channel?", "availability": "in_data"}]},
            # round 2: the model raises a term nobody's answer caused; it is recorded, not asked
            {"open_terms": [{"term": "shipping lead time", "question": "How is lead time measured?", "availability": "derivable"},
                            {"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable"}]},
        ])
        monkeypatch.setattr(common, "compile_for", lambda *a, **k: CompiledContext(purpose="glossary_suggestion", header={}, body={}))
        monkeypatch.setattr(common, "llm_json", lambda *a, **k: (next(answers), "stub-model"))
        router = SimpleNamespace(mode=lambda p: "auto", available=lambda *a: True)

        def hypotheses_step(*args):
            with session_scope() as s:
                run = s.get(AnalysisRun, rid)
                report = svc.resolve(s, s.get(User, user_id), wid, ResolveIn(question=QUESTION), router=router,
                                     call_ctx=SimpleNamespace(), agent_ctx=SimpleNamespace(run=SimpleNamespace(id=rid, plan=run.plan)))
            if report["needs_clarification"]:
                raise MeaningRequired("Review the remaining business meaning", details={"questions": report["clarification"],
                                                                                         "grounding": report})
            return {"grounding": report}

        monkeypatch.setattr(engine.RunContext, "load", lambda *a: SimpleNamespace())
        monkeypatch.setattr("analystos.agents.dispatch.dispatch", hypotheses_step)

        # round 1: one question, asked once; the plain fields and the missing reasons are not questions
        assert engine.execute_task(rid, "hypotheses", services=SimpleNamespace())["status"] == "WAITING_USER"
        with session_scope() as s:
            waiting = s.scalar(select(RunTask).where(RunTask.run_id == rid, RunTask.key == "hypotheses"))
            assert waiting.output["meaning_questions"] == ["What counts as a late delivery?"]
            review = waiting.output["grounding"]["meaning_review"]
            assert [lim["term"] for lim in review["limitations"] if lim["kind"] == "absent_data"] == ["return reason"]
            assert {"sales channel", "region", "category"} <= {r["term"].lower() for r in review["resolved_without_asking"]}

        # the owner answers the one question
        body = {"term": "Late delivery", "definition": "An order delivered after its promised date",
                "columns": ["r.orders.delivered_date", "r.orders.promised_date"], "owner_id": user_id, "state": "approved",
                "evidence": "Retail delivery policy", "examples": "Promised 3 May, delivered 5 May is late"}
        saved = client.post(f"/api/workspaces/{wid}/grounding/bindings", headers=headers, json=body)
        assert saved.status_code == 200, saved.text
        binding_id = saved.json()["id"]
        rechecked = client.post(f"/api/workspaces/{wid}/grounding/runs/{rid}/recheck", headers=headers)
        assert rechecked.status_code == 200, rechecked.text
        with session_scope() as s:
            rounds = list(s.scalars(select(Artifact).where(Artifact.run_id == rid, Artifact.name.like("Meaning review round%"))))
            assert [a.name for a in rounds] == ["Meaning review round 1"]
            assert rounds[0].content["asked"] == [mr.key_of("late delivery")] and rounds[0].content["owner_minutes"] >= 0
            assert rounds[0].content["binding_revisions"] == {}

        # round 2 passes: nothing new is asked, the answer is pinned with its revision
        assert engine.execute_task(rid, "hypotheses", services=SimpleNamespace())["status"] == "COMPLETED"
        with session_scope() as s:
            done = s.scalar(select(RunTask).where(RunTask.run_id == rid, RunTask.key == "hypotheses"))
            review = done.output["grounding"]["meaning_review"]
            assert review["round"] == 2 and review["questions"] == []
            assert [q["term"] for q in review["not_asked"]] == ["shipping lead time"]
            assert review["pinned"] == [{"id": binding_id, "term": "Late delivery", "revision": 1}]
            assert review["answered"] == ["Late delivery"]
        assert mr.finding_caveats(rid) == [
            "Data limitation: No field in the selected data records return reason. Findings can show what goes with the "
            "outcome, but not return reason."]
        explained = client.get(f"/api/workspaces/{wid}/grounding/runs/{rid}", headers=headers)
        assert explained.status_code == 200, explained.text
        assert [r["round"] for r in explained.json()["rounds"]] == [1]
        assert explained.json()["grounding"]["meaning_review"]["round"] == 2

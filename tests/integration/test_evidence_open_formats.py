"""P4-K04 on a real run: the v1 flow on the local orchestrator (ServiceNow mock, no model provider,
preview publication), then its evidence in open formats, through the HTTP API:

* every verified finding is an OKF Attested Computation carrying the query hash, result hash,
  q-value, effect size, verified_by and stale_after of the rows the REV actually checked;
* the published dataset has an ODCS v3.2.0 contract in the workspace pack that validates against the
  pinned upstream schema;
* every governed query of the run has OpenLineage START/COMPLETE events that validate against the
  pinned OpenLineage 2-0-2 and facet schemas.

Set ANALYSTOS_EVIDENCE_OUT to a path to write the run's samples (one document, contract, events).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import timedelta

import pytest
import uvicorn
import yaml
from sqlalchemy import select

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
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _wait(run_id: str, statuses: set[str], timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in statuses:
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def test_run_evidence_in_open_formats(control_db, servicenow_url, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # the preview destination
    monkeypatch.setenv("ANALYSTOS_GRAPH_ENABLED", "false")
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from fastapi.testclient import TestClient

        from analystos.api.app import app
        from analystos.db.base import session_scope
        from analystos.db.models import (
            Approval,
            Experiment,
            Insight,
            KnowledgeDocument,
            LineageEdge,
            QueryExecution,
            User,
        )
        from analystos.evidence.schemas import validate_odcs, validate_openlineage
        from analystos.governance.approvals import decide
        from analystos.knowledge import okf, store
        from analystos.knowledge.attested import check_attested, finding_path, sql_hash, validate
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace
        from analystos.workflows.orchestrator import run_local

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="K04 evidence", objective="Find the drivers of SLA breaches in IT incidents",
                                  policy={"require_approved_metrics": False})
            s.flush()
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident", "change_request"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id, admin_email = ws.id, src.id, admin.email
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None)
        assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
        with session_scope() as s:
            approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            decide(s, approval.id, s.scalar(select(User).where(User.email == "approver@analystos.local")), approve=True)
        assert run_local(run.id) == "COMPLETED"

        with TestClient(app) as api:
            r = api.post("/api/auth/login", json={"email": admin_email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

            # ---- findings as Attested Computations
            with session_scope() as s:
                verified = [i.id for i in s.scalars(select(Insight).where(Insight.run_id == run.id, Insight.status == "verified"))]
                unverified = s.scalar(select(Insight.id).where(Insight.run_id == run.id, Insight.status != "verified"))
            assert verified
            for insight_id in verified:
                r = api.get(f"/api/insights/{insight_id}/attested", headers=auth)
                assert r.status_code == 200, r.text
                body = r.json()
                fm = body["frontmatter"]
                assert check_attested(fm) == []
                doc = okf.parse_document(body["path"], body["document"].encode())
                assert doc.type == "Attested Computation" and doc.trust_tier == "machine-confirmed"
                att = fm["analystos"]["attestation"]
                with session_scope() as s:
                    ins = s.get(Insight, insight_id)
                    exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == ins.hypothesis_id,
                                                            Experiment.role == "primary"))
                    q = s.get(QueryExecution, exp.query_ids[0])
                    assert att["query_hash"] == sql_hash(q.executed_sql or q.sql) and att["result_hash"] == q.result_hash
                    assert att["queries"][0]["fingerprint"] == q.fingerprint and validate(doc) == []
                    assert att["q_value"] == exp.result.get("p_adjusted", exp.result.get("p_value"))
                    assert att["effect_size"] == exp.result.get("effect_size") and att["run_id"] == run.id
                    assert att["plan_hash"] and len(att["spec_hash"]) == 64
                    assert att["verified_by"][0]["by"] == "process:analystos-rev"
                    verified_at = okf.parse_instant(att["verified_by"][0]["at"])
                    assert okf.parse_instant(fm["stale_after"]) == verified_at + timedelta(days=90)
                    assert {"reproducible_rerun", "second_method"} <= {c["check"] for c in att["checks"] if c["passed"]}
            if unverified:
                assert api.get(f"/api/insights/{unverified}/attested", headers=auth).status_code == 422

            r = api.post(f"/api/workspaces/{ws_id}/analysis/{run.id}/findings/attest", headers=auth)
            assert r.status_code == 200, r.text
            assert sorted(r.json()["written"]) == sorted(finding_path(i) for i in verified)
            with session_scope() as s:
                pack = store.workspace_pack(s, ws_id)
                files = store.revision_files(s, pack)
                assert okf.check_conformance(files) == []
                docs = list(s.scalars(select(KnowledgeDocument).where(KnowledgeDocument.pack_id == pack.id,
                                                                      KnowledgeDocument.path.like("findings/%"))))
                assert {d.type for d in docs} == {"Attested Computation"} and len(docs) == len(verified)
                assert s.scalar(select(LineageEdge).where(LineageEdge.workspace_id == ws_id, LineageEdge.relation == "attested_as"))
            again = api.post(f"/api/workspaces/{ws_id}/analysis/{run.id}/findings/attest", headers=auth).json()
            assert again["written"] == [] and len(again["unchanged"]) == len(verified)

            # ---- ODCS contract of the published dataset
            r = api.get(f"/api/workspaces/{ws_id}/contracts", headers=auth)
            assert r.status_code == 200, r.text
            contracts = r.json()
            assert contracts, "the published dataset has no ODCS contract"
            for c in contracts:
                assert validate_odcs(c["contract"]) == []
                assert c["contract"]["apiVersion"] == "v3.2.0" and c["version"] == "1.0.0"
                assert yaml.safe_load(files[c["path"]]) == c["contract"]
                props = c["contract"]["schema"][0]["properties"]
                assert any(p.get("semanticType") == "measure" for p in props)
                assert {p["name"] for p in props if p.get("semanticType") == "column"}

            # ---- OpenLineage events for the run's governed queries
            r = api.get(f"/api/workspaces/{ws_id}/analysis/{run.id}/openlineage", headers=auth)
            assert r.status_code == 200, r.text
            events = r.json()
            with session_scope() as s:
                ran = list(s.scalars(select(QueryExecution).where(QueryExecution.run_id == run.id,
                                                                  QueryExecution.status != "rejected")))
            assert len(events) == 2 * len(ran) > 0
            for e in events:
                assert validate_openlineage(e) == [], (e["eventType"], validate_openlineage(e)[:3])
            by_run: dict[str, list[str]] = {}
            for e in events:
                by_run.setdefault(e["run"]["runId"], []).append(e["eventType"])
            assert all(types[0] == "START" and types[1] in ("COMPLETE", "FAIL") for types in by_run.values())
            completes = [e for e in events if e["eventType"] == "COMPLETE"]
            executed = {q.executed_sql for q in ran if q.status == "ok"}
            assert {e["job"]["facets"]["sql"]["query"] for e in completes} <= executed
            assert all(e["run"]["facets"]["parent"]["job"]["name"] == f"analysis_run.{run.id}" for e in events)
            assert any(e["inputs"] for e in completes)
            assert api.get(f"/api/workspaces/{ws_id}/analysis/{run.id}/openlineage").status_code == 401
            out = os.environ.get("ANALYSTOS_EVIDENCE_OUT")
            if out:  # the live run's samples for the dated evidence file
                with open(out, "w") as fh:
                    json.dump({"run_id": run.id, "verified_findings": len(verified), "attested_document": body["document"],
                               "contracts": [{k: c[k] for k in ("path", "version")} for c in contracts],
                               "contract": contracts[0]["contract"], "queries": len(ran), "openlineage_events": len(events),
                               "openlineage_sample": [e for e in events if e["run"]["runId"] == completes[0]["run"]["runId"]]},
                              fh, indent=2, default=str)
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()

"""S07 on Postgres under strict tenancy: a full `investigate.v1` run with no model (deterministic paths) passes its
investigation gates, labels every finding, serves each one as an Evidence v2 object, publishes to the preview
destination; then a policy change (a fingerprint component) voids the findings, the published dashboards show a
banner and a Request Desk task is escalated to a person. Another workspace's user and another tenant see nothing."""
from __future__ import annotations

import socket
import threading
import time

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servicenow_url():
    import uvicorn

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


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_gates_labels_evidence_and_void_banner(control_db, servicenow_url, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # the preview destination, never a live Superset
    monkeypatch.setenv("ANALYSTOS_GRAPH_ENABLED", "false")
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import Approval, Artifact, Insight, RunEvent, RunTask, User
        from analystos.desk.models import Request, RequestEvent
        from analystos.evidence.claims import LABELS
        from analystos.governance.approvals import decide
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace
        from analystos.tenancy import tenant_scope
        from analystos.workflows.orchestrator import run_local

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="s07 evidence", objective="Find the drivers of SLA breaches in IT incidents",
                                  policy={"require_approved_metrics": False})
            s.flush()
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident"])
        run = create_run(admin, ws_id, objective=None)
        assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
        with session_scope() as s:
            approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            decide(s, approval.id, approver, approve=True)
        assert run_local(run.id) == "COMPLETED"

        # ---- T5: every gate ran after its step, in the analyst's order; the blocking ones passed
        with session_scope() as s:
            outputs = {t.key: t.output or {} for t in s.scalars(select(RunTask).where(RunTask.run_id == run.id))}
            gates = {k: [(c["check"], c["passed"], c["on_fail"]) for c in o.get("checks") or []] for k, o in outputs.items()
                     if o.get("checks")}
            assert [c for c, _, _ in gates["profile"]] == ["baseline"] and [c for c, _, _ in gates["hypotheses"]] == ["hypotheses"]
            assert [c for c, _, _ in gates["insights"]] == ["segmentation", "statistics"]
            assert [c for c, _, _ in gates["verify"]] == ["alternatives", "conclusions"]
            assert all(passed for k in gates for c, passed, on_fail in gates[k] if on_fail == "block")
            checks = [e.payload for e in s.scalars(select(RunEvent).where(RunEvent.run_id == run.id,
                                                                          RunEvent.type == "investigation.check"))]
            assert len(checks) == 6
            # ---- T3: every concluded finding carries a label decided from its verdict
            insights = list(s.scalars(select(Insight).where(Insight.run_id == run.id)))
            assert insights and all(i.claim_label in LABELS for i in insights)
            verified = [i for i in insights if i.status == "verified"]
            assert verified and all(i.claim_label in ("observed", "supported_association", "possible_contributor")
                                    for i in verified)
            assert all(i.claim_label in ("rejected", "unknown") for i in insights if i.status == "failed_verification")
            dashboards = [a for a in s.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "dashboard"))]
            assert dashboards and all(a.status == "published" and a.banner is None for a in dashboards)

        # ---- T4: the Evidence v2 objects, through the API, and only for those who may see them
        with TestClient(app) as api:
            headers = _login(api, "admin@analystos.local")
            ev = api.get(f"/v2/runs/{run.id}/evidence", headers=headers).json()["evidence"]
            assert {e["evidence_id"] for e in ev} == {i.id for i in insights}
            one = api.get(f"/v2/evidence/{verified[0].id}", headers=headers)
            assert one.status_code == 200, one.text
            body = one.json()
            assert body["label"] == verified[0].claim_label and body["review_status"] == "verified"
            assert body["fingerprint"] and body["query_execution_ids"] and body["dataset_versions"]
            assert body["claim_id"].startswith("clm_") and body["sample"]["n"] > 0 and body["void_reason"] is None
            assert any(x.startswith("statistics: Benjamini-Hochberg") for x in body["limitations"])
            outsider = _login(api, "analyst@analystos.local")  # not a member of this workspace
            assert api.get(f"/v2/evidence/{verified[0].id}", headers=outsider).status_code in (403, 404)

            # ---- T7: a verdict-relevant policy change voids the findings; dashboards get a banner + a Desk task
            r = api.put(f"/api/workspaces/{ws_id}/policy", headers=headers, json={"alpha": 0.01})  # merged into the policy
            assert r.status_code == 200, r.text
            after = api.get(f"/v2/evidence/{verified[0].id}", headers=headers).json()
            assert after["review_status"] == "void" and "workspace policy" in after["void_reason"]
            arts = api.get(f"/api/workspaces/{ws_id}/artifacts", headers=headers, params={"run_id": run.id}).json()
            presenting = {a["name"] for a in arts if a["type"] == "chart" and (a["content"] or {}).get("insight_codes")}
            dash = [a for a in arts if a["type"] == "dashboard" and a["status"] == "published"]
            for a in dash:  # a banner exactly where a chart presents a (now void) finding
                assert bool(a["banner"]) == bool(set((a["content"] or {}).get("charts") or []) & presenting), a["name"]
            assert any(a["banner"] for a in dash)
            banners = [a["banner"] for a in arts if a.get("banner")]  # bannered dashboards and charts
            assert all(a["status"] == "published" for a in arts if a.get("banner"))
            assert all(b["state"] == "void" and b["request_id"] for b in banners), banners
            queue = api.get("/api/requests", headers=headers, params={"view": "all", "workspace_id": ws_id}).json()
            void_tasks = [q for q in queue if (q.get("triage") or {}).get("flags") == ["void_banner"]]
            assert {q["id"] for q in void_tasks} == {b["request_id"] for b in banners}

        with session_scope() as s:
            for req in s.scalars(select(Request).where(Request.id.in_([b["request_id"] for b in banners]))):
                assert (req.state, req.channel, req.priority, req.request_class) == ("escalated", "system", "high", "R9")
                assert req.sla_due_at is not None and req.data_pack["findings"] and "re-verify" in req.text.lower()
                types = [e.type for e in s.scalars(select(RequestEvent).where(RequestEvent.request_id == req.id)
                                                   .order_by(RequestEvent.seq))]
                assert types[:3] == ["created", "triaged", "escalated"]
            assert s.scalars(select(RunEvent).where(RunEvent.workspace_id == ws_id,
                                                    RunEvent.type == "publication.void_banner")).first() is not None
        # strict tenancy: another tenant sees neither the findings, the banners nor the Desk tasks
        with tenant_scope("tnt_s07_other"), session_scope() as s:
            assert s.get(Insight, verified[0].id) is None
            assert s.scalars(select(Artifact).where(Artifact.workspace_id == ws_id)).first() is None
            assert s.scalars(select(Request).where(Request.workspace_id == ws_id)).first() is None
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()

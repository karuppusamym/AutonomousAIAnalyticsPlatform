"""The MVP flow (spec v1 §62) on the local orchestrator with no model provider: every agent must
degrade to its labelled deterministic path and still produce verified, evidence-backed output.
Publishes to the preview destination when Superset is unreachable (as in CI)."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

pytestmark = pytest.mark.integration


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


def _wait_schedule_run(srun_id: str, timeout: float = 120) -> None:
    """The run reaches its terminal state first; complete_from_run then finishes the schedule_run (report,
    diff) in its own transaction, so a reader can see the run COMPLETED before the schedule_run succeeds."""
    from analystos.db.base import session_scope
    from analystos.db.models import ScheduleRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            if s.get(ScheduleRun, srun_id).status in ("succeeded", "failed", "skipped"):
                return
        time.sleep(0.5)
    raise AssertionError(f"schedule run {srun_id} did not finish")


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


def test_full_run_without_models(control_db, servicenow_url, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SERVICENOW_PASSWORD", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # force the preview destination
    # P4-S03: the whole flow runs without Neo4j. The graph is off (the default) and even its URI is dead.
    monkeypatch.setenv("ANALYSTOS_GRAPH_ENABLED", "false")
    monkeypatch.setenv("ANALYSTOS_NEO4J_URI", f"bolt://127.0.0.1:{_free_port()}")
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import AnalysisRun, Approval, Artifact, Insight, QueryExecution, User
        from analystos.governance.approvals import decide
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="e2e local", objective="Find the drivers of SLA breaches in IT incidents",
                                  policy={"require_approved_metrics": False})  # the gate: test_semantic_layer.py
            s.flush()
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident", "change_request"]},
                                  secret_ref="env:SERVICENOW_PASSWORD")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None)
        assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"

        with session_scope() as s:
            approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            assert approval is not None and approval.destination == "preview"
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            decide(s, approval.id, approver, approve=True)
        from analystos.workflows.orchestrator import run_local

        assert run_local(run.id) == "COMPLETED"

        with session_scope() as s:
            r = s.get(AnalysisRun, run.id)
            verified = list(s.scalars(select(Insight).where(Insight.run_id == run.id, Insight.status == "verified")))
            arts = list(s.scalars(select(Artifact).where(Artifact.run_id == run.id)))
            queries = s.scalar(select(QueryExecution.id).where(QueryExecution.run_id == run.id).limit(1))
            by_type: dict[str, list] = {}
            for a in arts:
                by_type.setdefault(a.type, []).append(a)
            assert len(verified) >= 3, [i.title for i in verified]
            assert all(i.verification["verify"]["reproducible"] for i in verified)
            assert all(any(e["type"] == "query" for e in i.evidence) for i in verified)
            assert by_type.get("dataset")
            assert len(by_type.get("metric", [])) >= 3
            assert len(by_type.get("chart", [])) >= 5
            assert {"executive", "operational", "topic_activity"} <= {a.name for a in by_type.get("dashboard", [])}
            assert all(a.status == "published" and a.platform == "preview" for a in by_type["dashboard"])
            assert queries is not None
            assert r.summary.get("published") is True
            assert r.summary["graph_projection"].get("skipped") is True
            # Context packages still carry the table neighbourhood, now from the Postgres lineage graph.
            from analystos.context.service import build_context_package

            package = build_context_package(s, ws_id, "SLA breaches", r.scope["assets"])
            assert package["graph"], "no neighbourhood without Neo4j"
            assert {g["table"] for g in package["graph"]} <= set(r.scope["assets"])
            assert {"dataset", "table"} & {g["type"] for g in package["graph"]}
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()


def test_phase3_schedule_monitor_report(control_db, servicenow_url, monkeypatch):
    """Scheduled re-analysis with diff + report, monitors -> alerts -> investigation, idempotent firing."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SERVICENOW_PASSWORD", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import Alert, AnalysisRun, Artifact, Monitor, Notification, ScheduleRun, User
        from analystos.services import monitors as mon_svc
        from analystos.services import schedules as sch_svc
        from analystos.services.reports import generate_report, report_file
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace
        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="phase3", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                                  secret_ref="env:SERVICENOW_PASSWORD")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident"])
        baseline = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
        assert _wait(baseline.id, {"COMPLETED", "FAILED"}) == "COMPLETED"

        # On-demand report in every format, stored with a verified hash
        with session_scope() as s:
            art = generate_report(s, baseline.id, kind="operational", formats=("md", "html", "pdf", "xlsx"), actor="user:test")
            art_id = art.id
        with session_scope() as s:
            art = s.get(Artifact, art_id)
            pdf, mime, _ = report_file(art, "pdf")
            assert pdf.startswith(b"%PDF") and mime == "application/pdf"
            assert report_file(art, "xlsx")[0][:2] == b"PK"

        # Scheduled re-analysis: run-now twice through the same code path as cron; diff vs baseline
        with session_scope() as s:
            sch = sch_svc.create_schedule(s, s.merge(admin), ws_id, name="weekly", kind="reanalysis", cron="0 7 * * 1",
                                          config={"baseline_run_id": baseline.id, "refresh_first": True,
                                                  "report": {"kind": "weekly_summary", "formats": ["html", "pdf"]}})
            s.flush()
            sch_id = sch.id
        srun_id = sch_svc.run_now(admin, sch_id)
        with session_scope() as s:
            rerun_id = s.get(ScheduleRun, srun_id).result["run_id"]
        assert _wait(rerun_id, {"COMPLETED", "FAILED"}) == "COMPLETED"
        _wait_schedule_run(srun_id)
        with session_scope() as s:
            srun = s.get(ScheduleRun, srun_id)
            rerun = s.get(AnalysisRun, rerun_id)
            assert srun.status == "succeeded", srun.error
            changes = rerun.summary["changes"]
            assert changes["previous_run_id"] == baseline.id
            # Same data: every previous finding is re-tested with the same spec and still holds.
            assert len(changes["persisting"]) + len(changes["changed"]) >= 1
            assert changes["resolved"] == [] and changes["not_retested"] == [], changes
            assert all(m["previous_value"] == m["value"] for m in changes["metrics"] if m["previous_value"] is not None)
            assert rerun.summary.get("report_artifact_id")
            from analystos.db.models import RunTask

            publish_tasks = s.scalars(select(RunTask).where(RunTask.run_id == rerun_id, RunTask.key.in_(["publish_request", "publish"])))
            assert {t.status for t in publish_tasks} == {"SKIPPED"}  # scheduled runs do not propose publication
            assert s.scalar(select(Notification).where(Notification.workspace_id == ws_id, Notification.kind == "report"))

        # Cron claim is idempotent: the same due slot cannot produce two firings
        from datetime import timedelta

        from analystos.core.ids import utcnow
        with session_scope() as s:
            s.get(__import__("analystos.db.models", fromlist=["Schedule"]).Schedule, sch_id).next_run_at = utcnow() - timedelta(minutes=1)
        first, second = sch_svc.claim_due(), sch_svc.claim_due()
        assert len(first) == 1 and second == []

        # Monitors: drift + change point on weekly volume, a threshold that must fire, and data quality
        with session_scope() as s:
            a = s.merge(admin)
            m_thr = mon_svc.create_monitor(s, a, ws_id, name="Volume above 1", kind="metric_threshold",
                                           config={"metric": "record_count", "grain": "month", "op": ">", "value": 1},
                                           auto_investigate=True)
            m_cp = mon_svc.create_monitor(s, a, ws_id, name="Volume regime", kind="change_point",
                                          config={"metric": "record_count", "grain": "week", "recent_periods": 60})
            m_dq = mon_svc.create_monitor(s, a, ws_id, name="Incident DQ", kind="data_quality", config={})
            s.flush()
            thr_id, cp_id, dq_id = m_thr.id, m_cp.id, m_dq.id
        r = mon_svc.evaluate_monitor(thr_id)
        assert r["alert"] and r["alert_id"]
        again = mon_svc.evaluate_monitor(thr_id)
        assert again["alert_id"] == r["alert_id"]  # de-duplicated
        cp = mon_svc.evaluate_monitor(cp_id)
        assert cp["points"] >= 40 and "message" in cp
        dq = mon_svc.evaluate_monitor(dq_id)
        assert dq.get("baseline_set") and not dq["alert"]
        with session_scope() as s:
            alert = s.get(Alert, r["alert_id"])
            assert alert.investigation_run_id  # auto-investigation allowed at autonomy 3
            inv = s.get(AnalysisRun, alert.investigation_run_id)
            assert inv.origin["type"] == "alert" and inv.origin["publish"] == "skip"
            assert s.get(Monitor, thr_id).state == "alerting"
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()


def test_replan_after_visualize_supersedes_bundle_artifacts(control_db, servicenow_url, monkeypatch):
    """P4-C07: a redirect after `visualize` must not let charts, metrics or dashboards of the
    superseded plan version into the publish bundle. The oracle is the event log: every bundle
    entry must have been (re)created after the replan."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SERVICENOW_PASSWORD", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.artifacts.registry import save_artifact
        from analystos.db.base import session_scope
        from analystos.db.models import AnalysisRun, Approval, RunEvent, RunTask, User
        from analystos.services.runs import create_run, submit_feedback
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="replan", objective="Find the drivers of SLA breaches in IT incidents",
                                  policy={"require_approved_metrics": False})  # the gate: test_semantic_layer.py
            s.flush()
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                                  secret_ref="env:SERVICENOW_PASSWORD")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident"])
        run = create_run(admin, ws_id, objective=None)
        assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
        with session_scope() as s:
            assert s.scalar(select(RunTask.status).where(RunTask.run_id == run.id, RunTask.key == "visualize")) == "COMPLETED"
            first = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            first_id = first.id
            # Plan-v1 outputs the redirected plan will not reproduce (e.g. a finding that no longer holds).
            v1_chart, v1_metric = first.payload["charts"][0], first.payload["metrics"][0]
            save_artifact(s, workspace_id=ws_id, run_id=run.id, type_="chart", name="finding_stale_v1",
                          content={**v1_chart, "key": "finding_stale_v1"})
            save_artifact(s, workspace_id=ws_id, run_id=run.id, type_="metric", name="stale_v1_metric",
                          content={**v1_metric, "name": "stale_v1_metric"}, status="validated")

        out = submit_feedback(admin, run.id, text="Focus only on Network Operations incidents", kind="redirect")
        assert out["replan"]["plan_version"] == 2
        with session_scope() as s:
            replanned_at = s.scalar(select(RunEvent.id).where(RunEvent.run_id == run.id, RunEvent.type == "run.replanned")
                                    .order_by(RunEvent.id.desc()))
        deadline, second = time.time() + 900, None
        while time.time() < deadline and second is None:
            with session_scope() as s:
                assert s.get(AnalysisRun, run.id).status != "FAILED"
                second = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending",
                                                         Approval.id != first_id))
                if second is not None:
                    s.expunge(second)
            time.sleep(0.5)
        assert second is not None, "no new publication approval after the redirect"
        with session_scope() as s:
            assert s.get(Approval, first_id).status == "invalidated"
            after = list(s.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.id > replanned_at)))
        created = {"chart": {e.payload["key"] for e in after if e.type == "chart.created"},
                   "metric": {e.payload["name"] for e in after if e.type == "metric.created"},
                   "dashboard": {e.payload["key"] for e in after if e.type == "dashboard.created"}}
        bundle = second.payload
        assert bundle["charts"] and bundle["metrics"] and bundle["dashboards"]
        assert "finding_stale_v1" not in {c["key"] for c in bundle["charts"]}
        assert "stale_v1_metric" not in {m["name"] for m in bundle["metrics"]}
        assert {c["key"] for c in bundle["charts"]} <= created["chart"], "stale charts in the bundle"
        assert {m["name"] for m in bundle["metrics"]} <= created["metric"], "stale metrics in the bundle"
        assert {d["key"] for d in bundle["dashboards"]} <= created["dashboard"], "stale dashboards in the bundle"
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()

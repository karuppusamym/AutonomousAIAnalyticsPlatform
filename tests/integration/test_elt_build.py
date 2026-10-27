"""P4-E04/E06 on the compose Postgres: an investigate run's dataset and KPIs become a dbt project, are
dry-run and approved, and dbt Core builds them through the BuildGateway as the build identity.

Negative tests: Postgres refuses the build role any write outside its target schema and any write to a
source schema; the BuildGateway refuses a missing, pending, expired or tampered approval and a target
that is no longer designated. The dbt tests need a dbt Core executable (`ANALYSTOS_DBT_EXECUTABLE`,
e.g. a venv with the `dbt` extra) and skip cleanly without one. Set ANALYSTOS_EVIDENCE_OUT to a path to
write the live run's evidence JSON."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import timedelta

import psycopg
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration
TARGET = "aos_mart"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg(url: str) -> str:
    return make_url(url).render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")


def _wait(run_id: str, statuses: set[str], timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            status, error = run.status, run.error
        if status in statuses:
            return status if status != "FAILED" else f"FAILED: {error}"
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


@pytest.fixture(scope="module")
def world(control_db):
    """A staged ServiceNow source, a completed investigate run (no models, no publication), one of its
    KPIs approved in the semantic model, playbook.elt_build enabled and a designated target schema."""
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    from analystos.connectors.servicenow_mock import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    from analystos.build import service as build_svc
    from analystos.capabilities import enablement, registry
    from analystos.connectors.naming import staging_schema_for
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Artifact, User
    from analystos.semantic import service as semantic
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    get_settings.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="elt build", objective="Find the drivers of SLA breaches in IT incidents")
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": f"http://127.0.0.1:{port}", "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        src_schema = src.staging_schema or staging_schema_for(src.id)
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(run.id, {"COMPLETED", "FAILED", "WAITING_USER"}) == "COMPLETED"
    with session_scope() as s:
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        metrics = [a.name for a in s.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "metric"))]
        pending = {r.name for r in semantic.metric_rows(s, ws_id, status="proposed")}
        approved = next(m for m in metrics if m in pending)
        semantic.decide_metric(s, ws_id, approved, approver, approve=True)
        enablement.set_enabled(s, s.merge(admin), ws_id, build_svc.PLAYBOOK, True, registry.current())
        build_svc.designate_target(s, s.merge(admin), ws_id, TARGET)
    yield {"ws": ws_id, "source": src_id, "src_schema": src_schema, "run": run.id, "admin": admin,
           "approved_metric": approved, "metrics": metrics}
    server.should_exit = True


def _runner_or_skip():
    from analystos.build.runner import DbtCoreRunner
    from analystos.core.config import get_settings

    runner = DbtCoreRunner(get_settings().dbt_executable)
    if not runner.available():
        pytest.skip("no dbt Core executable (set ANALYSTOS_DBT_EXECUTABLE; `pip install '.[dbt]'` in its own venv)")
    return runner


def _plan(world) -> tuple[str, str, str]:
    """An elt_build run up to its approval gate: (run id, job id, approval id)."""
    from analystos.build import service as build_svc
    from analystos.db.base import session_scope
    from analystos.db.models import BuildJob

    run = build_svc.start_build(world["admin"], world["ws"], from_run_id=world["run"], target_schema=TARGET)
    assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
    with session_scope() as s:
        job = s.scalar(select(BuildJob).where(BuildJob.run_id == run.id))
        return run.id, job.id, job.approval_id


def _approve(approval_id: str) -> None:
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.governance.approvals import decide

    with session_scope() as s:
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        decide(s, approval_id, approver, approve=True)


# ------------------------------------------------------------------------------ database-level refusals
def test_build_role_writes_only_its_target_and_never_a_source(world, analytics_plane):
    from analystos.build.targets import build_role_for
    from analystos.core.config import get_settings
    from analystos.staging.roles import role_for

    settings = get_settings()
    role = build_role_for(settings, world["ws"])
    src = world["src_schema"]
    with psycopg.connect(_pg(settings.analytics_builder_url), autocommit=True) as conn:
        conn.execute(f'SET ROLE "{role}"')
        conn.execute(f"CREATE TABLE {TARGET}.probe AS SELECT 1 AS x")  # the designated target: allowed
        assert conn.execute(f"SELECT count(*) FROM {src}.incident").fetchone()[0] > 0  # sources: readable
        for stmt in ("CREATE TABLE public.probe (x int)", "CREATE SCHEMA evil",
                     f"CREATE TABLE {src}.probe (x int)", f"INSERT INTO {src}.incident SELECT * FROM {src}.incident LIMIT 1",
                     f"DELETE FROM {src}.incident", f"DROP TABLE {src}.incident", f"ALTER TABLE {src}.incident ADD COLUMN y int"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
        conn.execute(f"DROP TABLE {TARGET}.probe")
        conn.execute("RESET ROLE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # the login alone holds nothing
            conn.execute(f"CREATE TABLE {TARGET}.probe2 (x int)")
    # the query identity never writes, not even into the build target
    with psycopg.connect(_pg(settings.analytics_reader_url), autocommit=True) as conn:
        conn.execute(f'SET ROLE "{role_for(settings, world["ws"])}"')
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
            conn.execute(f"CREATE TABLE {TARGET}.probe3 (x int)")


def test_targets_cannot_be_sources_or_undesignated(world):
    from analystos.build import service as build_svc
    from analystos.core.errors import Forbidden
    from analystos.db.base import session_scope

    with session_scope() as s, pytest.raises(Forbidden):
        build_svc.designate_target(s, s.merge(world["admin"]), world["ws"], world["src_schema"])
    with pytest.raises(Forbidden, match="not a designated build target"):
        build_svc.start_build(world["admin"], world["ws"], from_run_id=world["run"], target_schema="elsewhere")


# ------------------------------------------------------------------------------ gateway refusals
def test_gateway_refuses_missing_pending_expired_and_tampered_approvals(world):
    _runner_or_skip()
    from analystos.build.gateway import BuildGateway
    from analystos.core.config import get_settings
    from analystos.core.errors import ApprovalRequired, Conflict
    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import Approval, AuditEvent, BuildJob

    gw = BuildGateway(get_settings())
    # pending approval, then no approval at all
    _, job_id, approval_id = _plan(world)
    with pytest.raises(ApprovalRequired, match="pending"):
        gw.execute(job_id, approval_id, actor="test")
    with session_scope() as s:
        assert s.get(BuildJob, job_id).status == "refused"
    with pytest.raises(Conflict):  # a refused job never runs again
        gw.execute(job_id, approval_id, actor="test")
    _, job_id, _ = _plan(world)
    with pytest.raises(ApprovalRequired, match="only under an approved"):
        gw.execute(job_id, None, actor="test")

    # expired
    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        s.get(Approval, approval_id).expires_at = utcnow() - timedelta(minutes=1)
    with pytest.raises(ApprovalRequired, match="expired"):
        gw.execute(job_id, approval_id, actor="test")
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "expired"

    # tampered project after approval: the recomputed project hash no longer matches the approved payload
    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        job = s.get(BuildJob, job_id)
        files = dict(job.project_files)
        model = next(p for p in files if p.endswith(".sql") and "time_spine" not in p)
        files[model] = files[model].replace("SELECT", "SELECT 1 AS injected,", 1)
        job.project_files = files
    with pytest.raises(ApprovalRequired, match="payload changed"):
        gw.execute(job_id, approval_id, actor="test")
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "invalidated"
        assert s.get(BuildJob, job_id).status == "refused"
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "build.refused", AuditEvent.target == job_id))

    # tampered target schema: same, the approval named another schema
    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        s.get(BuildJob, job_id).target_schema = world["src_schema"]
    with pytest.raises(ApprovalRequired, match="payload changed"):
        gw.execute(job_id, approval_id, actor="test")


def test_gateway_refuses_a_target_no_longer_designated(world):
    _runner_or_skip()
    from analystos.build.gateway import BuildGateway
    from analystos.core.config import get_settings
    from analystos.core.errors import Forbidden
    from analystos.db.base import session_scope
    from analystos.db.models import Approval, BuildTarget

    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        target = s.scalar(select(BuildTarget).where(BuildTarget.workspace_id == world["ws"], BuildTarget.schema_name == TARGET))
        target.status = "retired"
    try:
        with pytest.raises(Forbidden, match="not a designated build target"):
            BuildGateway(get_settings()).execute(job_id, approval_id, actor="test")
        with session_scope() as s:
            assert s.get(Approval, approval_id).status == "invalidated"
    finally:
        with session_scope() as s:
            s.scalar(select(BuildTarget).where(BuildTarget.workspace_id == world["ws"],
                                               BuildTarget.schema_name == TARGET)).status = "active"


def test_gateway_rechecks_the_data_scope_at_execution(world):
    """Approved under one scope, executed under another: a column tagged restricted, or the asset
    deselected, after approval refuses the build and invalidates the approval (not the plan-time list)."""
    _runner_or_skip()
    from analystos.build.gateway import BuildGateway
    from analystos.core.config import get_settings
    from analystos.core.errors import PolicyDenied
    from analystos.db.base import session_scope
    from analystos.db.models import Approval, AuditEvent, BuildJob, SourceAsset, SourceColumn

    def asset():
        with session_scope() as s:
            return s.scalar(select(SourceAsset.id).where(SourceAsset.source_id == world["source"],
                                                         SourceAsset.name == "incident"))

    def refused(job_id, approval_id, match):
        with pytest.raises(PolicyDenied, match=match):
            BuildGateway(get_settings()).execute(job_id, approval_id, actor="test")
        with session_scope() as s:
            assert s.get(Approval, approval_id).status == "invalidated"
            assert s.get(BuildJob, job_id).status == "refused"
            assert s.scalar(select(AuditEvent).where(AuditEvent.action == "build.refused", AuditEvent.target == job_id))

    asset_id = asset()
    # a column the model reads is tagged restricted after approval
    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        model = next(t for p, t in s.get(BuildJob, job_id).project_files.items() if p.endswith(".sql") and "source(" in t)
        col = next(c for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset_id)
                                        .order_by(SourceColumn.ordinal)) if f'"{c.name}"' in model or f" {c.name}" in model)
        col_id, old_tags = col.id, list(col.tags or [])
        col.tags = [*old_tags, "restricted"]
    try:
        refused(job_id, approval_id, "no longer in the requester's scope")
    finally:
        with session_scope() as s:
            s.get(SourceColumn, col_id).tags = old_tags

    # the asset is deselected after approval
    _, job_id, approval_id = _plan(world)
    _approve(approval_id)
    with session_scope() as s:
        s.get(SourceAsset, asset_id).selected = False
    try:
        refused(job_id, approval_id, "outside the run's scope|no longer in the requester's scope")
    finally:
        with session_scope() as s:
            s.get(SourceAsset, asset_id).selected = True


# ------------------------------------------------------------------------------ live dbt build
def test_live_dbt_build_through_the_build_gateway(world):
    runner = _runner_or_skip()
    from analystos.build.targets import build_role_for
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Approval, AuditEvent, BuildJob, LineageEdge
    from analystos.staging.roles import role_for
    from analystos.workflows.orchestrator import run_local

    settings = get_settings()
    run_id, job_id, approval_id = _plan(world)
    with session_scope() as s:
        job = s.get(BuildJob, job_id)
        approval = s.get(Approval, approval_id)
        assert job.status == "awaiting_approval" and approval.action == "elt_build"
        assert approval.payload["project_hash"] == job.project_hash and approval.payload["target_schema"] == TARGET
        assert approval.payload["engine"] == "postgres:analytics" and approval.plan_hash == s.get(AnalysisRun, run_id).plan_hash
        assert job.dry_run["ok"] and job.estimate["rows"] > 0 and job.rollback["statements"]
        built_metrics = {m["metric"] for m in job.dry_run["metrics"]}
        assert world["approved_metric"] in built_metrics  # approved in the semantic model: built
        skipped = {m["metric"] for m in job.dry_run["skipped_metrics"]}
        assert set(world["metrics"]) - {world["approved_metric"]} <= skipped  # require_approved_metrics: the rest are not
        plan_snapshot = {"relations": list(job.relations), "estimate": dict(job.estimate), "rollback": dict(job.rollback),
                         "tests": job.dry_run["tests"], "metrics": job.dry_run["metrics"],
                         "skipped_metrics": job.dry_run["skipped_metrics"], "project_hash": job.project_hash,
                         "payload_hash": approval.payload_hash, "files": sorted(job.project_files)}
    _approve(approval_id)
    started = time.time()
    assert run_local(run_id) == "COMPLETED"
    elapsed = round(time.time() - started, 1)

    with session_scope() as s:
        job = s.get(BuildJob, job_id)
        assert job.status == "succeeded", job.error or job.log_tail[-2000:]
        assert s.get(Approval, approval_id).status == "executed"
        counts = job.run_results["counts"]
        assert counts.get("success", 0) >= 1 and counts.get("pass", 0) >= 1 and not counts.get("error") and not counts.get("fail")
        assert job.manifest["osi_conformance"]["valid"], job.manifest["osi_conformance"]
        events = job.openlineage
        assert {e["eventType"] for e in events} == {"START", "COMPLETE"}
        edges = [(e.from_type, e.relation, e.to_type, e.to_id) for e in s.scalars(select(LineageEdge).where(
            LineageEdge.workspace_id == world["ws"], LineageEdge.run_id == run_id))]
        model_rel = next(r for r in job.relations if "time_spine" not in r)
        assert ("build_job", "produced", "table", model_rel) in edges
        assert any(e[1] == "transformed_into" and e[3] == model_rel for e in edges)
        assert any(e[1] == "materialized_by" and e[0] == "dataset" for e in edges)
        assert any(e[1] == "authorized" and e[0] == "approval" for e in edges)
        actions = [a.action for a in s.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id))]
        assert {"build.started", "build.node", "build.completed"} <= set(actions)
        result = {k: getattr(job, k) for k in ("id", "status", "engine", "runner", "target_schema", "project_name", "project_hash",
                                               "relations", "run_results")}
        result["osi_conformance"] = job.manifest["osi_conformance"]
        result["dbt_version"] = job.manifest.get("dbt_version")
        result["openlineage_sample"] = events[-1]
        result["lineage_edges"] = edges
        result["audit_actions"] = sorted(set(actions))
    # the build ran as the workspace build role, and the query identity reads the result
    with psycopg.connect(_pg(settings.analytics_reader_url), autocommit=True) as conn:
        conn.execute(f'SET ROLE "{role_for(settings, world["ws"])}"')
        rows = conn.execute(f"SELECT count(*) FROM {model_rel}").fetchone()[0]
    with psycopg.connect(_pg(settings.analytics_loader_url), autocommit=True) as conn:
        owner = conn.execute("SELECT tableowner FROM pg_tables WHERE schemaname = %s AND tablename = %s",
                             tuple(model_rel.split("."))).fetchone()[0]
    assert rows == plan_snapshot["estimate"]["rows"]
    assert owner == build_role_for(settings, world["ws"])
    out = os.environ.get("ANALYSTOS_EVIDENCE_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump({"dbt": runner.version(), "plan": plan_snapshot, "result": result, "table_rows": rows, "owner": owner,
                       "elapsed_seconds": elapsed}, fh, indent=2, default=str)

"""P4-K03 acceptance on the real stack: the Ossie semantic model, the metric approval workflow with
separation of duties, conflicts (SEM-005), dbt round trip through the database, and the publish gate
on the deterministic local flow (approved metrics required for publication)."""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from analystos.db import vectors

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
DBT_DOC = Path(__file__).parents[1] / "fixtures" / "ossie" / "dbt_1_12_0_osi_document.json"


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _workspace(api, owner: dict, approver_email: str = "approver@analystos.local", name: str = "semantic") -> str:
    r = api.post("/api/workspaces", headers=owner, json={"name": name, "objective": "Governed KPIs for orders"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=owner, json={"email": approver_email, "role": "approver"}).status_code == 200
    return ws


@pytest.fixture(scope="module")
def people(api):
    return {"analyst": _login(api, "analyst@analystos.local"), "approver": _login(api, "approver@analystos.local")}


def test_new_workspaces_require_approved_metrics(api, people):
    ws = _workspace(api, people["analyst"], name="policy default")
    policy = api.get(f"/api/workspaces/{ws}", headers=people["analyst"]).json()["policy"]
    assert policy["require_approved_metrics"] is True


def test_dbt_import_approval_sod_conflicts_and_round_trip(api, people):
    from analystos.semantic import ossie

    analyst, approver = people["analyst"], people["approver"]
    ws = _workspace(api, analyst)
    assert api.put(f"/api/workspaces/{ws}/policy", headers=analyst,
                   json={"separation_of_duties": True}).status_code == 200
    base = f"/api/workspaces/{ws}/semantic"

    # dbt 1.12 osi_document.json (real dbt output): structure lands, every metric is only a proposal
    report = api.post(f"{base}/import/dbt", headers=analyst, json=json.loads(DBT_DOC.read_text(encoding="utf-8")))
    assert report.status_code == 200, report.text
    report = report.json()
    assert report["datasets"] == 2 and len(report["proposed"]) == 6
    assert any("average_order_value" in i for i in report["issues"])
    model = api.get(base, headers=analyst).json()
    assert model["approved"] == [] and {m["status"] for m in model["metrics"]} == {"proposed"}
    assert [d["name"] for d in model["model"]["datasets"]] == ["orders", "customers"]

    # separation of duties: the importer cannot approve, through either door
    denied = api.post(f"{base}/metrics/net_revenue/approve", headers=analyst)
    assert denied.status_code == 403 and "separation of duties" in denied.json()["error"]["message"]
    approval_id = api.get(f"{base}/metrics/net_revenue", headers=analyst).json()[-1]["approval_id"]
    assert api.post(f"/api/approvals/{approval_id}/approve", headers=analyst).status_code == 403

    for name in ("net_revenue", "orders", "returned", "return_rate", "customers_total"):
        r = api.post(f"{base}/metrics/{name}/approve", headers=approver, json={"reason": "matches finance definition"})
        assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert api.get(f"{base}/metrics/net_revenue", headers=analyst).json()[-1]["decided_by"]
    # the generic approvals inbox works too, and the metric takes effect there as well
    aov = api.get(f"{base}/metrics/average_order_value", headers=analyst).json()[-1]
    assert api.post(f"/api/approvals/{aov['approval_id']}/reject", headers=approver, json={"reason": "mangled by dbt"}).status_code == 200
    assert api.get(f"{base}/metrics/average_order_value", headers=analyst).json()[-1]["status"] == "rejected"

    # SEM-005: a duplicate expression under a new name, and a competing definition of an approved name, are flagged
    dup = api.post(f"{base}/metrics", headers=analyst, json={"name": "revenue_total", "expression": "sum( orders.amount )"}).json()
    assert dup["created"] and dup["conflicts"][0]["kind"] == "duplicate_expression"
    assert dup["conflicts"][0]["names"] == ["net_revenue", "revenue_total"]
    variant = api.post(f"{base}/metrics", headers=analyst,
                       json={"name": "net_revenue", "expression": "SUM(orders.amount) - SUM(orders.discount)"}).json()
    assert variant["metric"]["version"] == 2
    kinds = {(c["kind"], tuple(c["names"])) for c in api.get(f"{base}/conflicts", headers=analyst).json()}
    assert ("conflicting_definition", ("net_revenue",)) in kinds and ("duplicate_expression", ("net_revenue", "revenue_total")) in kinds
    # the proposal repeated is not a second proposal
    assert api.post(f"{base}/metrics", headers=analyst, json={"name": "revenue_total", "expression": "SUM(orders.amount)"}).json()["created"] is False
    # approving v2 supersedes v1; rejecting the duplicate clears that flag
    assert api.post(f"{base}/metrics/net_revenue/approve", headers=approver).json()["version"] == 2
    assert [m["status"] for m in api.get(f"{base}/metrics/net_revenue", headers=analyst).json()] == ["deprecated", "approved"]
    api.post(f"{base}/metrics/revenue_total/reject", headers=approver, json={"reason": "duplicate of net_revenue"})
    assert api.get(f"{base}/conflicts", headers=analyst).json() == []

    # Ossie download validates against the pinned schema and carries approved metrics only
    doc = ossie.parse_text(api.get(f"{base}/ossie", headers=analyst).text)
    assert ossie.schema_problems(doc) == []
    assert {m["name"] for m in doc["semantic_model"][0]["metrics"]} == {"net_revenue", "orders", "returned", "return_rate", "customers_total"}

    # dbt round trip through the database: export -> import elsewhere -> approve -> export is identical
    exported = api.get(f"{base}/export/dbt", headers=analyst).json()
    (path, text), = exported["files"].items()
    assert path.startswith("osi/") and path.endswith(".json")
    ws2 = _workspace(api, analyst, name="semantic copy")
    again = api.post(f"/api/workspaces/{ws2}/semantic/import/dbt", headers=analyst, json=json.loads(text)).json()
    assert len(again["proposed"]) == 5
    for p in again["proposed"]:
        assert api.post(f"/api/workspaces/{ws2}/semantic/metrics/{p['name']}/approve", headers=approver).status_code == 200
    re_exported = api.get(f"/api/workspaces/{ws2}/semantic/export/dbt", headers=analyst).json()
    first, second = json.loads(text), json.loads(next(iter(re_exported["files"].values())))
    first["semantic_model"][0].pop("name"), second["semantic_model"][0].pop("name")  # the model takes the workspace's name
    assert first == second

    # Superset export: approved metrics, certified
    superset = api.get(f"{base}/export/superset", headers=analyst).json()
    assert {m["metric_name"] for m in superset} == {"net_revenue", "orders", "returned", "return_rate", "customers_total"}
    assert all(json.loads(m["extra"])["certification"] for m in superset)

    # deprecation retires the stable definition
    assert api.post(f"{base}/metrics/returned/deprecate", headers=approver, json={"reason": "unused"}).status_code == 200
    assert "returned" not in api.get(base, headers=analyst).json()["approved"]

    # audit trail and events
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent, RunEvent

    with session_scope() as s:
        actions = set(s.scalars(select(AuditEvent.action).where(AuditEvent.workspace_id == ws)))
        events = set(s.scalars(select(RunEvent.type).where(RunEvent.workspace_id == ws)))
    assert {"semantic.metric.proposed", "semantic.metric.approved", "semantic.metric.self_approval_blocked",
            "semantic.metric.deprecated", "semantic.imported"} <= actions
    assert {"semantic.metric.proposed", "semantic.metric.approved", "semantic.conflict.detected", "semantic.model.updated"} <= events


def test_semantic_api_authorization(api, people):
    ws = _workspace(api, people["analyst"], name="semantic authz")
    base = f"/api/workspaces/{ws}/semantic"
    assert api.get(base).status_code == 401
    # an approver may approve but not propose (proposals need editor, the right the approval later re-checks)
    r = api.post(f"{base}/metrics", headers=people["approver"], json={"name": "x", "expression": "COUNT(*)"})
    assert r.status_code == 403
    bad = api.post(f"{base}/metrics", headers=people["analyst"], json={"name": "x", "expression": "amount"})
    assert bad.status_code == 422 and "aggregate" in bad.json()["error"]["message"]
    assert api.post(f"{base}/import/dbt", headers=people["analyst"], json={"version": "0.2.0", "semantic_model": []}).status_code == 422
    assert api.get(f"{base}/ossie", headers=people["analyst"]).status_code == 409  # no datasets yet


# ------------------------------------------------------------------------------ publish gate, live flow
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


def test_publication_requires_approved_metrics(control_db, servicenow_url, monkeypatch):
    """With separation enabled, review proposed KPIs independently, then publish their reused definitions.

    Planning and publication each stop for their current explicit approval rather than bypassing a gate.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # preview destination
    monkeypatch.setenv("ANALYSTOS_GRAPH_ENABLED", "false")
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.core.errors import Forbidden
        from analystos.db.base import session_scope
        from analystos.db.models import AnalysisRun, Approval, Artifact, RunTask, SemanticMetric, User
        from analystos.governance.approvals import decide
        from analystos.semantic import service as semantic
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace, set_policy
        from analystos.workflows.orchestrator import run_local

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="gated publish", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            set_policy(s, admin, ws.id, {"separation_of_duties": True})
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id, admin_id = ws.id, src.id, admin.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident"])

        def reach_publication(run_id):
            assert _wait(run_id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
            with session_scope() as s:
                proposal = s.scalar(select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
                                    .order_by(Approval.created_at.desc()))
                if proposal.action == "execute_plan":
                    decide(s, proposal.id, s.scalar(select(User).where(User.email == "approver@analystos.local")), approve=True)
                else:
                    return
            assert run_local(run_id) == "WAITING_USER"

        first = create_run(admin, ws_id, objective=None)
        reach_publication(first.id)
        with session_scope() as s:
            task = s.scalar(select(RunTask).where(RunTask.run_id == first.id, RunTask.key == "publish_request"))
            assert task.status == "COMPLETED"
            publication = s.scalar(select(Approval).where(Approval.run_id == first.id,
                                                         Approval.action == "publish_dashboard", Approval.status == "pending"))
            assert publication is not None and publication.evidence["kpis_to_approve"]
            proposals = list(s.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == ws_id)))
            assert len(proposals) >= 3 and {p.status for p in proposals} == {"proposed"}
            names = sorted(p.name for p in proposals)
            with pytest.raises(Forbidden):
                semantic.decide_metric(s, ws_id, names[0], s.get(User, admin_id), approve=True)
        with session_scope() as s:
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            for name in names:
                assert semantic.decide_metric(s, ws_id, name, approver, approve=True).status == "approved"

        second = create_run(admin, ws_id, objective=None)
        reach_publication(second.id)
        with session_scope() as s:
            semantic_task = s.scalar(select(RunTask).where(RunTask.run_id == second.id, RunTask.key == "semantic"))
            assert sorted(semantic_task.output["approved_metrics"]) == names and semantic_task.output["proposed_metrics"] == []
            approval = s.scalar(select(Approval).where(Approval.run_id == second.id, Approval.status == "pending"))
            assert {m["status"] for m in approval.payload["metrics"]} == {"approved"}
            decide(s, approval.id, s.scalar(select(User).where(User.email == "approver@analystos.local")), approve=True)
        assert run_local(second.id) == "COMPLETED"
        with session_scope() as s:
            assert s.get(AnalysisRun, second.id).summary.get("published") is True
            dashboards = s.scalars(select(Artifact).where(Artifact.run_id == second.id, Artifact.type == "dashboard"))
            assert all(d.status == "published" for d in dashboards)
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()


def test_migration_0017_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT
    from analystos.db.models import SemanticMetric, SemanticModel

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig17"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0017")
        insp = inspect(engine)
        later = {"semantic_model": {"approval_id", "decided_by", "decided_at", "tenant_id"},  # 0034 (P4-05), 0057
                 "semantic_metric": {"tenant_id"}}
        for model in (SemanticModel, SemanticMetric):
            assert {c["name"] for c in insp.get_columns(model.__tablename__)} == \
                set(model.__table__.columns.keys()) - later.get(model.__tablename__, set())
        command.downgrade(cfg, "-1")
        assert "semantic_metric" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()


def test_metric_self_review_follows_disabled_separation_policy_in_both_routes(api, people):
    owner = people["analyst"]
    ws = _workspace(api, owner, name="single reviewer metric policy")
    base = f"/api/workspaces/{ws}/semantic"
    assert api.get(f"/api/workspaces/{ws}", headers=owner).json()["policy"]["separation_of_duties"] is False
    imported = api.post(f"{base}/import/dbt", headers=owner,
                        json=json.loads(DBT_DOC.read_text(encoding="utf-8")))
    assert imported.status_code == 200
    response = api.post(f"{base}/metrics/net_revenue/approve", headers=owner,
                        json={"reason":"Owner reviewed in single-reviewer mode"})
    assert response.status_code == 200 and response.json()["status"] == "approved", response.text
    metric = api.get(f"{base}/metrics/orders", headers=owner).json()[-1]
    response = api.post(f"/api/approvals/{metric['approval_id']}/approve", headers=owner)
    assert response.status_code == 200, response.text
    assert api.get(f"{base}/metrics/orders", headers=owner).json()[-1]["status"] == "approved"

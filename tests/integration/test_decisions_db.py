"""P4-T08/T09 against Postgres: decisions persist beside model calls, user signals label them through
the API, the nightly calibration downgrades a miscalibrated backend, the admin report shows it and
an administrator restores it; migration 0013 applies and reverts."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fakes import FakeTransport

from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import ModelRouteUnavailable
from analystos.core.ids import new_id, utcnow
from analystos.db import vectors
from analystos.db.base import session_scope
from analystos.db.models import (
    Alert,
    DecisionCalibration,
    DecisionOutcome,
    DecisionRecord,
    Feedback,
    Insight,
    ModelCall,
)
from analystos.decisions import Question, store
from analystos.decisions import breaker as breaker_mod
from analystos.decisions.service import DecisionService, default_store
from analystos.llm.cache import ResponseCache
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.usage import DbUsageSink

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


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


@pytest.fixture(scope="module")
def world(api):
    admin, analyst, viewer = (_login(api, f"{u}@analystos.local") for u in ("admin", "analyst", "approver"))
    r = api.post("/api/workspaces", headers=analyst, json={"name": "decisions", "objective": "Find the drivers of SLA breaches"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
    return {"ws": ws, "admin": admin, "analyst": analyst, "viewer": viewer}


@pytest.fixture(autouse=True)
def _clean():
    breaker_mod.reset_all()
    store.invalidate()
    yield
    with session_scope() as s:
        s.query(DecisionCalibration).delete()
    store.invalidate()


def _router(decide) -> ModelRouter:
    platform = PlatformSettings()
    return ModelRouter(transport=FakeTransport(decide=decide), sink=DbUsageSink(), max_retries=0,
                       api_key_lookup=lambda _e: "sk-test-000000000000000000000000", settings_provider=lambda: platform,
                       cache=ResponseCache(None))


def _triage(svc: DecisionService, ws: str, subject: str):
    q = Question.escalation("material?", levels=["info", "warning", "critical"], baseline="warning", escalate_to="critical",
                            escalate_at=0.8)
    return svc.decide("alert_triage", {"signal": "backlog up 40%"}, q, facts={"points": 12}, ctx=CallContext(workspace_id=ws),
                      subject=subject)


def test_decisions_persist_with_probabilities_and_jev_outage_is_recorded(world):
    run_id = new_id("run")
    svc = DecisionService(_router(lambda p: {"model": "typesafe/jev-1.13", "answers": {"p": {"noul": 0.93}}, "usage": {}}))
    assert isinstance(svc.store, type(default_store(svc.router)))
    d = svc.decide("stop_check", {"objective": "o", "findings": "f"}, Question.probability("answered?"),
                   facts={"round": 1, "supported": 3}, ctx=CallContext(workspace_id=world["ws"], run_id=run_id))
    down = DecisionService(_router(lambda p: ModelRouteUnavailable("decisions API HTTP 404")))
    fallback = _triage(down, world["ws"], "alert:x")
    with session_scope() as s:
        row = s.get(DecisionRecord, d.id)
        assert (row.backend, row.answer, row.authority, row.run_id) == ("jev", True, "bounded_stop", run_id)
        assert row.probabilities == {"yes": 0.93, "no": 0.07} and row.model == "typesafe/jev-1.13"
        assert s.scalar(select(ModelCall.status).where(ModelCall.run_id == run_id, ModelCall.purpose == "stop_check")) == "ok"
        f = s.get(DecisionRecord, fallback.id)
        assert f.backend == "rules" and f.answer == "warning" and "jev: failed" in f.fallback_reason
        assert [a["outcome"] for a in f.attempts] == ["answered", "failed"]


def test_signals_calibration_downgrade_report_and_restore_through_the_api(api, world):
    ws = world["ws"]
    # 25 alerts JEV called material (p=0.9) that the team dismissed.
    svc = DecisionService(_router(lambda p: {"model": "typesafe/jev-1.13", "answers": {"p": {"noul": 0.9}}, "usage": {}}))
    alert_ids = []
    with session_scope() as s:
        for i in range(25):
            key = f"cond:{new_id('k')}:2026-08"
            a = Alert(id=new_id("alr"), workspace_id=ws, severity="critical", title=f"a{i}", message="m", data={}, dedupe_key=key)
            s.add(a)
            alert_ids.append((a.id, key))
    for _, key in alert_ids:
        assert _triage(svc, ws, f"alert:{key}").value == "critical"
    for alert_id, _ in alert_ids:
        r = api.post(f"/api/alerts/{alert_id}/dismiss", headers=world["analyst"])
        assert r.status_code == 200 and r.json()["status"] == "resolved" and r.json()["data"]["dismissed_by"]
    with session_scope() as s:
        labels = list(s.scalars(select(DecisionOutcome).where(DecisionOutcome.workspace_id == ws,
                                                             DecisionOutcome.source == "alert.dismiss")))
        assert len(labels) == 25 and {o.label for o in labels} == {"no"} and {o.backend for o in labels} == {"jev"}

    assert api.get("/api/admin/decisions/calibration", headers=world["analyst"]).status_code == 403
    assert api.post("/api/admin/decisions/calibration/run", headers=world["analyst"], json={}).status_code == 403
    r = api.post("/api/admin/decisions/calibration/run", headers=world["admin"], json={})
    assert r.status_code == 200, r.text
    assert "alert_triage:jev" in r.json()["downgraded"]
    report = api.get("/api/admin/decisions/calibration", headers=world["admin"]).json()
    assert "alert_triage:jev" in report["downgraded"]
    row = next(x for x in report["rows"] if (x["purpose"], x["backend"]) == ("alert_triage", "jev"))
    assert row["action"] == "downgraded" and row["brier"] == pytest.approx(0.81) and row["n"] >= 25
    assert next(p for p in report["purposes"] if p["purpose"] == "alert_triage")["effective"] == ["rules"]

    # The service now decides alert triage by rules, visibly.
    store.invalidate()
    d = _triage(svc, ws, "alert:new")
    assert d.backend == "rules" and d.value == "warning" and "downgraded" in d.fallback_reason
    listed = api.get("/api/admin/decisions", headers=world["admin"], params={"purpose": "alert_triage", "limit": 5}).json()
    assert listed[0]["id"] == d.id and listed[0]["backend"] == "rules"

    # Reversible by an administrator only.
    assert api.post("/api/admin/decisions/backends/alert_triage/jev", headers=world["analyst"],
                    json={"downgraded": False}).status_code == 403
    assert api.post("/api/admin/decisions/backends/alert_triage/rules", headers=world["admin"],
                    json={"downgraded": True}).status_code == 422  # InvalidInput
    r = api.post("/api/admin/decisions/backends/alert_triage/jev", headers=world["admin"],
                 json={"downgraded": False, "note": "new JEV pin"})
    assert r.status_code == 200 and r.json()["action"] == "admin_restored"
    store.invalidate()
    assert _triage(svc, ws, "alert:after").backend == "jev"


def test_finding_accept_and_feedback_correction_label_decisions(api, world):
    ws = world["ws"]
    ins_id, fb_id = new_id("ins"), new_id("fb")
    with session_scope() as s:
        s.add(Insight(id=ins_id, workspace_id=ws, run_id=new_id("run"), code="I-1", title="t", finding="f"))
        s.add(Feedback(id=fb_id, workspace_id=ws, run_id=None, user_id="u", kind="redirect", text="that is wrong", data={}))
        for purpose, subject, probs in (("rev_second_opinion", f"insight:{ins_id}", {"yes": 0.8, "no": 0.2}),
                                        ("feedback_classification", f"feedback:{fb_id}", {"redirect": 0.6, "question": 0.4})):
            s.add(DecisionRecord(id=new_id("dec"), workspace_id=ws, purpose=purpose, authority="route", backend="jev",
                                 inputs_hash="h", subject=subject, options={}, answer=None, probabilities=probs, latency_ms=1,
                                 cost_usd=0.0, attempts=[], enforced=[]))
    assert api.post(f"/api/insights/{ins_id}/outcome", headers=world["viewer"], json={"signal": "accept"}).status_code == 403
    r = api.post(f"/api/insights/{ins_id}/outcome", headers=world["analyst"], json={"signal": "accept"})
    assert r.status_code == 200 and r.json()["labelled_decisions"] == 1
    assert api.post(f"/api/feedback/{fb_id}/correct", headers=world["analyst"], json={"kind": "grant"}).status_code == 422
    r = api.post(f"/api/feedback/{fb_id}/correct", headers=world["analyst"], json={"kind": "reject_finding"})
    assert r.status_code == 200 and r.json() == {"feedback": fb_id, "kind": "reject_finding", "was": "redirect",
                                                 "labelled_decisions": 1}
    with session_scope() as s:
        got = {o.purpose: (o.label, o.source) for o in s.scalars(select(DecisionOutcome).where(DecisionOutcome.workspace_id == ws,
                                                                                              DecisionOutcome.source != "alert.dismiss"))}
        assert got == {"rev_second_opinion": ("yes", "finding.accept"),
                       "feedback_classification": ("reject_finding", "feedback_classification.correct")}
        assert s.get(Feedback, fb_id).data["corrected_kind"] == "reject_finding"


def test_nightly_calibration_runs_once_per_day_across_schedulers(control_db):
    from analystos.decisions.calibration import maybe_run_nightly
    from analystos.services.schedules import nightly_calibration

    nightly_calibration()
    with session_scope() as s:
        runs = s.query(DecisionCalibration).filter(DecisionCalibration.action == "run").count()
        assert runs == 1
        assert maybe_run_nightly(s) is None  # ran less than 24 h ago
        assert maybe_run_nightly(s, now=utcnow().replace(year=utcnow().year + 1)) is not None


def test_cli_calibrate_dry_run(control_db, capsys):
    from analystos.cli import main

    assert main(["calibrate", "--dry-run"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    with session_scope() as s:
        assert s.query(DecisionCalibration).count() == 0


def test_migration_0013_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig13"
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
        command.upgrade(cfg, "0013")
        insp = inspect(engine)
        assert {"decision", "decision_outcome", "decision_calibration"} <= set(insp.get_table_names())
        from analystos.db import models

        for table in ("decision", "decision_outcome", "decision_calibration"):
            # tenant_id arrives with 0057 (ADR-009); the 0013 schema predates it
            assert {c["name"] for c in insp.get_columns(table)} == set(models.Base.metadata.tables[table].columns.keys()) - {"tenant_id"}
        command.downgrade(cfg, "-1")
        assert "decision" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()

"""P7-03 (ADR-0021) acceptance on the local stack with real runs (ServiceNow mock, no model key):

1. A schedule pins its completed baseline: definition + content hash, capability manifests, metric
   and method versions, and the baseline's AnalysisSpec set.
2. A pack upgrade (a newer agent manifest in the registry) and the approval of a new metric version
   both show *upgrade available*; the next fire still runs the pinned versions (bound refs, metric v1,
   the same question set, no model call), states "nothing changed" on unchanged data and marks the
   baseline's narrative stale.
3. A retired pin blocks the schedule (the fire is skipped, nothing runs) and notifies the owner.
4. A typed `AnalysisWork` work order executes its frozen specs through the same replay path.
"""
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
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _wait(run_id: str, timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in ("COMPLETED", "FAILED", "CANCELLED", "REJECTED"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def _wait_schedule_run(srun_id: str, timeout: float = 60) -> None:
    """The run reaches its terminal state first; complete_from_run then finishes the schedule_run in its
    own transaction, so a reader can see the run COMPLETED a moment before the schedule_run succeeds."""
    from analystos.db.base import session_scope
    from analystos.db.models import ScheduleRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            if s.get(ScheduleRun, srun_id).status in ("succeeded", "failed", "skipped"):
                return
        time.sleep(0.2)
    raise AssertionError(f"schedule run {srun_id} did not finish")


def _propose_and_approve(ws: str, expression: str) -> None:
    from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.semantic import service as semantic

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        defn = SemanticMetricDef(name="breach_rate", expressions=[DialectExpression(expression=expression)],
                                 description="Share of incidents that missed their SLA.", source_columns=["sn.incident.made_sla"])
        semantic.propose_metric(s, ws, defn, proposed_by=admin.id, via="user")
    with session_scope() as s:
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        assert semantic.decide_metric(s, ws, "breach_rate", approver, approve=True).status == "approved"


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    import os

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.runtime.context import default_router
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "SERVICENOW_PASSWORD", "ANALYSTOS_SUPERSET_URL")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["SERVICENOW_PASSWORD"] = "admin"
    os.environ["ANALYSTOS_SUPERSET_URL"] = "http://127.0.0.1:9"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="pinned schedules", objective="Find the drivers of SLA breaches in IT incidents")
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:SERVICENOW_PASSWORD")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    _propose_and_approve(ws_id, "avg(case when made_sla then 0 else 1 end)")  # breach_rate v1, before the baseline
    baseline = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(baseline.id) == "COMPLETED"
    yield {"ws": ws_id, "admin": admin, "baseline": baseline.id}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


def test_pinned_schedule_upgrade_available_nothing_changed_and_retired_blocks(world, monkeypatch):
    from analystos.capabilities import registry as reg
    from analystos.contracts.capability import CapabilityManifest
    from analystos.db.base import session_scope
    from analystos.db.models import (
        AnalysisRun,
        Artifact,
        Hypothesis,
        ModelCall,
        Notification,
        RunEvent,
        Schedule,
        ScheduleRun,
    )
    from analystos.registries.hypotheses import TESTED, spec_hash
    from analystos.services import definitions, pins
    from analystos.services import schedules as sch_svc

    ws, admin, baseline = world["ws"], world["admin"], world["baseline"]
    with session_scope() as s:
        base = s.get(AnalysisRun, baseline)
        caps = base.capabilities
        assert caps["definition"]["key"] == "playbook.investigate" and caps["definition"]["source"] == "builtin"
        assert caps["definition"]["content_hash"] == definitions.manifest_digest(reg.current().get("playbook.investigate"))
        assert caps["semantic"]["metrics"]["breach_rate"]["version"] == 1 and caps["methods"]
        base_refs = list(caps["refs"])
        base_specs = {spec_hash(h.spec) for h in s.scalars(select(Hypothesis).where(
            Hypothesis.run_id == baseline, Hypothesis.status.in_(TESTED)))}
        assert base_specs
        sch = sch_svc.create_schedule(s, s.merge(admin), ws, name="weekly SLA", kind="reanalysis", cron="0 7 * * 1",
                                      config={"baseline_run_id": baseline, "refresh_first": False,
                                              "report": {"kind": "weekly_summary", "formats": ["html"]}})
        s.flush()
        sid = sch.id
        assert sch.pins["refs"] == base_refs and {a["spec_hash"] for a in sch.pins["analyses"]} == base_specs
        assert sch.pin_status["state"] == "current"

    # A pack upgrade: a newer agent.investigator in the installed registry.
    snap = reg.current()
    raw = snap.get("agent.investigator").model_dump(mode="json")
    cur = raw["version"]  # whatever is installed; the upgrade bumps its minor
    nxt = ".".join([cur.split(".")[0], str(int(cur.split(".")[1]) + 1), "0"])
    newer = CapabilityManifest.model_validate({**raw, "version": nxt, "summary": raw["summary"] + " (pack upgrade)"})
    monkeypatch.setattr(reg, "_current", reg.pinned(snap, [newer]))
    with session_scope() as s:
        assert pins.refresh_workspace(s, ws) == {sid: "upgrade_available"}
    # Approving a new metric version: also an upgrade, and still nothing changes for the next fire.
    _propose_and_approve(ws, "avg(case when made_sla then 0.0 else 1.0 end)")
    with session_scope() as s:
        st = pins.status(s, s.get(Schedule, sid))
        assert st.state == "upgrade_available"
        assert {(i.type, i.id, i.pinned, i.current) for i in st.items if i.state == "newer"} >= {
            ("capability", "agent.investigator", f"agent.investigator@{cur}", f"agent.investigator@{nxt}"),
            ("metric", "breach_rate", "breach_rate@v1", "breach_rate@v2")}
        assert s.scalar(select(RunEvent).where(RunEvent.workspace_id == ws, RunEvent.type == "schedule.upgrade_available"))

    srun = sch_svc.run_now(admin, sid)
    with session_scope() as s:
        fire_run = s.get(ScheduleRun, srun).result["run_id"]
    assert _wait(fire_run) == "COMPLETED"
    _wait_schedule_run(srun)
    with session_scope() as s:
        run = s.get(AnalysisRun, fire_run)
        assert run.capabilities["refs"] == base_refs  # the installed agent.investigator, not the upgrade
        assert run.capabilities["semantic"]["metrics"]["breach_rate"]["version"] == 1
        assert run.capabilities["pinned"]["revision"] == 1 and run.origin["replay"] is True
        fired = {spec_hash(h.spec) for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == fire_run))}
        assert fired == base_specs  # the frozen set, never re-planned from the objective
        calls = list(s.scalars(select(ModelCall).where(ModelCall.run_id == fire_run)))
        assert all(c.status == "skipped" for c in calls)
        result = s.get(ScheduleRun, srun).result
        assert result["pins"]["upgrade_available"] is True and result["pin_revision"] == 1
        assert result["changes"]["new"] == result["changes"]["resolved"] == result["changes"]["changed"] == 0
        assert result["nothing_changed"] is True and result["summary"].startswith("Nothing changed")
        narrative = s.scalar(select(Artifact).where(Artifact.run_id == baseline, Artifact.type == "narrative"))
        assert narrative.status == "stale" and result["stale_narratives"] == [narrative.id]
        assert s.scalar(select(Artifact).where(Artifact.run_id == fire_run, Artifact.type == "narrative")).status == "final"

    # A retired pin (an agent the baseline bound is no longer installed) blocks and notifies.
    gone = {k: v for k, v in reg.current().manifests.items() if k != "agent.profiler"}
    monkeypatch.setattr(reg, "_current", reg.Snapshot(manifests=gone, digest="profiler-retired"))
    with session_scope() as s:
        runs_before = len(list(s.scalars(select(AnalysisRun.id).where(AnalysisRun.workspace_id == ws))))
    blocked = sch_svc.run_now(admin, sid)
    with session_scope() as s:
        row = s.get(ScheduleRun, blocked)
        assert row.status == "skipped" and "agent.profiler@1.0.0" in row.error
        assert len(list(s.scalars(select(AnalysisRun.id).where(AnalysisRun.workspace_id == ws)))) == runs_before
        assert s.scalar(select(Notification).where(Notification.workspace_id == ws, Notification.title == "Schedule blocked: weekly SLA"))


def test_an_analysis_work_order_replays_its_frozen_specs(world):
    from analystos.contracts.work import WorkOrderSpec
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Hypothesis, ModelCall
    from analystos.services import work_orders

    ws, admin, baseline = world["ws"], world["admin"], world["baseline"]
    with session_scope() as s:
        spec = next(h.spec for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == baseline,
                                                                       Hypothesis.status == "supported")))
        wo = work_orders.create(s, s.merge(admin), ws, WorkOrderSpec.model_validate({
            "kind": "diagnose", "objective": "Re-test the strongest SLA driver on today's data",
            "spec": {"type": "analysis", "analyses": [spec], "statements": ["The strongest driver still holds"]}}))
        s.flush()
        wo_id = wo.id
    run, replayed = work_orders.start(admin, ws, wo_id, expected_revision=1)
    assert not replayed and _wait(run.id) == "COMPLETED"
    with session_scope() as s:
        hyps = list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id)))
        assert {h.origin for h in hyps} == {"work_order"} and len(hyps) == 1 and hyps[0].status in ("supported", "rejected", "inconclusive")
        assert all(c.status == "skipped" for c in s.scalars(select(ModelCall).where(ModelCall.run_id == run.id)))
        assert s.get(AnalysisRun, run.id).origin["work_order_id"] == wo_id

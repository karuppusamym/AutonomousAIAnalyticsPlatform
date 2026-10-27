"""P7-03 (ADR-0021) on the in-memory control plane: definition lifecycle, triggers refusing drafts
outside dev, run bindings recording content hash + semantic/method versions, and pinned schedules
(pack upgrade and metric approval show "upgrade available" without changing the next fire; a retired
pin blocks the schedule and notifies its owner)."""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import select

from analystos.capabilities import binding
from analystos.capabilities import registry as reg
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
from analystos.core.errors import Conflict, InvalidInput, PolicyDenied, PreconditionFailed
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    Hypothesis,
    Notification,
    RunEvent,
    Schedule,
    ScheduleRun,
    SemanticMetric,
    User,
    Workspace,
    WorkspaceMember,
)
from analystos.services import definitions as defs
from analystos.services import pins
from analystos.services import runs as runs_svc
from analystos.services import schedules as sch_svc

WS = "ws_p7"
SPEC = {"method": "rate_by_segment", "asset": "sn.incident", "outcome": {"type": "is_true", "column": "made_sla"},
        "segment": {"type": "column", "column": "priority"}}


@pytest.fixture
def world(sqlite_db):
    with session_scope() as s:
        s.add(User(id="usr_owner", email="o@x", name="Owner", password_hash="x", is_admin=False, active=True, attributes={}))
        s.add(User(id="usr_view", email="v@x", name="Viewer", password_hash="x", is_admin=False, active=True, attributes={}))
        s.add(Workspace(id=WS, name="p7", description="", objective="Find the drivers of SLA breaches", autonomy_level=3,
                        status="active", settings={}, policy_version=1, created_by="usr_owner"))
        s.add(WorkspaceMember(workspace_id=WS, user_id="usr_owner", role="owner"))
        s.add(WorkspaceMember(workspace_id=WS, user_id="usr_view", role="viewer"))
    return {"owner": _user("usr_owner"), "viewer": _user("usr_view")}


def _user(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
    return u


def _metric(s, name: str, version: int, status: str, expression: str) -> SemanticMetric:
    m = SemanticMetric(id=f"smet_{name}_{version}", workspace_id=WS, name=name, version=version, status=status,
                       definition={"name": name, "expression": expression}, expression=expression,
                       normalized_expression=expression.lower(), proposed_by="usr_owner", proposed_via="user",
                       content_hash=f"h{version}")
    s.add(m)
    return m


def _playbook_spec(key: str = "playbook.weekly_sla", version: str = "1.0.0") -> dict:
    base = reg.current().get("playbook.investigate").model_dump(mode="json", exclude={"source"})
    return {**base, "id": key, "version": version, "summary": "Weekly SLA investigation (workspace-authored)"}


def test_linear_workflow_builder_shape_publishes_as_a_versioned_playbook(world):
    spec = {"apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.metadata_review",
            "version": "1.0.0", "summary": "Review selected table metadata", "determinism": "model",
            "side_effect": "read_source", "cost_class": "llm_large", "certification": {"status": "draft"},
            "tags": ["workspace_workflow"], "spec": {"framing": False, "steps": [
                {"key": "metadata", "title": "Discover metadata", "use": "agent.metadata", "after": [], "optional": False}]}}
    with session_scope() as s:
        owner = s.merge(world["owner"])
        row = defs.create_draft(s, owner, WS, DefinitionDraftIn(kind="playbook", key=spec["id"],
                                                                title="Metadata review", spec=spec))
        assert row.status == "draft" and row.spec["certification"]["status"] == "draft"
        defs.publish(s, owner, row, row.revision)
        ref, published = defs.resolve(s, WS, {"id": row.id})
        assert ref.version == 1 and ref.status == "published"
        assert published["spec"]["steps"][0]["use"] == "agent.metadata"


# ------------------------------------------------------------------------------------ lifecycle
def test_draft_publish_is_immutable_and_revision_checked(world):
    owner = world["owner"]
    with session_scope() as s:
        row = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="ml_spec", key="orders_forecast",
                                                                         spec={"task": "forecast", "horizon": 4}))
        assert (row.version, row.status, row.revision) == (1, "draft", 1)
        first_hash = row.content_hash
        with pytest.raises(Conflict, match="already has a draft"):
            defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="ml_spec", key="orders_forecast",
                                                                       spec={"task": "forecast"}))
        with pytest.raises(PreconditionFailed):
            defs.update_draft(s, s.merge(owner), row, DefinitionPatch(spec={"task": "forecast", "horizon": 8}), 7)
        defs.update_draft(s, s.merge(owner), row, DefinitionPatch(spec={"task": "forecast", "horizon": 8}), 1)
        assert row.revision == 2 and row.content_hash != first_hash
        defs.publish(s, s.merge(owner), row, 2)
        assert row.status == "published" and row.published_by == "usr_owner" and row.published_at is not None
        with pytest.raises(Conflict, match="immutable"):
            defs.update_draft(s, s.merge(owner), row, DefinitionPatch(spec={"task": "classify"}), row.revision)
        nxt = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="ml_spec", key="orders_forecast",
                                                                         spec={"task": "forecast", "horizon": 12}))
        assert nxt.version == 2
        ref, spec = defs.resolve(s, WS, {"kind": "ml_spec", "key": "orders_forecast"})
        assert ref.version == 1 and spec["horizon"] == 8  # newest *published*, never the draft
        with pytest.raises(InvalidInput):
            defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="ml_spec", key="bad", spec={"task": "dream"}))
        types = {e.type for e in s.scalars(select(RunEvent).where(RunEvent.workspace_id == WS))}
        assert {"definition.draft_saved", "definition.published"} <= types


def test_viewer_cannot_author_and_playbook_references_are_validated(world):
    with session_scope() as s:
        with pytest.raises(Exception, match="cannot perform"):
            defs.create_draft(s, s.merge(world["viewer"]), WS, DefinitionDraftIn(kind="query_tool", key="q", spec={}))
        bad = _playbook_spec()
        bad["spec"] = copy.deepcopy(bad["spec"])
        bad["spec"]["steps"][1]["use"] = "agent.no_such_agent"
        with pytest.raises(InvalidInput, match="not a registered agent"):
            defs.create_draft(s, s.merge(world["owner"]), WS, DefinitionDraftIn(kind="playbook", key="playbook.weekly_sla", spec=bad))
        with pytest.raises(InvalidInput, match="must equal the definition key"):
            defs.create_draft(s, s.merge(world["owner"]), WS, DefinitionDraftIn(kind="playbook", key="playbook.other",
                                                                                spec=_playbook_spec()))


def test_builtin_yaml_is_published_by_manifest_version_and_digest(world):
    with session_scope() as s:
        ref, spec = defs.resolve(s, WS, "playbook.investigate")
    manifest = reg.current().get("playbook.investigate")
    assert ref.source == "builtin" and ref.status == "published" and ref.version == manifest.version
    assert ref.content_hash == defs.manifest_digest(manifest) and spec["id"] == "playbook.investigate"


def test_triggers_refuse_drafts_outside_dev_and_retired_versions(world):
    owner = world["owner"]
    with session_scope() as s:
        row = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="playbook", key="playbook.weekly_sla",
                                                                         spec=_playbook_spec()))
        draft = {"id": row.id}
        for trigger in ("api", "schedule", "alert", "mcp"):
            with pytest.raises(PolicyDenied, match=f"is a draft: {trigger} runs published versions only"):
                runs_svc._capabilities(s, WS, playbook=None, definition=draft, pins=None, trigger=trigger)
        with pytest.raises(PolicyDenied, match="draft"):  # a schedule may not even name it
            sch_svc.create_schedule(s, s.merge(owner), WS, name="weekly", kind="reanalysis", cron="0 7 * * 1",
                                    config={"definition": draft})
        from analystos.services.monitors import create_monitor

        with pytest.raises(PolicyDenied, match="draft: monitor runs published versions only"):  # nor a monitor
            create_monitor(s, s.merge(owner), WS, name="mttr drift", kind="data_quality", config={"investigate_definition": draft})
        s.get(Workspace, WS).settings = {"environment": "dev"}
        s.flush()
        caps = runs_svc._capabilities(s, WS, playbook=None, definition=draft, pins=None, trigger="api")
        assert caps["definition"]["status"] == "draft" and caps["definition"]["dev"] is True
        s.get(Workspace, WS).settings = {}
        defs.publish(s, s.merge(owner), row, row.revision)
        caps = runs_svc._capabilities(s, WS, playbook="playbook.weekly_sla", definition=None, pins=None, trigger="api")
        assert caps["playbook"] == "playbook.weekly_sla" and caps["definition"]["version"] == 1
        defs.retire(s, s.merge(owner), row, reason="wrong SLA clock")
        with pytest.raises(PolicyDenied, match="retired"):
            runs_svc._capabilities(s, WS, playbook=None, definition={"id": row.id}, pins=None, trigger="api")


# ------------------------------------------------------------------------------------ bindings and pins
def _new_run(run_id: str, capabilities: dict | None = None, origin: dict | None = None) -> None:
    with session_scope() as s:
        s.add(AnalysisRun(id=run_id, workspace_id=WS, objective="Find the drivers of SLA breaches", status="NEW", autonomy_level=3,
                          plan={}, plan_version=0, scope={"hash": "h", "assets": ["sn.incident"]}, instructions=[], constraints={},
                          control="run", requested_by="usr_owner", summary={}, origin=origin or {"type": "user"},
                          capabilities=capabilities or {}))


def _bind(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        run.capabilities = binding.bind_run(s, run).to_json()
        return dict(run.capabilities)


def _baseline() -> str:
    """A completed run bound with metric mttr_hours v1, with one tested hypothesis."""
    with session_scope() as s:
        _metric(s, "mttr_hours", 1, "approved", "AVG(resolve_hours)")
    _new_run("run_base")
    caps = _bind("run_base")
    assert caps["definition"]["key"] == "playbook.investigate" and caps["definition"]["source"] == "builtin"
    assert caps["definition"]["content_hash"] == defs.manifest_digest(reg.current().get("playbook.investigate"))
    assert caps["semantic"]["metrics"] == {"mttr_hours": {"id": "smet_mttr_hours_1", "version": 1, "content_hash": "h1"}}
    assert caps["methods"] and all("@" in m for m in caps["methods"])
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_base")
        run.status, run.finished_at = "COMPLETED", utcnow()
        s.add(Hypothesis(id="hyp_1", workspace_id=WS, run_id="run_base", code="H-1", question="Does priority drive SLA?",
                         statement="Priority drives SLA breaches", spec=SPEC, status="supported", origin="agent"))
        s.add(Hypothesis(id="hyp_2", workspace_id=WS, run_id="run_base", code="H-2", question="Novel?",
                         statement="A novelty question", spec={**SPEC, "segment": {"type": "column", "column": "category"}},
                         status="supported", origin="novelty"))
    return "run_base"


def _schedule(owner: User) -> str:
    with session_scope() as s:
        sch = sch_svc.create_schedule(s, s.merge(owner), WS, name="weekly SLA", kind="reanalysis", cron="0 7 * * 1",
                                      config={"baseline_run_id": "run_base", "refresh_first": False})
        s.flush()
        assert sch.pins["revision"] == 1 and sch.pins["baseline_run_id"] == "run_base"
        assert [a["spec"] for a in sch.pins["analyses"]] == [SPEC]  # the novelty question is not part of the pinned set
        assert sch.pin_status["state"] == "current"
        return sch.id


def _fire_binding(schedule_id: str, run_id: str) -> dict:
    """What the next fire would bind: the run _reanalysis creates, planned by the engine's binding step."""
    with session_scope() as s:
        caps = runs_svc._capabilities(s, WS, playbook=None, definition=None, pins=pins.for_run(s.get(Schedule, schedule_id)),
                                      trigger="schedule")
    _new_run(run_id, caps, origin={"type": "schedule", "schedule_id": schedule_id, "replay": True})
    return _bind(run_id)


def _bumped(cap_id: str, version: str) -> reg.Snapshot:
    snap = reg.current()
    raw = snap.get(cap_id).model_dump(mode="json")
    newer = CapabilityManifest.model_validate({**raw, "version": version, "summary": raw["summary"] + " (pack upgrade)"})
    return reg.pinned(snap, [newer])


def test_pack_upgrade_shows_upgrade_and_the_next_fire_keeps_the_pinned_versions(world, monkeypatch):
    _baseline()
    sid = _schedule(world["owner"])
    base_refs = _bind_refs("run_base")
    cur = reg.current().get("agent.investigator").version  # whatever is installed; the test bumps its minor
    nxt = ".".join([cur.split(".")[0], str(int(cur.split(".")[1]) + 1), "0"])
    monkeypatch.setattr(reg, "_current", _bumped("agent.investigator", nxt))
    with session_scope() as s:
        st = pins.refresh(s, s.get(Schedule, sid))
    assert st.state == "upgrade_available" and st.upgrade_hash
    item = next(i for i in st.items if i.id == "agent.investigator")
    assert (item.pinned, item.current, item.state) == (f"agent.investigator@{cur}", f"agent.investigator@{nxt}", "newer")
    assert {"path": "version", "from": cur, "to": nxt} in item.diff
    caps = _fire_binding(sid, "run_fire1")
    assert f"agent.investigator@{cur}" in caps["refs"] and f"agent.investigator@{nxt}" not in caps["refs"]
    assert caps["refs"] == base_refs
    assert caps["pinned"]["analyses"][0]["spec"] == SPEC and "manifests" not in caps["pinned"]
    with session_scope() as s:
        notes = [n.title for n in s.scalars(select(Notification).where(Notification.workspace_id == WS))]
        assert "Upgrade available: weekly SLA" in notes
        assert s.scalar(select(RunEvent).where(RunEvent.type == "schedule.upgrade_available")) is not None


def _bind_refs(run_id: str) -> list[str]:
    with session_scope() as s:
        return list(s.get(AnalysisRun, run_id).capabilities["refs"])


def test_approving_a_new_metric_version_shows_an_upgrade_and_does_not_change_the_next_fire(world):
    _baseline()
    sid = _schedule(world["owner"])
    with session_scope() as s:  # what semantic.service.apply_decision does: v1 superseded by the approved v2
        s.get(SemanticMetric, "smet_mttr_hours_1").status = "deprecated"
        _metric(s, "mttr_hours", 2, "approved", "AVG(resolve_hours) FILTER (WHERE reopened = false)")
        s.flush()
        pins.refresh_workspace(s, WS)
        st = pins.status(s, s.get(Schedule, sid))
    assert st.state == "upgrade_available"
    item = next(i for i in st.items if i.type == "metric")
    assert (item.pinned, item.current) == ("mttr_hours@v1", "mttr_hours@v2")
    assert any(d["path"] == "expression" for d in item.diff)
    caps = _fire_binding(sid, "run_fire2")
    assert caps["semantic"]["metrics"]["mttr_hours"]["version"] == 1
    fire = type("R", (), {"capabilities": caps})
    assert binding.bound_metric_ids(fire) == ["smet_mttr_hours_1"]

    # The owner accepts: a new schedule revision whose pins are the current versions.
    with session_scope() as s:
        sch = s.get(Schedule, sid)
        with pytest.raises(PreconditionFailed):
            pins.accept_upgrade(s, s.merge(world["owner"]), sch, expected_revision=sch.revision + 5)
        with pytest.raises(Conflict, match="changed since it was reviewed"):
            pins.accept_upgrade(s, s.merge(world["owner"]), sch, expected_revision=sch.revision, upgrade_hash="stale")
        out = pins.accept_upgrade(s, s.merge(world["owner"]), sch, expected_revision=sch.revision, upgrade_hash=st.upgrade_hash)
        assert out["pin_revision"] == 2 and sch.revision == 2 and sch.pins["baseline_run_id"] is None
        assert sch.pins["semantic"]["metrics"]["mttr_hours"]["version"] == 2 and sch.pin_status["state"] == "current"
        assert [a["spec"] for a in sch.pins["analyses"]] == [SPEC]  # the question set carries over
    assert _fire_binding(sid, "run_fire3")["semantic"]["metrics"]["mttr_hours"]["version"] == 2


def test_a_retired_pin_blocks_the_schedule_and_notifies_the_owner(world, monkeypatch):
    _baseline()
    sid = _schedule(world["owner"])
    snap = reg.current()
    gone = {k: v for k, v in snap.manifests.items() if k != "agent.profiler"}
    monkeypatch.setattr(reg, "_current", reg.Snapshot(manifests=gone, digest="retired-profiler"))
    with session_scope() as s:
        sch = s.get(Schedule, sid)
        srun = sch_svc._claim(s, sch, utcnow(), "manual")
    sch_svc.execute(srun)
    with session_scope() as s:
        row = s.get(ScheduleRun, srun)
        assert row.status == "skipped" and row.result["blocked"] is True
        assert "agent.profiler@1.0.0" in row.error and "no longer installed" in row.error
        assert s.get(Schedule, sid).pin_status["state"] == "blocked"
        notes = list(s.scalars(select(Notification).where(Notification.workspace_id == WS, Notification.user_id == "usr_owner")))
        assert any(n.title == "Schedule blocked: weekly SLA" and "retired or rejected" in n.body for n in notes)
        assert s.scalar(select(AnalysisRun).where(AnalysisRun.id != "run_base")) is None  # nothing ran


def test_a_retired_workspace_definition_blocks_and_a_deprecated_one_warns(world):
    owner = world["owner"]
    with session_scope() as s:
        row = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="playbook", key="playbook.weekly_sla",
                                                                         spec=_playbook_spec()))
        defs.publish(s, s.merge(owner), row, row.revision)
        ref = defs.ref_of(row).model_dump()
        sch = Schedule(id="sch_def", workspace_id=WS, name="def pinned", kind="reanalysis", cron="0 7 * * 1", timezone="UTC",
                       config={}, enabled=True, owner_id="usr_owner", revision=1,
                       pins={"revision": 1, "playbook": row.key, "definition": ref, "manifests": {}, "methods": [],
                             "semantic": {"model": None, "metrics": {}}, "analyses": []}, pin_status={})
        s.add(sch)
        s.flush()
        assert pins.refresh(s, sch).state == "current"
        defs.deprecate(s, s.merge(owner), row, reason="superseded soon")
        assert s.get(Schedule, "sch_def").pin_status["state"] == "deprecated"
        defs.retire(s, s.merge(owner), row, reason="wrong SLA clock")
        st = s.get(Schedule, "sch_def").pin_status
        assert st["state"] == "blocked" and "wrong SLA clock" in st["blocking"][0]
        assert s.scalar(select(RunEvent).where(RunEvent.type == "schedule.blocked")) is not None


def test_changes_verdict_states_nothing_changed_only_when_true():
    from analystos.services.changes import verdict

    same = {"new": [], "persisting": [{}, {}], "changed": [], "resolved": [], "not_retested": [], "new_questions": [],
            "metrics": [{"name": "mttr", "value": 4.0, "previous_value": 4.0}]}
    assert verdict(same)["nothing_changed"] is True and verdict(same)["summary"].startswith("Nothing changed")
    moved = {**same, "metrics": [{"name": "mttr", "value": 5.0, "previous_value": 4.0}]}
    assert verdict(moved)["nothing_changed"] is False and "1 KPI(s) moved" in verdict(moved)["summary"]
    novel = {**same, "new_questions": [{}]}
    assert verdict(novel)["nothing_changed"] is True and "novelty round added 1 new question" in verdict(novel)["summary"]
    assert verdict({**same, "resolved": [{}]})["nothing_changed"] is False


def test_only_an_approved_newer_semantic_model_is_an_upgrade(world):
    from analystos.db.models import SemanticModel

    def model(version: int, status: str) -> SemanticModel:
        return SemanticModel(id=f"smod_{version}", workspace_id=WS, name="m", version=version, status=status, datasets=[],
                             relationships=[], custom_extensions=[], origin="user", content_hash=f"m{version}", created_by="u")
    with session_scope() as s:
        s.add(model(1, "approved"))
        sch = Schedule(id="sch_sem", workspace_id=WS, name="sem", kind="reanalysis", cron="0 7 * * 1", timezone="UTC", config={},
                       enabled=True, owner_id="usr_owner", revision=1, pin_status={},
                       pins={"revision": 1, "manifests": {}, "methods": [], "analyses": [],
                             "semantic": {"model": {"id": "smod_1", "version": 1, "content_hash": "m1"}, "metrics": {}}})
        s.add(sch)
        s.add(model(2, "proposed"))  # an agent's proposal is not a published or approved version
        s.flush()
        assert binding.semantic_versions(s, WS)["model"]["id"] == "smod_1"
        assert pins.status(s, sch).state == "current"
        s.add(model(3, "approved"))
        s.flush()
        st = pins.status(s, sch)
        assert st.state == "upgrade_available" and st.items[0].current == "semantic_model@v3"

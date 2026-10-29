"""P7-19 agent forms on the in-memory control plane: a form compiles to the same Agent manifest the YAML
path loads, is stored as a draft `agent` definition and published; the server refuses any grant beyond
the workspace's (by the form or the raw definitions API); a workspace playbook uses the published agent;
a run binds that exact version; a schedule keeps it after a newer version is published."""
from __future__ import annotations

import pytest
from tests.unit.playbook_signoff import publish_signed

from analystos.capabilities import agent_forms, binding
from analystos.capabilities import registry as reg
from analystos.capabilities.agents import body, to_agent_spec
from analystos.capabilities.validation import validate_kinds
from analystos.contracts.agent_form import AgentForm
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import DefinitionDraftIn
from analystos.core.errors import Forbidden, InvalidInput, PolicyDenied
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Schedule, User, Workspace, WorkspaceCapability, WorkspaceMember
from analystos.services import definitions as defs
from analystos.services import pins
from analystos.services import runs as runs_svc
from analystos.services import schedules as sch_svc

WS = "ws_agents"
KEY = "agent.table_notes"


@pytest.fixture
def world(sqlite_db):
    with session_scope() as s:
        for uid, _role in (("usr_owner", "owner"), ("usr_editor", "editor")):
            s.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
        s.add(Workspace(id=WS, name="agents", description="", objective="Document the incident table", autonomy_level=3,
                        status="active", settings={}, policy_version=1, created_by="usr_owner"))
        s.flush()
        for uid, role in (("usr_owner", "owner"), ("usr_editor", "editor")):
            s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
        for cap in ("skill.catalog_lookup", "skill.row_count"):
            s.add(WorkspaceCapability(workspace_id=WS, capability_id=cap, enabled=True, updated_by="usr_owner"))
    return {"owner": _user("usr_owner"), "editor": _user("usr_editor")}


def _user(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
    return u


def _form(**over) -> AgentForm:
    raw = {"key": KEY, "title": "Table notes", "purpose": "Note the size and columns of every selected table",
           "capabilities": ["skill.catalog_lookup", "skill.row_count"], "default_actions": ["skill.catalog_lookup"],
           "knowledge": {"sections": ["glossary"], "budget_chars": 4000}, "output": {"name": "Table notes"},
           "budget": {"llm_calls": 2, "usd": 0.05, "queries": 1, "max_steps": 2},
           "autonomy": {"mode": "propose", "pii_access": "none", "max_rows": 1000}}
    return AgentForm.model_validate({**raw, **over})


def _published_agent(owner: User, form: AgentForm | None = None) -> str:
    with session_scope() as s:
        row = agent_forms.save(s, s.merge(owner), WS, form or _form())
        defs.publish(s, s.merge(owner), row, row.revision)
        return row.id


def _playbook(key: str = "playbook.notes") -> dict:
    return {"apiVersion": "analystos/v1", "kind": "Playbook", "id": key, "version": "1.0.0",
            "summary": "Collect metadata, then note every table", "determinism": "model", "side_effect": "write_internal",
            "cost_class": "llm_large", "certification": {"status": "draft"}, "tags": ["workspace_workflow"],
            "spec": {"framing": False, "steps": [
                {"key": "metadata", "title": "Collect metadata", "use": "agent.metadata", "after": [], "optional": False},
                {"key": "notes", "title": "Write table notes", "use": KEY, "after": ["metadata"], "optional": False}]}}


# ------------------------------------------------------------------------------------ form -> manifest
def test_the_form_compiles_to_the_manifest_the_yaml_path_loads(world):
    with session_scope() as s:
        spec = agent_forms.compile_form(s, WS, _form(), version=1)
    m = CapabilityManifest.model_validate({**spec, "source": "pack:test"})
    assert not [p for p in validate_kinds({**reg.current().manifests, m.id: m}) if m.id in p]
    b = body(m)
    assert m.entry == "builtin:generic" and m.version == "1.0.0" and m.certification.status == "draft"
    assert b.tools == ["metadata.read", "sql.execute"]  # derived from the capabilities, never from the form
    assert b.model_purpose == "agent_actions" and b.budget.queries == 1 and b.policies.pii_access == "none"
    assert [a.model_dump() for a in b.default_actions] == [{"capability": "skill.catalog_lookup", "input": {"assets": "$scope.assets"}}]
    assert b.knowledge is not None and b.knowledge.sections == ["glossary"]
    spec_view = to_agent_spec(m)
    assert spec_view.output("agent_output") is not None and spec_view.skills == ["catalog_lookup", "row_count"]
    assert agent_forms.form_of(spec) == _form().model_dump(mode="json")


def test_deterministic_agents_need_a_fillable_default_action(world):
    with session_scope() as s:
        with pytest.raises(InvalidInput, match="needs at least one default action"):
            agent_forms.compile_form(s, WS, _form(default_actions=[], autonomy={"mode": "deterministic"}), version=1)
        with pytest.raises(InvalidInput, match="skill.row_count cannot be a default action"):
            agent_forms.compile_form(s, WS, _form(default_actions=["skill.row_count"]), version=1)
        spec = agent_forms.compile_form(s, WS, _form(autonomy={"mode": "deterministic"}), version=1)
    assert spec["spec"]["model_purpose"] is None and spec["spec"]["budget"]["llm_calls"] == 0
    assert spec["determinism"] == "deterministic"


def test_an_owner_saves_a_draft_and_publishes_an_immutable_version(world):
    owner = world["owner"]
    with session_scope() as s:
        row = agent_forms.save(s, s.merge(owner), WS, _form())
        assert (row.kind, row.key, row.version, row.status) == ("agent", KEY, 1, "draft")
        out = defs.out(row)
        assert out["form"]["capabilities"] == ["skill.catalog_lookup", "skill.row_count"]
        row = agent_forms.save(s, s.merge(owner), WS, _form(purpose="Note the size of every selected table only"),
                               definition_id=row.id, expected_revision=row.revision)
        assert row.revision == 2 and row.spec["spec"]["goal"].endswith("table only")
        defs.publish(s, s.merge(owner), row, row.revision)
        assert row.status == "published"
        with pytest.raises(Exception, match="immutable"):
            agent_forms.save(s, s.merge(owner), WS, _form(), definition_id=row.id, expected_revision=row.revision)
        v2 = agent_forms.save(s, s.merge(owner), WS, _form(title="Table notes v2"))
        assert v2.version == 2 and v2.spec["version"] == "2.0.0"


# ------------------------------------------------------------------------------------ grants never widen
def test_an_editor_cannot_author_an_agent(world):
    with session_scope() as s, pytest.raises(Forbidden):
        agent_forms.save(s, s.merge(world["editor"]), WS, _form())


@pytest.mark.parametrize(("form", "reason"), [
    ({"capabilities": ["skill.catalog_lookup", "skill.column_profile_lookup"]}, "skill.column_profile_lookup@1.0.0 is not enabled"),
    ({"capabilities": ["skill.catalog_lookup", "tool.superset_publish"]}, "not executable by a declarative agent"),
    ({"autonomy": {"mode": "propose", "pii_access": "allowed"}}, "pii_access allowed exceeds the workspace policy"),
    ({"budget": {"llm_calls": 2, "usd": 50, "queries": 1}}, "cost budget of at most the workspace run budget"),
    ({"budget": {"llm_calls": 2, "usd": 0.05, "queries": 10_000}}, "query budget of at most the workspace limit"),
])
def test_the_form_cannot_grant_beyond_the_workspace(world, form, reason):
    with session_scope() as s:
        with pytest.raises(PolicyDenied, match=reason) as exc:
            agent_forms.save(s, s.merge(world["owner"]), WS, _form(**form))
        assert exc.value.details["problems"]


def test_the_raw_definitions_route_gets_the_same_grant_check(world):
    owner = world["owner"]
    with session_scope() as s:
        spec = agent_forms.compile_form(s, WS, _form(), version=1)
        cases = [
            ({**spec, "entry": "python:analystos.agents.metadata:run"}, "generic only"),
            ({**spec, "requires": ["skill.column_profile_lookup"]}, "requires or permissions"),
            ({**spec, "spec": {**spec["spec"], "tools": ["metadata.read", "sql.execute", "superset.publish"]}},
             "not the tool of a granted capability"),
            ({**spec, "spec": {**spec["spec"], "capabilities": ["skill.*"]}}, "no patterns"),
            ({**spec, "spec": {**spec["spec"], "model_purpose": "planning"}}, "agent_actions only"),
            ({**spec, "side_effect": "write_external"}, "cannot write outside the platform"),
        ]
        for bad, reason in cases:
            with pytest.raises((PolicyDenied, InvalidInput), match=reason):
                defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="agent", key=KEY, spec=bad))
        shadow = {**spec, "id": "agent.metadata"}
        with pytest.raises(PolicyDenied, match="cannot replace it"):
            defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="agent", key="agent.metadata", spec=shadow))
        # A browser that claims certification still stores a draft: publication decides, per workspace.
        row = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(
            kind="agent", key=KEY, spec={**spec, "certification": {"status": "certified", "evidence": "trust me"}}))
        assert row.spec["certification"] == {"status": "draft", "evidence": None}


# ------------------------------------------------------------------------------------ playbooks and runs
def _new_run(run_id: str, capabilities: dict, origin: dict | None = None, autonomy: int = 2) -> None:
    with session_scope() as s:
        s.add(AnalysisRun(id=run_id, workspace_id=WS, objective="Document the incident table", status="NEW", autonomy_level=autonomy,
                          plan={}, plan_version=0, scope={"hash": "h", "assets": ["sn.incident"]}, instructions=[], constraints={},
                          control="run", requested_by="usr_owner", summary={}, origin=origin or {"type": "user"},
                          capabilities=capabilities))


def _bind(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        run.capabilities = binding.bind_run(s, run).to_json()
        return dict(run.capabilities)


def test_a_workspace_playbook_uses_only_a_published_agent_and_a_run_binds_that_version(world):
    owner = world["owner"]
    with session_scope() as s:
        draft = agent_forms.save(s, s.merge(owner), WS, _form())
        with pytest.raises(InvalidInput, match=f"uses {KEY}, which is not a registered agent"):
            defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="playbook", key="playbook.notes", spec=_playbook()))
        defs.publish(s, s.merge(owner), draft, draft.revision)
        agent_id = draft.id
        pb = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="playbook", key="playbook.notes", spec=_playbook()))
        publish_signed(s, s.merge(owner), pb)
        caps = runs_svc._capabilities(s, WS, playbook=None, definition={"id": pb.id}, pins=None, trigger="api")
    _new_run("run_a", caps)
    bound = _bind("run_a")
    assert f"{KEY}@1.0.0" in bound["refs"] and "skill.row_count@1.0.0" in bound["refs"]
    agent = bound["manifests"][KEY]
    assert agent["source"] == f"definition:{agent_id}" and agent["certification"]["status"] == "certified"
    assert [s["key"] for s in binding.run_playbook(type("R", (), {"capabilities": bound})).body.model_dump()["steps"]] == \
        ["metadata", "notes"]

    # The owner later disables a capability the agent was granted: the next run refuses the step.
    with session_scope() as s:
        s.query(WorkspaceCapability).filter_by(workspace_id=WS, capability_id="skill.row_count").one().enabled = False
    _new_run("run_b", caps)
    with session_scope() as s, pytest.raises(PolicyDenied, match="exceeds this workspace's grants"):
        binding.bind_run(s, s.get(AnalysisRun, "run_b"))


def test_a_scheduled_run_keeps_its_published_agent_version(world):
    owner = world["owner"]
    v1 = _published_agent(owner)
    with session_scope() as s:
        pb = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="playbook", key="playbook.notes", spec=_playbook()))
        publish_signed(s, s.merge(owner), pb)
        caps = runs_svc._capabilities(s, WS, playbook=None, definition={"id": pb.id}, pins=None, trigger="api")
    _new_run("run_base", caps)
    _bind("run_base")
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_base")
        run.status, run.finished_at = "COMPLETED", utcnow()
    with session_scope() as s:
        sch = sch_svc.create_schedule(s, s.merge(owner), WS, name="nightly notes", kind="reanalysis", cron="0 7 * * *",
                                      config={"baseline_run_id": "run_base", "refresh_first": False})
        s.flush()
        sid = sch.id
        assert sch.pins["manifests"][KEY]["source"] == f"definition:{v1}" and sch.pin_status["state"] == "current"

    v2 = _published_agent(owner, _form(title="Table notes v2", purpose="Note only the size of every selected table"))
    with session_scope() as s:
        st = pins.status(s, s.get(Schedule, sid))
    assert st.state == "upgrade_available"
    item = next(i for i in st.items if i.id == KEY)
    assert (item.pinned, item.current, item.state) == (f"{KEY}@1.0.0", f"{KEY}@2.0.0", "newer")

    def fire(run_id: str) -> dict:
        with session_scope() as s:
            fire_caps = runs_svc._capabilities(s, WS, playbook=None, definition=None,
                                               pins=pins.for_run(s.get(Schedule, sid)), trigger="schedule")
        _new_run(run_id, fire_caps, origin={"type": "schedule", "schedule_id": sid, "replay": True}, autonomy=3)
        return _bind(run_id)

    fired = fire("run_fire1")  # the fire binds v1, not the newer published v2
    assert f"{KEY}@1.0.0" in fired["refs"] and fired["manifests"][KEY]["source"] == f"definition:{v1}"
    assert fired["manifests"][KEY]["spec"]["goal"] == _form().purpose

    with session_scope() as s:
        sch = s.get(Schedule, sid)
        pins.accept_upgrade(s, s.merge(owner), sch, expected_revision=sch.revision, upgrade_hash=st.upgrade_hash)
        assert sch.pins["manifests"][KEY]["source"] == f"definition:{v2}" and sch.pin_status["state"] == "current"
    assert fire("run_fire2")["manifests"][KEY]["spec"]["role"] == "Table notes v2"

    with session_scope() as s:  # retiring the pinned version blocks the schedule
        defs.retire(s, s.merge(owner), s.get(defs.Definition, v2), reason="wrong table set")
        assert s.get(Schedule, sid).pin_status["state"] == "blocked"

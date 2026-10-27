"""Declarative agents (P4-X03): the generic propose -> validate -> execute runtime, and enforcement of
every agent manifest field (capabilities, tools, model purposes, budget, policies). Model proposals
come from tests/fakes.py FakeTransport; the control plane is SQLite; no services."""
from __future__ import annotations

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import generic
from analystos.capabilities import registry
from analystos.capabilities.agents import to_agent_spec
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.core.errors import BudgetExceeded
from analystos.db.base import session_scope
from analystos.db.models import (
    AgentMessage,
    AnalysisRun,
    RunTask,
    Source,
    SourceAsset,
    SourceColumn,
    ToolDefinition,
    ToolExecution,
    User,
    Workspace,
    WorkspaceCapability,
    WorkspaceMember,
    WorkspacePolicy,
)
from analystos.llm.router import ModelRouter
from analystos.runtime.context import RunContext, Services
from analystos.tools.registry import BUILTIN_TOOLS

ASSET = "sn.incident"


@pytest.fixture
def world(sqlite_db, monkeypatch):
    from analystos.services import platform_settings

    state = {"settings": PlatformSettings()}
    monkeypatch.setattr(platform_settings, "get", lambda: state["settings"])
    registry.reload(entry_points=False)
    with session_scope() as s:
        s.add(User(id="usr_1", email="a@x", name="A", password_hash="-", active=True, attributes={}))
        s.add(Workspace(id="ws_1", name="W", autonomy_level=3, created_by="usr_1", policy_version=1, settings={}))
        s.add(WorkspaceMember(workspace_id="ws_1", user_id="usr_1", role="analyst"))
        s.add(WorkspacePolicy(workspace_id="ws_1", version=1, document={}, created_by="usr_1"))
        s.add(WorkspaceCapability(workspace_id="ws_1", capability_id="agent.data_dictionary", enabled=True, updated_by="usr_1"))
        for t in BUILTIN_TOOLS:
            s.add(ToolDefinition(id=t.tool_id, spec=t.model_dump(), enabled=True))
        s.add(Source(id="src_1", workspace_id="ws_1", kind="servicenow", name="SN", config={}, status="ready"))
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="sn", name="incident",
                          source_name="incident", selected=True, row_count=1200, business_name="Incident",
                          description="IT incidents", semantics={"role": "fact"}, stats={}))
        for i, (name, sem, tags, key, profile) in enumerate([
                ("number", "id", [], True, {"null_rate": 0.0, "distinct": 1200}),
                ("priority", "categorical", [], False, {"null_rate": 0.0, "distinct": 4, "top_values": [{"value": "1"}]}),
                ("caller_email", "text", ["pii"], False, {"null_rate": 0.1, "distinct": 700}),
                ("made_sla", "boolean", [], False, {"null_rate": 0.02, "distinct": 2})]):
            s.add(SourceColumn(asset_id="ast_1", name=name, ordinal=i, data_type="text", semantic_type=sem, tags=tags,
                               is_key=key, profile=profile, semantics={}))
        s.add(AnalysisRun(id="run_1", workspace_id="ws_1", objective="Document the incident table for new analysts",
                          status="RUNNING", autonomy_level=3, plan={"steps": []}, plan_version=1, scope={"hash": "h"},
                          instructions=[], constraints={}, control="run", requested_by="usr_1", summary={}, origin={},
                          capabilities={}))
        s.add(RunTask(id="tsk_1", run_id="run_1", key="dictionary", agent_id="data_dictionary", title="d", status="RUNNING",
                      depends_on=[], input={}, output={}, plan_version=1, seq=0))
    yield state
    registry.reload(entry_points=False)


def _manifest(**spec_changes) -> CapabilityManifest:
    m = registry.current().get("agent.data_dictionary")
    return m.model_copy(update={"spec": {**m.spec, **spec_changes}})


def _ctx(manifest: CapabilityManifest, proposals: list | None = None, *, key: bool = True, policy: dict | None = None):
    calls = iter(proposals or [])
    transport = FakeTransport(chat=lambda payload: chat_json(next(calls)))
    router = ModelRouter(transport=transport, api_key_lookup=lambda env: "k" if key else None)
    with session_scope() as s:
        run, task = s.get(AnalysisRun, "run_1"), s.get(RunTask, "tsk_1")
        user, ws = s.get(User, "usr_1"), s.get(Workspace, "ws_1")
        s.expunge_all()
    scope = DataScope(workspace_id="ws_1", user_id="usr_1", role="analyst", source_ids=["src_1"], assets=[ASSET],
                      asset_sources={ASSET: "src_1"}, columns={ASSET: ["number", "priority", "caller_email", "made_sla"]})
    ctx = RunContext(run=run, task=task, user=user, workspace=ws, policy=WorkspacePolicyDoc(**(policy or {})), scope=scope,
                     agent=to_agent_spec(manifest), services=Services(router=router, gateway=None), manifest=manifest)
    return ctx, transport


def _modes(world, **modes):
    world["settings"] = PlatformSettings(llm=LLMSettings(purpose_modes=modes, cache_enabled=False))


def _statuses(out):
    return [(r["capability"], r["status"]) for r in out["actions"]]


def test_deterministic_path_runs_the_default_actions_without_a_model(world):
    _modes(world, agent_actions="off")
    ctx, transport = _ctx(_manifest())
    out = generic.run_agent(ctx, _manifest(output=None))
    assert transport.chat_calls == [] and out["mode"] == "deterministic" and ctx.usage["llm_calls"] == 0
    assert _statuses(out) == [("skill.catalog_lookup", "executed")]
    table = out["results"][0]["result"]["tables"][0]
    assert table["asset"] == ASSET and table["row_count"] == 1200 and table["key_columns"] == ["number"]
    # policies.pii_access: none (the manifest) removes the PII column the workspace would allow
    assert [c["name"] for c in table["columns"]] == ["number", "priority", "made_sla"]
    with session_scope() as s:  # the action passed the tool gate as metadata.read and was audited
        assert s.query(ToolExecution).filter_by(tool_id="metadata.read", status="ok").count() == 1
    _modes(world, agent_actions="auto")  # auto: default actions exist, so the rule path answers
    ctx, transport = _ctx(_manifest())
    assert generic.run_agent(ctx, _manifest(output=None))["mode"] == "deterministic" and transport.chat_calls == []


def test_model_proposals_are_validated_and_bad_actions_rejected_with_reasons(world):
    _modes(world, agent_actions="always")
    rounds = [
        {"actions": [{"capability": "skill.catalog_lookup", "input": {"assets": [ASSET]}},
                     {"capability": "skill.no_such", "input": {}},
                     {"capability": "skill.row_count", "input": {"asset": ASSET}},
                     {"capability": "skill.column_profile_lookup", "input": {"asset": ASSET, "columns": "priority"}},
                     {"capability": "skill.column_profile_lookup", "input": {"asset": "hr.salaries"}}], "done": False},
        {"actions": [{"capability": "skill.column_profile_lookup", "input": {"asset": ASSET, "columns": ["priority"]}}],
         "done": True, "summary": "The incident table holds 1200 rows; priority has 4 values."},
    ]
    ctx, transport = _ctx(_manifest(), rounds)
    out = generic.run_agent(ctx, _manifest(output=None))
    assert out["mode"] == "model" and len(transport.chat_calls) == 2
    reasons = {r["capability"]: r["reason"] for r in out["actions"] if r["status"] == "rejected"}
    assert reasons["skill.no_such"] == "skill.no_such is not a registered capability"
    assert reasons["skill.row_count"] == "skill.row_count is not bound to agent.data_dictionary"
    assert [r["reason"] for r in out["actions"] if r["capability"] == "skill.column_profile_lookup" and r["status"] == "rejected"] == [
        "input does not match skill.column_profile_lookup input_schema at columns: 'priority' is not of type 'array'",
        "hr.salaries outside the authorized scope of this run"]
    assert [s for c, s in _statuses(out) if s == "executed"] == ["executed", "executed"]
    assert "not a registered capability" in transport.chat_calls[1]["messages"][1]["content"]  # summarised back
    assert out["summary_source"] == "model" and "1200" in out["summary"]
    assert ctx.usage["llm_calls"] == 2
    with session_scope() as s:
        said = [m.content for m in s.query(AgentMessage).all()]
    assert any("skill.no_such rejected" in m for m in said)


def test_a_summary_with_numbers_not_in_the_results_is_replaced_by_the_template(world):
    _modes(world, agent_actions="always")
    rounds = [{"actions": [{"capability": "skill.catalog_lookup", "input": {"assets": [ASSET]}}], "done": True,
               "summary": "The incident table holds 98765 rows."}]
    ctx, _ = _ctx(_manifest(), rounds)
    out = generic.run_agent(ctx, _manifest(output=None))
    assert out["summary_source"] == "template" and "98765" not in out["summary"]


def test_the_llm_call_budget_caps_the_loop(world):
    _modes(world, agent_actions="always")
    manifest = _manifest(budget={"llm_calls": 1, "max_steps": 5}, output=None)
    again = {"actions": [{"capability": "skill.catalog_lookup", "input": {"assets": [ASSET]}}], "done": False}
    ctx, transport = _ctx(manifest, [again, again, again])
    out = generic.run_agent(ctx, manifest)
    assert len(transport.chat_calls) == 1 and ctx.usage["llm_calls"] == 1 and out["mode"] == "model"
    with session_scope() as s:
        assert any("llm_calls budget (1) is used up" in m.content for m in s.query(AgentMessage).all())


def test_no_model_available_falls_back_to_the_default_actions(world):
    _modes(world, agent_actions="always")
    ctx, transport = _ctx(_manifest(), key=False)
    out = generic.run_agent(ctx, _manifest(output=None))
    assert out["mode"] == "deterministic" and _statuses(out) == [("skill.catalog_lookup", "executed")]


def test_policy_and_enablement_reject_actions(world):
    _modes(world, agent_actions="off")
    with session_scope() as s:
        s.get(WorkspacePolicy, 1).document = {"tool_denylist": ["metadata.read"]}
    ctx, _ = _ctx(_manifest())
    out = generic.run_agent(ctx, _manifest(output=None))
    assert out["actions"][0]["status"] == "rejected" and "tool_denied_by_workspace_policy" in out["actions"][0]["reason"]

    with session_scope() as s:
        s.get(WorkspacePolicy, 1).document = {}
        s.add(WorkspaceCapability(workspace_id="ws_1", capability_id="skill.catalog_lookup", enabled=False, updated_by="usr_1"))
    ctx, _ = _ctx(_manifest())
    out = generic.run_agent(ctx, _manifest(output=None))
    assert out["actions"][0]["reason"] == "capability skill.catalog_lookup@1.0.0 is disabled for this workspace"

    unbound_tool = _manifest(tools=["profile.table"], output=None)  # the agent's tool binding is enforced by the gate
    with session_scope() as s:
        s.query(WorkspaceCapability).filter_by(capability_id="skill.catalog_lookup").delete()
    ctx, _ = _ctx(unbound_tool)
    out = generic.run_agent(ctx, unbound_tool)
    assert "tool_not_bound_to_agent_data_dictionary" in out["actions"][0]["reason"]


def test_validation_refuses_external_side_effects_uncertified_actions_and_spent_query_budgets(world):
    agent = _manifest()
    ctx, _ = _ctx(agent)
    base = registry.current().get("skill.catalog_lookup")
    exporter = base.model_copy(update={"id": "skill.exporter", "side_effect": "write_external"})
    draft = base.model_copy(update={"id": "skill.draft_lookup", "certification": base.certification.model_copy(update={"status": "draft"})})
    counter = registry.current().get("skill.row_count")
    allowed = {m.id: m for m in (exporter, draft, counter)}
    explicit = {"skill.exporter": True, "skill.draft_lookup": True, "skill.row_count": True}

    def reason(capability, inputs):
        with pytest.raises(generic.ActionRejected) as err:
            generic.validate_action(ctx, agent, allowed, {"capability": capability, "input": inputs}, explicit)
        return err.value.reason

    assert "writes outside the platform" in reason("skill.exporter", {"assets": [ASSET]})
    assert "is draft, not certified: autonomous runs" in reason("skill.draft_lookup", {"assets": [ASSET]})  # run autonomy 3
    assert reason("skill.row_count", {"asset": ASSET}) == "agent.data_dictionary query budget (0) exhausted for this step"
    assert "malformed action" in reason("skill.row_count", "not an object")


def test_run_sql_enforces_the_agent_query_budget(world):
    class _Gateway:
        def run_sql_for(self, scope, **_):
            runner = lambda sql, **kw: {"sql": sql}  # noqa: E731
            runner.dialect = "postgres"
            return runner

    manifest = _manifest(budget={"queries": 1})
    ctx, _ = _ctx(manifest)
    ctx.services = Services(router=ctx.services.router, gateway=_Gateway())
    run = ctx.run_sql()
    assert run("SELECT 1") == {"sql": "SELECT 1"} and ctx.usage["queries"] == 1
    with pytest.raises(BudgetExceeded, match="query budget \\(1\\) exhausted"):
        run("SELECT 2")


def test_python_agents_only_route_their_declared_purposes_within_budget(world):
    from analystos.agents.common import llm_json

    _modes(world)
    investigator = registry.current().get("agent.investigator")  # model_purposes: hypothesis/follow-up
    budget = investigator.spec["budget"]["llm_calls"]  # read from the manifest: the agent's own limit
    ctx, transport = _ctx(investigator, [{"hypotheses": []}] * (budget + 1))
    assert llm_json(ctx, "planning", "planning.v1", {"objective": "x"}) == (None, "purpose_not_declared")
    for _ in range(budget):
        data, model = llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1", {"objective": "x"})
        assert data == {"hypotheses": []} and model
    assert llm_json(ctx, "follow_up_generation", "follow_up_generation.v1", {"objective": "x"}) == (None, "agent_budget_exhausted")
    assert len(transport.chat_calls) == budget

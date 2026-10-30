"""FND-006 agent contract (no services): the `kind: Agent` manifest is the only source of the contract
(the AgentSpec view, the agent_definition rows and the admin API derive from it), and its `knowledge`
and `output_contract` fields are enforced: the context compiler never puts an undeclared knowledge
section in an agent's prompt, and an agent cannot persist an undeclared output or content that fails
its declared schema. The control plane is SQLite; model calls never happen."""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from tests.unit.test_generic_agent import ASSET, _ctx, _manifest, _modes, world  # noqa: F401  (fixture)

from analystos.agents import common, generic
from analystos.capabilities import registry
from analystos.capabilities.agents import (
    GENERIC_OUTPUT_SCHEMA,
    body,
    contract_for,
    declared_outputs,
    enforce_output,
    to_agent_spec,
)
from analystos.context.compiler import KNOWLEDGE_SECTIONS, KnowledgeItem
from analystos.contracts.platform import ContextSettings, LLMSettings, PlatformSettings
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.contracts.registry import AgentKnowledge, AgentSpec
from analystos.core.errors import InvalidInput, OutputContractViolation
from analystos.db.base import session_scope
from analystos.db.models import AgentDefinition, AgentMessage, Artifact

VALID_SPEC = {"method": "rate_by_segment", "asset": ASSET, "outcome": {"column": "made_sla"},
              "segment": {"column": "priority"}}


def _agents() -> list:
    return registry.load(entry_points=False).list("Agent")


# ------------------------------------------------------------------------------------ one source of truth
def test_every_agent_view_field_equals_its_manifest():
    agents = _agents()
    assert len(agents) >= 18
    for m in agents:
        b, spec = body(m), to_agent_spec(m)
        assert (spec.id, spec.capability_id, spec.version, spec.entry) == (m.id.split(".", 1)[1], m.id, m.version, m.entry)
        assert (spec.name, spec.description, spec.capabilities) == (b.role, b.goal, list(m.tags))
        assert spec.skills == [c.split(".", 1)[1] for c in b.capabilities if c.startswith("skill.")]
        assert (spec.tools, spec.model_purposes, spec.prompt_version, spec.phase) == (b.tools, b.purposes, b.prompt_version, b.phase)
        assert spec.model_profile == (b.purposes[0] if b.purposes else "none")
        assert (spec.policies, spec.budget, spec.knowledge, spec.behaviours) == (b.policies, b.budget, b.knowledge, b.behaviours)
        assert spec.output_contract == declared_outputs(b)
        assert (spec.certification, spec.source) == (m.certification.status, m.source)


def test_generic_output_implies_its_declaration():
    spec = to_agent_spec(registry.load(entry_points=False).get("agent.data_dictionary"))
    decl = spec.output("agent_output")
    assert decl is not None and decl.schema_ == GENERIC_OUTPUT_SCHEMA


def _admin_agents(s) -> dict:
    from analystos.api.routers import admin

    return {a["id"]: a for a in admin.agents(None, s)}


def test_admin_api_and_rows_derive_from_the_manifest_never_the_row(sqlite_db):
    from analystos.api.routers import admin
    from analystos.tools.registry import get_agent_spec, load_agent_specs, sync_agent_definitions

    registry.reload(entry_points=False)
    with session_scope() as s:
        sync_agent_definitions(s, load_agent_specs())  # what `analystos seed` does for agents
    with session_scope() as s:  # a stale or hand-edited row (same version): the drift FND-006 closes
        row = s.get(AgentDefinition, "investigator")
        row.spec = {**row.spec, "tools": ["superset.publish"], "name": "Edited", "verification_required": True}
    with session_scope() as s:
        shown = _admin_agents(s)
        assert get_agent_spec(s, "investigator").tools == ["context.search", "artifact.write", "sql.execute"]
    snap = registry.current()
    assert set(shown) == {m.id.split(".", 1)[1] for m in snap.list("Agent")}
    for m in snap.list("Agent"):
        view = shown[m.id.split(".", 1)[1]]
        expected = to_agent_spec(m).model_dump(mode="json", by_alias=True)
        assert {k: v for k, v in view.items() if k != "enabled"} == expected  # every field shown equals the manifest
    assert shown["investigator"]["output_contract"] == [{"type": "hypothesis", "schema": "contract:analysis.AnalysisSpec"}]
    assert shown["integration"]["enabled"] is False and shown["investigator"]["enabled"] is True  # the row's switch
    with session_scope() as s:
        sync_agent_definitions(s)  # a reload refreshes the cache even without a version bump
    with session_scope() as s:
        assert s.get(AgentDefinition, "investigator").spec == to_agent_spec(snap.get("agent.investigator")).model_dump(
            mode="json", by_alias=True)
    admin_user = SimpleNamespace(id="usr_admin")
    with pytest.raises(InvalidInput, match="capability manifest"):
        admin.register_agent({"id": "rogue", "name": "Rogue", "description": ""}, admin_user)
    with session_scope() as s, pytest.raises(InvalidInput, match="capability manifest"):
        admin.patch_agent("investigator", admin.EnabledPatch(spec={"tools": ["superset.publish"]}), admin_user, s)
    with session_scope() as s:
        out = admin.patch_agent("investigator", admin.EnabledPatch(enabled=False), admin_user, s)
    assert out["enabled"] is False and out["tools"] == ["context.search", "artifact.write", "sql.execute"]
    registry.reload(entry_points=False)


@pytest.mark.parametrize(("change", "problem"), [
    ({"output_contract": [{"type": "spreadsheet"}]}, "unknown output type spreadsheet"),
    ({"output_contract": [{"type": "chart", "schema": "contract:bi.NoSuchSpec"}]}, "has no Pydantic model NoSuchSpec"),
    ({"output_contract": [{"type": "chart", "schema": "bi.ChartSpec"}]}, "schema must be contract:"),
    ({"output_contract": [{"type": "chart", "schema": {"type": "no-such-type"}}]}, "not a valid JSON Schema"),
    ({"output_contract": [{"type": "chart"}, {"type": "chart"}]}, "more than once"),
    ({"knowledge": {"sections": ["glossary", "secrets"]}}, "knowledge.sections"),
    ({"knowledge": {"sections": ["glossary"], "note": "x"}}, "Extra inputs are not permitted"),
])
def test_bad_contract_declarations_fail_the_load(change, problem):
    raw = registry.load(entry_points=False).get("agent.data_dictionary").model_dump(mode="json")
    raw = {**raw, "id": "agent.bad_contract", "spec": {**raw["spec"], **change}}
    snap = registry.load(entry_points=False, extra=[("test", raw)], strict=False)
    assert any("agent.bad_contract" in p and problem in p for p in snap.problems), snap.problems


def test_spec_v3_spelling_of_knowledge_loads():
    assert AgentKnowledge.model_validate({"purposes": ["glossary"], "budget_chars": 12000}).sections == ["glossary"]


# ------------------------------------------------------------------------------------ knowledge contract
@pytest.fixture
def knowledge(monkeypatch):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False), context=ContextSettings())
    requested: list[list[str]] = []

    def load(session, workspace_id, sections, **kw):
        requested.append(list(sections))
        return [KnowledgeItem(id=f"{s}:1", section=s, name=f"SLA breach {s}", text=f"SLA breach priority {s} guidance " * 4,
                              source="test") for s in sections]

    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    monkeypatch.setattr("analystos.context.compiler.load_knowledge", load)
    monkeypatch.setattr(common, "context_header", lambda ctx: {"workspace": "w"})
    monkeypatch.setattr(common, "session_scope", nullcontext)
    return requested


def _kctx(agent):
    scope = DataScope(workspace_id="ws1", user_id="u1", role="analyst", assets=[ASSET])
    return SimpleNamespace(workspace=SimpleNamespace(id="ws1"), policy=WorkspacePolicyDoc(), scope=scope, run=None,
                           agent=agent)


def _sections(compiled) -> set[str]:
    return {k for k in compiled.body if k in KNOWLEDGE_SECTIONS}


def _receipt_sections(compiled) -> set[str]:
    return {r["section"] for r in compiled.receipts if r["section"] != "catalog"}


def test_an_agent_only_receives_its_declared_knowledge_sections(knowledge):
    objective = {"objective": "What drives SLA breach by priority?"}
    investigator = contract_for(None, "investigator")
    full = common.compile_for(_kctx(investigator), "hypothesis_generation", objective, catalog=None)
    assert _sections(full) == set(investigator.knowledge_sections) & set(KNOWLEDGE_SECTIONS)

    narrow = investigator.model_copy(update={"knowledge": AgentKnowledge(sections=["glossary"])})
    compiled = common.compile_for(_kctx(narrow), "hypothesis_generation", objective, catalog=None)
    assert _sections(compiled) == _receipt_sections(compiled) == {"glossary"}
    assert knowledge[-1] == ["glossary"]  # undeclared sections are not even loaded

    none = investigator.model_copy(update={"knowledge": None})  # no knowledge block: no knowledge at all
    compiled = common.compile_for(_kctx(none), "hypothesis_generation", objective, catalog=None)
    assert _sections(compiled) == set() and compiled.receipts == []

    sql = contract_for(None, "sql")  # the Ask path carries the sql agent's contract too
    compiled = common.compile_for(_kctx(sql), "sql_generation", {"question": "SLA breach by priority"}, catalog=None,
                                  objective="SLA breach by priority")
    assert _sections(compiled) <= {"glossary", "metrics"} and _sections(compiled)

    # a caller with no agent contract (feedback, the knowledge preview) keeps the purpose profile
    compiled = common.compile_for(_kctx(None), "hypothesis_generation", objective, catalog=None)
    assert _sections(compiled) == set(PlatformSettings().context.profiles["hypothesis_generation"].sections) - {"catalog"}


def test_the_agent_knowledge_budget_caps_the_knowledge_sections(knowledge):
    agent = contract_for(None, "investigator")
    capped = agent.model_copy(update={"knowledge": AgentKnowledge(sections=agent.knowledge_sections, budget_chars=1000)})
    objective = {"objective": "What drives SLA breach by priority?"}
    compiled = common.compile_for(_kctx(capped), "hypothesis_generation", objective, catalog=None)
    uncapped = common.compile_for(_kctx(agent), "hypothesis_generation", objective, catalog=None)
    assert len(compiled.receipts) < len(uncapped.receipts)
    assert any(o.get("reason") == "agent knowledge budget" for o in compiled.omitted)


# ------------------------------------------------------------------------------------ output contract
def test_enforce_output_refuses_undeclared_and_off_schema_outputs():
    viz = contract_for(None, "visualization")
    with pytest.raises(OutputContractViolation, match="does not declare output dataset") as exc:
        enforce_output(viz, "visualization", "dataset", {})
    assert exc.value.details["reason"] == "undeclared" and exc.value.details["declared"] == ["chart", "dashboard"]
    with pytest.raises(OutputContractViolation, match="does not match its declared schema at chart_type") as exc:
        enforce_output(viz, "visualization", "chart", {"key": "c", "title": "t", "chart_type": "pie3d", "intent": "x",
                                                      "dataset": "d"})
    assert exc.value.details["reason"] == "schema" and exc.value.code == "output_contract_violation"
    enforce_output(viz, "visualization", "chart", {"key": "c", "title": "t", "chart_type": "bar", "intent": "comparison",
                                                  "dataset": "d"})
    with pytest.raises(OutputContractViolation, match="at issues"):  # inline JSON Schema
        enforce_output(contract_for(None, "data_quality"), "data_quality", "quality_report", {"issues": "none"})
    with pytest.raises(OutputContractViolation, match="has no manifest"):
        enforce_output(None, "ghost", "chart", {})
    legacy = AgentSpec(id="legacy", name="Legacy", description="")  # a row-only agent declares nothing
    with pytest.raises(OutputContractViolation, match="does not declare output chart"):
        enforce_output(legacy, "legacy", "chart", {})


def _artifacts() -> list[tuple[str, str]]:
    with session_scope() as s:
        return sorted((a.type, a.name) for a in s.query(Artifact).all())


def test_agent_artifacts_are_checked_against_the_contract_before_they_are_written(world):  # noqa: F811
    from analystos.artifacts.registry import save_artifact

    with pytest.raises(OutputContractViolation, match="does not declare output profile"), session_scope() as s:
        save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="profile", name="p", content={"a": 1},
                      creator_agent="data_dictionary")
    with pytest.raises(OutputContractViolation, match="declared schema"), session_scope() as s:
        save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="agent_output", name="bad", content={"mode": "x"},
                      creator_agent="data_dictionary")
    with pytest.raises(OutputContractViolation, match="has no manifest"), session_scope() as s:
        save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="chart", name="c", content={}, creator_agent="ghost")
    assert _artifacts() == []  # nothing refused was written
    with session_scope() as s:  # a signed-in user's artifact is governed by its route, not an agent contract
        save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="report", name="r", content={"a": 1}, creator_user="usr_1")

    _modes(world, agent_actions="off")  # the generic runtime's own output satisfies its implied declaration
    ctx, _ = _ctx(_manifest())
    out = generic.run_agent(ctx, _manifest())
    assert out["artifact_id"] and _artifacts() == [("agent_output", "Data dictionary"), ("report", "r")]


def test_bound_contract_governs_the_run(world):  # noqa: F811
    """The contract comes from the version the run bound, not whatever the registry holds now."""
    from analystos.artifacts.registry import save_artifact
    from analystos.db.models import AnalysisRun

    bound = _manifest(output_contract=[{"type": "profile"}]).model_dump(mode="json")
    with session_scope() as s:
        s.get(AnalysisRun, "run_1").capabilities = {"manifests": {"agent.data_dictionary": bound}}
    with session_scope() as s:
        save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="profile", name="p", content={"a": 1},
                      creator_agent="data_dictionary")
    assert _artifacts() == [("profile", "p")]


def test_hypotheses_off_the_contract_are_dropped_with_their_reason(world):  # noqa: F811
    from analystos.agents.investigator import _contracted

    ctx, _ = _ctx(registry.current().get("agent.investigator"))
    good = {"statement": "Priority drives SLA breaches", "spec": VALID_SPEC}
    bad = {"statement": "Nonsense", "spec": {**VALID_SPEC, "method": "astrology"}}
    assert _contracted(ctx, [good, bad]) == [good]
    with session_scope() as s:
        said = [m.content for m in s.query(AgentMessage).all()]
    assert any("Hypothesis dropped" in m and "unknown analysis method 'astrology'" in m for m in said)
    ctx, _ = _ctx(_manifest())  # an agent that does not declare hypotheses cannot write any
    with pytest.raises(OutputContractViolation, match="does not declare output hypothesis"):
        _contracted(ctx, [good])


def test_run_records_are_checked_through_the_run_context(world):  # noqa: F811
    ctx, _ = _ctx(registry.current().get("agent.data_scientist"))
    experiment = {"method": "rate_by_segment", "params": VALID_SPEC, "result": {"supported": True}, "query_ids": ["q1"],
                  "role": "primary"}
    ctx.check_output("experiment", experiment)
    with pytest.raises(OutputContractViolation, match="at role"):
        ctx.check_output("experiment", {**experiment, "role": "guess"})
    with pytest.raises(OutputContractViolation, match="does not declare output insight"):
        ctx.check_output("insight", {"title": "t"})

"""Template playbooks (S06-T5): the six packs against their golden fixtures (tests/fixtures/playbooks/templates.yaml).

Each template declares inputs, steps, outputs, default limits and approval gates; its plan and plan hash
are pinned; every step resolves to a registered behaviour; the complexity router sends the fixture's goal
question to it; and its replan boundaries answer the S06 reason codes."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from analystos.autonomy.complexity import ambiguity_flags, route
from analystos.capabilities import registry
from analystos.capabilities.agents import entry_for
from analystos.capabilities.playbook import parse
from analystos.contracts.goals import REPLAN_REASONS, ComplexityFeatures, RunLimits
from analystos.runtime.plan import plan_hash

FIXTURES = yaml.safe_load((Path(__file__).resolve().parents[1] / "fixtures" / "playbooks" / "templates.yaml")
                          .read_text(encoding="utf-8"))
TEMPLATES = sorted(FIXTURES)


@pytest.fixture(scope="module")
def snap():
    return registry.load(packs_dir=None, entry_points=False)


def test_six_templates_ship_besides_investigate(snap):
    ids = {m.id for m in snap.list("Playbook")}
    assert {f"playbook.{t}" for t in TEMPLATES} <= ids and len(TEMPLATES) == 6
    assert "playbook.investigate" in ids


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_matches_its_golden_fixture(snap, name):
    fx = FIXTURES[name]
    pb = parse(snap.get(fx["id"]))
    assert pb.manifest.version == fx["version"]
    p3, p2 = pb.build_plan(fx["question"], autonomy_level=3), pb.build_plan(fx["question"], autonomy_level=2)
    assert [s["key"] for s in p3["steps"]] == fx["steps_autonomy_3"]
    assert [s["key"] for s in p2["steps"]] == fx["steps_autonomy_2"]
    assert {s["key"]: s["depends_on"] for s in p3["steps"]} == fx["depends_on"]
    assert pb.approval_gates() == fx["approval_gates"]
    assert [k for k, s in pb.steps.items() if s.type == "side_effect"] == fx["side_effects"]
    assert list(pb.body.outputs) == fx["outputs"]
    assert dict(pb.body.default_limits) == fx["default_limits"]
    assert [i.name for i in pb.body.inputs if i.required] == fx["inputs_required"]
    assert plan_hash(p3, constraints={}, scope_hash="scope-fixture", plan_version=1,
                     capabilities=[pb.ref]) == fx["plan_hash_autonomy_3"]


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_declares_inputs_outputs_limits_and_gates(snap, name):
    pb = parse(snap.get(FIXTURES[name]["id"]))
    m = pb.manifest
    assert m.certification.status == "tested" and "template" in m.tags
    assert pb.body.inputs and pb.body.inputs[0].name == "question" and pb.body.inputs[0].required
    assert pb.body.outputs
    RunLimits.model_validate(pb.body.default_limits)  # the default limits are valid goal limits
    assert set(pb.body.default_limits) >= {"max_steps", "max_duration_s", "max_cost_usd", "max_replans"}
    assert pb.body.default_limits["max_steps"] >= len(pb.steps)  # the whole plan fits its own step budget
    assert pb.body.router is not None and pb.body.router.intents
    for step in pb.steps.values():  # publication only under an approval gate that names its side effect
        if step.type == "side_effect":
            assert any(s.approval_for == step.key for s in pb.steps.values())
    if m.side_effect != "write_external":
        assert not [s for s in pb.steps.values() if s.type == "side_effect"]


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_step_resolves_to_a_registered_behaviour(snap, name):
    pb = parse(snap.get(FIXTURES[name]["id"]))
    for use, behaviour, _optional in pb.uses():
        manifest = snap.get(use)
        assert manifest.kind == "Agent"
        entry = entry_for(manifest, behaviour)
        assert entry is not None, (name, use, behaviour)
        if entry.startswith("python:"):
            assert callable(registry.resolve_python(entry)), entry


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_router_sends_the_fixture_question_to_its_template(snap, name):
    fx = FIXTURES[name]
    feats = ComplexityFeatures(**fx["route"]["features"], ambiguity_flags=ambiguity_flags(fx["question"]))
    d = route(feats, question=fx["question"], snapshot=snap, runnable=lambda pid: None)
    assert (d.playbook, d.tier) == (fx["route"]["playbook"], fx["route"]["tier"])
    for k, v in parse(snap.get(fx["id"])).body.default_limits.items():
        assert d.limits[k] == v  # the template's defaults, over the tier's


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_answers_every_replan_reason(snap, name):
    pb = parse(snap.get(FIXTURES[name]["id"]))
    for code in REPLAN_REASONS:
        trigger = pb.trigger_for(code)
        if code == "budget_downgrade":
            assert trigger is None
        else:
            assert pb.reset_keys(trigger), (name, code)  # each reason resets something in every template


def test_template_specific_boundaries_and_method_preferences(snap):
    variance, dq = parse(snap.get("playbook.variance")), parse(snap.get("playbook.dq_review"))
    assert variance.trigger_for("empty_result") == "empty_result"  # declared by the template itself
    assert variance.trigger_for("missing_dataset") == "redirect"  # the fallback
    assert dq.trigger_for("quality_failure") == "quality_failure" and dq.reset_keys("quality_failure") == {"quality", "finalize"}
    assert dq.manifest.determinism == "deterministic" and not dq.body.framing
    prefer = {n: parse(snap.get(f"playbook.{n}")).steps["hypotheses"].params["prefer_method_tags"]
              for n in ("kpi_review", "variance", "funnel", "cohort")}
    from analystos import methods

    tags = {t for m in methods.manifests().values() for t in m.tags}
    assert all(set(v) <= tags for v in prefer.values())  # every preferred tag names registered methods

    def best(tag_list: list[str]) -> set[str]:  # the methods a template ranks first
        w = {t: len(tag_list) - i for i, t in enumerate(tag_list)}
        score = {n: sum(w.get(t, 0) for t in m.tags) for n, m in methods.manifests().items()}
        return {n for n, v in score.items() if v == max(score.values())}

    assert {"segment", "time_series"} <= set(methods.manifests()[next(iter(best(prefer["variance"])))].tags)
    assert "cohort" in methods.manifests()[next(iter(best(prefer["cohort"])))].tags


def test_templates_are_off_until_a_workspace_enables_them(snap):
    from analystos.capabilities import enablement

    for name in TEMPLATES:
        m = snap.get(f"playbook.{name}")
        assert not enablement.is_enabled(m, snap, {})
        assert enablement.usable(m, snap, {m.id: True}, autonomous_run=False) is None
        assert "not certified" in enablement.usable(m, snap, {m.id: True}, autonomous_run=True)


def test_step_params_are_data_only():
    from pydantic import ValidationError

    from analystos.capabilities.playbook import Step

    Step(key="hypotheses", title="t", use="agent.investigator", params={"prefer_methods": ["trend"], "k": 3})
    with pytest.raises(ValidationError, match="scalar or a list of scalars"):
        Step(key="hypotheses", title="t", use="agent.investigator", params={"x": {"nested": "dict"}})

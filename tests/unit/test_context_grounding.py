"""Meaning must survive prompt compilation and remain bounded by reviewed evidence."""
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from analystos.agents import common
from analystos.agents.investigator import metric_candidates, question_coverage
from analystos.context.compiler import CompiledContext
from analystos.contracts.grounding import BindingIn
from analystos.contracts.platform import PlatformSettings
from analystos.contracts.policy import DataScope
from analystos.services.context_grounding import allowed_ref, binding_state, pinned_prompt_context, resolve_known


@pytest.fixture
def scope():
    return DataScope(workspace_id="w", user_id="u", role="editor", source_ids=["src"], assets=["s.incident"],
                     columns={"s.incident": ["breach", "team", "cost", "opened_at"]}, denied_columns=["*.secret"])


def test_pinned_reviewed_facts_reach_shared_model_input_with_receipts(monkeypatch, scope):
    settings = PlatformSettings()
    settings.context.cache_enabled = False
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: settings)
    monkeypatch.setattr(common, "context_header", lambda ctx: {})
    monkeypatch.setattr(common, "_compile", lambda ctx, purpose, profile, settings, *, required, **kw: (
        CompiledContext(purpose=purpose, header={}, body=required), True))
    ctx = SimpleNamespace(scope=scope, run=SimpleNamespace(objective="SLA", capabilities={}, origin={"reviewed_context": {
        "version": 7, "content_hash": "h", "facts": [{"key": "k", "subject": "SLA", "review_state": "reviewed", "value": "PINNED_FACT"}]}}))
    for purpose in ["planning", "hypothesis_generation", "semantic_modeling"]:
        compiled = common.compile_for(ctx, purpose, {"objective": "SLA"}, catalog=[])
        assert compiled.body["reviewed_business_context"]["facts"][0]["value"] == "PINNED_FACT"
        assert compiled.receipts[0]["version"] == 7


def test_pinned_unreviewed_foreign_and_injection_facts_are_withheld(scope):
    run = SimpleNamespace(origin={"reviewed_context": {"version": 1, "facts": [
        {"subject": "other.table.col", "review_state": "reviewed", "value": "foreign"},
        {"subject": "s.incident.secret", "review_state": "reviewed", "value": "secret"},
        {"subject": "Term", "review_state": "suggested", "value": "guess"},
    ]}})
    context, receipts = pinned_prompt_context(run, scope)
    assert context["facts"] == [] and context["withheld_count"] == 3 and receipts == []


@pytest.mark.parametrize("body,expected", [
    ({"state": "draft", "columns": []}, "draft"),
    ({"state": "approved", "columns": ["s.incident.breach"]}, "resolved"),
    ({"state": "approved", "columns": ["s.incident.removed"]}, "outdated"),
    ({"state": "approved", "columns": ["s.incident.secret"]}, "unavailable"),
    ({"state": "approved", "columns": ["x.incident.breach"]}, "unavailable"),
    ({"state": "approved", "columns": [], "expires_on": "2026-10-24"}, "outdated"),
    ({"state": "approved", "columns": [], "effective_from": "2026-10-26"}, "not_yet_effective"),
    ({"state": "retired", "columns": []}, "outdated"),
    ({"state": "approved", "columns": [], "source_ids": ["foreign"]}, "unavailable"),
])
def test_lifecycle_and_scope(scope, body, expected):
    assert binding_state(body, scope, today=date(2026, 10, 25)) == expected


def test_unknown_columns_cannot_be_claimed_as_mapped(scope):
    assert allowed_ref("s.incident.breach", scope)
    assert not allowed_ref("s.incident.secret", scope)
    assert not allowed_ref("s.incident.invented", scope)


def test_conflicting_reviewed_definitions_remain_ambiguous(scope):
    known = [{"id": "a", "term": "Breach", "definition": "Flag true means breached", "columns": ["s.incident.breach"]},
             {"id": "b", "term": "Breach", "definition": "Flag false means breached", "columns": ["s.incident.breach"]}]
    resolved = resolve_known("Explain breach by team", known, [], scope)
    assert len(resolved) == 2 and {r["state"] for r in resolved} == {"ambiguous"}


def test_six_generic_tests_do_not_cover_a_different_outcome():
    proposals = [{"spec": {"method": "numeric_by_segment", "outcome": {"column": "cost"}, "segment": {"column": "team"}}}] * 6
    assert question_coverage("Why did revenue decline?", proposals)["missing"] == ["revenue"]
    assert question_coverage("Compare cost by team", proposals)["covered"]


def test_reviewed_metric_planned_before_hypotheses(scope):
    grounding = {"resolutions": [{"id": "b", "revision": 2, "term": "Breach rate", "state": "resolved",
                  "metric": {"outcome_column": "s.incident.breach", "true_value": False}}]}
    proposals = metric_candidates(grounding, {"s.incident": {"breach": "boolean", "team": "categorical"}}, scope)
    assert proposals[0]["spec"]["method"] == "rate_by_segment"
    assert proposals[0]["spec"]["outcome"] == {"type": "equals", "column": "breach", "value": False}


def test_approval_requires_owner_evidence_examples_and_typed_contract():
    with pytest.raises(ValidationError):
        BindingIn(term="SLA", definition="Service agreement", state="approved")
    with pytest.raises(ValidationError):
        BindingIn(term="SLA", definition="Service agreement", state="approved", owner_id="u", evidence="policy", examples="case", kind="metric")
    with pytest.raises(ValidationError):
        BindingIn(term="Exclude", definition="Exclude cancelled", state="approved", owner_id="u", evidence="policy", examples="case", kind="population_rule")


@pytest.mark.parametrize("evidence,refs,accepted", [
    (["known"], ["s.incident.breach"], True),
    (["invented"], ["s.incident.breach"], False),
    (["known"], ["s.incident.secret"], False),
    (["known"], ["s.incident.cost"], False),
])
def test_model_resolution_needs_reviewed_evidence_and_its_exact_columns(monkeypatch, scope, evidence, refs, accepted):
    from analystos.contracts.grounding import ResolveIn
    from analystos.services import context_grounding as svc

    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: scope)
    monkeypatch.setattr(svc, "candidates", lambda *a: [{"id": "known", "term": "SLA", "definition": "Reviewed flag", "columns": ["s.incident.breach"], "synonyms": []}])
    monkeypatch.setattr(svc, "domains_for", lambda *a: set())
    monkeypatch.setattr(svc, "scoped_bindings", lambda *a: [])
    monkeypatch.setattr(svc, "catalog", lambda *a: [])
    router = SimpleNamespace(mode=lambda p: "auto", available=lambda p, c: True,
        complete_json=lambda *a, **k: SimpleNamespace(data={"resolutions": [{"term": "Service target", "evidence_ids": evidence, "columns": refs, "reason": "Use reviewed policy"}]}))
    report = svc.resolve(SimpleNamespace(merge=lambda u: u), object(), "w", ResolveIn(question="Compare service target by team"), router=router, call_ctx=SimpleNamespace())
    assert bool([r for r in report["resolutions"] if r["by"] == "model_with_reviewed_evidence"]) == accepted


def test_population_rule_becomes_typed_filter_but_policy_prose_does_not(monkeypatch, scope):
    from analystos.agents import investigator as inv
    monkeypatch.setattr(inv, "validated_lookups", lambda ctx: [])
    ctx = SimpleNamespace(scope=scope, _resolved_grounding={"resolutions": [
        {"state": "resolved", "kind": "population_rule", "predicate": {"column": "s.incident.team", "op": "!=", "value": "Cancelled"}},
        {"state": "resolved", "kind": "policy", "definition": "Escalate immediately"}]})
    accepted, rejected = inv._accept(ctx, [{"statement": "Breach differs by team", "spec": {"asset": "s.incident", "method": "rate_by_segment",
        "outcome": {"type": "is_true", "column": "breach"}, "segment": {"type": "column", "column": "team"}}}],
        {"s.incident": {"breach": "boolean", "team": "categorical"}}, origin="heuristic", seen=set())
    assert not rejected
    assert accepted[0]["spec"]["filters"] == [{"column": "team", "op": "!=", "value": "Cancelled", "origin": "policy"}]


def test_required_brief_is_refused_not_truncated_when_over_budget(monkeypatch, scope):
    from analystos.contracts.platform import PurposeProfile

    settings = PlatformSettings()
    settings.context.cache_enabled = False
    settings.context.profiles["hypothesis_generation"] = PurposeProfile(sections=[], max_chars=1000)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: settings)
    value = "Reviewed business definition. " * 100
    ctx = SimpleNamespace(scope=scope, run=SimpleNamespace(objective="SLA", capabilities={}, origin={"reviewed_context": {
        "version": 7, "facts": [{"key": "k", "subject": "SLA", "review_state": "reviewed", "value": value}]}}))
    compiled = common.compile_for(ctx, "hypothesis_generation", {"objective": "SLA"}, catalog=[])
    assert compiled.refused
    assert compiled.body["reviewed_business_context"]["facts"][0]["value"] == value


def test_run_resolution_uses_the_agent_model_gate(monkeypatch, scope):
    from analystos.contracts.grounding import ResolveIn
    from analystos.services import context_grounding as svc
    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: scope)
    monkeypatch.setattr(svc, "candidates", lambda *a: [])
    monkeypatch.setattr(svc, "domains_for", lambda *a: set())
    monkeypatch.setattr(svc, "scoped_bindings", lambda *a: [])
    monkeypatch.setattr(svc, "catalog", lambda *a: [])
    monkeypatch.setattr(common, "compile_for", lambda *a, **k: CompiledContext(purpose="glossary_suggestion", header={}, body={}))
    gated = []
    def gate(ctx, purpose, prompt_name, payload, **kwargs):
        gated.append((purpose, prompt_name))
        return None, "agent_budget_exhausted"
    monkeypatch.setattr(common, "llm_json", gate)
    router = SimpleNamespace(mode=lambda p: "auto", available=lambda *a: True)
    report = svc.resolve(SimpleNamespace(merge=lambda u: u), object(), "w", ResolveIn(question="Explain service performance"),
                         router=router, call_ctx=SimpleNamespace(), agent_ctx=SimpleNamespace())
    assert gated == [("glossary_suggestion", "context_resolution.v1")]
    assert report["model_status"] == "agent_budget_exhausted"


def test_grouping_a_reviewed_breach_flag_reuses_meaning_but_new_weights_do_not():
    from analystos.services.context_grounding import existing_metric_view
    meanings = [{"term": "SLA breach", "state": "resolved", "definition": "A breach is made_sla = false.", "columns": ["s.incident.made_sla"]},
                {"term": "Assignment group", "state": "resolved", "definition": "The responsible team", "columns": ["s.incident.team"]}]
    proposal = {"term": "SLA breach rate by assignment group", "definition": "Proportion of incidents that breach SLA by group",
                "columns": ["s.incident.made_sla", "s.incident.team"], "missing_contract_fields": []}
    assert existing_metric_view(proposal, meanings)
    assert not existing_metric_view({**proposal, "term": "Weighted SLA breach rate"}, meanings)
    assert not existing_metric_view({**proposal, "missing_contract_fields": ["denominator"]}, meanings)
    assert not existing_metric_view({**proposal, "columns": ["s.incident.invented"]}, meanings)
    assert not existing_metric_view({**proposal, "term": "SLA breach growth by assignment group"}, meanings)


def test_reporting_period_uses_explicit_year_or_reviewed_policy_without_guessing():
    from analystos.services.question_constraints import period_filters, reporting_period
    assert reporting_period("Investigate September 2026", []) == ("2026-09-01", "2026-10-01")
    assert reporting_period("Investigate September", []) is None
    known = [{"term": "Reporting year", "definition": "The reporting year is 2026"}]
    assert reporting_period("Investigate September", known) == ("2026-09-01", "2026-10-01")
    assert period_filters("Since September 2026", "s.incident.opened_at", ("2026-09-01", "2026-10-01")) == [
        {"column": "s.incident.opened_at", "op": ">=", "value": "2026-09-01"}]
    assert len(period_filters("Investigate September 2026 before changing the process", "s.incident.opened_at", ("2026-09-01", "2026-10-01"))) == 2


def test_model_filters_cannot_invent_literals_or_restricted_columns():
    from analystos.services.question_constraints import grounded_filters
    known = [{"id": "k", "term": "Priority 1", "definition": "Priority code 1", "columns": ["s.incident.priority"]}]
    candidate = {"column": "s.incident.priority", "op": "=", "value": 1, "evidence_ids": ["k"]}
    accepted, omitted = grounded_filters("Investigate P1 incidents", [candidate], known, lambda c: c == "s.incident.priority")
    assert accepted and not omitted
    assert not grounded_filters("Investigate P1 incidents", [{**candidate, "value": 5}], known, lambda c: True)[0]
    assert not grounded_filters("Investigate P1 incidents", [{**candidate, "column": "secret.salary"}], known, lambda c: False)[0]
    dictionary = [{**known[0], "definition": "P1 uses priority = 100"}]
    assert grounded_filters("Investigate P1 incidents", [{**candidate, "value": 100}], dictionary, lambda c: True)[0]


def test_question_reporting_filters_are_applied_to_the_executable_spec(monkeypatch, scope):
    from analystos.agents import investigator as inv
    monkeypatch.setattr(inv, "validated_lookups", lambda ctx: [])
    ctx = SimpleNamespace(scope=scope, _resolved_grounding={"analysis_filters": [
        {"column": "s.incident.opened_at", "op": ">=", "value": "2026-09-01"},
        {"column": "s.incident.opened_at", "op": "<", "value": "2026-10-01"}]})
    accepted, rejected = inv._accept(ctx, [{"statement": "Breach differs by team", "spec": {"asset": "s.incident", "method": "rate_by_segment",
        "outcome": {"type": "is_true", "column": "breach"}, "segment": {"type": "column", "column": "team"}}}],
        {"s.incident": {"breach": "boolean", "team": "categorical", "opened_at": "datetime"}}, origin="heuristic", seen=set())
    assert not rejected
    assert [(f["column"], f["op"], f["value"]) for f in accepted[0]["spec"]["filters"]] == [
        ("opened_at", ">=", "2026-09-01"), ("opened_at", "<", "2026-10-01")]

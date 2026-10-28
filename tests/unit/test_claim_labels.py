"""S07-T3 (v1 §7.2): every finding carries a claim label decided by code, and the numbers guard refuses
causal wording unless the label is `supported_association` and the method is a causal design."""
from __future__ import annotations

import pytest

from analystos.evidence import claims
from analystos.evidence.facts import bind_finding, facts_from
from analystos.methods import registry as method_registry

RATE_SPEC = {"method": "rate_by_segment", "asset": "src_x.incident",
             "outcome": {"type": "equals", "column": "made_sla", "value": False, "label": "missed SLA"},
             "segment": {"type": "column", "column": "priority", "label": "priority"}}
RATE_STAT = {"test": "chi_square_independence", "n": 20000, "p_value": 1e-14, "p_adjusted": 1e-12, "effect_size": 0.21,
             "effect_label": "cramers_v",
             "highlights": {"top_segment": "P1", "top_rate": 0.412, "top_n": 2000, "baseline_segment": "P4",
                            "baseline_rate": 0.131, "baseline_n": 9000, "rate_ratio": 3.14, "n_groups": 4},
             "groups": [{"segment": s, "n": n} for s, n in (("P1", 2000), ("P2", 4000), ("P3", 5000), ("P4", 9000))]}
GOOD = "Priority P1 misses SLA 41.2% of the time versus 13.1% for P4 (3.1x)."


def _bind(text: str, spec=RATE_SPEC, stat=RATE_STAT, label=None):
    return bind_finding(spec, stat, facts_from(spec, stat), text, claim_label=label)


def test_causal_language_blocked():
    assert _bind(GOOD).ok
    for causal in ("Priority P1 drives SLA misses: 41.2% of P1 incidents miss SLA.",
                   "P1 incidents miss SLA at 41.2% because of their priority.",
                   "Being P1 leads to a 41.2% SLA miss rate.",
                   "The P1 priority caused 41.2% of P1 incidents to miss SLA."):
        b = _bind(causal, label="supported_association")  # even a supported association is not a causal design
        assert not b.ok and any("causal language" in p for p in b.problems), causal
        assert any("not a causal design" in p for p in b.problems)
    b = _bind("P1 incidents miss SLA at 41.2%, due to priority.", label="possible_contributor")
    assert not b.ok and "labelled possible_contributor" in b.problems[-1]


def test_associational_words_and_masked_group_labels_pass():
    assert not claims.causal_terms("SLA misses are associated with P1 and coincide with backlog; the top driver is P1")
    stat = {**RATE_STAT, "highlights": {**RATE_STAT["highlights"], "top_segment": "Due to vendor"},
            "groups": [{"segment": "Due to vendor", "n": 2000}, {"segment": "P4", "n": 9000}]}
    text = "Due to vendor misses SLA 41.2% of the time versus 13.1% for P4 (3.1x)."
    assert _bind(text, stat=stat).ok  # a segment value is a label, not a causal claim
    assert not _bind("P4 is due to vendor delays.", stat=stat).ok  # outside the label it is causal wording


def test_causal_wording_needs_a_supported_association_from_a_causal_design():
    manifest = {"apiVersion": "analystos/v1", "kind": "Method", "id": "method.ab_test", "version": "0.1.0",
                "summary": "A/B test", "entry": "python:tests.unit.test_methods_registry:RowShare",
                "side_effect": "read_source", "spec": {"order": 95, "causal_design": True}}
    method_registry.reset(method_registry.build(extra=[("entrypoint:acme", manifest)], entry_points=False))
    try:
        assert claims.causal_design_methods() == ["ab_test"]
        assert claims.causal_allowed("supported_association", "ab_test")
        assert not claims.causal_allowed("possible_contributor", "ab_test")
        assert not claims.causal_allowed("supported_association", "rate_by_segment")
        assert claims.causal_problems("treatment drives conversion", label="supported_association", method="ab_test") == []
    finally:
        method_registry.reset()
    assert claims.causal_design_methods() == []  # none registered today (A/B, diff-in-diff are P2)


@pytest.mark.parametrize(("method", "verified", "failed", "strength", "label"), [
    ("rate_by_segment", True, [], "strong", "supported_association"),
    ("rate_by_segment", True, [], "weak", "possible_contributor"),
    ("trend", True, [], "moderate", "observed"),
    ("rate_by_segment", False, ["second_method"], None, "rejected"),
    ("rate_by_segment", False, ["sample_size"], None, "unknown"),
    ("trend", False, ["evidence_complete", "sample_size"], None, "unknown"),
    ("trend", False, ["reproducible_rerun", "sample_size"], None, "rejected"),
])
def test_label_follows_the_verdict(method, verified, failed, strength, label):
    checks = [{"check": c, "passed": False} for c in failed] + [{"check": "method_fit", "passed": True}]
    assert claims.label_for(method, verified=verified, checks=checks, strength=strength) == label


def test_draft_labels_and_legacy_findings():
    assert claims.draft_label("rate_by_segment") == "possible_contributor"
    assert claims.draft_label("trend") == "unknown" and claims.claim_kind("pareto") == "observation"

    class Legacy:
        claim_label = None
        status = "verified"
    assert claims.label_of(Legacy()) == "supported_association"
    Legacy.status = "failed_verification"
    assert claims.label_of(Legacy()) == "rejected"
    assert set(claims.LABELS) == {"observed", "supported_association", "possible_contributor", "rejected", "unknown"}

"""P7-07: evaluation gates. Owner thresholds are versioned in config/eval_gates.yaml; a seeded regression
fails the gate (a broken binder that accepts fabricated numbers, a void checker that never voids, a cost
rise past the tolerance, a precision drop); live tiers are skipped with a reason, deterministic ones
never. No services."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from evaluation import gates as G
from evaluation import grounding

from analystos.contracts.evidence import Binding


def test_the_gates_file_is_versioned_and_every_ci_tier_has_a_runner():
    gates = G.load()
    assert gates.version >= 1 and len(gates.digest) == 64
    assert any(c["version"] == gates.version for c in gates.changelog)
    ci = G.deterministic_ci_tiers(gates, database=True)
    assert {"grounding", "analytical_component", "cost", "ask_fake", "ask_off"} <= set(ci)
    assert all(t in G.RUNNERS for t in ci)
    g = gates.tiers["grounding"].metrics
    assert g["fabricated_accepted"].max == 0 and g["bound_numeric_share"].min == 1.0 and g["void_on_change_recall"].min == 1.0
    assert {n for n, t in gates.tiers.items() if t.kind == "live"} == {"ask_live", "analytical_platform_live", "model_matrix"}


def test_a_version_without_a_changelog_entry_is_refused(tmp_path):
    current = G.load().version
    raw = G.GATES_FILE.read_text(encoding="utf-8").replace(f"version: {current}\n", f"version: {current + 1}\n", 1)
    path = tmp_path / "eval_gates.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match=f"version {current + 1} has no changelog entry"):
        G.load(path)


def test_check_flags_regressions_and_missing_metrics():
    gates = G.load()
    good = {"max_rise": 0.02, "missing_baseline": 0}
    assert G.check("cost", good, gates) == []
    assert G.check("cost", {**good, "max_rise": 0.11}, gates) == ["cost: max_rise 0.11 > 0.1"]
    assert G.check("cost", {"missing_baseline": 0}, gates) == ["cost: max_rise missing from the suite's metrics"]


# ------------------------------------------------------------------------------------ seeded regressions
def _accept_everything(spec, stat, facts, *texts):
    return Binding(ok=True, mentions=[], problems=[])


def test_the_grounding_suite_passes_on_the_platform_binder():
    metrics = grounding.run(seeds=(1,), domains=("itsm",)).metrics()
    assert G.check("grounding", {**metrics, "findings": 99}, G.load()) == []
    assert metrics["fabricated_variants"] > 0 and metrics["swapped_variants"] > 0 and metrics["fabricated_accepted"] == 0


def test_a_binder_that_accepts_fabricated_numbers_fails_the_gate():
    """The seeded regression: the numbers guard stops checking values (what the legacy guard did for 1, 2, 3)."""
    from analystos.evidence.facts import bind_finding

    def lenient(spec, stat, facts, *texts):  # binds as before, then forgives every problem
        b = bind_finding(spec, stat, facts, *texts)
        return Binding(ok=True, mentions=b.mentions, problems=[])

    result = G.run_gates(["grounding"], runners={"grounding": lambda: G.grounding_metrics(seeds=(1,), domains=("itsm",),
                                                                                          bind=lenient)})
    tier = result["tiers"]["grounding"]
    assert result["passed"] is False and tier["status"] == "failed"
    assert any(f.startswith("grounding: fabricated_accepted") for f in tier["failures"])
    assert any(f.startswith("grounding: adversarial_accepted") for f in tier["failures"])


def test_a_void_checker_that_never_voids_fails_the_gate(monkeypatch):
    """P7-01 plugs its VerificationRecord voiding in the same way; a checker that misses a change fails."""
    monkeypatch.setitem(grounding.VOID_CHECKERS, "never", lambda case: False)
    metrics = grounding.run(seeds=(), domains=()).metrics()
    assert metrics["void_checkers"] == ["never", "staleness"] and metrics["void_on_change_recall"] == 0.0
    assert "grounding: void_on_change_recall 0 < 1" in G.check("grounding", metrics, G.load())
    monkeypatch.setitem(grounding.VOID_CHECKERS, "always", lambda case: True)
    monkeypatch.delitem(grounding.VOID_CHECKERS, "never")
    assert grounding.run(seeds=(), domains=()).metrics()["void_false_positives"] == 3


def test_a_cost_rise_past_the_tolerance_fails_the_gate():
    cg = G._cost_gate_module()
    measured = cg.measure(cg.load_runs(cg.FIXTURES))
    lowered = {"runs": {n: {"calls": max(g["calls"] - 2, 1), "tokens": g["tokens"], "usd": g["usd"]} for n, g in measured.items()}}
    metrics = G.cost_metrics(baseline=lowered)  # the same replay against a baseline two calls cheaper per run
    assert metrics["max_rise"] > 0.10
    assert [f for f in G.check("cost", metrics, G.load()) if f.startswith("cost: max_rise")]
    assert G.check("cost", G.cost_metrics(), G.load()) == []  # the committed baseline passes


def test_a_precision_drop_in_the_analytical_benchmark_fails_the_gate():
    summary = SimpleNamespace(precision=0.85, recall=0.95, fdr=0.02, null_fdr=0.01, incomplete=[], replicates=45, verified=80,
                              by_method={"rate_by_segment": {"null_fdr": 0.0}, "trend": {"null_fdr": 0.08}})
    failures = G.check("analytical_component", G.analytical_metrics(summary), G.load())
    assert failures == ["analytical_component: precision 0.85 < 0.9", "analytical_component: null_fdr_per_method_max 0.08 > 0.05"]


def test_the_cli_exits_non_zero_on_a_seeded_regression(monkeypatch, tmp_path, capsys):
    import importlib.util

    from evaluation.gates import ROOT

    spec = importlib.util.spec_from_file_location("eval_gates_cli", ROOT / "scripts" / "eval_gates.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setitem(G.RUNNERS, "cost", lambda: {"max_rise": 0.4, "missing_baseline": 0})
    out = tmp_path / "gate.json"
    assert cli.main(["--tiers", "cost", "--out", str(out)]) == 1
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["passed"] is False and result["config"]["version"] == G.load().version
    assert "EVAL GATE FAILED: cost: max_rise 0.4 > 0.1" in capsys.readouterr().err
    monkeypatch.setitem(G.RUNNERS, "cost", lambda: {"max_rise": 0.0, "missing_baseline": 0})
    assert cli.main(["--tiers", "cost"]) == 0


def test_live_tiers_skip_without_a_key_and_deterministic_tiers_never_skip(monkeypatch):
    for key in G.LIVE_KEYS:
        monkeypatch.delenv(key, raising=False)
    result = G.run_gates(["ask_live"], runners={})
    assert result["tiers"]["ask_live"]["status"] == "skipped" and result["passed"] is True
    assert G.run_gates(["ask_live"], runners={}, require_live=True)["passed"] is False  # a release needs the live tiers
    result = G.run_gates(["grounding"], runners={})  # no runner: a deterministic gate cannot pass by skipping
    assert result["tiers"]["grounding"]["status"] == "skipped" and result["passed"] is False


def test_a_suite_that_crashes_fails_its_gate():
    def boom():
        raise RuntimeError("dataset generator changed")

    result = G.run_gates(["cost"], runners={"cost": boom})
    assert result["passed"] is False and "suite failed: RuntimeError" in result["tiers"]["cost"]["failures"][0]

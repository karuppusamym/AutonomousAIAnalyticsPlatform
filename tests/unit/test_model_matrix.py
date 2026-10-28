"""S12 §3 per-model evaluation matrix (FR-055) and the CI release gates: candidates never include a blocked
family (Claude, Gemini), rows are gated per model, the tier gates the worst default-route model, stored rows of
a blocked family are never shown as evaluated, and the release-safety gate fails when a suite did not run.
No services (SQLite; the suites are fakes)."""
from __future__ import annotations

import pytest
from evaluation import gates as G
from evaluation import model_matrix as MM
from evaluation import release_safety as RS

from analystos.llm.config import ModelsConfig, family, load_models_config
from analystos.ops import model_matrix

GOOD = {"execution_correctness": 0.97, "answer_correctness": 0.93, "metric_grounding": 1.0, "confident_wrong": 0,
        "profile_calls": 4, "pinned_share": 1.0, "blocked_family_calls": 0}


def _config(**over) -> ModelsConfig:
    base = load_models_config().model_dump()
    base.update(over)
    return ModelsConfig.model_validate(base)


def test_candidates_follow_routing_and_never_include_blocked_families():
    cfg = load_models_config()
    assert {"anthropic", "google"} <= set(cfg.blocked_families)
    cands, excluded = model_matrix.candidates(cfg)
    assert cands and all(family(c.model) not in cfg.blocked_families for c in cands)
    assert all(c.model in cfg.allowlist for c in cands)
    assert "decision" not in {c.profile for c in cands}  # typed decisions are not scored by the Ask suite
    defaults = [c for c in cands if c.default]
    assert len(defaults) == len({c.profile for c in cands})  # exactly one default route per profile
    for c in defaults:
        assert cfg.profiles[c.profile].models[0] == c.model or cfg.profiles[c.profile].models[0] not in cfg.allowlist

    # a profile that still lists a blocked model: excluded with the reason, never a candidate
    profiles = cfg.model_dump()["profiles"]
    profiles["coding"]["models"] = ["anthropic/claude-sonnet-5", "google/gemini-3.5-flash", "qwen/qwen3-32b"]
    cfg2 = _config(profiles=profiles, allowlist=[*cfg.allowlist, "anthropic/claude-sonnet-5", "google/gemini-3.5-flash"])
    cands2, excluded2 = model_matrix.candidates(cfg2)
    coding = [c for c in cands2 if c.profile == "coding"]
    assert [c.model for c in coding if c.default] == ["qwen/qwen3-32b"]
    reasons = {(e["model"], e["reason"]) for e in excluded2 if e["profile"] == "coding"}
    assert {("anthropic/claude-sonnet-5", "blocked_family"), ("google/gemini-3.5-flash", "blocked_family")} <= reasons


def test_rows_are_gated_per_model_and_the_tier_on_the_worst_default_route():
    calls = []

    def runner(c):
        calls.append(c.model)
        if c.default and c.profile == "coding":
            return {**GOOD, "execution_correctness": 0.90}  # the default route regresses
        if c.profile == "low_cost" and not c.default:
            return {**GOOD, "profile_calls": 0, "pinned_share": None}  # the suite never routed to this profile
        if c.model == "openai/gpt-oss-120b":
            raise RuntimeError("provider timeout")
        return GOOD

    out = MM.run("fake", runner=runner)
    assert all(family(m) not in out["blocked_families"] for m in calls)
    rows = {(r["profile"], r["model"]): r for r in out["rows"]}
    coding_default = next(r for r in out["rows"] if r["profile"] == "coding" and r["default_route"])
    assert coding_default["status"] == "failed" and coding_default["failures"] == ["execution_correctness 0.9 < 0.95"]
    assert any(r["status"] == "not_exercised" for r in rows.values())
    assert any(r["status"] == "error" and "provider timeout" in r["failures"][0] for r in rows.values())
    m = out["metrics"]
    assert m["execution_correctness"] == 0.90 and m["blocked_family_evaluated"] == 0 and m["errors"] >= 1
    failures = G.check("model_matrix_fake", m)
    assert "model_matrix_fake: execution_correctness 0.9 < 0.95" in failures
    assert any(f.startswith("model_matrix_fake: errors") for f in failures)


def test_a_clean_matrix_passes_its_gate():
    out = MM.run("fake", runner=lambda c: GOOD)
    assert G.check("model_matrix_fake", out["metrics"]) == []
    assert out["metrics"]["models_evaluated"] == len(model_matrix.candidates()[0])


def test_a_route_that_reaches_a_blocked_family_fails_the_gate():
    out = MM.run("fake", runner=lambda c: {**GOOD, "blocked_family_calls": 1 if c.default else 0})
    assert any("blocked_family_calls" in f for f in G.check("model_matrix_fake", out["metrics"]))


def test_matrix_rows_are_stored_and_served_without_blocked_families(sqlite_db):
    from analystos.db.base import session_scope
    from analystos.db.models import ModelMatrixResult

    out = MM.run("fake", runner=lambda c: GOOD)
    with session_scope() as s:
        batch = model_matrix.record(s, out["rows"], suite="ask", mode="fake", gates_version=6, gates_sha256="x" * 64)
        # a row written before Claude was blocked (not through `record`, which refuses it)
        s.add(ModelMatrixResult(id="mev_old", batch_id="meb_old", suite="ask", mode="live", profile="coding",
                              model="anthropic/claude-sonnet-5", provider="openrouter", status="passed", metrics={}, failures=[]))
        with pytest.raises(Exception, match="blocked family"):
            model_matrix.record(s, [{"profile": "coding", "model": "google/gemini-3.5-flash", "status": "passed"}],
                                suite="ask", mode="fake", gates_version=6, gates_sha256=None)
    with session_scope() as s:
        view = model_matrix.matrix(s)
    models = {r["model"] for r in view["evaluations"]}
    assert not any(family(m) in {"anthropic", "google"} for m in models)
    assert {"model": "anthropic/claude-sonnet-5", "reason": "blocked_family"}.items() <= next(
        e for e in view["excluded"] if e["model"] == "anthropic/claude-sonnet-5").items()
    assert all(r["eligible"] for r in view["evaluations"]) and all(r["batch_id"] == batch for r in view["evaluations"])
    assert view["thresholds"]["metrics"]["execution_correctness"]["min"] == 0.95
    assert view["not_evaluated"] == []


def test_evaluations_api_needs_an_administrator(sqlite_db, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api import deps
    from analystos.api.app import app
    from analystos.db import base, models
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security.auth import issue_token
    from analystos.tenancy import resolve

    models.Base.metadata.tables["tenant_member"].create(sqlite_db.kw["bind"])
    with session_scope() as s:
        s.add(User(id="usr_root", email="root@x", name="R", password_hash="-", is_admin=True, attributes={}))
        s.add(User(id="usr_p", email="p@x", name="P", password_hash="-", is_admin=False, attributes={}))
    monkeypatch.setattr(deps, "SessionLocal", base.SessionLocal)
    resolve.invalidate()
    client = TestClient(app)
    url = "/v2/admin/models/evaluations"
    assert client.get(url, headers={"Authorization": f"Bearer {issue_token('usr_p', 'p@x')}"}).status_code == 403
    r = client.get(url, headers={"Authorization": f"Bearer {issue_token('usr_root', 'root@x')}"})
    assert r.status_code == 200 and r.json()["evaluations"] == [] and r.json()["not_evaluated"]
    assert set(r.json()["blocked_families"]) >= {"anthropic", "google"}


def test_release_safety_fails_closed_when_a_suite_did_not_run():
    full = {"tests": 120, "passed": 120, "failed": 0, "skipped": 0}
    ok = RS.run(runner=lambda files: full)
    assert G.check("release_safety", ok) == []
    leak = RS.run(runner=lambda files: {**full, "passed": 119, "failed": 1} if "test_tenancy_rls.py" in " ".join(files) else full)
    assert "release_safety: cross_tenant_leaks 1 > 0" in G.check("release_safety", leak)
    no_db = RS.run(runner=lambda files: {**full, "passed": 100, "skipped": 20} if "integration" in " ".join(files) else full)
    assert "release_safety: not_run 20 > 0" in G.check("release_safety", no_db)
    nothing = RS.run(runner=lambda files: {"tests": 0, "passed": 0, "failed": 0, "skipped": 0})
    assert any("sql_safety_pass_share" in f for f in G.check("release_safety", nothing))


def test_the_s12_tiers_are_wired_into_ci():
    gates = G.load()
    assert gates.version >= 6
    for name in ("release_safety", "model_matrix_fake"):
        assert gates.tiers[name].kind == "deterministic" and gates.tiers[name].needs_database and gates.tiers[name].blocking
        assert name in G.deterministic_ci_tiers(gates, database=True)
        assert name not in G.deterministic_ci_tiers(gates)  # not in the pre-database step
    assert gates.tiers["model_matrix"].kind == "live" and "model_matrix" in G.RUNNERS
    m = gates.tiers["model_matrix"].metrics
    assert (m["execution_correctness"].min, m["answer_correctness"].min, m["metric_grounding"].min) == (0.95, 0.90, 0.95)
    assert m["confident_wrong"].max == 0 and m["blocked_family_evaluated"].max == 0
    r = gates.tiers["release_safety"].metrics
    assert r["sql_safety_pass_share"].min == 1.0 and r["cross_tenant_leaks"].max == 0 and r["publish_integrity_share"].min == 1.0


def test_nightly_gate_result_imports_into_the_matrix(sqlite_db, tmp_path):
    import json

    from analystos.db.base import session_scope
    from analystos.ops.cli import main

    out = MM.run("live", runner=lambda c: GOOD)
    gate = {"config": {"version": 6, "sha256": "s" * 64}, "tiers": {"model_matrix": {
        "status": "passed", "metrics": {**out["metrics"], "mode": "live", "rows": out["rows"]}}}}
    path = tmp_path / "nightly.json"
    path.write_text(json.dumps(gate), encoding="utf-8")
    assert main(["model-matrix-import", str(path)]) == 0
    with session_scope() as s:
        view = model_matrix.matrix(s, mode="live")
    assert len(view["evaluations"]) == len(out["rows"]) and view["evaluations"][0]["gates_version"] == 6

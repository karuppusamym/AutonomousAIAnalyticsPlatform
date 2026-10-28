"""S05-T5 model-switch evaluation gate (spec S05 §7.5, FR-055) and S05-T7 air-gapped posture. Hermetic: suites
run through an injected runner, never a provider."""
from __future__ import annotations

import pytest

from analystos.contracts.platform import PRESETS, FeatureFlags, LLMSettings, PlatformSettings
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.llm.config import ModelsConfig
from analystos.llm.layers import ModelOverride, resolve
from analystos.services import model_switch
from analystos.services.model_switch import GateConfig, changed_primaries, grade, load_gate


def _cfg() -> ModelsConfig:
    return ModelsConfig.model_validate({
        "providers": {"openrouter": {"kind": "chat", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "K"}},
        "allowlist": ["a/good", "b/weak", "c/same"],
        "profiles": {"coding": {"models": ["a/good", "b/weak", "c/same"]}},
        "routing": {"sql_generation": "coding", "planning": "coding"}})


def scoring(scores: dict[str, float], calls: list | None = None):
    """A runner where `model` passes round(score * n) of n cases."""
    def run(*, purpose, profile, model, suite, cases, max_tokens):
        if calls is not None:
            calls.append((purpose, model, suite))
        if model not in scores:
            return {"status": "unavailable", "reason": f"no key for {model}"}
        n = len(cases)
        ok = round(scores[model] * n)
        return {"status": "scored", "results": [{"case": i, "passed": i < ok, "problems": []} for i in range(n)]}
    return run


@pytest.fixture
def db(sqlite_db):
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        s.add(User(id="usr_admin", email="a@x", name="A", password_hash="-", is_admin=True, attributes={}))
        s.add(User(id="usr_owner", email="o@x", name="O", password_hash="-", is_admin=False, attributes={}))
    return session_scope


def _gate(**kw) -> GateConfig:
    base = load_gate()
    return base.model_copy(update={"tolerance": 0.1, "min_score": 0.5, **kw})


def test_shipped_gate_config_has_suites_for_every_mapped_purpose():
    gate = load_gate()
    assert gate.default_suite in gate.suites and set(gate.purpose_suites.values()) <= set(gate.suites)
    assert all(suite.cases for suite in gate.suites.values())


def test_grading_is_deterministic():
    sql = {"json_keys": ["sql"], "regex": r"select.+count.+from\s+sn\.incident.+group\s+by\s+priority",
           "not_contains": ["delete"]}
    assert grade('{"sql": "SELECT priority, count(*) FROM sn.incident GROUP BY priority"}', sql) == []
    assert grade('{"sql": "DELETE FROM sn.incident"}', sql)  # pattern and forbidden word
    assert grade("not json", {"json_keys": ["sql"]}) == ["not a JSON object"]
    assert grade("P1 rose to 52 (+30%).", {"contains": ["52", "30%"], "not_contains": ["60%"]}) == []


def test_only_changed_primary_models_are_gated():
    cfg, platform = _cfg(), PlatformSettings()
    before = resolve(cfg, platform, [])
    after = resolve(cfg, platform, [("workspace", 1, ModelOverride(profile_models={"coding": ["b/weak", "a/good"]}))])
    assert changed_primaries(before, after) == [
        {"purpose": "sql_generation", "profile": "coding", "from": "a/good", "to": "b/weak"},
        {"purpose": "planning", "profile": "coding", "from": "a/good", "to": "b/weak"}]
    off = PlatformSettings(llm=LLMSettings(purpose_modes={"planning": "off"}))
    assert [c["purpose"] for c in changed_primaries(resolve(cfg, off, []), resolve(cfg, off, [
        ("tenant", 1, ModelOverride(allowlist=["c/same"]))]))] == ["sql_generation"]  # an off purpose is not gated
    gone = resolve(cfg, platform, [("tenant", 1, ModelOverride(allowlist=[]))])
    assert changed_primaries(before, gone) == []  # losing every model fails closed; nothing to evaluate


def test_a_regression_beyond_tolerance_blocks_and_an_admin_override_is_audited(db):
    from analystos.db.models import AuditEvent, ModelEvaluation, User

    change = [{"purpose": "sql_generation", "profile": "coding", "from": "a/good", "to": "b/weak"}]
    runner = scoring({"a/good": 1.0, "b/weak": 2 / 3})
    with db() as s:
        with pytest.raises(Conflict, match="regresses more than 0.10") as exc:
            model_switch.check(s, s.get(User, "usr_admin"), change, runner=runner, gate=_gate())
        gate = exc.value.details["model_switch_gate"]
        assert gate["blocked"] and gate["evaluations"][0]["new"]["score"] == pytest.approx(0.6667)
    with db() as s:  # the evaluations survive the blocked save (own transaction)
        assert s.query(ModelEvaluation).count() == 2
    with db() as s:
        with pytest.raises(Forbidden):
            model_switch.check(s, s.get(User, "usr_owner"), change, runner=runner, gate=_gate(), override_reason="trust me")
        with pytest.raises(InvalidInput):
            model_switch.check(s, s.get(User, "usr_admin"), change, runner=runner, gate=_gate(), override_reason="  ")
        out = model_switch.check(s, s.get(User, "usr_admin"), change, runner=runner, gate=_gate(),
                                 override_reason="weak model is 5x cheaper; accepted by the owner")
        assert out["overridden"] and out["evaluations"][0]["new"]["reused"]  # stored scores are reused
        assert s.query(AuditEvent).filter(AuditEvent.action == "models.switch_gate.overridden").count() == 1


def test_within_tolerance_passes_and_an_unavailable_model_fails_closed(db):
    from analystos.db.models import User

    with db() as s:
        admin = s.get(User, "usr_admin")
        ok = model_switch.check(s, admin, [{"purpose": "planning", "profile": "coding", "from": "a/good", "to": "c/same"}],
                                runner=scoring({"a/good": 1.0, "c/same": 1.0}), gate=_gate())
        assert not ok["blocked"] and ok["evaluations"][0]["verdict"] == "passed"
        with pytest.raises(Conflict, match="could not be evaluated"):
            model_switch.check(s, admin, [{"purpose": "planning", "profile": "coding", "from": "a/good", "to": "z/nokey"}],
                               runner=scoring({"a/good": 1.0}), gate=_gate())
        with pytest.raises(Conflict, match="below the minimum"):  # no baseline (new purpose): min_score still applies
            model_switch.check(s, admin, [{"purpose": "planning", "profile": "coding", "from": None, "to": "b/weak"}],
                               runner=scoring({"b/weak": 1 / 3}), gate=_gate())


def test_platform_settings_save_runs_the_gate(db, sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.db.models import User
    from analystos.llm import config as llm_config
    from analystos.services import platform_settings

    models.Base.metadata.tables["platform_setting"].create(sqlite_db.kw["bind"])
    cfg = _cfg()
    monkeypatch.setattr(llm_config, "load_models_config", lambda *a, **k: cfg)
    monkeypatch.setattr(platform_settings, "_validate_references", lambda settings: None)
    calls: list = []
    with db() as s, pytest.raises(Conflict):
        platform_settings.update(s, s.get(User, "usr_admin"), {"llm": {"profile_models": {"coding": ["b/weak"]}}},
                                 gate=platform_settings.SwitchGate(runner=scoring({"a/good": 1.0, "b/weak": 0.0}, calls)))
    assert {c[1] for c in calls} == {"a/good", "b/weak"} and {c[2] for c in calls} == {"sql", "json_contract"}
    with db() as s:  # internal callers (no gate) are unchanged
        out = platform_settings.update(s, s.get(User, "usr_admin"), {"llm": {"profile_models": {"coding": ["b/weak"]}}})
        assert out["version"] == 1 and "model_switch_gate" not in out


# ----------------------------------------------------------------------------- S05-T7 air-gapped posture
def _airgapped_cfg(**local) -> ModelsConfig:
    return ModelsConfig.model_validate({
        "providers": {"local": {"type": "openai_compatible", "egress": "internal", "base_url": "http://llm.svc:8000/v1",
                                "region": "on-prem", "capabilities": {"json_mode": True, "context_window": 32768}, **local},
                      "typesafe": {"kind": "decision", "base_url": "https://openrouter.ai/api/alpha/decisions",
                                   "api_key_env": "K"}},
        "allowlist": ["qwen/qwen3-32b", "typesafe/jev"],
        "profiles": {"low_cost": {"provider": "local", "models": ["qwen/qwen3-32b"]},
                     "decision": {"provider": "typesafe", "models": ["typesafe/jev"]}},
        "routing": {"summarization": "low_cost", "risk_check": "decision"}})


def test_airgap_posture_passes_with_the_preset_and_a_local_provider():
    from analystos.services.airgap import posture

    settings = PlatformSettings(llm=LLMSettings(purpose_modes=PRESETS["air_gapped"]), features=FeatureFlags(jev_decisions=False))
    report = posture(config=_airgapped_cfg(), settings=settings, air_gapped=True)
    failing = [c for c in report["checks"] if not c["ok"]]
    assert report["ok"], failing
    assert [c["name"] for c in failing] == ["public providers"]  # typesafe is configured but refused (a warning)
    assert report["purposes"]["summarization"]["served_locally"]
    assert not report["purposes"]["risk_check"]["served_locally"] and report["purposes"]["risk_check"]["rules_fallback"]


def test_airgap_posture_names_what_is_missing():
    from analystos.services.airgap import posture

    report = posture(config=_airgapped_cfg(base_url="", base_url_env="UNSET_LOCAL_URL", capabilities={}),
                     settings=PlatformSettings(), air_gapped=False)
    bad = {c["name"] for c in report["checks"] if not c["ok"] and c["severity"] == "error"}
    assert not report["ok"]
    assert {"deployment flag", "local: endpoint", "JEV decisions", "air_gapped preset"} <= bad
    assert any(c["name"] == "local: capabilities" and c["severity"] == "warning" for c in report["checks"])

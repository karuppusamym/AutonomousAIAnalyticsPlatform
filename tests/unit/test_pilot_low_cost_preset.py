"""ADR-016 (owner delegation, 2026-10-30): the dev and pilot model policy and the provisional JEV price."""
from __future__ import annotations

from analystos.contracts.platform import (
    DETERMINISTIC_CAPABLE,
    DEV_DEFAULT_PRESET,
    PILOT_DAILY_SPEND_CAP_USD,
    PILOT_LOW_COST_DISABLED,
    PILOT_LOW_COST_MODELS,
    PRESET_PATCHES,
    PRESETS,
    PlatformSettings,
)
from analystos.llm.config import load_models_config
from analystos.services.platform_settings import _deep_merge


def _applied() -> PlatformSettings:
    """What apply_preset stores, built the same way (without a database)."""
    patch = _deep_merge(PRESET_PATCHES[DEV_DEFAULT_PRESET], {"llm": {"purpose_modes": PRESETS[DEV_DEFAULT_PRESET], "ladders": {},
                                                                      "escalation": {}}})
    return PlatformSettings.model_validate(_deep_merge(PlatformSettings().model_dump(), patch))


def test_the_dev_default_is_deterministic_first_with_the_two_dollar_cap():
    s = _applied()
    assert DEV_DEFAULT_PRESET == "pilot_low_cost"
    assert s.llm.purpose_modes == {p: "auto" for p in DETERMINISTIC_CAPABLE}
    assert s.llm.daily_spend_cap_usd == PILOT_DAILY_SPEND_CAP_USD == 2.0
    assert s.features.jev_decisions is True


def test_only_the_low_cost_open_models_and_jev_stay_enabled():
    cfg = load_models_config()
    named = set(cfg.allowlist) | set(cfg.models)
    # Every model models.yaml names is decided: enabled by the preset or disabled by it. A new model fails here.
    assert named <= set(PILOT_LOW_COST_MODELS) | set(PILOT_LOW_COST_DISABLED), named - set(PILOT_LOW_COST_MODELS)
    enabled = set(cfg.allowlist) - set(_applied().llm.disabled_models)
    assert enabled == set(PILOT_LOW_COST_MODELS)
    assert not any(m.split("/", 1)[0] in {"anthropic", "google"} for m in enabled)
    assert not set(PILOT_LOW_COST_MODELS) & set(PILOT_LOW_COST_DISABLED)


def test_every_enabled_model_has_a_price_so_the_pre_call_estimate_works():
    cfg = load_models_config()
    for m in PILOT_LOW_COST_MODELS:
        assert cfg.estimate_cost(m, 1_000, 200) is not None, m


def test_jev_has_a_provisional_price_at_least_the_most_expensive_low_cost_model():
    cfg = load_models_config()
    jev = cfg.models["typesafe/jev-1.13"]
    assert jev.price_provisional is True
    others = [cfg.models[m] for m in PILOT_LOW_COST_MODELS if m != "typesafe/jev-1.13"]
    assert jev.input_usd_per_mtok >= max(m.input_usd_per_mtok for m in others)
    assert jev.output_usd_per_mtok >= max(m.output_usd_per_mtok for m in others)
    assert not any(m.price_provisional for m in others)
    # A typical decision call (~400 tokens) stays far below a cent.
    assert cfg.estimate_cost("typesafe/jev-1.13", 400, 50) < 0.001


def test_a_priced_jev_passes_the_approval_threshold_instead_of_falling_back():
    """Before N-13, every JEV purpose stopped at the pre-call estimate (missing price) and fell back to rules."""
    from analystos.llm.router import CallContext, ModelRouter

    cfg = load_models_config()
    router = ModelRouter(config=cfg, settings_provider=_applied, api_key_lookup=lambda _n: "k")
    profile = cfg.profiles["decision"]
    ctx = CallContext(expensive_model_approval_usd=0.01)
    kept = router._within_cost("hypothesis_priority", "decision", profile, ["typesafe/jev-1.13"], ctx,
                               input_tokens=400, output_tokens=50, request_hash="h", request={})
    assert kept == ["typesafe/jev-1.13"]

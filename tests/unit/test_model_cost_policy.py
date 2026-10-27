"""Owner decision 2026-10-27: Claude and Gemini are never routed (cost); provider keys may live in `.env`."""
from __future__ import annotations

from analystos.llm.config import family, load_models_config


def test_shipped_config_blocks_claude_and_gemini():
    cfg = load_models_config()
    assert {"anthropic", "google"} <= set(cfg.blocked_families)
    assert not [m for m in cfg.allowlist if family(m) in cfg.blocked_families]
    for name, profile in cfg.profiles.items():
        assert not [m for m in profile.models + list(profile.escalation_models or []) if family(m) in cfg.blocked_families], name


def test_router_drops_blocked_families_even_when_allowed():
    from analystos.llm.router import CallContext, ModelRouter

    cfg = load_models_config().model_copy(deep=True)
    cfg.allowlist = [*cfg.allowlist, "anthropic/claude-sonnet-5"]
    profile = cfg.profiles["reasoning_strong"].model_copy(update={"models": ["anthropic/claude-sonnet-5", "qwen/qwen3-32b"]})
    router = ModelRouter(cfg, api_key_lookup=lambda env: "k")
    assert router._allowed(profile.models, profile, CallContext(), residency=False) == ["qwen/qwen3-32b"]


def test_provider_key_falls_back_to_the_env_file(tmp_path, monkeypatch):
    from analystos.core import config

    env = tmp_path / ".env"
    env.write_text('# comment\nFOO_KEY="quoted value"\nexport BAR_KEY=plain # note\nEMPTY=\n', encoding="utf-8")
    monkeypatch.setitem(config.Settings.model_config, "env_file", str(env))
    monkeypatch.delenv("FOO_KEY", raising=False)
    monkeypatch.delenv("BAR_KEY", raising=False)
    assert config.secret_env("FOO_KEY") == "quoted value"
    assert config.secret_env("BAR_KEY") == "plain"
    assert config.secret_env("EMPTY") is None and config.secret_env("MISSING") is None
    monkeypatch.setenv("FOO_KEY", "from-process")
    assert config.secret_env("FOO_KEY") == "from-process"  # the process environment wins

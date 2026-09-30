import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.core.errors import ModelRouteUnavailable, UpstreamUnavailable
from analystos.llm.jev import JevDecisions
from analystos.llm.router import CallContext, ModelRouter, parse_json_text

pytestmark = pytest.mark.usefixtures("pinned_models_config")

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def make(transport, sink=None, settings=None):
    from analystos.contracts.platform import PlatformSettings
    from analystos.llm.cache import ResponseCache

    platform = settings or PlatformSettings()
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


def _jev_always():
    """Decision purposes with a rule path answer from rules by default (P4-T02); these tests exercise JEV."""
    from analystos.contracts.platform import LLMSettings, PlatformSettings

    return PlatformSettings(llm=LLMSettings(purpose_modes={"hypothesis_priority": "always", "chart_selection": "always"}))


def test_json_completion_and_usage_recorded():
    sink = Sink()
    r = make(FakeTransport(chat=lambda p: chat_json({"ok": True})), sink)
    resp = r.complete_json("planning", "sys", "user")
    assert resp.data == {"ok": True} and resp.cost_usd == 0.001
    assert sink.records[-1]["status"] == "ok" and sink.records[-1]["purpose"] == "planning"


def test_fallback_to_next_model_on_transient_error():
    seen = []

    def chat(p):
        seen.append(p["model"])
        return UpstreamUnavailable("503") if len(seen) == 1 else chat_json({"x": 1}, model=p["model"])

    resp = make(FakeTransport(chat=chat)).complete_json("planning", "s", "u")
    assert seen == ["openai/gpt-5.4-mini", "google/gemini-3.5-flash"] and resp.model == "google/gemini-3.5-flash"


def test_workspace_allowlist_narrowing_fails_closed():
    r = make(FakeTransport(chat=lambda p: chat_json({})))
    with pytest.raises(ModelRouteUnavailable):
        r.complete("planning", [{"role": "user", "content": "x"}], ctx=CallContext(allowed_models=["some/other-model"]))


def test_verification_excludes_primary_family():
    r = make(FakeTransport())
    _, _, models = r.candidates("verification", CallContext(exclude_families=["openai"]))
    assert models and all(not m.startswith(("anthropic/", "openai/")) for m in models)


def test_no_key_means_unavailable_not_silent_reroute():
    from analystos.contracts.platform import PlatformSettings

    r = ModelRouter(transport=FakeTransport(), sink=Sink(), api_key_lookup=lambda _: None, settings_provider=PlatformSettings)
    assert r.available("planning") is False
    with pytest.raises(ModelRouteUnavailable):
        r.complete("planning", [{"role": "user", "content": "x"}])


def test_secrets_are_redacted_before_leaving():
    t = FakeTransport(chat=lambda p: chat_json({}))
    make(t).complete("planning", [{"role": "user", "content": "use key sk-or-v1-abcdefghijklmnopqrstuvwxyz and password=hunter2 "
                                                                "postgresql://u:secretpw@h/db"}])
    sent = t.chat_calls[0]["messages"][0]["content"]
    assert "abcdefghijklmnop" not in sent and "hunter2" not in sent and "secretpw" not in sent


def test_parse_json_text_handles_fences():
    assert parse_json_text("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert parse_json_text("Sure: {\"a\": [1,2]} done") == {"a": [1, 2]}


def test_jev_score_choice_and_escalation():
    def decide(p):
        q = p["questions"]
        answers = {}
        for k, v in q.items():
            if v["type"] == "score":
                answers[k] = {"type": "score", "score": 1.9, "probabilities": {"2": 0.9}, "confidence": 0.9}
            elif v["type"] == "choice":
                answers[k] = {"type": "choice", "choice": "line", "probabilities": {"line": 0.95, "bar": 0.05}}
            else:
                answers[k] = {"type": "noul", "noul": 0.97}
        return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {"cost": 0.00001}}

    t = FakeTransport(decide=decide)
    jev = JevDecisions(make(t, settings=_jev_always()))
    scores = jev.score_hypotheses("objective", {"0": "h zero", "1": "h one"})
    assert set(scores) == {"0", "1"} and scores["0"].value == pytest.approx(1.9)
    assert jev.choose("chart_selection", {"x": "y"}, "pick", {"line": "l", "bar": "b"}).value == "line"
    assert jev.consequential("publish it").value == pytest.approx(0.97)
    assert t.decide_calls[0]["model"] == "typesafe/jev-1.13"


def test_jev_invalid_choice_is_ignored():
    t = FakeTransport(decide=lambda p: {"answers": {"pick": {"choice": "rm -rf", "probabilities": {}}}})
    assert JevDecisions(make(t, settings=_jev_always())).choose("chart_selection", {}, "pick", {"line": "l", "bar": "b"}) is None
    assert len(t.decide_calls) == 1


def test_jev_unavailable_returns_none():
    from analystos.contracts.platform import PlatformSettings

    r = ModelRouter(transport=FakeTransport(), sink=Sink(), api_key_lookup=lambda _: None, settings_provider=PlatformSettings)
    assert JevDecisions(r).consequential("x") is None


# ---------------------------------------------------------------- admin control plane + token economy
from analystos.contracts.platform import PlatformSettings  # noqa: E402
from analystos.core.errors import LLMDisabled  # noqa: E402


def _settings(**llm):
    s = PlatformSettings()
    return s.model_copy(update={"llm": s.llm.model_copy(update=llm)})


def test_purpose_off_is_a_decision_not_an_outage():
    t = FakeTransport(chat=lambda p: chat_json({}))
    r = make(t, settings=_settings(purpose_modes={"planning": "off"}))
    assert r.available("planning") is False and r.mode("planning") == "off"
    with pytest.raises(LLMDisabled):
        r.complete_json("planning", "s", "u")
    assert t.chat_calls == []


def test_cache_hit_avoids_second_call_and_records_savings():
    sink = Sink()
    t = FakeTransport(chat=lambda p: chat_json({"a": 1}))
    r = make(t, sink, settings=_settings(cacheable_purposes=["planning"]))
    first = r.complete_json("planning", "same", "prompt")
    second = r.complete_json("planning", "same", "prompt")
    assert len(t.chat_calls) == 1 and second.cached and second.data == first.data
    hit = sink.records[-1]
    assert hit["status"] == "cache_hit" and hit["tokens_saved"] == 150


def test_non_cacheable_purpose_is_not_cached():
    t = FakeTransport(chat=lambda p: chat_json({"a": 1}))
    r = make(t, settings=_settings(cacheable_purposes=[]))
    r.complete_json("planning", "s", "u")
    r.complete_json("planning", "s", "u")
    assert len(t.chat_calls) == 2


def test_oversize_prompt_refused_before_sending():
    sink = Sink()
    t = FakeTransport(chat=lambda p: chat_json({}))
    r = make(t, sink, settings=_settings(max_prompt_tokens=50))
    with pytest.raises(LLMDisabled, match="above the admin limit"):
        r.complete_json("planning", "s", "x" * 1000)
    assert t.chat_calls == [] and sink.records[-1]["status"] == "refused"


def test_admin_routing_and_model_overrides():
    t = FakeTransport(chat=lambda p: chat_json({}, model=p["model"]))
    r = make(t, settings=_settings(routing_overrides={"planning": "low_cost"},
                                   profile_models={"low_cost": ["deepseek/deepseek-v4-flash"]}))
    assert r.complete_json("planning", "s", "u").model == "deepseek/deepseek-v4-flash"
    r2 = make(t, settings=_settings(disabled_models=["anthropic/claude-sonnet-5"]))
    _, _, models = r2.candidates("planning", CallContext())
    assert "anthropic/claude-sonnet-5" not in models


def test_budget_pressure_downgrades_to_low_cost():
    class LowBudget(Sink):
        def remaining_fraction(self, ctx):
            return 0.1

    t = FakeTransport(chat=lambda p: chat_json({}, model=p["model"]))
    resp = make(t, LowBudget()).complete_json("planning", "s", "u")
    assert resp.model == "google/gemini-3.5-flash-lite"


def test_budget_downgrade_respects_workspace_allowlist_and_verification_family():
    class LowBudget(Sink):
        def remaining_fraction(self, ctx):
            return 0.1

    t = FakeTransport(chat=lambda p: chat_json({}, model=p["model"]))
    # A workspace restricted to one provider never gets downgraded to another provider's model.
    resp = make(t, LowBudget()).complete_json("planning", "s", "u", ctx=CallContext(allowed_models=["openai/gpt-5.4"]))
    assert resp.model == "openai/gpt-5.4"
    # Independent-family verification is never downgraded (the exclusion is the point of the call).
    resp = make(t, LowBudget()).complete_json("verification", "s", "u", ctx=CallContext(exclude_families=["anthropic"]))
    _, _, verification_models = make(t).candidates("verification", CallContext(exclude_families=["anthropic"]))
    assert resp.model in verification_models


def test_jev_feature_flag_turns_decisions_off():
    s = PlatformSettings()
    s = s.model_copy(update={"features": s.features.model_copy(update={"jev_decisions": False})})
    r = make(FakeTransport(), settings=s)
    assert r.mode("risk_check") == "off" and JevDecisions(r).consequential("publish") is None


def test_credit_refusal_cools_the_provider_down_instead_of_trying_every_model():
    """Seen live: OpenRouter answered HTTP 402 and the router tried every fallback model per call (32 errors)."""
    from analystos.core.errors import ProviderQuotaExhausted
    from analystos.llm import router as router_mod

    router_mod._PROVIDER_COOLDOWN.clear()
    calls = []

    def chat(p):
        calls.append(p["model"])
        return ProviderQuotaExhausted("HTTP 402: requires more credits")

    r = make(FakeTransport(chat=chat))
    with pytest.raises(ProviderQuotaExhausted):
        r.complete_json("planning", "s", "u")
    assert len(calls) == 1  # no fallback to other models of the same provider
    assert not r.available("planning")
    with pytest.raises(ProviderQuotaExhausted, match="cooldown"):
        r.complete_json("planning", "s", "u")
    assert len(calls) == 1  # failed fast, no network call
    router_mod._PROVIDER_COOLDOWN.clear()
    assert r.available("planning")


def test_json_profiles_ask_openrouter_for_minimal_reasoning():
    """Live runs (2026-09-29): reasoning models spent max_tokens on hidden reasoning and returned empty or truncated
    JSON for purposes whose answer code validates anyway, so those profiles request minimal reasoning."""
    seen = []

    def chat(p):
        seen.append(p)
        return chat_json({"ok": True}, model=p["model"])

    r = make(FakeTransport(chat=chat))
    r.complete_json("planning", "s", "u")
    r.complete_json("sql_generation", "s", "u")
    assert [p["reasoning"] for p in seen] == [{"effort": "minimal"}, {"effort": "minimal"}]
    assert all(p["max_tokens"] <= 6000 for p in seen)

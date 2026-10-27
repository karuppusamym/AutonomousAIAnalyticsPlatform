"""P4-T01 execution ladder, P4-T02 deterministic-first defaults, P4-T07 price table and budget counters
(no services: the router runs on a fake transport, the counters on an in-memory Redis stand-in)."""
from types import SimpleNamespace

import pytest
from tests.fakes import FakeRedis, FakeTransport, chat_json

from analystos.contracts.platform import DETERMINISTIC_CAPABLE, PRESETS, LLMSettings, PlatformSettings
from analystos.core.errors import LLMDisabled, ModelRouteUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.jev import JevDecisions
from analystos.llm.router import ModelRouter, ladder_for_mode, mode_of
from analystos.runtime.budget_counters import BudgetCounters

pytestmark = pytest.mark.usefixtures("pinned_models_config")

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self, remaining=1.0):
        self.records = []
        self.remaining = remaining

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None

    def remaining_fraction(self, ctx):
        return self.remaining


def make(transport, sink=None, **llm):
    platform = PlatformSettings(llm=LLMSettings(**llm))
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


# ------------------------------------------------------------------------------ ladder
def test_every_routed_purpose_has_a_valid_default_ladder_matching_its_profile():
    cfg = load_models_config()
    assert set(cfg.ladders) == set(cfg.routing)
    for purpose, rungs in cfg.ladders.items():
        assert len(rungs) == len(set(rungs)) and rungs[0] == "cache"
        model_rungs = [r for r in rungs if r in ("decision", "llm_small", "llm_large")]
        routed = cfg.model_rung(cfg.routing[purpose])
        # A chat purpose may put a typed decision rung (the decision profile) before its own chat rung.
        assert model_rungs in ([routed], ["decision", routed]) and len(model_rungs) == len(set(model_rungs)), purpose


def test_deterministic_first_is_the_default_for_every_purpose_with_a_rule_path():
    """P4-T02: `auto` by default (not only under token_saver), and the default preset is the defaults."""
    r = make(FakeTransport())
    assert {p: r.mode(p) for p in DETERMINISTIC_CAPABLE} == {p: "auto" for p in DETERMINISTIC_CAPABLE}
    assert r.mode("rev_second_opinion") == "always" and r.mode("sql_generation") == "auto"  # registry first
    assert r.mode("verification") == "always"  # model-backed, but only called when the workspace policy opts in
    assert PRESETS["balanced"] == {}
    assert all(m == "always" for m in PRESETS["max_quality"].values()) and "insight_narrative" in PRESETS["max_quality"]


def test_modes_are_ladder_presets_and_overrides_take_precedence():
    default = ["cache", "rules", "llm_small"]
    assert ladder_for_mode(default, "off") == ["cache", "rules"]
    assert ladder_for_mode(default, "auto") == ["cache", "rules", "llm_small"]
    assert ladder_for_mode(default, "always") == ["cache", "llm_small", "rules"]
    assert ladder_for_mode(["cache", "llm_large"], "off") == ["cache", "rules"]
    assert [mode_of(x) for x in (["cache", "rules"], ["cache", "registry", "llm_large"], ["cache", "decision", "rules"])] == \
        ["off", "auto", "always"]
    r = make(FakeTransport(), purpose_modes={"insight_narrative": "always", "summarization": "off"},
             ladders={"insight_narrative": ["cache", "rules", "llm_small"]})
    assert r.ladder("insight_narrative") == ["cache", "rules", "llm_small"] and r.mode("insight_narrative") == "auto"
    assert r.ladder("summarization") == ["cache", "rules"] and not r.available("summarization")


def test_jev_flag_off_removes_the_decision_rung():
    s = PlatformSettings()
    s = s.model_copy(update={"features": s.features.model_copy(update={"jev_decisions": False})})
    r = ModelRouter(transport=FakeTransport(), sink=Sink(), api_key_lookup=KEY.get, settings_provider=lambda: s)
    assert "decision" not in r.ladder("rev_second_opinion") and r.mode("rev_second_opinion") == "off"


def test_every_row_records_the_rung_that_answered():
    sink = Sink()
    t = FakeTransport(chat=lambda p: chat_json({"a": 1}, model=p["model"]),
                      decide=lambda p: {"answers": {"p": {"noul": 0.9}}, "usage": {"cost": 0.00002}})
    r = make(t, sink, cacheable_purposes=["planning"], max_prompt_tokens=500)
    r.complete_json("planning", "s", "u")  # strong profile
    r.complete_json("planning", "s", "u")  # same request: cache
    r.complete_json("insight_narrative", "s", "u")  # low_cost profile
    JevDecisions(r).probability("rev_second_opinion", {"claim": "c"}, "supported?")
    JevDecisions(r).probability("stop_check", {"x": "y"}, "stop?")  # auto: the rule decides
    r.record_skip("sql_generation", None, estimated_tokens=10, reason="verified query", rung="registry")
    with pytest.raises(LLMDisabled):
        r.complete("summarization", [{"role": "user", "content": "x" * 5000}])  # refused: rules answer
    rows = [(x["purpose"], x["status"], x["answered_by"]) for x in sink.records]
    assert rows == [("planning", "ok", "llm_large"), ("planning", "cache_hit", "cache"), ("insight_narrative", "ok", "llm_small"),
                    ("rev_second_opinion", "ok", "decision"), ("stop_check", "skipped", "rules"),
                    ("sql_generation", "skipped", "registry"), ("summarization", "refused", "rules")]
    assert all(x.get("answered_by") for x in sink.records)


def test_budget_downgrade_answers_from_the_small_rung():
    sink = Sink(remaining=0.1)
    make(FakeTransport(chat=lambda p: chat_json({}, model=p["model"])), sink).complete_json("planning", "s", "u")
    assert sink.records[-1]["answered_by"] == "llm_small" and sink.records[-1]["profile"] == "low_cost"


def test_failed_attempts_record_the_rung_they_tried():
    from analystos.core.errors import UpstreamUnavailable

    sink = Sink()
    r = make(FakeTransport(chat=lambda p: UpstreamUnavailable("503")), sink)
    with pytest.raises(ModelRouteUnavailable):
        r.complete_json("planning", "s", "u")
    assert {x["answered_by"] for x in sink.records} == {"llm_large"} and {x["status"] for x in sink.records} == {"error"}


# ------------------------------------------------------------------------------ price table (P4-T07)
def test_cost_is_provider_reported_else_price_table_else_missing_price(caplog):
    cfg = load_models_config()
    assert cfg.prices_version and cfg.prices_version != "unversioned"
    sink = Sink()
    reported = make(FakeTransport(chat=lambda p: chat_json({}, model="anthropic/claude-sonnet-5", cost=0.5)), sink)
    reported.complete_json("planning", "s", "u")
    assert sink.records[-1]["cost_usd"] == 0.5 and sink.records[-1]["cost_source"] == "provider"

    def unreported(p):
        body = chat_json({}, model="anthropic/claude-sonnet-5")
        body["usage"].pop("cost")
        return body

    make(FakeTransport(chat=unreported), sink).complete_json("planning", "s", "u")
    expected = (100 * 3.0 + 50 * 15.0) / 1_000_000
    assert sink.records[-1]["cost_usd"] == pytest.approx(expected)
    assert sink.records[-1]["cost_source"] == f"price_table@{cfg.prices_version}"

    # The decision model has no price: its cost is unknown, recorded and flagged, never a silent $0.
    t = FakeTransport(decide=lambda p: {"answers": {"p": {"noul": 0.5}}, "usage": {"input_tokens": 200}})
    with caplog.at_level("ERROR"):
        JevDecisions(make(t, sink)).probability("rev_second_opinion", {"claim": "c"}, "supported?")
    last = sink.records[-1]
    assert last["cost_source"] == "missing_price" and "missing price" in last["error"]
    assert "no price for model typesafe/jev-1.13" in caplog.text


# ------------------------------------------------------------------------------ counters (P4-T07)
def test_counters_seed_once_then_increment_atomically():
    redis = FakeRedis()
    c = BudgetCounters(None, "t:", client=redis)
    seeds = []
    key = c.run_key("run_1", "queries")
    assert c.incr(key, lambda: seeds.append(1) or 5, 60) == 6  # seeded from the database aggregate, then +1
    assert c.incr(key, lambda: seeds.append(1) or 99, 60) == 7
    assert seeds == [1]
    c.record_model_usage(workspace_id="ws", run_id="run_1", purpose="planning", tokens=10, cost_usd=0.5, billable=True)
    assert c.run_key("run_1", "usd") not in redis.data  # never created by an increment: seeded on first read
    assert c.read(c.run_key("run_1", "usd"), lambda: 0.5, 60) == 0.5
    c.record_model_usage(workspace_id="ws", run_id="run_1", purpose="planning", tokens=10, cost_usd=0.25, billable=True)
    assert c.read(c.run_key("run_1", "usd"), lambda: 999, 60) == 0.75


def test_counters_fall_back_visibly_when_redis_fails(caplog):
    class Broken(FakeRedis):
        def get(self, key):
            raise ConnectionError("redis down")

        def register_script(self, _source):
            def run(keys, args):
                raise ConnectionError("redis down")
            return run

    c = BudgetCounters(None, "t:", client=Broken())
    with caplog.at_level("WARNING"):
        assert c.read("k", lambda: 1, 60) is None
    assert "falling back to database aggregates" in caplog.text
    assert not c.available and c.incr("k", lambda: 1, 60) is None  # cooling down: straight to the database
    assert BudgetCounters("redis://127.0.0.1:1/0").available is False


def test_run_query_budget_counts_in_redis_not_in_the_database(monkeypatch, budget_counters):
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.core.errors import BudgetExceeded
    from analystos.runtime import context as ctx_mod

    counted = []

    class _S:
        def __enter__(self):
            return SimpleNamespace(scalar=lambda q: counted.append(str(q)) or 0)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ctx_mod, "session_scope", lambda: _S())
    ctx = ctx_mod.RunContext(run=SimpleNamespace(id="run_q"), task=None, user=None, workspace=None,
                             policy=WorkspacePolicyDoc(max_queries_per_run=3), scope=None, agent=None, services=None)
    for _ in range(3):
        ctx.check_query_budget()
    with pytest.raises(BudgetExceeded, match="per-run query budget"):
        ctx.check_query_budget()
    assert len(counted) == 1 and "query_execution" in counted[0]  # the one-time seed, then counters only


def test_purpose_caps_are_validated():
    from analystos.core.errors import InvalidInput
    from analystos.services.platform_settings import _validate_references

    _validate_references(PlatformSettings(llm=LLMSettings(purpose_run_caps={"planning": {"calls": 1}},
                                                          ladders={"planning": ["cache", "rules"]})))
    for bad in ({"purpose_run_caps": {"planning": {"minutes": 1}}}, {"ladders": {"nope": ["rules"]}},
                {"ladders": {"planning": ["rules", "rules"]}}):
        with pytest.raises(InvalidInput):
            _validate_references(PlatformSettings(llm=LLMSettings(**bad)))

"""N-14: a purpose that wanted a model but had no allowed route (models disabled by the settings, a spend cap, a
missing price) is answered by rules visibly — a structured reason (purpose, cause, rung) in the run's messages and
a `model.fallback` event, folded into the run detail and labelled in live benchmark reports. No services."""
from types import SimpleNamespace

import pytest
from tests.fakes import FakeRedis, FakeTransport, chat_json

from analystos.agents import common
from analystos.contracts.events import EVENT_TYPES
from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import ApprovalRequired, SpendCapReached
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.fallback import ModelFallback, cause_of, fallback_for, summarize
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.budget_counters import BudgetCounters, CapExceeded, CapSpec

pytestmark = pytest.mark.usefixtures("pinned_models_config")

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}
PURPOSE = "hypothesis_generation"


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def router(transport=None, sink=None, config=None, **llm):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False, **llm))
    return ModelRouter(config=config, transport=transport or FakeTransport(chat=lambda p: chat_json({"hypotheses": []})),
                       sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0, settings_provider=lambda: platform,
                       cache=ResponseCache(None)), platform


def route_models(purpose=PURPOSE):
    """Every model the purpose may route to (first tier and large tier) with nothing disabled."""
    r, _ = router()
    ctx = CallContext()
    name, profile, first = r.candidates(purpose, ctx)
    return list(dict.fromkeys(first + r._large_tier(name, profile, ctx)))


def run_ctx(r, platform, monkeypatch, *, task="hypotheses"):
    said, events = [], []
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    ctx = SimpleNamespace(router=r, policy=WorkspacePolicyDoc(), run=SimpleNamespace(objective="x"),
                          task=SimpleNamespace(key=task),
                          call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1", run_id="run1"),
                          say=lambda text, **k: said.append((text, k.get("data") or {})),
                          event=lambda type_, payload: events.append((type_, payload)))
    return ctx, said, events


# ------------------------------------------------------------------------------ the router says why
def test_models_disabled_by_the_settings_are_named_as_the_cause():
    disabled = route_models()
    r, _ = router(disabled_models=disabled)
    why = r.unavailable(PURPOSE, CallContext())
    assert why is not None and why.details["cause"] == "models_disabled"
    assert set(why.details["disabled_models"]) == set(disabled)
    assert "the platform settings disable" in why.message


def test_an_allowlist_that_excludes_every_model_is_not_called_disabled():
    r, _ = router()
    why = r.unavailable(PURPOSE, CallContext(allowed_models=["no/such-model"]))
    assert why is not None and "cause" not in why.details and "allowlist" in why.message


def test_an_unpriced_model_under_an_approval_limit_is_flagged_as_missing_price():
    config = load_models_config().model_copy(deep=True)
    for m in route_models("planning"):
        config.models.pop(m, None)
    r, _ = router(config=config)
    ctx = CallContext.for_policy(WorkspacePolicyDoc(expensive_model_approval_usd=0.01), workspace_id="ws1")
    with pytest.raises(ApprovalRequired) as exc:
        r.complete_json("planning", "s", "u", ctx=ctx)
    assert exc.value.details["missing_price"] is True
    assert fallback_for("planning", common.model_outcome(exc.value)).cause == "missing_price"


# ------------------------------------------------------------------------------ classification
@pytest.mark.parametrize(("code", "details", "cause"), [
    ("no_route", {"cause": "models_disabled"}, "models_disabled"),
    ("no_route", {}, "allowlist"),
    ("cap_reached", {"cap": "platform_daily"}, "cap_reached"),
    ("budget_exceeded", {}, "budget_exceeded"),
    ("approval_required", {"missing_price": True}, "missing_price"),
    ("approval_required", {"missing_price": False}, "approval_required"),
    ("no_api_key", {}, "no_api_key"),
    ("mode_off", {}, None),  # an administrator's choice, not a fallback
    ("context_over_budget", {}, None),
    ("invalid_output", {}, None),
])
def test_only_route_causes_are_fallbacks(code, details, cause):
    assert cause_of(code, details) == cause


def test_the_business_sentence_names_the_purpose_in_plain_words():
    fb = ModelFallback(purpose=PURPOSE, cause="models_disabled", detail="router detail")
    assert fb.sentence == "Answered by rules: no model is enabled for hypothesis generation."
    assert fb.to_dict()["message"] == fb.sentence and fb.to_dict()["rung"] == "rules"


def test_summarize_folds_per_purpose_and_cause():
    p = ModelFallback(purpose=PURPOSE, cause="models_disabled", detail="d").to_dict()
    q = ModelFallback(purpose="insight_narrative", cause="cap_reached", detail="c").to_dict()
    out = summarize([{**p, "task": "hypotheses"}, {**p, "task": "followups:1"}, {**p, "task": "hypotheses"}, q,
                     {"purpose": "x", "cause": "not-a-cause"}])
    assert [(r["purpose"], r["cause"], r["count"]) for r in out] == [(PURPOSE, "models_disabled", 3),
                                                                      ("insight_narrative", "cap_reached", 1)]
    assert out[0]["tasks"] == ["hypotheses", "followups:1"]


def test_model_fallback_is_a_known_event_type():
    assert "model.fallback" in EVENT_TYPES


# ------------------------------------------------------------------------------ the run says so
def test_disabled_models_fall_back_to_rules_visibly(monkeypatch):
    sink = Sink()
    transport = FakeTransport(chat=lambda p: chat_json({"hypotheses": [{"statement": "x"}]}))
    r, platform = router(transport, sink, disabled_models=route_models())
    ctx, said, events = run_ctx(r, platform, monkeypatch)
    data, reason = common.llm_json(ctx, PURPOSE, "hypothesis_generation.v1", {"objective": "x"})
    assert data is None and reason == "no_route" and transport.chat_calls == []
    (text, extra), = said
    assert text == "Answered by rules: no model is enabled for hypothesis generation."
    assert extra["purpose"] == PURPOSE and extra["model_fallback"]["cause"] == "models_disabled"
    (type_, payload), = events
    assert type_ == "model.fallback" and payload["purpose"] == PURPOSE and payload["cause"] == "models_disabled"
    assert payload["rung"] == "rules" and payload["task"] == "hypotheses" and "disable" in payload["detail"]
    skip = sink.records[-1]
    assert skip["status"] == "skipped" and skip["answered_by"] == "rules" and skip["error"].startswith("fallback models_disabled")


def test_a_step_reports_each_fallback_once_and_accounts_every_avoided_call(monkeypatch):
    sink = Sink()
    r, platform = router(sink=sink, disabled_models=route_models())
    ctx, said, events = run_ctx(r, platform, monkeypatch)
    for _ in range(3):
        common.llm_json(ctx, PURPOSE, "hypothesis_generation.v1", {"objective": "x"})
    assert len(said) == 1 and len(events) == 1
    assert sum(1 for rec in sink.records if rec["status"] == "skipped") == 3


def test_mode_off_is_not_reported_as_a_fallback(monkeypatch):
    r, platform = router(purpose_modes={PURPOSE: "off"})
    ctx, said, events = run_ctx(r, platform, monkeypatch)
    data, reason = common.llm_json(ctx, PURPOSE, "hypothesis_generation.v1", {"objective": "x"})
    assert data is None and reason == "mode_off" and events == [] and said == []


class CapSink(Sink):
    def __init__(self):
        super().__init__()
        self.counters = BudgetCounters(None, "t:", client=FakeRedis())

    def reserve(self, *, ctx, purpose, model, estimate_usd):
        c = self.counters
        try:
            return c.reserve([CapSpec("platform_daily", c.platform_day_key(), 0.0, 3600, lambda: 0.0)], estimate_usd)
        except CapExceeded as exc:
            raise SpendCapReached(f"{exc.cap.name} cap reached", details={"cap": exc.cap.name, "remedy": "wait"}) from None


def test_a_spend_cap_fallback_keeps_its_remedy_and_carries_the_structured_reason(monkeypatch):
    r, platform = router(sink=CapSink())
    ctx, said, events = run_ctx(r, platform, monkeypatch)
    data, reason = common.llm_json(ctx, PURPOSE, "hypothesis_generation.v1", {"objective": "x"})
    assert data is None and reason == "cap_reached"
    text, extra = said[-1]
    assert "deterministic fallback" in text and extra["remedy"] == "wait"
    assert extra["model_fallback"]["cause"] == "cap_reached"
    assert [e[1]["cause"] for e in events] == ["cap_reached"]


# ------------------------------------------------------------------------------ run detail and reports
def test_the_run_detail_lists_the_runs_fallbacks(sqlite_db):
    from analystos.api.routers.analysis import model_fallbacks
    from analystos.db.base import session_scope
    from analystos.db.models import RunEvent

    payload = ModelFallback(purpose=PURPOSE, cause="models_disabled", detail="d").to_dict()
    with session_scope() as s:
        s.add_all([RunEvent(workspace_id="ws1", run_id="run1", type="model.fallback", payload={**payload, "task": "hypotheses"}),
                   RunEvent(workspace_id="ws1", run_id="run1", type="model.fallback", payload=payload),
                   RunEvent(workspace_id="ws1", run_id="run2", type="model.fallback", payload=payload),
                   RunEvent(workspace_id="ws1", run_id="run1", type="agent.message", payload={"content": "x"})])
    with session_scope() as s:
        out = model_fallbacks(s, "run1")
    assert [(f["purpose"], f["cause"], f["count"], f["tasks"]) for f in out] == [(PURPOSE, "models_disabled", 2, ["hypotheses"])]
    assert out[0]["message"] == "Answered by rules: no model is enabled for hypothesis generation."


def test_live_reports_label_a_tier_that_fell_back(sqlite_db):
    from evaluation.live import fallback_label, fallback_lines, fallbacks

    from analystos.db.base import session_scope
    from analystos.db.models import RunEvent

    with session_scope() as s:
        s.add(RunEvent(workspace_id="ws1", run_id="run1", type="model.fallback",
                       payload=ModelFallback(purpose=PURPOSE, cause="models_disabled", detail="a | b").to_dict()))
    found = fallbacks(run_ids=["run1"])
    assert fallback_label(found) == " — fell back to rules (1 purpose)"
    lines = fallback_lines(found)
    assert lines[0].startswith("### Fell back to rules")
    assert any(line.startswith(f"| {PURPOSE} | models_disabled | 1 | rules | a / b |") for line in lines)
    assert fallback_label([]) == "" and fallback_lines(None) == [] and fallbacks() == []

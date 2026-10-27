"""P4-T04 prompt-cache-stable layout (request shape per model capability, cached-token accounting)
and P4-T06 knowledge version in the L0 response-cache key."""
import pytest
from tests.fakes import FakeTransport

from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.llm.cache import ResponseCache
from analystos.llm.config import load_models_config
from analystos.llm.replay import chat_key
from analystos.llm.router import CallContext, ModelRouter, cached_prompt_tokens, normalize_messages, wire_messages

pytestmark = pytest.mark.usefixtures("pinned_models_config")

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}
LAYOUT = [{"role": "system", "content": "static system text", "cache": True},
          {"role": "user", "content": '{"workspace_context":{"workspace":"ITSM"}}', "cache": True},
          {"role": "user", "content": '{"objective":"sla"}'}]


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def _router(transport, *, cache=False, sink=None, **llm):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=cache, **llm))
    return ModelRouter(transport=transport, sink=sink or Sink(), api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None))


def _ok(usage=None, model="anthropic/claude-sonnet-5"):
    return lambda p: {"model": p["model"] if model is None else model, "choices": [{"message": {"content": '{"ok":1}'}}],
                      "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 20, "cost": 0.001}}


# ------------------------------------------------------------------ request shape
def test_cache_capable_model_gets_cache_control_breakpoints_after_the_stable_prefix():
    wire = wire_messages(LAYOUT, cache_control=True)
    assert [m["role"] for m in wire] == ["system", "user"]  # header + volatile part merged into one user turn
    assert wire[0]["content"] == [{"type": "text", "text": "static system text", "cache_control": {"type": "ephemeral"}}]
    header, variable = wire[1]["content"]
    assert header["cache_control"] == {"type": "ephemeral"} and "cache_control" not in variable
    assert variable["text"] == '{"objective":"sla"}'  # volatile content last


def test_other_models_get_the_same_order_as_plain_strings():
    wire = wire_messages(LAYOUT, cache_control=False)
    assert wire == [{"role": "system", "content": "static system text"},
                    {"role": "user", "content": '{"workspace_context":{"workspace":"ITSM"}}\n\n{"objective":"sla"}'}]
    assert normalize_messages(wire_messages(LAYOUT, cache_control=True)) == wire
    assert chat_key(LAYOUT) == chat_key(wire) == chat_key(wire_messages(LAYOUT, cache_control=True))  # replay matches


def test_at_most_four_breakpoints_the_last_ones_kept():
    many = [{"role": "system", "content": f"s{i}", "cache": True} for i in range(6)]
    blocks = wire_messages(many, cache_control=True)[0]["content"]
    assert [b.get("cache_control") is not None for b in blocks] == [False, False, True, True, True, True]


def test_capability_flag_comes_from_models_yaml():
    config = load_models_config()
    assert config.prompt_cache("anthropic/claude-sonnet-5") and config.prompt_cache("anthropic/claude-opus-5")
    assert not config.prompt_cache("openai/gpt-5.4") and not config.prompt_cache("unknown/model")


def test_router_sends_blocks_to_anthropic_and_strings_to_openai():
    transport = FakeTransport(chat=_ok())
    # Sonnet first (max_quality's always_large); the default small tier is OpenAI (plain strings, below)
    _router(transport, escalation={"planning": "always_large"}).complete("planning", LAYOUT, ctx=CallContext(), json_output=True)
    sent = transport.chat_calls[0]
    assert sent["model"].startswith("anthropic/")
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["messages"][0]["content"][0]["text"].endswith("Respond with a single valid JSON value only.")

    transport = FakeTransport(chat=_ok(model=None))
    _router(transport).complete("verification", LAYOUT, ctx=CallContext(), json_output=True)  # independent family: openai first
    sent = transport.chat_calls[0]
    assert sent["model"].startswith("openai/")
    assert all(isinstance(m["content"], str) for m in sent["messages"])
    assert sent["messages"][1]["content"].startswith('{"workspace_context"')


# ------------------------------------------------------------------ accounting
def test_cached_prompt_tokens_from_each_provider_shape():
    assert cached_prompt_tokens({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}}) == 800
    assert cached_prompt_tokens({"cache_read_input_tokens": 640}) == 640
    assert cached_prompt_tokens({"prompt_cache_hit_tokens": 512}) == 512
    assert cached_prompt_tokens({"input_tokens_details": {"cached_tokens": 7}}) == 7
    assert cached_prompt_tokens({"prompt_tokens": 10}) == 0 and cached_prompt_tokens(None) == 0
    assert cached_prompt_tokens({"prompt_tokens_details": {"cached_tokens": None}}) == 0


def test_cached_share_is_recorded_per_call():
    usage = {"prompt_tokens": 1000, "completion_tokens": 20, "cost": 0.0004, "prompt_tokens_details": {"cached_tokens": 700}}
    sink = Sink()
    response = _router(FakeTransport(chat=_ok(usage)), sink=sink).complete("planning", LAYOUT, ctx=CallContext(), json_output=True)
    assert response.cached_input_tokens == 700 and response.input_tokens == 1000
    record = sink.records[-1]
    assert record["status"] == "ok" and cached_prompt_tokens(record["response"]["usage"]) == 700  # what DbUsageSink stores
    assert record["request"]["messages"][0]["cache"] is True  # the logical layout is what replay re-sends


# ------------------------------------------------------------------ P4-T06 knowledge version
def test_cache_key_includes_the_knowledge_version():
    base = ResponseCache.key("planning", ["m"], {"x": 1}, "ws1")
    assert ResponseCache.key("planning", ["m"], {"x": 1}, "ws1", knowledge_version=None) == base  # unchanged without one
    v1 = ResponseCache.key("planning", ["m"], {"x": 1}, "ws1", knowledge_version="kv:1")
    v2 = ResponseCache.key("planning", ["m"], {"x": 1}, "ws1", knowledge_version="kv:2")
    assert len({base, v1, v2}) == 3 and v1.startswith("aos:llm:tenant:-:scope:-:policy:-:ws1:planning:")


def test_a_knowledge_edit_misses_the_response_cache():
    transport = FakeTransport(chat=_ok())
    router = _router(transport, cache=True)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for kv in ("kv:before", "kv:before", "kv:after", "kv:after"):
        router.complete("planning", messages, ctx=CallContext(workspace_id="ws1", knowledge_version=kv), json_output=True)
    assert len(transport.chat_calls) == 2  # one miss per knowledge version; repeats are hits
    statuses = [r["status"] for r in router.sink.records]
    assert statuses == ["ok", "cache_hit", "ok", "cache_hit"]


def test_measure_script_reports_the_provider_cached_share():
    import importlib.util

    from analystos.core.config import REPO_ROOT

    spec = importlib.util.spec_from_file_location("measure_prompt_cache", REPO_ROOT / "scripts" / "measure_prompt_cache.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    seen = []

    def chat(p):
        seen.append(p)
        cached = 0 if len(seen) == 1 else 900  # first call writes the cache, later calls read it
        return {"model": p["model"], "choices": [{"message": {"content": '{"ok":1}'}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": cached}}}

    report = script.probe("hypothesis_generation", 4, router=_router(FakeTransport(chat=chat),
                                                                     escalation={"hypothesis_generation": "always_large"}))
    assert report["cached_share"] == 0.675 and report["cached_share_after_first_call"] == 0.9
    assert all(p["messages"][0]["content"][0]["cache_control"] for p in seen)  # production layout, breakpoints sent
    assert len({p["messages"][1]["content"][-1]["text"] for p in seen}) == 4  # volatile part differs: no L0 hits
    assert report["prefix_estimated_tokens"] > 500

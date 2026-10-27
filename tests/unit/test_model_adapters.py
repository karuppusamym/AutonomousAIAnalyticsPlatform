"""S05-T2: OpenAI Responses, Azure OpenAI and Gemini adapters (ported from Atlas) under the router protocol.
The real HttpTransport over an httpx.MockTransport sees exactly the URL, headers and body that would leave."""
from __future__ import annotations

import json

import httpx
import pytest

from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import ModelRouteUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import ModelsConfig
from analystos.llm.router import CallContext, HttpTransport, ModelRouter


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def _wire(answer):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer(request))
    return seen, handler


def _router(provider: dict, model: str, *, blocked: list[str] | None = None, family_model: str | None = None):
    cfg = ModelsConfig.model_validate({
        "providers": {"p": provider}, "allowlist": [model], "blocked_families": blocked or [],
        "models": {model: {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}},
        "profiles": {"chat": {"provider": "p", "models": [model], "max_tokens": 300}},
        "routing": {"planning": "chat"}})
    sink = Sink()
    router = ModelRouter(cfg, sink=sink, api_key_lookup={"KEY": "secret-key"}.get, max_retries=0,
                         settings_provider=PlatformSettings, cache=ResponseCache(None), air_gapped=False)
    assert isinstance(router.transport, HttpTransport)
    return router, sink


def _messages():
    return [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Count incidents."},
            {"role": "assistant", "content": "{}"}, {"role": "user", "content": "Again, with a total."}]


def test_openai_responses_roundtrip():
    seen, handler = _wire(lambda r: {
        "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"total": 7}'}]}],
        "usage": {"input_tokens": 50, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 20}}})
    router, sink = _router({"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "KEY",
                            "region": "us"}, "openai/gpt-5.4-mini")
    router.transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    r = router.complete("planning", _messages(), json_output=True, ctx=CallContext(workspace_id="ws1"))
    assert r.data == {"total": 7} and r.model == "openai/gpt-5.4-mini"
    (req,) = seen
    body = json.loads(req.content)
    assert str(req.url) == "https://api.openai.com/v1/responses" and req.headers["authorization"] == "Bearer secret-key"
    assert body["model"] == "gpt-5.4-mini" and body["max_output_tokens"] == 300
    assert body["instructions"].startswith("Return JSON.") and [m["role"] for m in body["input"]] == ["user", "assistant", "user"]
    assert body["text"] == {"format": {"type": "json_object"}}
    assert "temperature" not in body and "usage" not in body  # reasoning models reject temperature; no OpenRouter fields
    assert (r.input_tokens, r.output_tokens) == (50, 9) and sink.records[-1]["status"] == "ok"


def test_azure_openai_quotes_the_deployment_and_api_version():
    seen, handler = _wire(lambda r: {"choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                               "prompt_tokens_details": {"cached_tokens": 4}}})
    router, _ = _router({"type": "azure_openai", "base_url": "https://res.openai.azure.com", "api_key_env": "KEY",
                         "api_version": "2025-04-01-preview", "region": "eu",
                         "deployments": {"openai/gpt-5.4-mini": "gpt mini/prod"}}, "openai/gpt-5.4-mini")
    router.transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    router.complete_json("planning", "s", "u")
    (req,) = seen
    assert req.url.raw_path.decode() == "/openai/deployments/gpt%20mini%2Fprod/chat/completions?api-version=2025-04-01-preview"
    assert req.headers["api-key"] == "secret-key" and "model" not in json.loads(req.content)


def test_gemini_roundtrip_maps_roles_system_and_thinking_tokens():
    seen, handler = _wire(lambda r: {
        "candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": '{"total": 3}'}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 6, "thoughtsTokenCount": 30}})
    # Gemini is blocked on this platform (owner decision 2026-10-27); the adapter is kept for completeness and
    # exercised here with blocking off.
    router, _ = _router({"type": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                         "api_key_env": "KEY", "region": "eu"}, "google/gemini-3.5-flash")
    router.transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    r = router.complete("planning", _messages(), json_output=True)
    (req,) = seen
    body = json.loads(req.content)
    assert req.url.path == "/v1beta/models/gemini-3.5-flash:generateContent" and req.headers["x-goog-api-key"] == "secret-key"
    assert "key" not in str(req.url.query)  # the key travels in a header, never in the URL
    assert body["system_instruction"] == {"parts": [{"text": "Return JSON.\n\nRespond with a single valid JSON value only."}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 300
    assert r.data == {"total": 3} and (r.input_tokens, r.output_tokens) == (40, 36)


def test_gemini_is_blocked_on_this_platform_before_any_request():
    seen, handler = _wire(lambda r: {})
    router, _ = _router({"type": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                         "api_key_env": "KEY"}, "google/gemini-3.5-flash", blocked=["anthropic", "google"])
    router.transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelRouteUnavailable):
        router.complete_json("planning", "s", "u")
    assert seen == []


def test_the_shipped_example_and_models_file_route_nothing_to_blocked_families():
    from analystos.llm.config import load_models_config

    load_models_config.cache_clear()
    try:
        cfg = load_models_config()
    finally:
        load_models_config.cache_clear()
    assert {"anthropic", "google"} <= set(cfg.blocked_families)
    for name, profile in cfg.profiles.items():
        assert not [m for m in profile.models + profile.escalation_models
                    if m.split("/", 1)[0] in cfg.blocked_families], name

"""Providers beyond OpenRouter and the air-gapped egress guard (P4-S04, spec v3 §8).

The HTTP-level tests run the real HttpTransport over an httpx.MockTransport, so they see exactly the
URLs, headers and bodies that would leave the process."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from analystos.contracts.platform import PRESETS, LLMSettings, PlatformSettings
from analystos.core.errors import EgressBlocked, ModelRouteUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import ModelsConfig, ProviderConfig, load_models_config
from analystos.llm.providers import BedrockAdapter, BedrockUnavailable, anthropic_request
from analystos.llm.router import CallContext, HttpTransport, ModelRouter

CONFIG = Path(__file__).resolve().parents[2] / "config"


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


class Wire:
    """Records every request the HttpTransport sends; answers in the shape the URL asks for."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(200, json={"model": "claude-sonnet-5", "content": [{"type": "text", "text": '{"ok": true}'}],
                                             "stop_reason": "end_turn",
                                             "usage": {"input_tokens": 40, "output_tokens": 7, "cache_read_input_tokens": 60}})
        return httpx.Response(200, json={"model": "served-name", "choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    @property
    def hosts(self) -> set[str]:
        return {r.url.host for r in self.requests}

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def _router(config: ModelsConfig, *, air_gapped: bool, keys: dict | None = None, modes: dict | None = None):
    wire = Wire()
    router = ModelRouter(config, sink=Sink(), api_key_lookup=(keys or {}).get, max_retries=0, air_gapped=air_gapped,
                         settings_provider=lambda: PlatformSettings(llm=LLMSettings(purpose_modes=modes or {})),
                         cache=ResponseCache(None))
    assert isinstance(router.transport, HttpTransport)  # the router's own guarded transport, not a fake
    router.transport._client = httpx.Client(transport=httpx.MockTransport(wire))
    return router, wire


def _config(path: str) -> ModelsConfig:
    load_models_config.cache_clear()
    try:
        return load_models_config(CONFIG / path)
    finally:
        load_models_config.cache_clear()


# ----------------------------------------------------------------------------- air-gapped mode
def test_air_gapped_calls_only_reach_the_configured_internal_host():
    config = _config("models.airgapped.yaml")
    assert set(config.providers) == {"local"} and config.providers["local"].egress == "internal"
    router, wire = _router(config, air_gapped=True, modes=PRESETS["air_gapped"])
    answered = 0
    for purpose in config.routing:
        if router.mode(purpose) == "off":
            continue
        try:
            router.complete_json(purpose, "system", "user", ctx=CallContext(workspace_id="ws"))
            answered += 1
        except ModelRouteUnavailable:
            pass  # e.g. decision purposes: no decision provider offline
    for purpose in ("hypothesis_priority", "risk_check"):
        with pytest.raises(ModelRouteUnavailable):
            router.decide(purpose, {"objective": "x"}, {"q": {"type": "noul"}})
    assert answered >= 5  # chat purposes do reach the local model
    assert wire.hosts == {"llm.analystos.svc"}  # and nothing else, ever
    assert all("authorization" not in r.headers for r in wire.requests)  # local endpoint: no key configured
    assert {wire.body(i)["model"] for i in range(len(wire.requests))} <= {"qwen3-32b", "llama-3.3-70b-instruct"}


def test_air_gapped_refuses_public_providers_before_any_connection():
    """The default (OpenRouter) config under the air-gapped flag: every route is blocked by policy and
    the transport would refuse the host even if a route got through."""
    config = _config("models.yaml")
    router, wire = _router(config, air_gapped=True, keys={"OPENROUTER_API_KEY": "sk-test"})
    with pytest.raises(ModelRouteUnavailable, match="air-gapped"):
        router.complete_json("planning", "s", "u")
    with pytest.raises(ModelRouteUnavailable, match="air-gapped"):
        router.decide("hypothesis_priority", {"objective": "x"}, {"q": {"type": "noul"}})
    assert not router.available("planning")
    assert router.transport.allowed_hosts == set()  # no internal provider configured -> nothing reachable
    with pytest.raises(EgressBlocked):
        router.transport.post("https://openrouter.ai/api/v1/chat/completions", headers={}, payload={}, timeout=1)
    assert wire.requests == []


def test_transport_refuses_hosts_outside_the_configured_providers_even_when_online():
    config = _config("models.yaml")
    router, wire = _router(config, air_gapped=False, keys={"OPENROUTER_API_KEY": "sk-test"})
    assert router.transport.allowed_hosts == {"openrouter.ai"}
    with pytest.raises(EgressBlocked):
        router.transport.post("https://attacker.example/v1/chat/completions", headers={}, payload={}, timeout=1)
    router.complete_json("planning", "s", "u")
    assert wire.hosts == {"openrouter.ai"}


def test_decision_service_keeps_only_local_backends_when_air_gapped():
    from analystos.decisions.config import load
    from analystos.decisions.service import DecisionService
    from analystos.decisions.store import MemoryDecisionStore

    router, _ = _router(_config("models.airgapped.yaml"), air_gapped=True, modes=PRESETS["air_gapped"])
    service = DecisionService(router, store=MemoryDecisionStore(), settings_provider=router.settings_provider)
    for name, spec in load().purposes.items():
        order, _removed = service.chain(spec)
        assert set(order) <= {"rules", "local_classifier"}, (name, order)
        assert "rules" in order


def test_air_gapped_preset_extends_offline():
    offline, air = PRESETS["offline"], PRESETS["air_gapped"]
    decision_only = {"rev_second_opinion", "risk_check", "alert_triage", "decision_structured"}
    assert all(air[p] == "off" for p in decision_only)
    assert air["planning"] == "auto" and offline["planning"] == "off"  # a local model may answer after the rules


def test_internal_providers_pass_the_default_workspace_provider_policy():
    from analystos.contracts.policy import WorkspacePolicyDoc

    config = _config("models.airgapped.yaml")
    router, wire = _router(config, air_gapped=True)
    router.complete_json("planning", "s", "u", ctx=CallContext.for_policy(WorkspacePolicyDoc()))
    with pytest.raises(ModelRouteUnavailable, match="allowed_providers"):
        router.complete_json("planning", "s", "u", ctx=CallContext(allowed_providers=["openrouter"]))
    assert len(wire.requests) == 1


# ----------------------------------------------------------------------------- provider adapters
def _single(provider: dict, models: list[str], **extra) -> ModelsConfig:
    return ModelsConfig.model_validate({
        "providers": {"p": provider}, "allowlist": models,
        "models": {m: {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0} for m in models},
        "profiles": {"reasoning_strong": {"provider": "p", "models": models, "max_tokens": 300}},
        "routing": {"planning": "reasoning_strong"}, "ladders": {"planning": ["cache", "llm_large"]}, **extra})


def test_anthropic_messages_mapping():
    config = _single({"kind": "chat", "type": "anthropic", "base_url": "https://api.anthropic.com",
                      "api_key_env": "ANTHROPIC_API_KEY"}, ["anthropic/claude-sonnet-5"])
    router, wire = _router(config, air_gapped=False, keys={"ANTHROPIC_API_KEY": "sk-ant-test"})
    out = router.complete("planning", [{"role": "system", "content": "static rules", "cache": True}, {"role": "user", "content": "question"}],
                          json_output=True)
    req = wire.requests[-1]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test" and req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers
    body = wire.body()
    assert body["model"] == "claude-sonnet-5" and body["max_tokens"] == 300
    assert body["system"][0]["text"].startswith("static rules") and "response_format" not in body
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    assert out.data == {"ok": True} and out.model == "anthropic/claude-sonnet-5"
    assert out.input_tokens == 100 and out.cached_input_tokens == 60 and out.output_tokens == 7
    assert router.sink.records[-1]["cost_source"].startswith("price_table")


def test_anthropic_request_keeps_cache_breakpoints_and_starts_with_user():
    provider = ProviderConfig(kind="chat", type="anthropic", base_url="https://api.anthropic.com", api_key_env="K")
    body = anthropic_request({"model": "anthropic/claude-opus-5", "max_tokens": 10, "messages": [
        {"role": "system", "content": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": "a"}]}, provider)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["role"] == "user" and body["model"] == "claude-opus-5"


def test_azure_openai_deployment_url_and_key_header():
    config = _single({"kind": "chat", "type": "azure_openai", "base_url": "https://res.openai.azure.com",
                      "api_key_env": "AZURE_OPENAI_API_KEY", "api_version": "2025-04-01-preview",
                      "deployments": {"openai/gpt-5.4": "gpt54-prod"}}, ["openai/gpt-5.4"])
    router, wire = _router(config, air_gapped=False, keys={"AZURE_OPENAI_API_KEY": "az-test"})
    out = router.complete_json("planning", "s", "u")
    req = wire.requests[-1]
    assert req.url.path == "/openai/deployments/gpt54-prod/chat/completions"
    assert req.url.params["api-version"] == "2025-04-01-preview"
    assert req.headers["api-key"] == "az-test" and "authorization" not in req.headers
    body = wire.body()
    assert "model" not in body and "usage" not in body and body["response_format"] == {"type": "json_object"}
    assert out.model == "openai/gpt-5.4"


def test_azure_needs_an_api_version():
    with pytest.raises(ValueError, match="api_version"):
        ProviderConfig(kind="chat", type="azure_openai", base_url="https://x.openai.azure.com", api_key_env="K")


def test_openai_compatible_with_key_and_base_url_override(monkeypatch):
    monkeypatch.setenv("MY_LLM_URL", "http://vllm.internal:8000/v1/")
    config = _single({"kind": "chat", "type": "openai_compatible", "base_url": "http://unused:1/v1", "base_url_env": "MY_LLM_URL",
                      "api_key_env": "VLLM_KEY", "egress": "internal", "model_map": {"qwen/qwen3-32b": "Qwen/Qwen3-32B"}},
                     ["qwen/qwen3-32b"])
    router, wire = _router(config, air_gapped=True, keys={"VLLM_KEY": "local-key"})
    out = router.complete_json("planning", "s", "u")
    req = wire.requests[-1]
    assert str(req.url) == "http://vllm.internal:8000/v1/chat/completions" and req.headers["authorization"] == "Bearer local-key"
    assert wire.body()["model"] == "Qwen/Qwen3-32B" and out.model == "qwen/qwen3-32b"
    assert isinstance(wire.body()["messages"][0]["content"], str)


def test_missing_key_is_a_route_error_not_a_call():
    config = _single({"kind": "chat", "type": "anthropic", "base_url": "https://api.anthropic.com",
                      "api_key_env": "ANTHROPIC_API_KEY"}, ["anthropic/claude-sonnet-5"])
    router, wire = _router(config, air_gapped=False, keys={})
    assert not router.available("planning")
    with pytest.raises(ModelRouteUnavailable, match="ANTHROPIC_API_KEY"):
        router.complete_json("planning", "s", "u")
    assert wire.requests == []


def test_public_provider_types_cannot_claim_internal_egress():
    with pytest.raises(ValueError, match="public endpoint"):
        ProviderConfig(kind="chat", type="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="K", egress="internal")


def test_azure_openai_is_internal_only_with_an_explicit_private_link():
    """m3: an Azure OpenAI resource is a public endpoint unless the config acknowledges a private endpoint."""
    azure = {"kind": "chat", "type": "azure_openai", "base_url": "https://r.openai.azure.com", "api_version": "2024-10-21",
             "egress": "internal"}
    with pytest.raises(ValueError, match="private_link"):
        ProviderConfig(**azure)
    assert ProviderConfig(**azure, private_link=True).egress == "internal"
    with pytest.raises(ValueError, match="private_link applies"):
        ProviderConfig(kind="chat", type="openai_compatible", base_url="http://vllm:8000/v1", private_link=True)


def test_keys_are_never_part_of_a_models_config():
    with pytest.raises(ValueError):
        ProviderConfig(kind="chat", base_url="https://openrouter.ai/api/v1", api_key="sk-live-123")
    for path in CONFIG.glob("models*.yaml"):
        for name, provider in (yaml.safe_load(path.read_text(encoding="utf-8")).get("providers") or {}).items():
            assert "api_key" not in provider, (path.name, name)


def test_bedrock_without_boto3_fails_clearly(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    provider = ProviderConfig(kind="chat", type="bedrock", base_url="https://bedrock-runtime.eu-central-1.amazonaws.com",
                              aws_region="eu-central-1")
    with pytest.raises(BedrockUnavailable, match="boto3"):
        BedrockAdapter().chat(None, provider, None, {"model": "anthropic/claude-sonnet-5", "messages": []}, 5)


def test_bedrock_converse_mapping_with_a_client_double():
    calls = []

    class Client:
        def converse(self, **kw):
            calls.append(kw)
            return {"output": {"message": {"content": [{"text": "hi"}]}}, "usage": {"inputTokens": 5, "outputTokens": 1}}

    provider = ProviderConfig(kind="chat", type="bedrock", base_url="https://bedrock-runtime.eu-central-1.amazonaws.com",
                              model_map={"anthropic/claude-sonnet-5": "anthropic.claude-sonnet-5-v1:0"})
    out = BedrockAdapter(client_factory=lambda _p: Client()).chat(
        None, provider, None, {"model": "anthropic/claude-sonnet-5", "max_tokens": 50, "temperature": 0.1,
                               "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}, 5)
    assert calls[0]["modelId"] == "anthropic.claude-sonnet-5-v1:0" and calls[0]["system"] == [{"text": "s"}]
    assert out["choices"][0]["message"]["content"] == "hi" and out["usage"] == {"prompt_tokens": 5, "completion_tokens": 1}


def test_bedrock_endpoint_must_pass_the_egress_guard():
    """m4: boto3 opens its own connection; its endpoint host is checked against the transport's allowlist first."""
    calls = []

    class Client:
        meta = SimpleNamespace(endpoint_url="https://bedrock-runtime.eu-central-1.amazonaws.com")

        def converse(self, **kw):
            calls.append(kw)
            return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {}}

    provider = ProviderConfig(kind="chat", type="bedrock", base_url="https://bedrock-runtime.eu-central-1.amazonaws.com")
    adapter = BedrockAdapter(client_factory=lambda _p: Client())
    payload = {"model": "anthropic/claude-sonnet-5", "messages": [{"role": "user", "content": "u"}]}
    with pytest.raises(EgressBlocked, match="bedrock host"):
        adapter.chat(HttpTransport(allowed_hosts={"vllm.internal"}), provider, None, payload, 5)  # air-gapped: internal only
    assert not calls
    ok = adapter.chat(HttpTransport(allowed_hosts={"bedrock-runtime.eu-central-1.amazonaws.com"}), provider, None, payload, 5)
    assert ok["choices"][0]["message"]["content"] == "ok" and len(calls) == 1
    Client.meta = SimpleNamespace(endpoint_url=None)  # an endpoint it cannot name is refused under a guard
    with pytest.raises(EgressBlocked):
        adapter.chat(HttpTransport(allowed_hosts={"bedrock-runtime.eu-central-1.amazonaws.com"}), provider, None, payload, 5)


def test_example_provider_config_is_valid():
    config = _config("models.providers.example.yaml")
    assert {p.type for p in config.providers.values()} >= {"anthropic", "azure_openai", "bedrock", "openrouter",
                                                           "openai_compatible"}
    vllm = config.providers["onprem_vllm"]
    assert vllm.capabilities.context_window == 32768 and vllm.capabilities.tools is False and vllm.region == "eu"
    assert config.ladders and config.decisions  # inherited from models.yaml through `extends`

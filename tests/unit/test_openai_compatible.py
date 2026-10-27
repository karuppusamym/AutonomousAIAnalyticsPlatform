"""S05-T1: the `openai_compatible` provider (vLLM, Ollama, LM Studio, TGI, Groq, Together, a LiteLLM proxy)
against a local HTTP stub: the real router transport, egress guard and adapter, over a real socket."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import ModelRouteUnavailable
from analystos.llm.cache import ResponseCache
from analystos.llm.config import ModelCapabilities, ModelsConfig, ProviderConfig
from analystos.llm.providers import fit_capabilities
from analystos.llm.router import CallContext, ModelRouter


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


@pytest.fixture
def stub():
    """An OpenAI-compatible server: records each request, answers a chat completion in the served model name."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            out = json.dumps({"id": "cmpl-1", "model": body["model"],
                              "choices": [{"message": {"role": "assistant", "content": '{"answer": 42}'},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 21, "completion_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()
    server.server_close()


def _config() -> ModelsConfig:
    return ModelsConfig.model_validate({
        "providers": {"onprem_vllm": {"type": "openai_compatible", "base_url_env": "VLLM_URL", "api_key_env": "VLLM_KEY",
                                      "egress": "internal", "region": "eu",
                                      "model_map": {"local/sqlcoder-ft": "sqlcoder-7b-ft-v3"},
                                      "capabilities": {"json_mode": False, "tools": False, "context_window": 4096}}},
        "allowlist": ["local/sqlcoder-ft"],
        "models": {"local/sqlcoder-ft": {"input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0}},
        "profiles": {"coding_local": {"provider": "onprem_vllm", "models": ["local/sqlcoder-ft"], "max_tokens": 9000}},
        "routing": {"sql_generation": "coding_local"},
    })


def test_openai_compatible_roundtrip(stub, monkeypatch):
    base_url, seen = stub
    monkeypatch.setenv("VLLM_URL", base_url)  # base_url_env only: no base_url in the file
    sink = Sink()
    router = ModelRouter(_config(), sink=sink, api_key_lookup={"VLLM_KEY": "local-key"}.get, max_retries=0,
                         settings_provider=PlatformSettings, cache=ResponseCache(None), air_gapped=True)
    assert router.transport.allowed_hosts == {"127.0.0.1"}  # internal endpoint: the only host an air-gapped install reaches

    r = router.complete_json("sql_generation", "You write SQL.", "Count incidents by priority.",
                             ctx=CallContext(workspace_id="ws1", data_residency="eu"))

    assert r.data == {"answer": 42} and r.model == "local/sqlcoder-ft"  # the allowlisted id, not the served name
    assert (r.input_tokens, r.output_tokens, r.cost_usd) == (21, 5, 0.0)
    (req,) = seen
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer local-key"
    body = req["body"]
    assert body["model"] == "sqlcoder-7b-ft-v3"  # model_map
    assert "response_format" not in body  # json_mode: false -> the prompt asks for JSON, the router parses it
    assert "usage" not in body and "reasoning" not in body  # OpenRouter-only fields never reach the server
    assert all(isinstance(m["content"], str) for m in body["messages"])
    assert body["max_tokens"] < 4096  # the profile's 9000 is capped to what fits the declared context window
    assert sink.records[-1]["status"] == "ok" and sink.records[-1]["provider"] == "onprem_vllm"


def test_declared_capabilities_shape_the_request():
    body = {"model": "m", "messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 10_000,
            "response_format": {"type": "json_object"}, "tools": [{"type": "function"}], "tool_choice": "auto"}
    kept = fit_capabilities(body, ModelCapabilities(json_mode=True, tools=True), "m")
    assert kept == body  # declared capable (or not declared): unchanged
    fitted = fit_capabilities(body, ModelCapabilities(json_mode=False, tools=False, context_window=1000), "m")
    assert "response_format" not in fitted and "tools" not in fitted and "tool_choice" not in fitted
    assert fitted["max_tokens"] == 1000 - (int(400 / 3.6) + 1)
    with pytest.raises(ModelRouteUnavailable, match="does not fit the declared context_window"):
        fit_capabilities({**body, "messages": [{"role": "user", "content": "x" * 4000}]},
                         ModelCapabilities(context_window=1000), "m")


def test_provider_needs_a_base_url_or_its_env_and_fails_clearly_without_one(monkeypatch):
    with pytest.raises(ValueError, match="needs base_url or base_url_env"):
        ProviderConfig(type="openai_compatible")
    monkeypatch.delenv("VLLM_URL", raising=False)
    router = ModelRouter(_config(), sink=Sink(), api_key_lookup={"VLLM_KEY": "k"}.get, max_retries=0,
                         settings_provider=PlatformSettings, cache=ResponseCache(None))
    with pytest.raises(ModelRouteUnavailable, match="no base URL \\(set VLLM_URL\\)"):
        router.complete_json("sql_generation", "s", "u")


def test_model_capabilities_override_the_provider_declaration():
    cfg = _config()
    cfg.models["local/sqlcoder-ft"].capabilities = ModelCapabilities(json_mode=True)
    caps = cfg.capabilities_of("local/sqlcoder-ft", "onprem_vllm")
    assert caps.json_mode is True and caps.tools is False and caps.context_window == 4096

"""Model providers beyond OpenRouter (P4-S04, spec v3 §8 "Model providers").

The router speaks one request shape — OpenAI chat completions, as OpenRouter takes it — and reads one
response shape back. An adapter maps that shape to a provider's wire API and the answer back, so
routing, budgets, redaction, caching and the call record are the same whichever provider answers:

  openrouter         the router's own transport path (unchanged)
  openai_compatible  `<base_url>/chat/completions`; covers vLLM, Ollama, LM Studio, llama.cpp server, TGI,
                     Groq, Together, Fireworks and a LiteLLM proxy. Bearer key when `api_key_env` is set,
                     none for a local endpoint. Such a server cannot be asked what it supports, so the
                     provider declares `capabilities` (json_mode, tools, context_window): a request is
                     shaped to fit them instead of being rejected by the server
  azure_openai       `<base_url>/openai/deployments/<deployment>/chat/completions?api-version=...`, `api-key` header
  anthropic          the Messages API (`<base_url>/v1/messages`): system turns lifted into `system`,
                     cache_control blocks kept, usage mapped back (cache reads included)
  bedrock            the Converse API through boto3 (AWS credentials from the standard chain, never
                     config); without boto3 installed the provider fails with a clear, non-retryable error

Keys come from the environment only (`api_key_env`, a Kubernetes secret in the Helm chart). Every
HTTP adapter goes through `transport.post`, so the transport's egress guard sees every host.
"""
from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlsplit

from analystos.core.errors import EgressBlocked, ModelRouteUnavailable
from analystos.llm.config import ModelCapabilities, ProviderConfig

OPENROUTER_ONLY_FIELDS = ("usage", "reasoning")  # `usage: {include: true}` and `reasoning: {effort}` are OpenRouter extensions


class PostTransport(Protocol):
    def post(self, url: str, *, headers: dict[str, str], payload: dict, timeout: float) -> dict: ...


def _poster(transport: Any, provider: ProviderConfig) -> PostTransport:
    if not callable(getattr(transport, "post", None)):
        raise ModelRouteUnavailable(f"the model transport cannot reach a provider of type {provider.type} (no post())")
    return transport


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return str(content or "")


def _openai_body(payload: dict, provider: ProviderConfig, logical_model: str) -> dict:
    body = {k: v for k, v in payload.items() if k not in OPENROUTER_ONLY_FIELDS}
    body["model"] = provider.wire_model(logical_model)
    # Plain-string content: cache_control blocks are an Anthropic/OpenRouter extension that local
    # OpenAI-compatible servers reject; their prefix caching needs only the stable order.
    body["messages"] = [{"role": m["role"], "content": _text(m.get("content"))} for m in payload["messages"]]
    return body


def _as_logical(body: dict, logical_model: str) -> dict:
    """Report the allowlisted id, not the provider's, so prices, allowlists and records line up."""
    return {**body, "model": logical_model}


CHARS_PER_TOKEN = 3.6  # the router's own estimate (context budgets); conservative for a window check
MIN_COMPLETION_TOKENS = 64


def fit_capabilities(body: dict, caps: ModelCapabilities, logical_model: str) -> dict:
    """Shape an OpenAI-style body to what the endpoint declared. `json_mode: false` drops
    response_format (the prompt still asks for JSON and the router still parses and validates it);
    `tools: false` drops tool fields; `context_window` caps max_tokens to what fits after the prompt,
    and a prompt that leaves no room is refused before any connection (the router tries the next model)."""
    out = dict(body)
    if caps.json_mode is False:
        out.pop("response_format", None)
    if caps.tools is False:
        for k in ("tools", "tool_choice", "parallel_tool_calls"):
            out.pop(k, None)
    if caps.context_window:
        prompt = int(sum(len(str(m.get("content") or "")) for m in out.get("messages") or []) / CHARS_PER_TOKEN) + 1
        room = caps.context_window - prompt
        if room < MIN_COMPLETION_TOKENS:
            raise ModelRouteUnavailable(
                f"prompt of ~{prompt} tokens does not fit the declared context_window {caps.context_window} of "
                f"{logical_model}", details={"model": logical_model, "prompt_tokens": prompt,
                                             "context_window": caps.context_window})
        if int(out.get("max_tokens") or 0) > room or not out.get("max_tokens"):
            out["max_tokens"] = room
    return out


class OpenAICompatibleAdapter:
    """`capabilities` is what the router resolved for this model (ModelsConfig.capabilities_of: the
    model's own declaration over the provider's); the provider's alone when called directly."""

    def chat(self, transport: Any, provider: ProviderConfig, api_key: str | None, payload: dict, timeout: float,
             capabilities: ModelCapabilities | None = None) -> dict:
        model = payload["model"]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        body = fit_capabilities(_openai_body(payload, provider, model), capabilities or provider.capabilities, model)
        out = _poster(transport, provider).post(f"{provider.url()}/chat/completions", headers=headers,
                                                payload=body, timeout=timeout)
        return _as_logical(out, model)


class AzureOpenAIAdapter:
    def chat(self, transport: Any, provider: ProviderConfig, api_key: str | None, payload: dict, timeout: float) -> dict:
        model = payload["model"]
        deployment = provider.deployments.get(model) or provider.wire_model(model)
        url = f"{provider.url()}/openai/deployments/{deployment}/chat/completions?api-version={provider.api_version}"
        body = _openai_body(payload, provider, model)
        body.pop("model", None)  # the deployment names the model
        out = _poster(transport, provider).post(url, headers={"api-key": api_key or ""}, payload=body, timeout=timeout)
        return _as_logical(out, model)


def anthropic_request(payload: dict, provider: ProviderConfig) -> dict:
    """OpenAI-shaped chat payload -> Anthropic Messages body. System turns become `system` (blocks,
    so cache breakpoints survive); the rest must alternate user/assistant, which the router's merged
    turns already guarantee."""
    system: list[dict] = []
    messages: list[dict] = []
    for m in payload["messages"]:
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
        if m["role"] == "system":
            system.extend(blocks)
        else:
            messages.append({"role": m["role"], "content": blocks})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "Proceed."}]})
    body: dict[str, Any] = {"model": provider.wire_model(payload["model"]), "messages": messages,
                            "max_tokens": int(payload.get("max_tokens") or 1024)}
    if system:
        body["system"] = system
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    return body


def anthropic_response(body: dict, logical_model: str) -> dict:
    text = "".join(b.get("text") or "" for b in body.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
    usage = body.get("usage") or {}
    read, created = int(usage.get("cache_read_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0)
    return {"model": logical_model, "choices": [{"message": {"content": text}, "finish_reason": body.get("stop_reason")}],
            "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0) + read + created,
                      "completion_tokens": int(usage.get("output_tokens") or 0), "cache_read_input_tokens": read}}


class AnthropicAdapter:
    def chat(self, transport: Any, provider: ProviderConfig, api_key: str | None, payload: dict, timeout: float) -> dict:
        headers = {"x-api-key": api_key or "", "anthropic-version": provider.anthropic_version}
        body = _poster(transport, provider).post(f"{provider.url()}/v1/messages", headers=headers,
                                                  payload=anthropic_request(payload, provider), timeout=timeout)
        return anthropic_response(body, payload["model"])


class BedrockAdapter:
    """Converse API via boto3. Credentials come from the AWS chain (IRSA / instance role / env), so
    `api_key_env` stays unset. Kept optional: boto3 is not a core dependency."""

    def __init__(self, client_factory: Any = None) -> None:
        self._factory = client_factory

    def _client(self, provider: ProviderConfig) -> Any:
        if self._factory is not None:
            return self._factory(provider)
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BedrockUnavailable("the bedrock provider needs boto3 (pip install boto3); it is not installed") from exc
        return boto3.client("bedrock-runtime", region_name=provider.aws_region)

    @staticmethod
    def check_egress(transport: Any, client: Any) -> None:
        """boto3 opens its own connections, outside HttpTransport: the endpoint it will call must pass
        the same egress guard (the configured provider hosts; internal ones only when air-gapped)."""
        allowed = getattr(transport, "allowed_hosts", None)
        if allowed is None:
            return
        endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None) or ""
        host = (urlsplit(endpoint).hostname or "").lower()
        if host not in allowed:
            raise EgressBlocked(f"model transport refused bedrock host '{host or 'unknown'}': not an allowed "
                                f"provider endpoint (allowed: {', '.join(sorted(allowed)) or 'none'})")

    def chat(self, transport: Any, provider: ProviderConfig, api_key: str | None, payload: dict, timeout: float) -> dict:
        client = self._client(provider)
        self.check_egress(transport, client)
        system = [{"text": _text(m.get("content"))} for m in payload["messages"] if m["role"] == "system"]
        messages = [{"role": m["role"], "content": [{"text": _text(m.get("content"))}]}
                    for m in payload["messages"] if m["role"] != "system"]
        kwargs: dict[str, Any] = {"modelId": provider.wire_model(payload["model"]), "messages": messages,
                                  "inferenceConfig": {"maxTokens": int(payload.get("max_tokens") or 1024),
                                                      "temperature": float(payload.get("temperature") or 0.0)}}
        if system:
            kwargs["system"] = system
        try:
            out = client.converse(**kwargs)
        except Exception as exc:  # botocore errors: no stable import without boto3
            from analystos.core.errors import UpstreamUnavailable

            raise UpstreamUnavailable(f"bedrock converse failed: {exc}"[:300]) from exc
        text = "".join(c.get("text") or "" for c in ((out.get("output") or {}).get("message") or {}).get("content") or [])
        usage = out.get("usage") or {}
        return {"model": payload["model"], "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": int(usage.get("inputTokens") or 0), "completion_tokens": int(usage.get("outputTokens") or 0)}}


class BedrockUnavailable(ModelRouteUnavailable):
    code, retryable = "bedrock_unavailable", False


ADAPTERS: dict[str, Any] = {"openai_compatible": OpenAICompatibleAdapter(), "azure_openai": AzureOpenAIAdapter(),
                            "anthropic": AnthropicAdapter(), "bedrock": BedrockAdapter()}


def adapter_for(provider: ProviderConfig) -> Any:
    adapter = ADAPTERS.get(provider.type)
    if adapter is None:
        raise ModelRouteUnavailable(f"no adapter for provider type {provider.type}")
    return adapter


def needs_key(provider: ProviderConfig) -> bool:
    """Whether the provider cannot be called without a key from the environment."""
    return provider.api_key_env is not None


__all__ = ["ADAPTERS", "AnthropicAdapter", "AzureOpenAIAdapter", "BedrockAdapter", "BedrockUnavailable",
           "OpenAICompatibleAdapter", "adapter_for", "anthropic_request", "anthropic_response", "fit_capabilities",
           "needs_key"]

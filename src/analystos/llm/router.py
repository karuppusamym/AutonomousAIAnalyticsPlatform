"""Model router (MOD-001..004, GOV-003): purpose -> profile -> allowed models, with fallback,
bounded retry, budget checks, redaction and a persisted record of every call.

Two call shapes:
  complete()  chat models via OpenRouter chat completions (text or JSON output), or any other provider
              type through its adapter (analystos.llm.providers: OpenAI-compatible, Azure, Anthropic, Bedrock)
  decide()    TypeSafe Jev via the OpenRouter Decisions API (typed choice/noul/score answers)

The router never silently routes around policy: if the workspace allowlist, provider list or
data-residency rule removes every model of a profile, the call fails with ModelRouteUnavailable,
and a call whose pre-call cost estimate exceeds the workspace approval threshold raises
ApprovalRequired. Either way the caller must degrade visibly.

Every call record carries the redacted request and the response (P4-C09) so a run can be
reconstructed and re-executed offline with `analystos.llm.replay.ReplayTransport`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from analystos.contracts.platform import MODEL_RUNGS
from analystos.core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    EgressBlocked,
    EscalationUnavailable,
    LLMDisabled,
    ModelKeyMissing,
    ModelOutputInvalid,
    ModelPolicyBlocked,
    ModelResidencyBlocked,
    ModelRouteUnavailable,
    ProviderQuotaExhausted,
    UpstreamUnavailable,
)
from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.llm.cache import ResponseCache, estimate_tokens
from analystos.llm.config import ModelsConfig, ProfileConfig, family, load_models_config
from analystos.llm.providers import adapter_for
from analystos.llm.redaction import redact, redact_obj

log = get_logger(__name__)


JSON_INSTRUCTION = "\n\nRespond with a single valid JSON value only."


@dataclass
class CallContext:
    """Who is calling and under which workspace policy. The policy travels with the call so the
    router can enforce it without reading the database; fields left at None mean "no workspace
    restriction" (platform-level calls such as admin checks)."""

    workspace_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    prompt_version: str | None = None
    allowed_models: list[str] = field(default_factory=list)  # workspace policy narrowing ([] = platform list)
    exclude_families: list[str] = field(default_factory=list)  # e.g. primary family, for verification
    allowed_providers: list[str] | None = None  # workspace policy; None = unrestricted, [] = none allowed
    data_residency: str | None = None  # required provider/model region; unknown region fails closed
    expensive_model_approval_usd: float | None = None  # pre-call estimate above this needs an approval
    knowledge_version: str | None = None  # workspace knowledge version (P4-T06): part of the L0 cache key
    context_receipts: list[dict] | None = None  # what the context compiler put in the prompt (P4-T03)

    @classmethod
    def for_policy(cls, policy: Any, **kwargs: Any) -> CallContext:
        """Context carrying every model-relevant field of a WorkspacePolicyDoc."""
        return cls(**kwargs).with_policy(policy)

    def with_policy(self, policy: Any) -> CallContext:
        if policy is None:
            return self
        self.allowed_models = list(getattr(policy, "allowed_models", None) or self.allowed_models)
        self.allowed_providers = list(policy.allowed_providers)
        self.data_residency = policy.data_residency or None
        self.expensive_model_approval_usd = policy.expensive_model_approval_usd
        return self


@dataclass
class ModelResponse:
    text: str
    data: Any
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1
    cached: bool = False
    cached_input_tokens: int = 0  # prompt tokens the provider served from its prompt cache (P4-T04)
    escalated_from: str | None = None  # the small-tier model whose answer failed validation (cheap first, escalate)
    escalation_reason: str | None = None
    validation_error: str | None = None  # the caller's `validate` rejected this (final) answer; not cached
    routed_model: str | None = None  # the allowlisted model the request was sent to (the L0 cache key)


@dataclass
class _Call:
    """What one chat call carries through its tiers."""

    purpose: str
    ctx: CallContext
    profile_name: str
    profile: ProfileConfig
    rung: str
    base_url: str
    key: str
    safe_messages: list[dict[str, Any]]
    request: dict[str, Any]
    request_hash: str
    json_output: bool
    max_tokens: int
    input_estimate: int
    validate: Callable[[ModelResponse], str | None] | None


@dataclass
class _Rejected:
    """A first-tier answer that failed deterministic validation (or was not JSON): escalate."""

    model: str
    reason: str
    attempts: int
    response: ModelResponse | None = None


def _failed_validation(validate: Callable[[ModelResponse], str | None] | None, response: ModelResponse) -> str | None:
    if validate is None:
        return None
    try:
        why = validate(response)
    except Exception as exc:  # a validator that cannot read the answer has rejected it
        why = f"unreadable answer ({type(exc).__name__}: {exc})"
    return str(why)[:300] if why else None


@dataclass
class DecisionResponse:
    answers: dict[str, Any]
    model: str
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


class UsageSink(Protocol):
    def record(self, *, ctx: CallContext, purpose: str, profile: str, provider: str, model: str, status: str,
               attempt: int, latency_ms: int, input_tokens: int, output_tokens: int, cost_usd: float,
               request_hash: str | None, error: str | None, tokens_saved: int = 0,
               request: dict | None = None, response: dict | None = None, answered_by: str | None = None,
               cost_source: str | None = None) -> None:
        """`request`/`response` are the redacted replay payloads (see analystos.llm.replay);
        `answered_by` is the ladder rung, `cost_source` provider | price_table@<v> | missing_price | none.
        Optional keywords, passed only when set: `reservation` (settle it to cost_usd), `escalated_from` and
        `escalation_reason` (a large-tier call made because a small-tier answer failed validation)."""

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        """Raise BudgetExceeded when the run/workspace budget is spent."""

    def remaining_fraction(self, ctx: CallContext) -> float:
        """Share of the run's budget still available (1.0 when unknown)."""

    # Optional: reserve(*, ctx, purpose, model, estimate_usd) -> reservation (hard spend caps; raises
    # BudgetExceeded) and note_cooldown(provider, seconds, reason). The router calls them when present.


class NullSink:
    def record(self, **_: Any) -> None:
        return None

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        return None

    def remaining_fraction(self, ctx: CallContext) -> float:
        return 1.0


class Transport(Protocol):
    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict: ...
    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict: ...


class HttpTransport:
    """The one HTTP client model calls leave through. `allowed_hosts` is the egress guard: a URL whose
    host is not a configured provider endpoint (on an air-gapped install: an `egress: internal` one)
    is refused before any connection is opened. None = unguarded (only for callers that pass their own)."""

    def __init__(self, *, allowed_hosts: set[str] | None = None, client: httpx.Client | None = None) -> None:
        self.allowed_hosts = {h.lower() for h in allowed_hosts} if allowed_hosts is not None else None
        self._client = client or httpx.Client()

    def _guard(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise EgressBlocked(f"model transport refused host '{host}': not an allowed provider endpoint "
                                f"(allowed: {', '.join(sorted(self.allowed_hosts)) or 'none'})")

    def post(self, url: str, *, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        self._guard(url)
        try:
            response = self._client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable(f"model provider timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"model provider unreachable: {exc}") from exc
        if response.status_code in (408, 409, 425, 429, 529) or response.status_code >= 500:
            raise UpstreamUnavailable(f"model provider HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code == 402:
            raise ProviderQuotaExhausted(f"model provider refused for credits HTTP 402: {response.text[:300]}")
        if response.status_code >= 400:
            raise ModelRouteUnavailable(f"model provider rejected request HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def _openrouter(self, url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self.post(url, headers={"Authorization": f"Bearer {api_key}", **OPENROUTER_HEADERS}, payload=payload,
                         timeout=timeout)

    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._openrouter(f"{base_url.rstrip('/')}/chat/completions", api_key, payload, timeout)

    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        return self._openrouter(base_url.rstrip("/"), api_key, payload, timeout)


_NO_PROVIDER = type("_NoProvider", (), {"egress": "internet"})()
OPENROUTER_HEADERS = {"HTTP-Referer": "https://github.com/karuppusamym/AIAnalystAgentOS", "X-Title": "Context2AI AnalystOS"}


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


PROVIDER_COOLDOWN_SECONDS = 60
_PROVIDER_COOLDOWN: dict[str, float] = {}  # provider -> monotonic time the cooldown ends (process-wide)


def _cooling_down(provider: str) -> bool:
    return cooldown_left(provider) is not None


def cooldown_left(provider: str) -> float | None:
    """Seconds until a provider that refused for credits (HTTP 402) is tried again, or None."""
    until = _PROVIDER_COOLDOWN.get(provider)
    if until is None:
        return None
    left = until - time.monotonic()
    if left <= 0:
        _PROVIDER_COOLDOWN.pop(provider, None)
        return None
    return left


def _quota_exhausted(provider: str, message: str) -> ProviderQuotaExhausted:
    left = cooldown_left(provider)
    return ProviderQuotaExhausted(message, details={"provider": provider, "status": 402,
                                                    "retry_in_s": int(left + 0.999) if left is not None else None})


def cached_prompt_tokens(usage: dict | None) -> int:
    """Prompt tokens served from the provider's prompt cache, from whichever usage field the
    provider reports: OpenRouter/OpenAI `prompt_tokens_details.cached_tokens`, Anthropic
    `cache_read_input_tokens`, DeepSeek `prompt_cache_hit_tokens`, Responses API
    `input_tokens_details.cached_tokens`."""
    usage = usage or {}
    for parent, key in (("prompt_tokens_details", "cached_tokens"), ("input_tokens_details", "cached_tokens"),
                        (None, "cache_read_input_tokens"), (None, "prompt_cache_hit_tokens")):
        holder = usage.get(parent) if parent else usage
        if isinstance(holder, dict) and holder.get(key) is not None:
            try:
                return int(holder[key])
            except (TypeError, ValueError):
                return 0
    return 0


def _parts(messages: list[dict[str, Any]]) -> list[tuple[str, list[tuple[str, bool]]]]:
    """Consecutive messages of one role merged into one turn of (text, ends-a-stable-prefix) parts.
    Wire content blocks are read back to text, so a wire payload and its logical request normalise
    to the same turns."""
    turns: list[tuple[str, list[tuple[str, bool]]]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            parts = [(str(b.get("text") or ""), bool(b.get("cache_control"))) for b in content if isinstance(b, dict)]
        else:
            parts = [(str(content or ""), bool(m.get("cache")))]
        if turns and turns[-1][0] == m["role"]:
            turns[-1][1].extend(parts)
        else:
            turns.append((m["role"], parts))
    return turns


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Provider-independent form of a chat request: one `{role, content}` per turn, parts joined by
    a blank line. Replay keys and comparisons use it, so a request with cache breakpoints and the
    same request without them are the same request."""
    return [{"role": role, "content": "\n\n".join(t for t, _ in parts)} for role, parts in _parts(messages)]


def wire_messages(messages: list[dict[str, Any]], *, cache_control: bool) -> list[dict[str, Any]]:
    """Messages as sent to the provider (P4-T04). Messages flagged `cache: True` end a stable prefix
    (static system text, workspace header). With `cache_control` (model capability `prompt_cache`)
    each such part becomes a text block with an ephemeral `cache_control` breakpoint (Anthropic
    allows four; the last four are kept); otherwise the same ordered text is sent as plain strings,
    which is what automatic prefix caching needs."""
    turns = _parts(messages)
    if not cache_control or not any(c for _, parts in turns for _, c in parts):
        return normalize_messages(messages)
    marks = [(ti, pi) for ti, (_, parts) in enumerate(turns) for pi, (_, c) in enumerate(parts) if c][-4:]
    out: list[dict[str, Any]] = []
    for ti, (role, parts) in enumerate(turns):
        blocks = []
        for pi, (text, _) in enumerate(parts):
            block: dict[str, Any] = {"type": "text", "text": text}
            if (ti, pi) in marks:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        out.append({"role": role, "content": blocks})
    return out


def parse_json_text(text: str) -> Any:
    text = text.strip()
    match = _JSON_BLOCK.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])


def ladder_for_mode(default: list[str], mode: str) -> list[str]:
    """Apply an increment-3 mode to a ladder: off drops the model rungs (rules answer), auto puts the
    deterministic rungs first, always puts the model rungs first (rules stay the fallback)."""
    models = [r for r in default if r in MODEL_RUNGS]
    other = [r for r in default if r not in MODEL_RUNGS]
    if "rules" not in other:
        other.append("rules")  # every caller has a deterministic fallback, even if it is "degrade visibly"
    if mode == "off":
        return other
    cache = [r for r in other if r == "cache"]
    rest = [r for r in other if r != "cache"]
    return cache + (rest + models if mode == "auto" else models + rest)


def mode_of(ladder: list[str]) -> str:
    """The increment-3 mode a ladder amounts to (what `model_gate` and JEV callers branch on)."""
    model_at = next((i for i, r in enumerate(ladder) if r in MODEL_RUNGS), None)
    if model_at is None:
        return "off"
    det_at = next((i for i, r in enumerate(ladder) if r in ("registry", "rules")), None)
    return "auto" if det_at is not None and det_at < model_at else "always"


def _price_error(cost_source: str, model: str, config: ModelsConfig) -> str | None:
    if cost_source != "missing_price":
        return None
    return f"missing price: {model} has no entry in price table {config.prices_version} and the provider reported no cost"


class ModelRouter:
    def __init__(self, config: ModelsConfig | None = None, *, sink: UsageSink | None = None,
                 transport: Transport | None = None, api_key_lookup: Callable[[str], str | None] | None = None,
                 max_retries: int = 2, settings_provider: Callable[[], Any] | None = None,
                 cache: ResponseCache | None = None, air_gapped: bool | None = None) -> None:
        self.config = config or load_models_config()
        self.sink = sink or NullSink()
        self.air_gapped = _air_gapped() if air_gapped is None else air_gapped
        # Egress guard: only configured provider hosts, and only internal ones on an air-gapped install.
        self.transport = transport or HttpTransport(allowed_hosts=self.config.egress_hosts(internal_only=self.air_gapped))
        self.api_key_lookup = api_key_lookup or (lambda env: (os.getenv(env) or "").strip() or None)
        self.max_retries = max_retries
        self.settings_provider = settings_provider or _default_settings
        self.cache = cache if cache is not None else ResponseCache(None)

    # ------------------------------------------------------------------ admin settings
    @property
    def settings(self):
        return self.settings_provider().llm

    def ladder(self, purpose: str) -> list[str]:
        """The purpose's execution ladder (spec v3 §4.1): admin `ladders` override, else the admin
        mode applied to the models.yaml default, else the default. JEV purposes lose their decision
        rung when the jev feature flag is off."""
        platform = self.settings_provider()
        llm = platform.llm
        default = self.config.default_ladder(purpose)
        if purpose in llm.ladders:
            rungs = list(llm.ladders[purpose])
        elif purpose in llm.purpose_modes:
            rungs = ladder_for_mode(default, llm.purpose_modes[purpose])
        else:
            rungs = default
        if not platform.features.jev_decisions:
            if purpose in self.config.routing and self.config.routing[purpose] == "decision":
                rungs = ladder_for_mode(rungs, "off")
            else:  # a chat purpose with a typed decision rung before its chat rung keeps the chat rung
                rungs = [r for r in rungs if r != "decision"]
        return rungs

    def mode(self, purpose: str) -> str:
        """off | auto | always, derived from the ladder (ADR-0012: the increment-3 modes are ladder presets)."""
        return mode_of(self.ladder(purpose))

    def record_skip(self, purpose: str, ctx: CallContext | None, *, estimated_tokens: int, reason: str,
                    rung: str = "rules") -> None:
        """Account for a model call avoided by a deterministic rung (shown as tokens saved)."""
        ctx = ctx or CallContext()
        self.sink.record(ctx=ctx, purpose=purpose, profile="-", provider="deterministic", model="-", status="skipped",
                         attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=None,
                         error=reason[:200], tokens_saved=estimated_tokens, answered_by=rung, cost_source="none")

    def _cost(self, model: str, usage: dict[str, Any], input_tokens: int, output_tokens: int) -> tuple[float, str]:
        """Provider-reported cost first, else the versioned price table (cached prompt tokens at the
        provider's cache-read discount); a model with neither is recorded as `missing_price`
        (surfaced in token savings), never silently as a known $0."""
        reported = usage.get("cost")
        if reported is not None:
            return float(reported), "provider"
        priced = self.config.estimate_cost(model, input_tokens, output_tokens, cached_prompt_tokens(usage))
        if priced is not None:
            return priced, f"price_table@{self.config.prices_version}"
        log.error("no price for model %s in price table %s and the provider reported no cost; cost recorded as missing_price",
                  model, self.config.prices_version)
        return 0.0, "missing_price"

    # ------------------------------------------------------------------ routing
    def candidates(self, purpose: str, ctx: CallContext, *, profile: str | None = None) -> tuple[str, ProfileConfig, list[str]]:
        """The purpose's first tier: the small models (cheap first), or under `always_large` the large tier
        followed by the small one. `profile` names the profile of a ladder rung other than the purpose's
        routed one (the `decision` rung of a chat purpose)."""
        llm = self.settings
        override = llm.routing_overrides.get(purpose)
        if profile is not None:
            if profile not in self.config.profiles:
                raise KeyError(f"No model profile '{profile}'")
            profile_name, profile_cfg = profile, self.config.profiles[profile]
            return self._resolve(profile_name, profile_cfg, ctx)
        if override and override in self.config.profiles:
            profile_name, profile = override, self.config.profiles[override]
        else:
            profile_name, profile = self.config.profile_for(purpose)
        profile_name, profile, models = self._resolve(profile_name, profile, ctx)
        policy = self.escalation_policy(purpose)
        if policy == "always_large" or (not models and policy != "never"):
            # always_large: Sonnet first. A workspace allowlist that admits only large models: they are the tier.
            large = self._large_tier(profile_name, profile, ctx)
            if large:
                models = large + [m for m in models if m not in large]
        return profile_name, profile, models

    def escalation_policy(self, purpose: str) -> str:
        """never | on_validation_failure | always_large: admin `llm.escalation`, else models.yaml."""
        return self.settings.escalation.get(purpose) or self.config.escalation_for(purpose)

    def escalation_tier(self, purpose: str, ctx: CallContext, profile_name: str, profile: ProfileConfig,
                        first: list[str]) -> list[str]:
        """The large models a validation failure of the first tier may escalate to ([] = none)."""
        if self.escalation_policy(purpose) != "on_validation_failure":
            return []
        return [m for m in self._large_tier(profile_name, profile, ctx) if m not in first]

    def _large_tier(self, profile_name: str, profile: ProfileConfig, ctx: CallContext) -> list[str]:
        models = self.settings.profile_escalation_models.get(profile_name) or profile.escalation_models
        return self._allowed(list(models), profile, ctx)

    def _resolve(self, profile_name: str, profile: ProfileConfig, ctx: CallContext) -> tuple[str, ProfileConfig, list[str]]:
        """Admin profile models, then platform allowlist - disabled models, workspace allowlist, family exclusions."""
        llm = self.settings
        if llm.profile_models.get(profile_name):
            profile = profile.model_copy(update={"models": list(llm.profile_models[profile_name])})
        return profile_name, profile, self._allowed(profile.models, profile, ctx)

    def _allowed(self, candidates: list[str], profile: ProfileConfig, ctx: CallContext) -> list[str]:
        llm = self.settings
        allowed = set(self.config.allowlist) - set(llm.disabled_models)
        if ctx.allowed_models:
            allowed &= set(ctx.allowed_models)
        excluded = set(profile.exclude_families) | set(ctx.exclude_families)
        models = [m for m in candidates if m in allowed and family(m) not in excluded]
        if self._policy_block(profile, ctx):
            return []
        if ctx.data_residency:
            want = ctx.data_residency.strip().lower()
            models = [m for m in models if (self.config.region_of(m, profile.provider) or "").strip().lower() == want]
        return models

    def _policy_block(self, profile: ProfileConfig, ctx: CallContext) -> str | None:
        if self.air_gapped:
            cfg = self.config.providers.get(profile.provider)
            if cfg is None or cfg.egress != "internal":
                return f"provider '{profile.provider}' is not an internal endpoint (air-gapped install)"
        internal = (self.config.providers.get(profile.provider) or _NO_PROVIDER).egress == "internal"
        if ctx.allowed_providers is not None and profile.provider not in ctx.allowed_providers \
                and not (internal and "internal" in ctx.allowed_providers):
            return f"provider '{profile.provider}' is not in the workspace allowed_providers {ctx.allowed_providers}"
        return None

    def _no_route(self, purpose: str, profile_name: str, profile: ProfileConfig, ctx: CallContext) -> ModelRouteUnavailable:
        """Why a profile has no model, as its own error class: the workspace provider list (or the
        air-gapped install), the data-residency filter, or the model allowlists."""
        head = f"no allowed model for purpose '{purpose}' (profile {profile_name})"
        details = {"purpose": purpose, "profile": profile_name, "provider": profile.provider}
        blocked = self._policy_block(profile, ctx)
        if blocked is not None:
            return ModelPolicyBlocked(f"{head}: {blocked}", details={**details, "allowed_providers": ctx.allowed_providers})
        if ctx.data_residency:
            unfiltered = dataclasses.replace(ctx, data_residency=None)
            if self._resolve(profile_name, profile, unfiltered)[2]:
                return ModelResidencyBlocked(
                    f"{head}: no model of profile {profile_name} has a known region matching the workspace data_residency "
                    f"'{ctx.data_residency}' (unknown regions fail closed)", details={**details, "data_residency": ctx.data_residency})
        return ModelRouteUnavailable(f"{head}: every model is excluded by the platform or workspace model allowlist",
                                     details=details)

    def unavailable(self, purpose: str, ctx: CallContext | None = None) -> ModelRouteUnavailable | None:
        """Why `available()` is False, as the error `complete()` would raise (not raised): mode off,
        no route (policy, residency, allowlist), no API key in this process, or a 402 cooldown.
        None when the purpose is available."""
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            return LLMDisabled(f"model use for '{purpose}' is turned off by the administrator", details={"purpose": purpose})
        try:
            profile_name, profile, models = self.candidates(purpose, ctx)
        except KeyError:
            return ModelRouteUnavailable(f"no model profile is configured for purpose '{purpose}'", details={"purpose": purpose})
        if not models:
            return self._no_route(purpose, profile_name, profile, ctx)
        try:
            self._provider(profile)
        except ModelRouteUnavailable as exc:
            return exc
        if _cooling_down(profile.provider):
            return _quota_exhausted(profile.provider, f"provider '{profile.provider}' refused for credits (HTTP 402); "
                                                      f"deterministic path until the cooldown ends")
        return None

    def _within_cost(self, purpose: str, profile_name: str, profile: ProfileConfig, models: list[str], ctx: CallContext,
                     *, input_tokens: int, output_tokens: int, request_hash: str, request: dict,
                     required: bool = True) -> list[str]:
        """Drop models whose pre-call estimate exceeds the workspace approval threshold. When none
        remain the call needs an approval: raise ApprovalRequired instead of calling silently
        (`required=False`, for an optional escalation tier: return [] instead).
        Models without price metadata cannot be proven below the limit and require approval."""
        limit = ctx.expensive_model_approval_usd
        if limit is None:
            return models
        estimates = {m: self.config.estimate_cost(m, input_tokens, output_tokens) for m in models}
        within = [m for m in models if estimates[m] is not None and estimates[m] <= limit]
        if within or not required:
            return within
        priced = [m for m in models if estimates[m] is not None]
        cheapest = min(priced, key=lambda m: estimates[m]) if priced else models[0]
        if estimates[cheapest] is None:
            message = (f"'{purpose}' has no price estimate for {cheapest}; cannot verify the workspace "
                       f"approval threshold ${limit:.4f} (expensive_model_approval_usd)")
        else:
            message = (f"'{purpose}' is estimated at ${estimates[cheapest]:.4f} on {cheapest}, above the workspace "
                       f"approval threshold ${limit:.4f} (expensive_model_approval_usd)")
        self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=cheapest,
                         status="approval_required", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                         request_hash=request_hash, error=message[:500], request=request, answered_by="rules",
                         cost_source="none")
        raise ApprovalRequired(message, details={"purpose": purpose, "model": cheapest, "estimated_usd": estimates[cheapest],
                                                 "limit_usd": limit, "decision": "approval_required"})

    def available(self, purpose: str, ctx: CallContext | None = None, *, profile: str | None = None) -> bool:
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            return False
        try:
            _, profile, models = self.candidates(purpose, ctx, profile=profile)
        except KeyError:
            return False
        provider = self.config.providers.get(profile.provider)
        keyed = provider is not None and (provider.api_key_env is None or bool(self.api_key_lookup(provider.api_key_env)))
        return bool(models and keyed) and not _cooling_down(profile.provider)

    def _provider(self, profile: ProfileConfig) -> tuple[str, str]:
        """(base URL, key). A provider without `api_key_env` (a local endpoint, Bedrock's AWS chain)
        is called without a key; keys are only ever read from the environment."""
        provider = self.config.providers.get(profile.provider)
        if provider is None:
            raise ModelRouteUnavailable(f"provider '{profile.provider}' is not configured")
        if provider.api_key_env is None:
            return provider.url(), ""
        key = self.api_key_lookup(provider.api_key_env)
        if not key:
            raise ModelKeyMissing(f"no API key for provider '{profile.provider}' (set {provider.api_key_env})",
                                  details={"provider": profile.provider, "env": provider.api_key_env})
        return provider.url(), key

    def _chat(self, profile: ProfileConfig, base_url: str, key: str, payload: dict, timeout: float) -> dict:
        """OpenRouter keeps the transport's own chat path (replay, fakes); every other provider type
        goes through its adapter, which maps the payload and the answer (analystos.llm.providers)."""
        provider = self.config.providers[profile.provider]
        if provider.type == "openrouter":
            return self.transport.chat(base_url=base_url, api_key=key, payload=payload, timeout=timeout)
        return adapter_for(provider).chat(self.transport, provider, key or None, payload, timeout)

    # ------------------------------------------------------------------ spend reservations
    def _reserve(self, ctx: CallContext, purpose: str, profile_name: str, profile: ProfileConfig, model: str, *,
                 input_tokens: int, output_tokens: int, request_hash: str | None, request: dict | None) -> Any:
        """Reserve the call's estimated cost under the hard spend caps before it is sent (sinks without
        `reserve` - tests, replay - have no caps). A refusal is recorded as a refused call and raised."""
        reserve = getattr(self.sink, "reserve", None)
        if reserve is None:
            return None
        estimate = self.config.reservation_estimate(model, input_tokens, output_tokens)
        try:
            return reserve(ctx=ctx, purpose=purpose, model=model, estimate_usd=estimate)
        except BudgetExceeded as exc:
            self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=model,
                             status="refused", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                             request_hash=request_hash, error=exc.message[:500], tokens_saved=input_tokens,
                             request=request, answered_by="rules", cost_source="none")
            raise

    def _record(self, reservation: Any = None, escalation: tuple[str, str] | None = None, **kw: Any) -> None:
        """sink.record plus the optional fields (reservation to settle; escalated_from/escalation_reason)."""
        if reservation is not None:
            kw["reservation"] = reservation
        if escalation is not None:
            kw["escalated_from"], kw["escalation_reason"] = escalation
        self.sink.record(**kw)

    # ------------------------------------------------------------------ chat
    def complete(self, purpose: str, messages: list[dict[str, str]], *, ctx: CallContext | None = None,
                 json_output: bool = False, max_tokens: int | None = None,
                 validate: Callable[[ModelResponse], str | None] | None = None,
                 escalate: str | None = None, escalated_from: str | None = None) -> ModelResponse:
        """Chat completion, cheap first. `validate` is the caller's deterministic check of an answer
        (None = usable, else the reason it is not); an unparseable JSON answer always fails it. Under the
        purpose's `on_validation_failure` policy a failed first-tier answer is re-asked once of the large
        tier, recorded with escalated_from/escalation_reason. `escalate` asks the large tier directly
        because a check further downstream failed (e.g. SQL the gateway still rejects after repairs);
        EscalationUnavailable when the policy or profile has no large tier."""
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            raise LLMDisabled(f"model use for '{purpose}' is turned off by the administrator")
        profile_name, profile, models = self.candidates(purpose, ctx)
        escalation = self.escalation_tier(purpose, ctx, profile_name, profile, models)
        llm = self.settings
        tier_before = [*models, *escalation]
        if profile.provider != "typesafe" and profile_name != "low_cost" and not profile.exclude_families \
                and "low_cost" in self.config.profiles \
                and getattr(self.sink, "remaining_fraction", lambda _c: 1.0)(ctx) < llm.downgrade_below_budget_fraction:
            # Budget nearly spent: finish the run on the cheapest profile instead of failing it. Same
            # allowlists and exclusions as any call; never for independent-family verification, and no
            # escalation to the large tier.
            low_name, low, downgraded = self._resolve("low_cost", self.config.profiles["low_cost"], ctx)
            if downgraded:
                profile_name, profile, models, escalation = low_name, low, downgraded, []
        stage: tuple[str, str] | None = None
        if escalate is not None:
            if not escalation:
                raise EscalationUnavailable(
                    f"'{purpose}' cannot escalate after '{escalate[:120]}': escalation policy "
                    f"{self.escalation_policy(purpose)}, no further large-tier model for profile {profile_name}",
                    details={"purpose": purpose, "policy": self.escalation_policy(purpose)})
            models, escalation, stage = escalation, [], (escalated_from or "caller", escalate[:200])
        if not models:
            raise self._no_route(purpose, profile_name, profile, ctx)
        rung = self.config.model_rung(profile_name)
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_messages = [{"role": m["role"], "content": redact(m["content"]), **({"cache": True} if m.get("cache") else {})}
                         for m in messages]
        if json_output:
            safe_messages[0] = {**safe_messages[0], "content": safe_messages[0]["content"] + JSON_INSTRUCTION}
        request_hash = stable_hash({"purpose": purpose, "messages": safe_messages})
        request = {"kind": "chat", "purpose": purpose, "messages": safe_messages, "json_output": json_output,
                   "max_tokens": max_tokens, "temperature": profile.temperature}
        estimate = estimate_tokens("".join(m["content"] for m in safe_messages))
        if estimate > llm.max_prompt_tokens:
            self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model="-",
                             status="refused", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                             request_hash=request_hash, error=f"prompt ~{estimate} tokens > limit {llm.max_prompt_tokens}",
                             tokens_saved=estimate, request=request, answered_by="rules", cost_source="none")
            raise LLMDisabled(f"prompt for '{purpose}' is ~{estimate} tokens, above the admin limit {llm.max_prompt_tokens}",
                              details={"oversize": True, "prompt_tokens": estimate, "limit": llm.max_prompt_tokens})
        out_tokens = max_tokens or profile.max_tokens
        models = self._within_cost(purpose, profile_name, profile, models, ctx, input_tokens=estimate,
                                   output_tokens=out_tokens, request_hash=request_hash, request=request)
        if escalation:  # a large model above the approval threshold is simply not an escalation target
            escalation = self._within_cost(purpose, profile_name, profile, escalation, ctx, input_tokens=estimate,
                                           output_tokens=out_tokens, request_hash=request_hash, request=request, required=False)
        call = _Call(purpose=purpose, ctx=ctx, profile_name=profile_name, profile=profile, rung=rung, base_url=base_url,
                     key=key, safe_messages=safe_messages, request=request, request_hash=request_hash,
                     json_output=json_output, max_tokens=out_tokens, input_estimate=estimate, validate=validate)
        cache_payload = None
        if llm.cache_enabled and purpose in llm.cacheable_purposes:
            cache_payload = {"m": normalize_messages(safe_messages), "json": json_output, "max": max_tokens,
                             "t": profile.temperature}
            # Keyed on the model that answered: any allowed model's stored answer is a hit, so a budget
            # downgrade (another tier) or an escalation still finds what an allowed model already said.
            hit = None
            for candidate in dict.fromkeys([*models, *escalation, *tier_before]):
                hit = self.cache.get(ResponseCache.key(purpose, candidate, cache_payload, ctx.workspace_id,
                                                       knowledge_version=ctx.knowledge_version))
                if hit:
                    break
            if hit:
                cached = ModelResponse(text=hit["text"], data=hit.get("data"), model=hit["model"], provider=profile.provider,
                                       cached=True)
                why = _failed_validation(validate, cached)
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=hit["model"],
                                 status="cache_hit", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                                 request_hash=request_hash, error=f"cached answer failed validation: {why}"[:500] if why else None,
                                 tokens_saved=int(hit.get("input_tokens", 0)) + int(hit.get("output_tokens", 0)),
                                 request=request, response={"model": hit["model"], "text": redact(hit["text"]), "cached": True},
                                 answered_by="cache", cost_source="none")
                if why is None or not escalation:
                    cached.validation_error = why
                    return cached
                models, escalation, stage = escalation, [], (hit["model"], f"cached answer: {why}"[:200])
        if _cooling_down(profile.provider):
            raise _quota_exhausted(profile.provider, f"provider '{profile.provider}' refused for credits recently; "
                                                     f"deterministic path until the cooldown ends")
        outcome = self._run_tier(call, models, stage=stage, can_escalate=bool(escalation))
        if isinstance(outcome, _Rejected):
            log.info("escalating %s from %s to the large tier: %s", purpose, outcome.model, outcome.reason)
            try:
                outcome_large = self._run_tier(call, escalation, stage=(outcome.model, outcome.reason[:200]),
                                               can_escalate=False, attempts_before=outcome.attempts)
            except ModelRouteUnavailable:
                if outcome.response is None:
                    raise
                outcome.response.validation_error = outcome.reason
                return outcome.response  # the large tier is down: the caller validates and degrades
            outcome = outcome_large
        assert isinstance(outcome, ModelResponse)
        if cache_payload is not None and not outcome.validation_error:
            self.cache.set(ResponseCache.key(purpose, outcome.routed_model or outcome.model, cache_payload, ctx.workspace_id,
                                             knowledge_version=ctx.knowledge_version), {"text": outcome.text, "data": outcome.data, "model": outcome.model,
                                       "input_tokens": outcome.input_tokens, "output_tokens": outcome.output_tokens},
                           llm.cache_ttl_hours * 3600)
        return outcome

    def _run_tier(self, call: _Call, models: list[str], *, stage: tuple[str, str] | None, can_escalate: bool,
                  attempts_before: int = 0) -> ModelResponse | _Rejected:
        """Try `models` in order with bounded retries. A transient failure retries / falls back within the
        tier; a validation failure (unparseable JSON, `validate` says no) returns _Rejected when the caller
        may escalate, else retries as before (JSON) or returns the answer marked invalid (validate)."""
        c = call
        last_error: Exception | None = None
        attempt = attempts_before
        for model in models:
            for retry in range(self.max_retries + 1):
                attempt += 1
                wire = wire_messages(c.safe_messages, cache_control=self.config.prompt_cache(model))
                payload: dict[str, Any] = {"model": model, "messages": wire, "temperature": c.profile.temperature,
                                           "max_tokens": c.max_tokens, "usage": {"include": True}}
                if c.json_output:
                    payload["response_format"] = {"type": "json_object"}
                if c.profile.reasoning_effort and self.config.providers[c.profile.provider].type == "openrouter":
                    payload["reasoning"] = {"effort": c.profile.reasoning_effort}
                reservation = self._reserve(c.ctx, c.purpose, c.profile_name, c.profile, model, input_tokens=c.input_estimate,
                                            output_tokens=c.max_tokens, request_hash=c.request_hash, request=c.request)
                started = time.perf_counter()
                base = {"ctx": c.ctx, "purpose": c.purpose, "profile": c.profile_name, "provider": c.profile.provider,
                        "request_hash": c.request_hash, "request": c.request, "answered_by": c.rung}
                try:
                    body = self._chat(c.profile, c.base_url, c.key, payload, c.profile.timeout_seconds)
                except (UpstreamUnavailable, ModelRouteUnavailable, ValueError) as exc:
                    last_error = exc
                    self._record(reservation, stage, **base, model=model, status="error", attempt=attempt,
                                 latency_ms=round((time.perf_counter() - started) * 1000), input_tokens=0, output_tokens=0,
                                 cost_usd=0.0, error=str(exc)[:500], cost_source="none")
                    if isinstance(exc, ProviderQuotaExhausted):  # 402: no model behind this provider will work either
                        _PROVIDER_COOLDOWN[c.profile.provider] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                        note = getattr(self.sink, "note_cooldown", None)
                        if note is not None:
                            note(c.profile.provider, PROVIDER_COOLDOWN_SECONDS, str(exc)[:300])
                        log.warning("provider %s refused for credits; cooling down %ss", c.profile.provider,
                                    PROVIDER_COOLDOWN_SECONDS)
                        raise _quota_exhausted(c.profile.provider, exc.message) from exc
                    if isinstance(exc, ModelRouteUnavailable):  # 4xx: this model will not work; try the next model
                        break
                    log.warning("model call failed purpose=%s model=%s attempt=%s: %s", c.purpose, model, attempt, exc)
                    if retry < self.max_retries:
                        time.sleep(min(0.5 * 2**retry, 4))
                    continue
                latency = round((time.perf_counter() - started) * 1000)
                text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                usage = body.get("usage") or {}
                answered_model = str(body.get("model") or model)
                in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
                cost, cost_source = self._cost(answered_model if answered_model in self.config.models else model,
                                               usage, in_tok, out_tok)
                spent = {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost, "cost_source": cost_source,
                         "latency_ms": latency}
                data = None
                if c.json_output:
                    try:
                        data = parse_json_text(text)
                    except (json.JSONDecodeError, ValueError) as exc:
                        # Billed all the same: the tokens and cost of the unusable answer are recorded.
                        last_error = exc
                        self._record(reservation, stage, **base, **spent, model=answered_model, status="error",
                                     attempt=attempt, error=f"invalid_json: {exc}"[:500])
                        if can_escalate:
                            return _Rejected(model=answered_model, reason=f"invalid_json: {exc}"[:200], attempts=attempt)
                        log.warning("model answer unparseable purpose=%s model=%s attempt=%s: %s", c.purpose, model, attempt, exc)
                        if retry < self.max_retries:
                            time.sleep(min(0.5 * 2**retry, 4))
                        continue
                response = ModelResponse(text=text, data=data, model=answered_model, provider=c.profile.provider,
                                         input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost, latency_ms=latency,
                                         attempts=attempt, cached_input_tokens=cached_prompt_tokens(usage), routed_model=model,
                                         escalated_from=stage[0] if stage else None,
                                         escalation_reason=stage[1] if stage else None)
                why = _failed_validation(c.validate, response)
                recorded_error = _price_error(cost_source, response.model, self.config)
                if why is not None and can_escalate:
                    self._record(reservation, stage, **base, **spent, model=response.model, status="error", attempt=attempt,
                                 error=f"validation: {why}"[:500],
                                 response={"model": response.model, "text": redact(text), "usage": usage})
                    return _Rejected(model=response.model, reason=f"validation: {why}"[:200], attempts=attempt,
                                     response=response)
                response.validation_error = why
                self._record(reservation, stage, **base, **spent, model=response.model, status="ok", attempt=attempt,
                             error=recorded_error or (f"validation: {why}"[:500] if why else None),
                             response={"model": response.model, "text": redact(text), "usage": usage})
                return response
        if isinstance(last_error, (json.JSONDecodeError, ValueError)):
            raise ModelOutputInvalid(f"no model returned valid output for purpose '{c.purpose}': {last_error}",
                                     details={"purpose": c.purpose, "attempts": attempt})
        raise ModelRouteUnavailable(f"all models failed for purpose '{c.purpose}': {last_error}")

    def complete_json(self, purpose: str, system: str, user: str, *, ctx: CallContext | None = None,
                      max_tokens: int | None = None) -> ModelResponse:
        return self.complete(purpose, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                             ctx=ctx, json_output=True, max_tokens=max_tokens)

    # ------------------------------------------------------------------ decisions (JEV)
    def decide(self, purpose: str, state: dict[str, Any], questions: dict[str, Any], *,
               ctx: CallContext | None = None, profile: str | None = None) -> DecisionResponse:
        """Typed decision via TypeSafe Jev. Only trusted, redacted text goes into `state`:
        objective, registry/catalog descriptions, computed statistics — never raw result rows.
        `profile` = the decision profile of a chat purpose's `decision` rung (e.g. request_triage)."""
        ctx = ctx or CallContext()
        if self.mode(purpose) == "off":
            raise LLMDisabled(f"decision model for '{purpose}' is turned off by the administrator")
        profile_name, profile, models = self.candidates(purpose, ctx, profile=profile)
        if not models:
            raise self._no_route(purpose, profile_name, profile, ctx)
        base_url, key = self._provider(profile)
        self.sink.check_budget(ctx, purpose)
        safe_state = redact_obj({k: (v[:4000] if isinstance(v, str) else v) for k, v in state.items()})
        request_hash = stable_hash({"purpose": purpose, "state": safe_state, "questions": questions})
        request = {"kind": "decision", "purpose": purpose, "state": safe_state, "questions": questions}
        models = self._within_cost(purpose, profile_name, profile, models, ctx,
                                   input_tokens=estimate_tokens(json.dumps(request, default=str)),
                                   output_tokens=profile.max_tokens, request_hash=request_hash, request=request)
        llm = self.settings
        cache_key = None
        if llm.cache_enabled and purpose in llm.cacheable_purposes:
            cache_key = ResponseCache.key(purpose, models[0], {"s": safe_state, "q": questions}, ctx.workspace_id,
                                          knowledge_version=ctx.knowledge_version)
            hit = self.cache.get(cache_key)
            if hit:
                self.sink.record(ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=hit["model"],
                                 status="cache_hit", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                                 request_hash=request_hash, error=None,
                                 tokens_saved=int(hit.get("input_tokens", 0)) + int(hit.get("output_tokens", 0)),
                                 request=request, response={"model": hit["model"], "answers": redact_obj(hit["answers"]),
                                                            "cached": True}, answered_by="cache", cost_source="none")
                return DecisionResponse(answers=hit["answers"], model=hit["model"], cached=True)
        last_error: Exception | None = None
        in_estimate = estimate_tokens(json.dumps(request, default=str))
        for attempt in range(1, self.max_retries + 2):
            # typed answers are a few tokens; the profile's max_tokens would over-reserve by orders of magnitude
            reservation = self._reserve(ctx, purpose, profile_name, profile, models[0], input_tokens=in_estimate,
                                        output_tokens=min(profile.max_tokens, 256), request_hash=request_hash,
                                        request=request)
            started = time.perf_counter()
            try:
                body = self.transport.decide(base_url=base_url, api_key=key,
                                             payload={"model": models[0], "state": safe_state, "questions": questions},
                                             timeout=profile.timeout_seconds)
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    raise UpstreamUnavailable("decision response had no answers")
                usage = body.get("usage") or {}
                latency = round((time.perf_counter() - started) * 1000)
                in_tok, out_tok = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
                answered_model = str(body.get("model") or models[0])
                cost, cost_source = self._cost(answered_model if answered_model in self.config.models else models[0],
                                               usage, in_tok, out_tok)
                result = DecisionResponse(answers=answers, model=answered_model, cost_usd=cost, latency_ms=latency,
                                          input_tokens=in_tok, output_tokens=out_tok)
                self._record(reservation, ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=result.model,
                                 status="ok", attempt=attempt, latency_ms=latency, input_tokens=result.input_tokens,
                                 output_tokens=result.output_tokens, cost_usd=result.cost_usd, request_hash=request_hash,
                                 error=_price_error(cost_source, result.model, self.config),
                                 request=request, response={"model": result.model, "answers": redact_obj(answers), "usage": usage},
                                 answered_by="decision", cost_source=cost_source)
                if cache_key:
                    self.cache.set(cache_key, {"answers": answers, "model": result.model, "input_tokens": result.input_tokens,
                                               "output_tokens": result.output_tokens}, llm.cache_ttl_hours * 3600)
                return result
            except (UpstreamUnavailable, ModelRouteUnavailable) as exc:
                last_error = exc
                self._record(reservation, ctx=ctx, purpose=purpose, profile=profile_name, provider=profile.provider, model=models[0],
                                 status="error", attempt=attempt, latency_ms=round((time.perf_counter() - started) * 1000),
                                 input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=request_hash, error=str(exc)[:500],
                                 request=request, answered_by="decision", cost_source="none")
                if isinstance(exc, ModelRouteUnavailable):
                    break
                time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
        raise ModelRouteUnavailable(f"decision model failed for purpose '{purpose}': {last_error}")


def _air_gapped() -> bool:
    from analystos.core.config import get_settings

    return bool(get_settings().air_gapped)


def _default_settings():
    try:
        from analystos.services.platform_settings import get

        return get()
    except Exception:  # pragma: no cover - settings must never break model routing
        from analystos.contracts.platform import PlatformSettings

        return PlatformSettings()


__all__ = ["MODEL_RUNGS", "JSON_INSTRUCTION", "ApprovalRequired", "BudgetExceeded", "CallContext", "DecisionResponse", "ModelResponse",
           "ModelRouter", "cached_prompt_tokens", "ladder_for_mode", "mode_of", "normalize_messages", "parse_json_text", "wire_messages"]

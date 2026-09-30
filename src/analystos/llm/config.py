from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from analystos.core.config import get_settings

ProviderType = Literal["openrouter", "openai_compatible", "azure_openai", "anthropic", "bedrock"]
Egress = Literal["internet", "internal"]
# Provider types that are an outbound call to a public cloud endpoint. Azure OpenAI can be reached over a
# private endpoint, but only an explicit `private_link: true` acknowledges that and allows `egress: internal`.
_PUBLIC_ONLY = ("openrouter", "anthropic", "bedrock", "azure_openai")
_PRIVATE_LINK_TYPES = ("azure_openai",)


class ProviderConfig(BaseModel):
    """One model provider (spec v3 §8 "Model providers"). `type` selects the wire mapping in
    analystos.llm.providers. Keys never live here: `api_key_env` names the environment variable (a
    Kubernetes secret in the Helm chart); None means the endpoint takes no key (a local vLLM/Ollama).
    `egress: internal` declares the endpoint reachable without leaving the installation, the only
    kind an air-gapped install may call."""

    model_config = ConfigDict(extra="forbid")  # a literal `api_key:` in a models file fails loudly

    kind: str
    base_url: str
    api_key_env: str | None = None
    type: ProviderType = "openrouter"
    base_url_env: str | None = None  # when set and present in the environment, overrides base_url (Helm values)
    egress: Egress = "internet"
    api_version: str | None = None  # azure_openai: the `api-version` query parameter
    deployments: dict[str, str] = Field(default_factory=dict)  # azure_openai: allowlisted model -> deployment name
    model_map: dict[str, str] = Field(default_factory=dict)  # allowlisted model -> provider model id
    anthropic_version: str = "2023-06-01"  # anthropic: the `anthropic-version` header
    aws_region: str | None = None  # bedrock
    region: str | None = None  # where the provider processes requests; None = unknown (fails a residency policy)
    private_link: bool = False  # azure_openai: the endpoint is a private endpoint inside the network (allows internal)

    @model_validator(mode="after")
    def _check(self) -> ProviderConfig:
        if self.type in _PUBLIC_ONLY and self.egress == "internal" and not (
                self.private_link and self.type in _PRIVATE_LINK_TYPES):
            hint = " without `private_link: true`" if self.type in _PRIVATE_LINK_TYPES else ""
            raise ValueError(f"a provider of type {self.type} is a public endpoint and cannot be declared egress: internal{hint}")
        if self.private_link and self.type not in _PRIVATE_LINK_TYPES:
            raise ValueError(f"private_link applies to {', '.join(_PRIVATE_LINK_TYPES)} providers only")
        if self.type == "azure_openai" and not self.api_version:
            raise ValueError("azure_openai providers need api_version")
        return self

    def url(self) -> str:
        """Effective base URL: the `base_url_env` override (deployment-specific) else `base_url`."""
        override = (os.getenv(self.base_url_env) or "").strip() if self.base_url_env else ""
        return (override or self.base_url).rstrip("/")

    def host(self) -> str:
        return (urlsplit(self.url()).hostname or "").lower()

    def wire_model(self, model: str) -> str:
        """The id the provider knows the model by: explicit map, else the full id for OpenRouter and
        the part after the family prefix elsewhere (`anthropic/claude-sonnet-5` -> `claude-sonnet-5`)."""
        if model in self.model_map:
            return self.model_map[model]
        return model if self.type == "openrouter" else model.split("/", 1)[-1]


class ModelMeta(BaseModel):
    """Optional per-model metadata. Prices (versioned by ModelsConfig.prices_version) serve the
    pre-call approval estimate and the recorded cost when the provider reports none."""

    region: str | None = None  # overrides the provider region; None = inherit (unknown if both are None)
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    # Provider prompt caching needs explicit breakpoints (Anthropic `cache_control`, also through
    # OpenRouter). Families with automatic prefix caching (OpenAI, DeepSeek, Gemini) leave it false:
    # the cache-stable prompt order is enough for them.
    prompt_cache: bool = False
    # Share of the input price a provider waives for prompt tokens served from its cache. None = the
    # family default in `cached_discount` (not stated in models.yaml today).
    cached_input_discount: float | None = Field(None, ge=0.0, le=1.0)


# Documented provider discounts on cache reads: Anthropic and DeepSeek bill a read at ~10% of input,
# Gemini at ~25%, OpenAI-style automatic caching at 50% (some newer models more; 0.5 is the floor).
CACHED_INPUT_DISCOUNT = {"anthropic": 0.9, "deepseek": 0.9, "google": 0.75}
DEFAULT_CACHED_INPUT_DISCOUNT = 0.5


EscalationPolicy = Literal["never", "on_validation_failure", "always_large"]


class ProfileConfig(BaseModel):
    models: list[str]
    # The large tier (cheap first, escalate): called only when an answer of `models` fails deterministic
    # validation, or first under `always_large`. Empty = the profile never escalates.
    escalation_models: list[str] = Field(default_factory=list)
    provider: str = "openrouter"
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout_seconds: float = 60
    exclude_families: list[str] = Field(default_factory=list)
    # OpenRouter `reasoning.effort` for this profile's calls. Every purpose here returns strict JSON that code
    # then validates, and a reasoning model spends the completion budget on hidden reasoning first: live runs
    # (2026-09-29) got empty content from deepseek-v4-flash at max_tokens 2500 and truncated JSON from
    # gemini-3.5-flash at 6000. None = the provider default.
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None


class EscalationConfig(BaseModel):
    default: EscalationPolicy = "on_validation_failure"
    purposes: dict[str, EscalationPolicy] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    allowlist: list[str]
    profiles: dict[str, ProfileConfig]
    routing: dict[str, str]
    models: dict[str, ModelMeta] = Field(default_factory=dict)
    prices_version: str = "unversioned"
    ladders: dict[str, list[str]] = Field(default_factory=dict)  # purpose -> default rungs (spec v3 §4.1)
    decisions: dict[str, Any] = Field(default_factory=dict)  # DecisionService purposes (analystos.decisions.config)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)

    def egress_hosts(self, *, internal_only: bool = False) -> set[str]:
        """Hosts the model transport may reach: every configured provider's, or only the internal ones."""
        return {p.host() for p in self.providers.values() if p.host() and (not internal_only or p.egress == "internal")}

    def region_of(self, model: str, provider: str) -> str | None:
        meta = self.models.get(model)
        if meta and meta.region:
            return meta.region
        cfg = self.providers.get(provider)
        return cfg.region if cfg else None

    def prompt_cache(self, model: str) -> bool:
        meta = self.models.get(model)
        return bool(meta and meta.prompt_cache)

    def cached_discount(self, model: str) -> float:
        """Share of the input price waived for a cached prompt token (per model, else per family)."""
        meta = self.models.get(model)
        if meta is not None and meta.cached_input_discount is not None:
            return meta.cached_input_discount
        return CACHED_INPUT_DISCOUNT.get(family(model), DEFAULT_CACHED_INPUT_DISCOUNT)

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float | None:
        """Upper-bound USD estimate for one call, or None when the model has no price metadata.
        Cached prompt tokens (part of `input_tokens`) are charged at the provider's cache-read discount."""
        meta = self.models.get(model)
        if meta is None or meta.input_usd_per_mtok is None or meta.output_usd_per_mtok is None:
            return None
        cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens or 0)))
        billed_input = input_tokens - cached * self.cached_discount(model)
        return (billed_input * meta.input_usd_per_mtok + output_tokens * meta.output_usd_per_mtok) / 1_000_000

    def cached_saving(self, model: str, cached_input_tokens: int) -> float | None:
        """Estimated USD the provider's prompt cache saved on `cached_input_tokens` (None: no price)."""
        meta = self.models.get(model)
        if meta is None or meta.input_usd_per_mtok is None:
            return None
        return cached_input_tokens * meta.input_usd_per_mtok * self.cached_discount(model) / 1_000_000

    def reservation_estimate(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """What a hard spend cap reserves before a call: the price-table estimate, or for a model
        with no price the estimate at the most expensive priced rates (never $0: unknown is not free)."""
        priced = self.estimate_cost(model, input_tokens, output_tokens)
        if priced is not None:
            return priced
        rates = [(m.input_usd_per_mtok, m.output_usd_per_mtok) for m in self.models.values()
                 if m.input_usd_per_mtok is not None and m.output_usd_per_mtok is not None]
        worst_in = max((r[0] for r in rates), default=15.0)
        worst_out = max((r[1] for r in rates), default=75.0)
        return (input_tokens * worst_in + output_tokens * worst_out) / 1_000_000

    def escalation_for(self, purpose: str) -> str:
        return self.escalation.purposes.get(purpose, self.escalation.default)

    def model_rung(self, profile_name: str) -> str:
        """The ladder rung a profile answers from: low_cost = L4 small, decision model = L3, else L5."""
        profile = self.profiles.get(profile_name)
        if profile is not None and self.providers.get(profile.provider) is not None \
                and self.providers[profile.provider].kind == "decision":
            return "decision"
        return "llm_small" if profile_name == "low_cost" else "llm_large"

    def default_ladder(self, purpose: str) -> list[str]:
        if purpose in self.ladders:
            return list(self.ladders[purpose])
        name = self.routing.get(purpose)
        return ["cache", self.model_rung(name) if name else "llm_large", "rules"]

    def profile_for(self, purpose: str) -> tuple[str, ProfileConfig]:
        name = self.routing.get(purpose)
        if not name or name not in self.profiles:
            raise KeyError(f"No model profile routed for purpose '{purpose}'")
        return name, self.profiles[name]

    def public_view(self) -> dict[str, Any]:
        return {
            "allowlist": self.allowlist,
            "profiles": {k: v.model_dump() for k, v in self.profiles.items()},
            "routing": self.routing,
            "providers": {k: {"kind": v.kind, "type": v.type, "base_url": v.url(), "egress": v.egress, "region": v.region}
                          for k, v in self.providers.items()},
            "models": {k: v.model_dump() for k, v in self.models.items()},
            "prices_version": self.prices_version,
            "ladders": self.ladders,
            "escalation": self.escalation.model_dump(),
        }


def family(model: str) -> str:
    return model.split("/", 1)[0]


def _read(path: Path) -> dict[str, Any]:
    """A models file may `extends: <file>` (relative to itself): its top-level sections replace the
    base's wholesale, so an air-gapped or Azure variant restates only providers, models and profiles."""
    data = yaml.safe_load(path.read_text()) or {}
    base = data.pop("extends", None)
    if base:
        return {**_read((path.parent / base).resolve()), **data}
    return data


@lru_cache
def load_models_config(path: Path | None = None) -> ModelsConfig:
    return ModelsConfig.model_validate(_read(path or get_settings().models_config))

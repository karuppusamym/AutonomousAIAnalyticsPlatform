"""Layered model configuration (spec S05 §5, ADR-005): file ⊕ platform ⊕ tenant ⊕ workspace.

The file (`config/models.yaml`) and the platform settings are the platform administrator's layers and
define what exists: providers, profiles, the allowlist, prices. A tenant override and then a workspace
override may only **narrow** what the layer above allows:

* allowlist — intersection (a model the layer above does not allow is a violation, never an addition);
* residency — the set of allowed regions shrinks (a region outside the layer above is a violation);
  a model whose region is unknown, or outside the set, is excluded (AT-15);
* budgets — each limit takes the minimum (raising one is a violation);
* purposes — a mode or ladder may drop model rungs or put deterministic rungs first, never add a model
  rung or move a model rung ahead; a profile may be chosen among the file's profiles of the same kind
  (a chat purpose can never be moved onto the decision model, nor off it);
* profile_models — a profile's models may be reordered or dropped, never added.

Resolution is a pure function: `resolve()` returns the effective configuration with the layer each value
comes from, which models were excluded and why, and every attempted widening (`violations`). Writes
refuse an override with violations; at read time a violation simply has no effect (a platform change
made after a tenant saved its override can never be widened by that override).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from analystos.contracts.platform import MODEL_RUNGS, Rung
from analystos.llm.config import ModelsConfig, family
from analystos.llm.router import ladder_for_mode, mode_of, platform_ladder

LayerName = Literal["file", "platform", "tenant", "workspace"]
OverrideMode = Literal["off", "auto", "always", "rules_only", "model_first"]
MODE_ALIASES = {"rules_only": "off", "model_first": "always"}
MODE_RANK = {"off": 0, "auto": 1, "always": 2}  # how early / whether a model answers
BUDGET_KEYS = ("per_run_usd", "per_day_usd", "per_user_day_usd")
_WIDENING_KEYS = {"providers": "providers are declared in the models file by a platform administrator",
                  "profiles": "profiles are declared in the models file; use profile_models to narrow one"}


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_run_usd: float | None = Field(None, ge=0.0, le=100_000.0)
    per_day_usd: float | None = Field(None, ge=0.0, le=100_000.0)
    per_user_day_usd: float | None = Field(None, ge=0.0, le=100_000.0)


class PurposeOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: OverrideMode | None = None
    ladder: list[Rung] | None = None
    profile: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> PurposeOverride:
        if self.mode is not None and self.ladder is not None:
            raise ValueError("set mode or ladder for a purpose, not both")
        if self.ladder is not None and (not self.ladder or len(set(self.ladder)) != len(self.ladder)):
            raise ValueError("a ladder must list each rung once and not be empty")
        return self


class ModelOverride(BaseModel):
    """A tenant or workspace override document. Everything is optional; an empty document narrows nothing."""

    model_config = ConfigDict(extra="forbid")

    allowlist: list[str] | None = None
    residency: list[str] | None = None  # allowed regions; a single string is one region
    budgets: Budgets = Field(default_factory=Budgets)
    purposes: dict[str, PurposeOverride] = Field(default_factory=dict)
    profile_models: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and set(data) == {"models"} and isinstance(data["models"], dict):
            data = data["models"]  # the spec's `models:` wrapper
        if isinstance(data, dict):
            for key, why in _WIDENING_KEYS.items():
                if key in data:
                    raise ValueError(f"'{key}' cannot be set in a tenant or workspace override: {why}; "
                                     "an override can only narrow (allowlist, residency, budgets, purposes, profile_models)")
        return data

    @field_validator("residency", mode="before")
    @classmethod
    def _regions(cls, v: Any) -> Any:
        if v is None:
            return None
        items = [v] if isinstance(v, str) else list(v)
        return list(dict.fromkeys(str(r).strip().lower() for r in items if str(r).strip()))

    @field_validator("allowlist")
    @classmethod
    def _dedupe(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else list(dict.fromkeys(m.strip() for m in v if m.strip()))


@dataclass
class EffectiveModels:
    """The resolved configuration of one scope. The router reads only what a tenant/workspace layer set
    (`layer_allowlist`, `regions`, `ladders`, `routing`, `profile_models`) and applies it on top of the
    file and platform layers it reads itself, so a cached copy never freezes a platform change."""

    allowlist: list[str]
    regions: list[str] | None
    budgets: dict[str, float | None]
    purposes: dict[str, dict[str, Any]]
    ladders: dict[str, list[str]] = field(default_factory=dict)
    routing: dict[str, str] = field(default_factory=dict)
    profile_models: dict[str, list[str]] = field(default_factory=dict)
    layer_allowlist: list[str] | None = None  # intersection of the tenant/workspace allowlists; None = not narrowed
    sources: dict[str, str] = field(default_factory=dict)
    excluded: dict[str, dict[str, str]] = field(default_factory=dict)
    violations: list[dict[str, str]] = field(default_factory=list)
    layers: list[dict[str, Any]] = field(default_factory=list)
    blocked_families: list[str] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {"allowlist": self.allowlist, "blocked_families": self.blocked_families, "residency": self.regions,
                "budgets": self.budgets,
                "purposes": self.purposes, "profile_models": self.profile_models, "sources": self.sources,
                "excluded_models": self.excluded, "violations": self.violations, "layers": self.layers}


def normalize_mode(mode: str) -> str:
    return MODE_ALIASES.get(mode, mode)


def narrow_ladder(upper: list[str], lower: list[str]) -> tuple[list[str], str | None]:
    """`lower` limited to what `upper` allows: no model rung `upper` lacks, and a model rung no earlier
    than `upper` puts it (mode rank). Deterministic rungs are always allowed. Returns (ladder, violation)."""
    problems = []
    added = [r for r in lower if r in MODEL_RUNGS and r not in upper]
    out = [r for r in lower if r not in MODEL_RUNGS or r in upper]
    if added:
        problems.append(f"adds model rung(s) {', '.join(added)}")
    if MODE_RANK[mode_of(out)] > MODE_RANK[mode_of(upper)]:
        problems.append(f"puts the model first ({mode_of(out)}) where the layer above is {mode_of(upper)}")
        out = ladder_for_mode(out, mode_of(upper))
    return out, ("; ".join(problems) or None)


def _is_decision(cfg: ModelsConfig, profile: str) -> bool:
    return cfg.model_rung(profile) == "decision"


def resolve(cfg: ModelsConfig, platform: Any, layers: list[tuple[LayerName, int, ModelOverride | None]]) -> EffectiveModels:
    """Resolve file ⊕ platform ⊕ each (layer, version, override) in order (tenant, then workspace)."""
    llm = platform.llm
    sources: dict[str, str] = {}
    excluded: dict[str, dict[str, str]] = {}
    violations: list[dict[str, str]] = []

    disabled = set(llm.disabled_models)
    blocked = set(cfg.blocked_families)
    allowlist = [m for m in cfg.allowlist if m not in disabled and family(m) not in blocked]
    for m in cfg.allowlist:
        if family(m) in blocked:
            excluded[m] = {"layer": "file", "reason": f"family {family(m)} is blocked on this platform"}
        elif m in disabled:
            excluded[m] = {"layer": "platform", "reason": "disabled by the platform administrator"}
    sources["allowlist"] = "platform" if disabled & set(cfg.allowlist) else "file"
    regions: list[str] | None = None
    sources["residency"] = "file"
    budgets: dict[str, float | None] = {"per_run_usd": None, "per_day_usd": llm.daily_spend_cap_usd, "per_user_day_usd": None}
    for k in BUDGET_KEYS:
        sources[f"budgets.{k}"] = "platform" if k == "per_day_usd" else "file"

    ladders_base: dict[str, list[str]] = {}
    routing: dict[str, str] = {}
    for purpose in cfg.routing:
        ladders_base[purpose] = platform_ladder(cfg, platform, purpose)
        sources[f"purposes.{purpose}.ladder"] = "platform" if (purpose in llm.ladders or purpose in llm.purpose_modes) else "file"
        override = llm.routing_overrides.get(purpose)
        routing[purpose] = override if override in cfg.profiles else cfg.routing[purpose]
        sources[f"purposes.{purpose}.profile"] = "platform" if override in cfg.profiles else "file"
    profile_models = {name: list(llm.profile_models.get(name) or p.models) for name, p in cfg.profiles.items()}
    for name in cfg.profiles:
        sources[f"profile_models.{name}"] = "platform" if llm.profile_models.get(name) else "file"

    ladders = dict(ladders_base)
    layer_ladders: dict[str, list[str]] = {}
    layer_routing: dict[str, str] = {}
    layer_profiles: dict[str, list[str]] = {}
    layer_allowlist: list[str] | None = None
    applied: list[dict[str, Any]] = [{"layer": "file", "version": cfg.prices_version}, {"layer": "platform"}]

    def violate(layer: str, path: str, message: str) -> None:
        violations.append({"layer": layer, "path": path, "message": message})

    for layer, version, doc in layers:
        applied.append({"layer": layer, "version": version, "present": doc is not None})
        if doc is None:
            continue
        if doc.allowlist is not None:
            wider = [m for m in doc.allowlist if m not in allowlist]
            if wider:
                violate(layer, "allowlist", f"not allowed by the layer above: {', '.join(wider)}")
            keep = set(doc.allowlist)
            for m in allowlist:
                if m not in keep:
                    excluded[m] = {"layer": layer, "reason": f"not on the {layer} allowlist"}
            allowlist = [m for m in allowlist if m in keep]
            layer_allowlist = [m for m in (layer_allowlist if layer_allowlist is not None else doc.allowlist) if m in keep]
            sources["allowlist"] = layer
        if doc.residency is not None:
            if regions is not None:
                wider = [r for r in doc.residency if r not in regions]
                if wider:
                    violate(layer, "residency", f"region(s) outside the layer above ({', '.join(regions) or 'none'}): "
                                                f"{', '.join(wider)}")
                regions = [r for r in regions if r in doc.residency]
            else:
                regions = list(doc.residency)
            sources["residency"] = layer
        for k in BUDGET_KEYS:
            value = getattr(doc.budgets, k)
            if value is None:
                continue
            current = budgets[k]
            if current is not None and value > current:
                violate(layer, f"budgets.{k}", f"{value} raises the layer above's limit {current}")
                continue
            budgets[k] = value
            sources[f"budgets.{k}"] = layer
        for purpose, po in doc.purposes.items():
            if purpose not in cfg.routing:
                violate(layer, f"purposes.{purpose}", "unknown model purpose")
                continue
            if po.mode is not None or po.ladder is not None:
                wanted = list(po.ladder) if po.ladder is not None else \
                    ladder_for_mode(ladders[purpose], normalize_mode(po.mode or "auto"))
                narrowed, problem = narrow_ladder(ladders[purpose], wanted)
                if problem:
                    violate(layer, f"purposes.{purpose}.{'ladder' if po.ladder is not None else 'mode'}", problem)
                ladders[purpose] = layer_ladders[purpose] = narrowed
                sources[f"purposes.{purpose}.ladder"] = layer
            if po.profile is not None:
                if po.profile not in cfg.profiles:
                    violate(layer, f"purposes.{purpose}.profile", f"unknown profile {po.profile}")
                elif _is_decision(cfg, po.profile) != _is_decision(cfg, cfg.routing[purpose]):
                    # JEV may rank or escalate among valid options (ADR-005); configuration can never wire a
                    # purpose onto the decision model or take a decision purpose off it.
                    violate(layer, f"purposes.{purpose}.profile",
                            f"profile {po.profile} is a different kind ({cfg.model_rung(po.profile)}) than the purpose's "
                            f"({cfg.model_rung(cfg.routing[purpose])})")
                else:
                    routing[purpose] = layer_routing[purpose] = po.profile
                    sources[f"purposes.{purpose}.profile"] = layer
        for name, models in doc.profile_models.items():
            if name not in cfg.profiles:
                violate(layer, f"profile_models.{name}", "unknown profile")
                continue
            wider = [m for m in models if m not in profile_models[name]]
            if wider:
                violate(layer, f"profile_models.{name}", f"not models of the profile in the layer above: {', '.join(wider)}")
            profile_models[name] = layer_profiles[name] = [m for m in models if m in profile_models[name]]
            sources[f"profile_models.{name}"] = layer

    if regions is not None:
        for name, models in profile_models.items():
            provider = cfg.profiles[name].provider
            for m in models:
                region = (cfg.region_of(m, provider) or "").strip().lower()
                if m in allowlist and region not in regions and m not in excluded:
                    excluded[m] = {"layer": sources["residency"],
                                   "reason": f"region {region or 'unknown'} is outside the residency {', '.join(regions) or 'none'}"}

    allowed = set(allowlist)
    purposes: dict[str, dict[str, Any]] = {}
    for purpose in cfg.routing:
        name = routing[purpose]
        provider = cfg.profiles[name].provider
        models = [m for m in profile_models[name] if m in allowed and (
            regions is None or (cfg.region_of(m, provider) or "").strip().lower() in regions)]
        ladder = ladders[purpose]
        mode = mode_of(ladder)
        entry: dict[str, Any] = {"ladder": ladder, "mode": mode, "profile": name, "provider": provider, "models": models,
                                 "rules_fallback": "rules" in ladder}
        if mode != "off" and not models:
            # Fail closed (S05 §7.1): visible, and only a rules rung answers instead.
            by_residency = regions is not None and any(m in allowed for m in profile_models[name])
            entry["model_unavailable"] = {"purpose": purpose, "reason": "residency" if by_residency else "allowlist"}
        purposes[purpose] = entry
    return EffectiveModels(allowlist=allowlist, regions=regions, budgets=budgets, purposes=purposes, ladders=layer_ladders,
                           routing=layer_routing, profile_models=layer_profiles, layer_allowlist=layer_allowlist,
                           sources=sources, excluded=excluded,
                           violations=violations, layers=applied, blocked_families=sorted(blocked))


__all__ = ["BUDGET_KEYS", "Budgets", "EffectiveModels", "ModelOverride", "PurposeOverride", "narrow_ladder",
           "normalize_mode", "resolve"]

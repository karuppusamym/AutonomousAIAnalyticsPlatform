"""Visible rules fallback (N-14): why a purpose that wanted a model was answered by a deterministic rung.

A model gate that says "call the model" and a router with no allowed route used to end in the rules rung with
nothing in the run but a reason code. `fallback_for` turns that outcome into one structured record — purpose,
cause, the router's detail and the rung that answered — which the run says in plain words, emits as a
`model.fallback` event and the live benchmark reports count, so a tier that fell back is labelled as such.

Only route causes are fallbacks. `mode_off` is an administrator's choice (already counted as answered by rules),
and an invalid answer, an oversize context or an agent refusal are reported where they happen."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# cause -> what stopped the model, as the business view says it ({what} = the purpose in plain words)
CAUSES: dict[str, str] = {
    "models_disabled": "no model is enabled for {what}",
    "allowlist": "no allowed model is available for {what}",
    "policy_blocked": "the workspace does not allow the model provider for {what}",
    "residency_blocked": "no model in the required data region is available for {what}",
    "no_api_key": "no model provider key is configured for {what}",
    "provider_cooldown": "the model provider is out of credits for now, so {what} was not sent",
    "cap_reached": "the model spend cap is reached, so {what} was not sent",
    "budget_exceeded": "this investigation's model budget is used up, so {what} was not sent",
    "missing_price": "the model for {what} has no price, so its cost cannot be checked",
    "approval_required": "the model for {what} costs more than the workspace allows without approval",
    "upstream_unavailable": "the model provider did not answer for {what}",
}

# ModelOutcome / router error code -> cause (codes not listed are not route fallbacks)
_BY_CODE = {"no_route": "allowlist", "model_route_unavailable": "allowlist", "policy_blocked": "policy_blocked",
            "egress_blocked": "policy_blocked", "residency_blocked": "residency_blocked", "no_api_key": "no_api_key",
            "provider_cooldown": "provider_cooldown", "provider_quota_exhausted": "provider_cooldown",
            "cap_reached": "cap_reached", "spend_cap_reached": "cap_reached", "spend_counters_unavailable": "cap_reached",
            "budget_exceeded": "budget_exceeded", "approval_required": "approval_required",
            "upstream_unavailable": "upstream_unavailable"}


@dataclass(frozen=True)
class ModelFallback:
    purpose: str
    cause: str  # a key of CAUSES
    detail: str  # the router's own message (expert view)
    rung: str = "rules"
    code: str = ""  # the ModelOutcome / error code it came from

    @property
    def sentence(self) -> str:
        """The plain sentence the business view shows, e.g. 'Answered by rules: no model is enabled for
        hypothesis generation.'"""
        what = self.purpose.replace("_", " ")
        return f"Answered by {self.rung}: {CAUSES[self.cause].format(what=what)}."

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "message": self.sentence}


def cause_of(code: str, details: dict[str, Any] | None = None) -> str | None:
    """The fallback cause of a reason code and its details, or None when the code is not a route fallback."""
    details = details or {}
    cause = _BY_CODE.get(code)
    if cause == "allowlist" and details.get("cause") == "models_disabled":
        return "models_disabled"
    if cause == "approval_required" and details.get("missing_price"):
        return "missing_price"
    return cause


def fallback_for(purpose: str, outcome: Any, *, rung: str = "rules") -> ModelFallback | None:
    """The fallback record of `outcome` (a ModelOutcome or an AnalystOSError), or None when it is not one."""
    code = str(getattr(outcome, "code", None) or outcome or "")
    details = getattr(outcome, "details", None)
    cause = cause_of(code, details if isinstance(details, dict) else None)
    if cause is None:
        return None
    detail = str(getattr(outcome, "message", None) or code)[:500]
    return ModelFallback(purpose=purpose, cause=cause, detail=detail, rung=rung, code=code)


def summarize(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`model.fallback` event payloads folded per (purpose, cause), first seen first, with how often and in which
    steps: what a run detail and a benchmark report show."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for p in payloads:
        purpose, cause = str(p.get("purpose") or ""), str(p.get("cause") or "")
        if not purpose or cause not in CAUSES:
            continue
        row = out.get((purpose, cause))
        if row is None:
            fb = ModelFallback(purpose=purpose, cause=cause, detail=str(p.get("detail") or ""),
                               rung=str(p.get("rung") or "rules"), code=str(p.get("code") or ""))
            row = out[(purpose, cause)] = {**fb.to_dict(), "count": 0, "tasks": []}
        row["count"] += 1
        task = p.get("task")
        if task and task not in row["tasks"]:
            row["tasks"].append(task)
    return list(out.values())


__all__ = ["CAUSES", "ModelFallback", "cause_of", "fallback_for", "summarize"]

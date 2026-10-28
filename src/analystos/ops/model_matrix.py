"""The per-model evaluation matrix (spec S12 §3, FR-055): every model profile x each model it may route to,
scored on the golden suite; rows in `model_matrix_result`, served at /v2/admin/models/evaluations.

Which models are evaluated is the routing truth: a profile's models and escalation models that are on the
allowlist and whose family is not in `blocked_families` (config/models.yaml; Claude and Gemini are blocked).
A blocked model is never evaluated, and rows recorded before a family was blocked are left out of the matrix
(reported under `excluded`), so the matrix only ever offers models the router could use.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.core.ids import new_id
from analystos.db.models import ModelMatrixResult

# The thresholds a model must meet to be eligible (config/eval_gates.yaml tier `model_matrix` is authoritative).
GATE_TIER = "model_matrix"


@dataclass(frozen=True)
class Candidate:
    profile: str
    model: str
    provider: str
    tier: str  # small | escalation
    default: bool  # the first model of the profile: what the router answers with first


def candidates(config: Any = None) -> tuple[list[Candidate], list[dict[str, str]]]:
    """(models to evaluate, excluded models with the reason). Decision-provider profiles (typed answers, not
    text) and profiles with no model are skipped: the golden suite cannot score them."""
    from analystos.llm.config import family, load_models_config

    config = config or load_models_config()
    allowed = set(config.allowlist)
    blocked = set(config.blocked_families)
    out: list[Candidate] = []
    excluded: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, profile in sorted(config.profiles.items()):
        provider = config.providers.get(profile.provider)
        if provider is not None and provider.kind == "decision":
            continue
        default_taken = False
        for tier, models in (("small", profile.models), ("escalation", profile.escalation_models)):
            for model in models:
                if (name, model) in seen:
                    continue
                seen.add((name, model))
                if family(model) in blocked:
                    excluded.append({"profile": name, "model": model, "reason": "blocked_family"})
                elif model not in allowed:
                    excluded.append({"profile": name, "model": model, "reason": "not_allowlisted"})
                elif family(model) in set(profile.exclude_families):
                    excluded.append({"profile": name, "model": model, "reason": "profile_excludes_family"})
                else:
                    is_default = tier == "small" and not default_taken
                    default_taken = default_taken or is_default
                    out.append(Candidate(name, model, profile.provider, tier, default=is_default))
    return out, excluded


def record(session: Session, rows: list[dict[str, Any]], *, suite: str, mode: str, gates_version: int,
           gates_sha256: str | None, commit: str | None = None) -> str:
    """Store one matrix run; returns its batch id. A row for a blocked family is refused (never stored)."""
    from analystos.core.errors import InvalidInput
    from analystos.llm.config import family, load_models_config

    blocked = set(load_models_config().blocked_families)
    batch = new_id("meb")
    for r in rows:
        if family(r["model"]) in blocked:
            raise InvalidInput(f"model {r['model']} is in a blocked family; it is never evaluated")
        session.add(ModelMatrixResult(id=new_id("mev"), batch_id=batch, suite=suite, mode=mode, profile=r["profile"],
                                    model=r["model"], provider=r.get("provider", ""), status=r["status"],
                                    metrics=r.get("metrics") or {}, failures=r.get("failures") or [],
                                    gates_version=gates_version, gates_sha256=gates_sha256, commit=commit))
    session.flush()
    return batch


def matrix(session: Session, *, profile: str | None = None, model: str | None = None, mode: str | None = None,
           latest: bool = True, limit: int = 200) -> dict[str, Any]:
    """Evaluation rows (the latest per profile x model by default) with eligibility. Rows of blocked families
    are dropped and listed under `excluded`; candidates without any row are listed as `not_evaluated`."""
    from analystos.llm.config import family, load_models_config

    config = load_models_config()
    blocked = set(config.blocked_families)
    q = select(ModelMatrixResult)
    if profile:
        q = q.where(ModelMatrixResult.profile == profile)
    if model:
        q = q.where(ModelMatrixResult.model == model)
    if mode:
        q = q.where(ModelMatrixResult.mode == mode)
    if latest:
        newest = (select(ModelMatrixResult.profile, ModelMatrixResult.model, ModelMatrixResult.mode,
                         func.max(ModelMatrixResult.created_at).label("at"))
                  .group_by(ModelMatrixResult.profile, ModelMatrixResult.model, ModelMatrixResult.mode).subquery())
        q = q.join(newest, (newest.c.profile == ModelMatrixResult.profile) & (newest.c.model == ModelMatrixResult.model)
                   & (newest.c.mode == ModelMatrixResult.mode) & (newest.c.at == ModelMatrixResult.created_at))
    q = q.order_by(ModelMatrixResult.profile, ModelMatrixResult.model, ModelMatrixResult.created_at.desc())
    cands, excluded = candidates(config)
    defaults = {(c.profile, c.model) for c in cands if c.default}
    rows, dropped = [], []
    for e in session.scalars(q.limit(max(1, min(int(limit), 1000)))):
        if family(e.model) in blocked:
            dropped.append({"profile": e.profile, "model": e.model, "reason": "blocked_family", "evaluated_at": e.created_at})
            continue
        rows.append({"id": e.id, "batch_id": e.batch_id, "suite": e.suite, "mode": e.mode, "profile": e.profile,
                     "model": e.model, "provider": e.provider, "status": e.status, "eligible": e.status == "passed",
                     "default_route": (e.profile, e.model) in defaults, "metrics": e.metrics, "failures": e.failures,
                     "gates_version": e.gates_version, "gates_sha256": e.gates_sha256, "commit": e.commit,
                     "evaluated_at": e.created_at})
    evaluated = {(r["profile"], r["model"]) for r in rows}
    not_evaluated = [{"profile": c.profile, "model": c.model, "default_route": c.default}
                     for c in cands if (c.profile, c.model) not in evaluated and (not profile or c.profile == profile)
                     and (not model or c.model == model)]
    return {"evaluations": rows, "not_evaluated": not_evaluated, "excluded": excluded + dropped,
            "blocked_families": sorted(blocked), "gate_tier": GATE_TIER, "thresholds": _thresholds()}


def _thresholds() -> dict[str, Any]:
    try:
        import yaml

        from analystos.core.config import DATA_ROOT

        gates = yaml.safe_load((DATA_ROOT / "config" / "eval_gates.yaml").read_text(encoding="utf-8"))
        tier = gates["tiers"][GATE_TIER]
        return {"version": gates["version"], "metrics": tier["metrics"]}
    except Exception:  # noqa: BLE001 - a view, not a gate
        return {}

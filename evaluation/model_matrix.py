"""Per-model evaluation matrix (spec S12 §3, FR-055): each model profile x each model it may route to, over the
golden Ask suite (evaluation/ask.py), with the model pinned: the profile's other models are disabled for the run,
so every call the profile serves comes from the candidate (measured, `pinned_share`).

  fake  the oracle transport (CI, needs Postgres): proves the harness, the pinning and the blocked-family rule;
        the scores measure the platform path, not the model.
  live  the configured provider (nightly, OPENROUTER_API_KEY): the measurement eligibility is decided on.

Candidates come from analystos.ops.model_matrix.candidates: allowlisted models whose family is not blocked
(Claude and Gemini are blocked in config/models.yaml), so a blocked model is never called here; the gate also
counts model calls of blocked families in the run (`blocked_family_calls`, must be 0).

A row passes when its model meets the `model_matrix` tier's per-model thresholds (config/eval_gates.yaml); the
tier's gate metrics are the worst default-route model (the first model of each profile the suite exercises).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

PER_MODEL = ("execution_correctness", "answer_correctness", "metric_grounding", "confident_wrong")
GATE_TIER = "model_matrix"


def _bounds(tier: str = GATE_TIER) -> dict[str, Any]:
    from evaluation import gates as G

    return G.load().tiers[tier].metrics


def row_failures(metrics: dict[str, Any], tier: str = GATE_TIER) -> list[str]:
    out = []
    for name, bound in _bounds(tier).items():
        if name not in PER_MODEL:
            continue
        value = metrics.get(name)
        if not isinstance(value, int | float):
            out.append(f"{name} missing")
            continue
        if bound.min is not None and value < bound.min - 1e-12:
            out.append(f"{name} {value:g} < {bound.min:g}")
        if bound.max is not None and value > bound.max + 1e-12:
            out.append(f"{name} {value:g} > {bound.max:g}")
    return out


@contextmanager
def _live_disabled(models: list[str]) -> Iterator[None]:
    """Disable `models` for this process only (the stored platform settings are never written)."""
    from analystos.contracts.platform import PlatformSettings
    from analystos.services import platform_settings as ps

    original = ps.current

    def current() -> tuple[int, PlatformSettings]:
        version, settings = original()
        merged = settings.model_dump()
        merged["llm"]["disabled_models"] = sorted(set(merged["llm"].get("disabled_models") or []) | set(models))
        return version, PlatformSettings.model_validate(merged)

    ps.current = current
    ps.invalidate()
    try:
        yield
    finally:
        ps.current = original
        ps.invalidate()


def _usage(workspace_ids: list[str], profile: str, model: str) -> dict[str, Any]:
    from sqlalchemy import func, select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall
    from analystos.llm.config import family, load_models_config
    from analystos.tenancy import system_scope

    blocked = set(load_models_config().blocked_families)
    with system_scope(), session_scope() as s:
        rows = s.execute(select(ModelCall.profile, ModelCall.model, func.count())
                         .where(ModelCall.workspace_id.in_(workspace_ids), ModelCall.status.in_(("ok", "error")))
                         .group_by(ModelCall.profile, ModelCall.model)).all()
    in_profile = sum(n for p, _, n in rows if p == profile)
    pinned = sum(n for p, m, n in rows if p == profile and m == model)
    return {"profile_calls": in_profile, "pinned_share": round(pinned / in_profile, 6) if in_profile else None,
            "blocked_family_calls": sum(n for _, m, n in rows if family(m or "") in blocked),
            "by_model": {f"{p}/{m}": n for p, m, n in rows}}


def ask_runner(mode: str, *, limit: int = 6) -> Callable[[Any], dict[str, Any]]:
    """A runner scoring one candidate on the Ask golden suite with its profile pinned to it."""
    def go(c: Any) -> dict[str, Any]:
        from analystos.llm.config import load_models_config
        from evaluation import ask
        from evaluation.gates import ask_metrics

        profile = load_models_config().profiles[c.profile]
        others = sorted({m for m in [*profile.models, *profile.escalation_models] if m != c.model})
        if mode == "live":
            with _live_disabled(others):
                result = ask.run("live", limit=limit)
        else:
            result = ask.run("fake", limit=limit, disabled_models=others)
        if not result.outcomes:
            raise RuntimeError("; ".join(result.problems) or "the suite produced no outcomes")
        m = ask_metrics(result)
        usage = _usage([v["workspace_id"] for v in result.setup.values()], c.profile, c.model)
        return {"execution_correctness": m["execution_accuracy"] or 0.0, "answer_correctness": m["outcome_accuracy"] or 0.0,
                "metric_grounding": m["answered_precision"] if m["answered_precision"] is not None else 1.0,
                "confident_wrong": m["confident_wrong"], "restricted_leaks": m["restricted_leaks"],
                "questions": m["questions"], "model_calls": m["model_calls"], **usage}
    return go


def run(mode: str = "fake", *, runner: Callable[[Any], dict[str, Any]] | None = None, limit: int = 6,
        profiles: list[str] | None = None, record: bool = False, commit: str | None = None) -> dict[str, Any]:
    """Score every candidate; returns rows, excluded models and the tier's gate metrics."""
    from analystos.llm.config import family, load_models_config
    from analystos.ops.model_matrix import candidates
    from evaluation import gates as G

    config = load_models_config()
    blocked = set(config.blocked_families)
    cands, excluded = candidates(config)
    if profiles:
        cands = [c for c in cands if c.profile in profiles]
    runner = runner or ask_runner(mode, limit=limit)
    rows: list[dict[str, Any]] = []
    for c in cands:
        assert family(c.model) not in blocked  # candidates() never yields one; the gate below re-checks the calls
        try:
            metrics = runner(c)
        except Exception as exc:  # one model failing to run is that model's result, not the matrix's
            rows.append({"profile": c.profile, "model": c.model, "provider": c.provider, "default_route": c.default,
                         "status": "error", "metrics": {}, "failures": [f"{type(exc).__name__}: {exc}"[:500]]})
            continue
        exercised = metrics.get("profile_calls", 1) != 0
        failures = row_failures(metrics) if exercised else []
        status = "not_exercised" if not exercised else ("failed" if failures else "passed")
        rows.append({"profile": c.profile, "model": c.model, "provider": c.provider, "default_route": c.default,
                     "status": status, "metrics": metrics, "failures": failures})
    gated = [r for r in rows if r["default_route"] and r["status"] in ("passed", "failed")]
    pinned = [r["metrics"]["pinned_share"] for r in rows if r["metrics"].get("pinned_share") is not None]

    def worst(key: str, fn: Callable[..., float]) -> float:
        values = [r["metrics"][key] for r in gated if isinstance(r["metrics"].get(key), int | float)]
        return fn(values) if values else 0.0

    gate_metrics = {
        "models_evaluated": sum(r["status"] in ("passed", "failed", "not_exercised") for r in rows),
        "errors": sum(r["status"] == "error" for r in rows),
        "blocked_family_evaluated": sum(family(r["model"]) in blocked for r in rows),
        "blocked_family_calls": sum(int(r["metrics"].get("blocked_family_calls") or 0) for r in rows),
        "pinned_share_min": min(pinned) if pinned else 0.0,
        "execution_correctness": worst("execution_correctness", min),
        "answer_correctness": worst("answer_correctness", min),
        "metric_grounding": worst("metric_grounding", min),
        "confident_wrong": worst("confident_wrong", sum),
        "default_route_models": len(gated),
    }
    out = {"mode": mode, "rows": rows, "excluded": excluded, "blocked_families": sorted(blocked), "metrics": gate_metrics}
    if record:
        from analystos.db.base import session_scope
        from analystos.ops.model_matrix import record as store
        from analystos.tenancy import system_scope

        gates = G.load()
        with system_scope(), session_scope() as s:
            out["batch_id"] = store(s, rows, suite="ask", mode=mode, gates_version=gates.version, gates_sha256=gates.digest,
                                    commit=commit)
    return out

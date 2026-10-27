"""Model-switch evaluation gate (spec S05 §7.5, FR-055, task S05-T5).

A configuration save that changes a purpose's primary model (the first model the router would try) must not
quietly make that purpose worse. For each such purpose the gate scores the purpose's golden suite
(`config/model_switch_gate.yaml`) on the current and on the new model and blocks the save when the new model
scores more than `tolerance` below the current one, below `min_score`, or cannot be evaluated at all (no key,
no route, a decision purpose without a typed suite): fail closed. A platform administrator may override with a
reason; the override is audited and returned with the save.

Suites run through the router like any call (allowlists, blocked families, spend caps, redaction and the
model_call record apply), pinned to one model; grading is deterministic. Scores are stored in
`model_evaluation` and reused for `reuse_hours`, so the current model is normally not re-run. Tests inject a
`runner`; nothing here is billable unless an administrator saves a model change or asks for a run.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, Conflict, Forbidden, InvalidInput
from analystos.core.ids import new_id, utcnow
from analystos.db.models import ModelEvaluation, User

GATE_FILE = Path(__file__).resolve().parents[3] / "config" / "model_switch_gate.yaml"


class Case(BaseModel):
    system: str
    user: str
    json_output: bool = Field(True, alias="json")
    expect: dict[str, Any] = Field(default_factory=dict)


class Suite(BaseModel):
    cases: list[Case]


class GateConfig(BaseModel):
    version: int
    tolerance: float = Field(0.1, ge=0.0, le=1.0)
    min_score: float = Field(0.5, ge=0.0, le=1.0)
    reuse_hours: int = Field(168, ge=0)
    max_tokens: int = Field(600, ge=16, le=8000)
    default_suite: str
    purpose_suites: dict[str, str] = Field(default_factory=dict)
    suites: dict[str, Suite]

    def suite_for(self, purpose: str) -> str:
        return self.purpose_suites.get(purpose, self.default_suite)


@lru_cache
def load_gate(path: Path = GATE_FILE) -> GateConfig:
    return GateConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def grade(answer: str, expect: dict[str, Any]) -> list[str]:
    """Why an answer fails a case's checks (empty = pass)."""
    problems: list[str] = []
    text = answer or ""
    low = text.lower()
    if expect.get("json_keys"):
        from analystos.llm.router import parse_json_text

        try:
            data = parse_json_text(text)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            problems.append("not a JSON object")
        else:
            missing = [k for k in expect["json_keys"] if k not in data]
            if missing:
                problems.append(f"missing keys {missing}")
            text = json.dumps(data)
            low = text.lower()
    for needle in expect.get("contains") or []:
        if str(needle).lower() not in low:
            problems.append(f"does not contain {needle!r}")
    for needle in expect.get("not_contains") or []:
        if str(needle).lower() in low:
            problems.append(f"contains {needle!r}")
    if expect.get("regex") and not re.search(expect["regex"], text, re.IGNORECASE | re.DOTALL):
        problems.append("does not match the expected pattern")
    return problems


class SuiteRunner(Protocol):
    def __call__(self, *, purpose: str, profile: str, model: str, suite: str, cases: list[Case],
                 max_tokens: int) -> dict[str, Any]: ...


def router_runner(*, purpose: str, profile: str, model: str, suite: str, cases: list[Case], max_tokens: int) -> dict[str, Any]:
    """Run the cases through the router pinned to `model` (profile `profile`, model rung first, no escalation)."""
    from analystos.llm.config import load_models_config
    from analystos.llm.router import CallContext, ModelRouter
    from analystos.runtime.context import default_router
    from analystos.services.platform_settings import get as platform

    cfg = load_models_config()
    if cfg.model_rung(profile) == "decision":
        return {"status": "unavailable", "reason": "decision purposes have no typed golden suite yet; override with a reason"}
    base = default_router()
    settings = platform()
    llm = settings.llm.model_copy(update={
        "profile_models": {**settings.llm.profile_models, profile: [model]},
        "routing_overrides": {**settings.llm.routing_overrides, purpose: profile},
        "purpose_modes": {**settings.llm.purpose_modes, purpose: "always"},
        "ladders": {k: v for k, v in settings.llm.ladders.items() if k != purpose},
        "escalation": {**settings.llm.escalation, purpose: "never"},
        "cache_enabled": False})
    patched = settings.model_copy(update={"llm": llm})
    pinned = ModelRouter(cfg, sink=base.sink, transport=base.transport, api_key_lookup=base.api_key_lookup, max_retries=0,
                         settings_provider=lambda: patched, cache=base.cache, air_gapped=base.air_gapped)
    reason = pinned.unavailable(purpose)
    if reason is not None:
        return {"status": "unavailable", "reason": reason.message[:300], "code": reason.code}
    results, cost = [], 0.0
    for i, case in enumerate(cases):
        try:
            r = pinned.complete(purpose, [{"role": "system", "content": case.system}, {"role": "user", "content": case.user}],
                                ctx=CallContext(agent_id="admin.model_switch_gate", prompt_version=f"switch_gate.{suite}.{i}"),
                                json_output=case.json_output, max_tokens=max_tokens)
        except AnalystOSError as exc:
            results.append({"case": i, "passed": False, "problems": [f"{exc.code}: {exc.message[:160]}"]})
            continue
        cost += float(r.cost_usd or 0.0)
        problems = grade(r.text, case.expect)
        results.append({"case": i, "passed": not problems, "problems": problems})
    return {"status": "scored", "results": results, "cost_usd": cost}


def _latest(session: Session, suite: str, version: int, model: str, reuse: timedelta) -> ModelEvaluation | None:
    row = session.scalar(select(ModelEvaluation).where(
        ModelEvaluation.suite == suite, ModelEvaluation.suite_version == version, ModelEvaluation.model == model,
        ModelEvaluation.status == "scored").order_by(ModelEvaluation.created_at.desc()).limit(1))
    if row is None or reuse.total_seconds() <= 0:
        return None
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=utcnow().tzinfo)
    return row if utcnow() - created <= reuse else None


def _view(row: ModelEvaluation, *, reused: bool = False) -> dict[str, Any]:
    return {"id": row.id, "suite": row.suite, "suite_version": row.suite_version, "purpose": row.purpose,
            "profile": row.profile, "model": row.model, "status": row.status, "score": row.score, "cases": row.cases,
            "passed": row.passed, "cost_usd": row.cost_usd, "created_at": row.created_at.isoformat() if row.created_at else None,
            "reused": reused, **({"reason": row.details.get("reason")} if row.details.get("reason") else {})}


def evaluate(session: Session, *, purpose: str, profile: str, model: str, actor: str,
             runner: SuiteRunner | None = None, gate: GateConfig | None = None, reuse: bool = True) -> dict[str, Any]:
    """Score `model` on the purpose's suite (a stored score of the same suite version is reused)."""
    gate = gate or load_gate()
    suite = gate.suite_for(purpose)
    if suite not in gate.suites:
        raise InvalidInput(f"model switch gate: suite {suite} for {purpose} is not defined")
    if reuse:
        hit = _latest(session, suite, gate.version, model, timedelta(hours=gate.reuse_hours))
        if hit is not None:
            return _view(hit, reused=True)
    started = time.perf_counter()
    try:
        out = (runner or router_runner)(purpose=purpose, profile=profile, model=model, suite=suite,
                                        cases=gate.suites[suite].cases, max_tokens=gate.max_tokens)
    except AnalystOSError as exc:
        out = {"status": "unavailable", "reason": f"{exc.code}: {exc.message[:200]}"}
    results = out.get("results") or []
    passed = sum(1 for r in results if r.get("passed"))
    scored = out.get("status") == "scored" and results
    row = ModelEvaluation(id=new_id("mev"), suite=suite, suite_version=gate.version, purpose=purpose, profile=profile,
                          model=model, status="scored" if scored else "unavailable",
                          score=round(passed / len(results), 4) if scored else None, cases=len(results), passed=passed,
                          cost_usd=float(out.get("cost_usd") or 0.0), created_by=actor,
                          details={"results": results, "reason": out.get("reason"),
                                   "seconds": round(time.perf_counter() - started, 2)})
    session.add(row)
    session.flush()
    return _view(row)


def primary_models(effective: Any) -> dict[str, tuple[str, str | None]]:
    """purpose -> (profile, first model the layers allow, or None) from an EffectiveModels."""
    return {p: (v["profile"], (v["models"] or [None])[0]) for p, v in effective.purposes.items()
            if v.get("mode") != "off"}


def changed_primaries(before: Any, after: Any) -> list[dict[str, Any]]:
    """Purposes whose primary model changes to another model (losing every model is a narrowing: fail closed,
    nothing to evaluate)."""
    a, b = primary_models(before), primary_models(after)
    out = []
    for purpose, (profile, new) in b.items():
        old = a.get(purpose, (profile, None))[1]
        if new is not None and new != old:
            out.append({"purpose": purpose, "profile": profile, "from": old, "to": new})
    return out


def check(session: Session, user: User, changes: list[dict[str, Any]], *, override_reason: str | None = None,
          runner: SuiteRunner | None = None, gate: GateConfig | None = None,
          record: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Evaluate every changed purpose; raise Conflict (409) with the evaluations when one blocks and there is no
    platform-administrator override. Evaluations are written in their own transaction so a blocked save keeps them."""
    gate = gate or load_gate()
    if not changes:
        return {"gated": False, "evaluations": []}
    if override_reason is not None and not override_reason.strip():
        raise InvalidInput("a model switch gate override needs a reason")
    if override_reason and not user.is_admin:
        raise Forbidden("only a platform administrator can override the model switch gate")
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    evaluations = []
    with system_scope(), session_scope() as own:  # global table; survives the caller's rollback on a block
        for change in changes:
            new = evaluate(own, purpose=change["purpose"], profile=change["profile"], model=change["to"],
                           actor=user.id, runner=runner, gate=gate)
            old = evaluate(own, purpose=change["purpose"], profile=change["profile"], model=change["from"],
                           actor=user.id, runner=runner, gate=gate) if change["from"] else None
            verdict, why = "passed", None
            if new["status"] != "scored":
                verdict, why = "blocked", f"the new model could not be evaluated: {new.get('reason') or 'unavailable'}"
            elif new["score"] < gate.min_score:
                verdict, why = "blocked", f"score {new['score']:.2f} is below the minimum {gate.min_score:.2f}"
            elif old is not None and old["status"] == "scored" and new["score"] < old["score"] - gate.tolerance - 1e-9:
                verdict, why = "blocked", (f"score {new['score']:.2f} regresses more than {gate.tolerance:.2f} from "
                                           f"{old['score']:.2f} on {change['from']}")
            evaluations.append({**change, "verdict": verdict, "reason": why, "new": new, "current": old})
    blocked = [e for e in evaluations if e["verdict"] == "blocked"]
    result = {"gated": True, "suite_version": gate.version, "tolerance": gate.tolerance, "evaluations": evaluations,
              "blocked": bool(blocked), "overridden": bool(blocked and override_reason)}
    if blocked and not override_reason:
        raise Conflict("model switch blocked by the evaluation gate: "
                       + "; ".join(f"{e['purpose']} -> {e['to']}: {e['reason']}" for e in blocked),
                       details={"model_switch_gate": result})
    if blocked:
        from analystos.governance.audit import audit

        audit(f"user:{user.id}", "models.switch_gate.overridden", target=",".join(e["purpose"] for e in blocked),
              decision="allow", reasons=[override_reason or ""], details={"evaluations": blocked}, session=session)
        result["override_reason"] = override_reason
    if record is not None:
        record(result)
    return result


def history(session: Session, *, purpose: str | None = None, model: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    q = select(ModelEvaluation).order_by(ModelEvaluation.created_at.desc()).limit(min(limit, 500))
    if purpose:
        q = q.where(ModelEvaluation.purpose == purpose)
    if model:
        q = q.where(ModelEvaluation.model == model)
    return [_view(r) for r in session.scalars(q)]


__all__ = ["GateConfig", "changed_primaries", "check", "evaluate", "grade", "history", "load_gate", "primary_models",
           "router_runner"]

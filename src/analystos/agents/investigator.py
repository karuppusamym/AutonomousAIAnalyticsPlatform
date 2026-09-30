"""Investigation / Hypothesis Agent (§13.7, §19, §20).

LLM proposes; code disposes. Every proposal is parsed into an AnalysisSpec and validated against
the authorized scope (asset, columns, denied columns) and method/type compatibility. Invalid
proposals are dropped with a recorded reason. A deterministic, profile-driven generator fills in
when no model is available or too few proposals survive. JEV scores priority.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select

from analystos import methods
from analystos.agents.common import asset_rows, catalog_for_prompt, defer_compile, llm_json, model_gate, task_output
from analystos.artifacts.registry import link
from analystos.capabilities import packs as pack_registry
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.core.errors import InvalidInput
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Experiment, Hypothesis, by_code
from analystos.decisions import Question
from analystos.events.bus import emit
from analystos.runtime.context import RunContext
from analystos.runtime.engine import add_task
from analystos.services.platform_settings import get as platform
from analystos.skills import hypothesis_templates as tmpl


# ---------------------------------------------------------------------------------- validation
def _semantic(ctx_types: dict[str, dict[str, str]], asset: str, column: str) -> str | None:
    return ctx_types.get(asset, {}).get(column)


def _method_unavailable(method: str) -> str | None:
    """A method whose manifest needs an extra this installation lacks (e.g. `extra:ml`, ADR-0025)
    is refused here with the reason, before it could fail mid-analysis."""
    manifest = methods.manifests().get(method)
    if manifest is None:
        return None
    from analystos.core.profiles import requirement_reason

    reason = next((r for req in manifest.requires if (r := requirement_reason(req))), None)
    return f"method {method} is unavailable on this installation: {reason}" if reason else None


def validate_spec(spec: AnalysisSpec, scope, types: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    if spec.asset not in scope.assets:
        return [f"asset {spec.asset} is not in the authorized scope"]
    unavailable = _method_unavailable(spec.method)
    if unavailable:
        return [unavailable]
    cols = set(scope.columns.get(spec.asset, []))
    denied = set(scope.denied_columns)
    derivs = [d for d in (spec.outcome, spec.segment, spec.time, *spec.drivers) if d is not None]
    for d in derivs:
        for c in d.columns():
            if c not in cols:
                errors.append(f"unknown column {spec.asset}.{c}")
            elif f"{spec.asset}.{c}" in denied or f"*.{c}" in denied:
                errors.append(f"restricted column {c}")
    for f in spec.filters:
        if f.column not in cols:
            errors.append(f"unknown filter column {f.column}")
        elif f"{spec.asset}.{f.column}" in denied:
            errors.append(f"restricted filter column {f.column}")
    if errors:
        return errors

    def sem(d: Derivation) -> str | None:
        return _semantic(types, spec.asset, d.column)

    errors += methods.get(spec.method).validate(spec, sem)
    if spec.segment is not None and spec.segment.type == "column":
        st = sem(spec.segment)
        if st in ("numeric",) :
            errors.append(f"numeric segment {spec.segment.column} must be bucketed (type=bucket with edges)")
        if st in ("id", "text"):
            errors.append(f"segment {spec.segment.column} is an identifier/free text; use a categorical or *_name column")
        if st == "datetime":
            errors.append(f"datetime segment {spec.segment.column} needs a derivation (date_trunc/hour_of_day/after_hours)")
    if spec.segment is not None and spec.segment.type == "bucket" and not spec.segment.edges:
        errors.append("bucket segment needs edges")
    return errors


def semantic_types(ctx: RunContext) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for asset, cols in asset_rows(ctx):
        out[f"{asset.schema_name}.{asset.name}"] = {c.name: (c.semantic_type or (c.profile or {}).get("semantic_type")) for c in cols}
    return out


def with_constraints(spec: AnalysisSpec, constraints: dict[str, Any]) -> AnalysisSpec:
    """Apply user-redirect filters (persisted on the run) to a spec for its asset."""
    extra = [Filter(column=f["column"], op=f["op"], value=f.get("value"), origin="user_redirect")
             for f in constraints.get("filters", []) if f.get("asset") in (None, spec.asset)]
    if not extra:
        return spec
    existing = {(f.column, f.op, str(f.value)) for f in spec.filters}
    return spec.model_copy(update={"filters": spec.filters + [f for f in extra if (f.column, f.op, str(f.value)) not in existing]})


# ---------------------------------------------------------------------------------- heuristics
# Domain playbooks (which outcome, which drivers, which labels) are data in domain packs
# (packs/<name>/templates.yaml, skills/hypothesis_templates); only the domain-neutral, role-driven
# playbook below lives here.
_TEXTY = tmpl.TEXTY


def humanize(column: str) -> str:
    return tmpl.humanize(column, pack_registry.hints().acronyms)


def table_columns(asset, cols, types: dict[str, dict[str, str]], denied: list[str]) -> list[tmpl.Col]:
    """The engine's view of one selected table: visible columns with type, crawler role and profile."""
    fq = f"{asset.schema_name}.{asset.name}"
    deny = set(denied)
    return [tmpl.Col(name=c.name, semantic_type=types.get(fq, {}).get(c.name), role=(c.semantics or {}).get("semantic_role"),
                     profile=c.profile or {}) for c in cols if f"{fq}.{c.name}" not in deny]


def proposals_for_table(fq: str, cols: list[tmpl.Col], packs: list) -> list[dict[str, Any]]:
    """Templates of every enabled pack first (in pack order), then the core role-driven playbook."""
    acronyms = pack_registry.hints().acronyms
    proposals: list[dict[str, Any]] = []
    for pack in packs:
        proposals += [{**p, "pack": pack.id} for p in tmpl.propose(fq, cols, pack.templates, acronyms)]
    st = {c.name: c.semantic_type for c in cols}
    prof = {c.name: c.profile for c in cols}
    categorical = [c for c in st if st[c] == "categorical" and 2 <= (prof[c].get("distinct") or 0) <= 30 and not _TEXTY.search(c)]
    datetimes = [c for c in st if st[c] == "datetime"]
    covered = {(p["spec"].get("outcome") or {}).get("column") for p in proposals}
    has_trend = any(_method_attr(p["spec"], "playbook") == "volume_trend" for p in proposals)
    return proposals + _role_proposals(fq, cols, st, categorical, datetimes, has_trend=has_trend, covered=covered)


def heuristic_proposals(ctx: RunContext, types: dict[str, dict[str, str]], packs: list | None = None) -> list[dict[str, Any]]:
    """Rule-based playbook over the largest selected table first (the next one when it yields nothing)."""
    packs = pack_registry.for_scope(ctx.scope, ctx.policy) if packs is None else packs
    rows = sorted(asset_rows(ctx), key=lambda ac: -(ac[0].row_count or 0))
    for asset, cols in rows[:2]:
        fq = f"{asset.schema_name}.{asset.name}"
        proposals = proposals_for_table(fq, table_columns(asset, cols, types, ctx.scope.denied_columns), packs)
        if proposals:
            return proposals
    return []


_MEASURE_ROLES = ("amount", "measure", "duration", "percent")


def _role_proposals(fq: str, visible: list[tmpl.Col], st: dict[str, str | None], categorical: list[str], datetimes: list[str], *,
                    has_trend: bool, covered: set[str | None] = frozenset()) -> list[dict[str, Any]]:
    """Domain-neutral playbook from crawler column roles (skills/catalog): measures and the first flag by
    segments, and a trend on the first event timestamp. Lets any database get rule-based hypotheses
    without a model or a domain pack; outcomes a pack template already covers are left to the pack.
    Each role is filled by the registered method that declares it (`Method.playbook`)."""
    by_segment, flag_rate, volume_trend = (methods.for_playbook(r) for r in ("measure_by_segment", "flag_by_segment", "volume_trend"))
    roles = {c.name: c.role for c in visible}
    measures = [c for c in st if st[c] == "numeric" and roles.get(c) in _MEASURE_ROLES and "count" not in c] if by_segment else []
    flags = [c for c in st if st[c] == "boolean" and c not in covered] if flag_rate else []
    segs = [c for c in categorical if roles.get(c) not in ("identifier", "foreign_key")][:3]
    out: list[dict[str, Any]] = []
    for m in measures[:3]:
        outcome = Derivation(type="column", column=m, label=humanize(m))
        for seg in segs:
            out.append({"question": f"Does {outcome.label} differ by {humanize(seg)}?",
                        "statement": f"{outcome.label.capitalize()} differs materially across {humanize(seg)}.", "priority": "medium",
                        "spec": {"method": by_segment.name, "asset": fq, "outcome": outcome.model_dump(),
                                 "segment": Derivation(type="column", column=seg, label=humanize(seg)).model_dump()}})
    for f in flags[:1]:
        outcome = Derivation(type="is_true", column=f, label=tmpl.flag_label(f, False, None, pack_registry.hints().acronyms))
        for seg in segs:
            out.append({"question": f"Does the rate of {outcome.label} vary by {humanize(seg)}?",
                        "statement": f"The rate of {outcome.label} differs materially across {humanize(seg)}.", "priority": "medium",
                        "spec": {"method": flag_rate.name, "asset": fq, "outcome": outcome.model_dump(),
                                 "segment": Derivation(type="column", column=seg, label=humanize(seg)).model_dump()}})
    times = [c for c in datetimes if roles.get(c) in ("timestamp", "date")] or datetimes
    if times and not has_trend and volume_trend:
        out.append({"question": "How has volume trended over time?", "statement": "Volume shows a significant trend or change point.",
                    "priority": "low", "spec": {"method": volume_trend.name, "asset": fq,
                                                "time": {"type": "date_trunc", "column": times[0], "grain": "month"}}})
    return out


def diverse_top(accepted: list[dict], limit: int) -> list[dict]:
    """Best hypothesis of each method first (an analyst tests different kinds of explanation), then of each
    outcome not yet covered (different questions before more of the same question), then by score."""
    picked, methods = [], set()
    for a in accepted:
        if a["spec"]["method"] not in methods:
            picked.append(a)
            methods.add(a["spec"]["method"])

    def outcome(a: dict) -> str | None:
        return ((a["spec"].get("outcome") or {}).get("column") or (a["spec"].get("outcome") or {}).get("end_column"))

    covered = {outcome(a) for a in picked}
    for a in accepted:
        if len(picked) >= limit:
            break
        if a not in picked and outcome(a) not in covered:
            picked.append(a)
            covered.add(outcome(a))
    for a in accepted:
        if len(picked) >= limit:
            break
        if a not in picked:
            picked.append(a)
    return sorted(picked[:limit], key=lambda a: -a["priority_score"])


def _round1_selection(accepted: list[dict], limit: int) -> list[dict]:
    """Round 1 keeps the playbook's best and gives a model a few slots of its own. Ranked together, a model's
    `high` proposals filled the whole cap (live held-out run 2026-09-29: five model proposals and three playbook
    ones, and the planted effects only the playbook proposes were never tested). The model's slots are a
    quarter of the cap (at least two); a playbook shorter than its share leaves the rest to the model."""
    model = [a for a in accepted if a.get("origin") != "heuristic"]
    playbook = [a for a in accepted if a.get("origin") == "heuristic"]
    room = min(len(model), max(2, limit // 4)) if playbook else limit
    room = max(room, limit - len(playbook))  # unused playbook share goes to the model
    picked = diverse_top(playbook, limit - min(room, len(model))) + diverse_top(model, room)
    return sorted(picked[:limit], key=lambda a: -a["priority_score"])


# ---------------------------------------------------------------------------------- persistence
def _next_code(session, run_id: str) -> int:
    return (session.scalar(select(func.count()).select_from(Hypothesis).where(Hypothesis.run_id == run_id)) or 0) + 1


def _accept(ctx: RunContext, proposals: list[dict[str, Any]], types, *, origin: str, seen: set[str]) -> tuple[list[dict], list[dict]]:
    accepted, rejected = [], []
    for p in proposals:
        try:
            spec = AnalysisSpec.model_validate(p.get("spec") or {})
        except ValidationError as exc:
            rejected.append({"statement": p.get("statement"), "reason": f"invalid spec: {exc.errors()[0].get('msg')}"})
            continue
        errors = validate_spec(spec, ctx.scope, types)
        if errors:
            rejected.append({"statement": p.get("statement"), "reason": "; ".join(errors[:3])})
            continue
        keys = identity_keys(spec)
        if keys & seen:
            continue
        seen |= keys
        h = stable_hash(spec.model_dump(exclude={"min_group_size", "top_k"}))
        accepted.append({**p, "spec": spec.model_dump(), "origin": p.get("origin", origin), "spec_hash": h})
    return accepted, rejected


def usable_hypotheses(ctx: RunContext, types) -> Any:
    """Escalation check for hypothesis generation (cheap first, escalate): an answer is usable when at
    least one proposal passes spec validation. Duplicates of rule proposals still count as usable."""
    def check(data: Any) -> str | None:
        props = [p for p in data.get("hypotheses") or [] if isinstance(p, dict)] if isinstance(data, dict) else []
        if not props:
            return "empty hypothesis list"
        accepted, rejected = _accept(ctx, props, types, origin="agent", seen=set())
        return None if accepted else f"no usable hypotheses: all {len(rejected)} proposals failed spec validation"
    return check


def identity_keys(spec: AnalysisSpec) -> set[str]:
    """What makes two hypotheses the same test: the full spec, plus any identity the method adds
    (e.g. a second driver model on the same outcome and population re-answers the same question)."""
    return {stable_hash(spec.model_dump(exclude={"min_group_size", "top_k"})), *methods.get(spec.method).identity_keys(spec)}


def _prioritise(ctx: RunContext, accepted: list[dict]) -> str:
    """Rank (ADR-0015 `rank`): JEV may reorder, never drop; a hypothesis it does not score keeps its label."""
    if not accepted:
        return "rules"
    rank = {"high": 2.0, "medium": 1.0, "low": 0.0}
    items = {str(i): a["statement"] for i, a in enumerate(accepted)}
    decision = ctx.decisions.decide(
        "hypothesis_priority", {"objective": ctx.run.objective},
        Question.scores("How valuable is testing each hypothesis for answering the objective?", items,
                        hint={str(i): rank.get(a.get("priority", "medium"), 1.0) for i, a in enumerate(accepted)}),
        ctx=ctx.call_ctx())
    for i, a in enumerate(accepted):
        score, item = decision.value[str(i)], decision.details.get(str(i)) or {}
        v = item.get("by") == "jev" and decision.backend == "jev"
        a["priority_score"] = round(score / 2, 3)
        a["priority_by"] = f"jev:{decision.model}" if v else "rules"
        # The decision itself, not only who made it (P4-C09): replay and audit need the probabilities.
        a["priority_decision"] = {"score": score, "probabilities": item.get("probabilities") or {},
                                  "confidence": item.get("confidence"), "model": decision.model,
                                  "decision_id": decision.id} if v else None
        a["priority"] = "high" if a["priority_score"] >= 0.7 else "medium" if a["priority_score"] >= 0.35 else "low"
    accepted.sort(key=lambda a: -a["priority_score"])
    return "jev" if decision.backend == "jev" else "rules"


def _contracted(ctx: RunContext, accepted: list[dict]) -> list[dict]:
    """The agent's output contract (FND-006, `hypothesis: contract:analysis.AnalysisSpec`): a spec off the
    declared schema is dropped with its reason; an undeclared output type refuses the whole write."""
    from analystos.core.errors import OutputContractViolation

    kept = []
    for a in accepted:
        try:
            ctx.check_output("hypothesis", a.get("spec"))
        except OutputContractViolation as exc:
            if exc.details.get("reason") != "schema":
                raise
            ctx.say(f"Hypothesis dropped: {exc.message}", kind="decision", data={"statement": a.get("statement")})
            continue
        kept.append(a)
    return kept


def _persist(ctx: RunContext, accepted: list[dict], *, iteration: int, round_key: str) -> list[str]:
    keys = []
    accepted = _contracted(ctx, accepted)
    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id, with_for_update=True)
        if run.plan_version != ctx.task.plan_version:
            from analystos.core.errors import RunCancelled

            raise RunCancelled("plan changed while this task was running; hypotheses discarded")
        n = _next_code(s, run.id)
        for a in accepted:
            spec = a["spec"]
            h = Hypothesis(id=new_id("hyp"), workspace_id=run.workspace_id, run_id=run.id, code=f"H-{n}",
                           question=a.get("question", ""), statement=a["statement"], spec=spec, priority=a["priority"],
                           priority_score=a["priority_score"], status="approved", methods=[spec["method"]],
                           iteration=iteration, origin=a.get("origin", "agent"), parent_id=a.get("parent_id"))
            s.add(h)
            s.flush()
            key = f"test:{h.code}"
            add_task(s, run, key=key, agent="data_scientist", title=f"Test {h.code}: {a['statement'][:120]}",
                     depends_on=[round_key], optional=True, input={"hypothesis_id": h.id}, seq=60 + n,
                     from_version=ctx.task.plan_version)
            emit(run.workspace_id, "hypothesis.created", {"code": h.code, "statement": h.statement, "priority": h.priority,
                                                          "priority_by": a.get("priority_by"),
                                                          "priority_decision": a.get("priority_decision"), "method": spec["method"]},
                 run_id=run.id, session=s)
            link(s, run.workspace_id, ("objective", run.id), "asks", ("hypothesis", h.id), run_id=run.id)
            keys.append(key)
            n += 1
    return keys


def generate_hypotheses(ctx: RunContext) -> dict:
    from analystos.registries.replay import replay_settings

    replay = replay_settings(ctx.run)
    if replay is not None:
        return replay_hypotheses(ctx, replay)
    types = semantic_types(ctx)
    quality = task_output(ctx.run.id, "quality").get("issues") or []
    required = {"objective": ctx.run.objective, "questions": ctx.run.plan.get("questions"), "focus": ctx.run.plan.get("focus"),
                "user_instructions": [i.get("text") for i in ctx.run.instructions],
                "constraints": ctx.run.constraints,
                # the context agent's resolved terms reach the prompt as the compiler's glossary section (with receipts)
                "known_quality_issues": [q.get("message") for q in quality if q.get("severity") in ("warning", "critical")][:10]}
    seen: set[str] = set()
    packs = pack_registry.for_scope(ctx.scope, ctx.policy)
    # Recurring analysis: re-test every previously verified claim with the identical spec first, so
    # "resolved" means the evidence changed — not that a different question was asked this time.
    carried, rejected = _accept(ctx, carried_forward(ctx), types, origin="carried", seen=seen)
    required["already_testing"] = [c["statement"] for c in carried]
    payload = defer_compile(ctx, "hypothesis_generation", required)  # compiled only if the model is asked
    # Deterministic-first (admin: llm.purpose_modes.hypothesis_generation): the profile-driven playbook
    # runs first; in `auto` mode the model is only asked when the playbook is not enough.
    rules, rej_rules = _accept(ctx, [{**p, "origin": "heuristic"} for p in heuristic_proposals(ctx, types, packs)], types,
                               origin="heuristic", seen=set(seen))
    enough = len(carried) + len(rules) >= platform().analysis.heuristic_hypotheses_sufficient
    data, model = (llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1", payload, max_tokens=6000,
                            validate=usable_hypotheses(ctx, types))
                   if model_gate(ctx, "hypothesis_generation", payload, deterministic_ok=enough) else (None, "deterministic"))
    llm_props = [{**p, "origin": "agent"} for p in (data or {}).get("hypotheses", []) if isinstance(p, dict)] if isinstance(data, dict) else []
    # A model adds to the profile playbook, it never replaces it: the playbook's proposals come first (ties in
    # the ranking and in diverse_top go to them) and the model's add other methods and outcomes. Model-first
    # (the max_quality preset) used to drop the playbook whenever the model gave 4 usable proposals, and the
    # live P4-V01 run (2026-09-28) then missed 4 of 10 planted effects the playbook alone finds.
    accepted, rej2 = _accept(ctx, [{**p, "origin": "heuristic"} for p in heuristic_proposals(ctx, types, packs)], types,
                             origin="heuristic", seen=seen)
    more, rejected_llm = _accept(ctx, llm_props, types, origin="agent", seen=seen)
    accepted += more
    rejected += rej2 + rejected_llm
    source = f"llm:{model} + rules" if more else f"rules ({model})"
    del rules, rej_rules
    for r in rejected:
        ctx.say(f"Dropped proposal '{(r.get('statement') or '')[:100]}': {r['reason']}", kind="decision")
    by = _prioritise(ctx, accepted)
    accepted = _round1_selection(accepted, max(platform().analysis.max_round1_hypotheses - len(carried), 3))
    for c in carried:
        c.update(priority="high", priority_score=1.0, priority_by="carried_forward")
    accepted = carried + accepted
    keys = _persist(ctx, accepted, iteration=1, round_key="hypotheses")
    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id, with_for_update=True)
        if ctx.policy.max_iterations > 1:
            add_task(s, run, key="followups:1", agent="investigator", title="Review results and propose follow-up hypotheses (round 2)",
                     depends_on=["test:*"], optional=True, input={"round": 1}, seq=90, from_version=ctx.task.plan_version)
    pack_refs = [p.ref for p in packs]
    ctx.say(f"Proposed {len(accepted)} hypotheses ({len(carried)} carried forward from the previous run; source: {source}; "
            f"priority by {by}; domain packs: {', '.join(pack_refs) or 'none'}); {len(rejected)} proposals rejected by validation.",
            kind="decision", data={"questions": (data or {}).get("questions") if isinstance(data, dict) else None,
                                   "domain_packs": pack_refs})
    return {"hypotheses": len(accepted), "tasks": keys, "rejected": rejected, "source": source, "priority_by": by,
            "domain_packs": pack_refs}


def replay_hypotheses(ctx: RunContext, settings: dict[str, Any]) -> dict:
    """Scheduled re-analysis (P4-T05, review C11): re-test the hypothesis registry with no model call,
    so on unchanged data the run asks the same questions and gets the same answers. No follow-up
    rounds (the registry already holds the previous rounds' questions). The opt-in novelty round is
    the only model use, within its own budget; its hypotheses are labelled `novelty`."""
    from analystos.agents.common import compact_json
    from analystos.llm.cache import estimate_tokens
    from analystos.registries.hypotheses import pinned_proposals, replay_proposals

    types = semantic_types(ctx)
    previous = (ctx.run.origin or {}).get("previous_run_id")
    pinned = settings.get("analyses") or []
    label = settings.get("label") or "registry"
    with session_scope() as s:
        if pinned:  # a frozen set (ADR-0021): exactly the baseline's (or the work order's) specs, never re-planned
            proposals = pinned_proposals(s, ctx.workspace.id, pinned, ctx.scope.assets, label=label)
        else:
            proposals = replay_proposals(s, ctx.workspace.id, previous, ctx.scope.assets, scope=settings["registry_scope"])
    seen: set[str] = set()
    accepted, rejected = _accept(ctx, proposals, types, origin=label, seen=seen)
    source = "pinned set" if pinned else "registry"
    for a in accepted:
        a["priority_by"] = "registry"
    if not accepted and pinned:
        raise InvalidInput(f"none of the {len(pinned)} pinned analyses is valid on the current scope: "
                           + "; ".join(r["reason"] for r in rejected[:3]))
    if not accepted:  # no usable baseline yet: the rule playbook, still without a model
        accepted, rejected_rules = _accept(ctx, [{**p, "origin": "heuristic"} for p in heuristic_proposals(ctx, types)], types,
                                           origin="heuristic", seen=seen)
        rejected += rejected_rules
        _prioritise(ctx, accepted)  # a replay run's router is off for hypothesis_priority: rule ranking
        accepted = diverse_top(accepted, platform().analysis.max_round1_hypotheses)
        source = "rules (empty registry)"
    avoided = {"objective": ctx.run.objective, "catalog": catalog_for_prompt(ctx), "already_testing": [a["statement"] for a in accepted]}
    ctx.router.record_skip("hypothesis_generation", ctx.call_ctx(), estimated_tokens=estimate_tokens(compact_json(avoided)) + 500,
                           reason=f"L1 registry: {len(accepted)} registered hypotheses replayed", rung="registry")
    novel: list[dict] = []
    if settings["novelty"]["enabled"]:
        novel, rejected_novel = _novelty_round(ctx, types, seen, settings["novelty"], avoided)
        rejected += rejected_novel
    for r in rejected:
        ctx.say(f"Not replayed: '{(r.get('statement') or '')[:100]}': {r['reason']}", kind="decision")
    keys = _persist(ctx, accepted + novel, iteration=1, round_key="hypotheses")
    novelty_note = f"novelty round added {len(novel)} new questions" if settings["novelty"]["enabled"] else "novelty round off"
    ctx.say(f"Scheduled re-analysis: replayed {len(accepted)} hypotheses (source: {source}; no model call); {novelty_note}; "
            f"{len(rejected)} not replayed.", kind="decision")
    return {"hypotheses": len(keys), "tasks": keys, "rejected": rejected, "source": source, "priority_by": "registry",
            "replayed": len(accepted), "novelty": len(novel)}


def _purpose_spend(run_id: str, purpose: str) -> float:
    from analystos.db.models import ModelCall

    with session_scope() as s:
        return float(s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(
            ModelCall.run_id == run_id, ModelCall.purpose == purpose)) or 0.0)


def _novelty_round(ctx: RunContext, types, seen: set[str], novelty: dict[str, Any],
                   payload: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Opt-in exploration: at most one model call, and only while the round's USD budget lasts."""
    budget = novelty["llm_budget_usd"]
    if _purpose_spend(ctx.run.id, "hypothesis_generation") >= budget:
        ctx.say(f"Novelty round skipped: its budget (${budget:.4f}) is used up.", kind="decision")
        return [], []
    data, model = llm_json(ctx, "hypothesis_generation", "hypothesis_generation.v1",
                           {**payload, "max_new_hypotheses": novelty["max_hypotheses"],
                            "instruction": "Propose only questions that are not in already_testing."}, max_tokens=3000)
    props = [{**p, "origin": "novelty"} for p in (data or {}).get("hypotheses", []) if isinstance(p, dict)] \
        if isinstance(data, dict) else []
    accepted, rejected = _accept(ctx, props, types, origin="novelty", seen=seen)
    accepted = accepted[:novelty["max_hypotheses"]]
    for a in accepted:
        a.update(priority="low", priority_score=0.3, priority_by=f"novelty:{model}")
    spent = _purpose_spend(ctx.run.id, "hypothesis_generation")
    if spent > budget:
        ctx.say(f"Novelty round spent ${spent:.4f}, above its ${budget:.4f} budget.", kind="decision")
    return accepted, rejected


def carried_forward(ctx: RunContext) -> list[dict]:
    """Verified claims of the previous run of the same recurring analysis, as proposals."""
    previous = (ctx.run.origin or {}).get("previous_run_id")
    if not previous:
        return []
    from analystos.db.models import Insight

    with session_scope() as s:
        rows = s.execute(select(Hypothesis, Insight.code).join(Insight, Insight.hypothesis_id == Hypothesis.id)
                         .where(Insight.run_id == previous, Insight.status == "verified").order_by(*by_code(Insight.code))).all()
        return [{"question": h.question, "statement": h.statement, "rationale": f"Re-test of {code} from run {previous}",
                 "spec": {k: v for k, v in h.spec.items()}, "priority": "high", "origin": "carried"} for h, code in rows]


def _results_summary(run_id: str) -> list[dict]:
    with session_scope() as s:
        out = []
        for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.status != "superseded")
                           .order_by(*by_code(Hypothesis.code))):
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary"))
            out.append({"code": h.code, "statement": h.statement, "status": h.status, "spec": h.spec,
                        "result": {k: (exp.result or {}).get(k) for k in ("test", "n", "p_value", "effect_size", "effect_label", "highlights")} if exp else None})
        return out


_PROMPT_STATS = ("test", "n", "p_value", "effect_size", "effect_label")
_PROMPT_HIGHLIGHTS = ("top_segment", "top_rate", "baseline_segment", "baseline_rate", "rate_ratio", "top_mean",
                      "baseline_mean", "top_median", "baseline_median", "top_share", "top_driver", "slope", "change_point")


def _spec_for_prompt(spec: dict[str, Any]) -> dict[str, Any]:
    """A spec without its defaults and empty fields: the model needs what was tested, not the schema."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()
                    if v not in (None, [], {}) and k not in ("start_hour", "end_hour", "top_k", "min_group_size", "origin")}
        return value
    return clean(spec)


def results_for_prompt(results: list[dict]) -> list[Any]:
    """Follow-up prompt input (Stream B): supported and inconclusive tests — the ones a follow-up can
    drill into or re-examine — with a compact spec and the few statistics that say what was found; every
    other test as one line. Repeats of a tested spec are removed in code (`identity_keys`), so the
    model needs to know what was asked, not the full spec of every rejected test."""
    out: list[Any] = []
    for r in results:
        if r.get("status") in ("supported", "inconclusive"):
            res = r.get("result") or {}
            hl = res.get("highlights") or {}
            stats = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items() if k in _PROMPT_STATS and v is not None}
            stats.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in hl.items()
                          if k in _PROMPT_HIGHLIGHTS and v is not None})
            out.append({"code": r["code"], "status": r["status"], "statement": r["statement"],
                        "spec": _spec_for_prompt(r.get("spec") or {}), "stats": stats})
        else:
            out.append(f"{r['code']} {r.get('status')}: {r.get('statement')}")
    return out


def _new_supported(run_id: str, round_no: int) -> int:
    """Supported hypotheses first tested in round `round_no` (AUT-005: stop when a round adds none)."""
    with session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(Hypothesis).where(
            Hypothesis.run_id == run_id, Hypothesis.iteration == round_no, Hypothesis.status == "supported")) or 0)


def follow_ups(ctx: RunContext) -> dict:
    round_no = int(ctx.task.input.get("round", 1))
    results = _results_summary(ctx.run.id)
    supported = [r for r in results if r["status"] == "supported"]
    if not supported:
        ctx.say("No supported hypothesis to drill into; stopping iteration.", kind="decision")
        return {"added": 0, "stop": "no_supported_results"}
    # Stop criterion (ADR-0015 bounded_stop): rule first (nothing new in the last round), JEV only as a
    # tie-break after the minimum rounds with enough supported findings.
    stop = ctx.decisions.decide(
        "stop_check", {"objective": ctx.run.objective,
                       "findings": "; ".join(f"{r['statement']} ({r['status']})" for r in results)[:3500]},
        Question.probability("Do `findings` already answer `objective` well enough that further drill-down analysis is unnecessary?"),
        facts={"round": round_no, "supported": len(supported), "new_supported_last_round": _new_supported(ctx.run.id, round_no)},
        ctx=ctx.call_ctx())
    if stop.value:
        if stop.backend == "rules":
            ctx.say("Stop check: the last round found no new supported finding — stopping iteration.", kind="decision")
            return {"added": 0, "stop": "rule_stop_check", "decision_id": stop.id}
        who = "JEV" if stop.backend == "jev" else stop.backend
        ctx.say(f"{who} stop check: P(objective answered)={stop.p:.2f} ≥ 0.85 — stopping iteration.", kind="decision")
        return {"added": 0, "stop": f"{stop.backend}_stop_check", "p": stop.p, "decision_id": stop.id}
    types = semantic_types(ctx)
    seen = set().union(*(identity_keys(AnalysisSpec.model_validate(r["spec"])) for r in results)) if results else set()
    # Deterministic follow-ups: drill into supported findings, and continue each tested outcome across the
    # dimensions it has not been broken down by yet (an analyst's KPI x dimension matrix).
    roles = {f"{a.schema_name}.{a.name}.{c.name}": (c.semantics or {}).get("semantic_role") for a, cols in asset_rows(ctx) for c in cols}
    display = [p.templates["display_name"] for p in pack_registry.for_scope(ctx.scope, ctx.policy) if p.templates.get("display_name")]
    drill = _drilldowns(supported, types, display) + _matrix_continuations(results, types, ctx.scope.denied_columns, roles)
    payload = defer_compile(ctx, "follow_up_generation", {"objective": ctx.run.objective, "results": results_for_prompt(results),
                                                          "constraints": ctx.run.constraints})
    data, model = (llm_json(ctx, "follow_up_generation", "follow_up_generation.v1", payload,
                            validate=usable_hypotheses(ctx, types))
                   if model_gate(ctx, "follow_up_generation", payload, deterministic_ok=bool(drill)) else (None, "deterministic"))
    props = [p for p in (data or {}).get("hypotheses", []) if isinstance(p, dict)] if isinstance(data, dict) else []
    accepted, rejected = _accept(ctx, props, types, origin="agent", seen=seen)
    if not accepted:
        accepted, _ = _accept(ctx, drill, types, origin="heuristic", seen=seen)
    codes = {r["code"]: r for r in results}
    with session_scope() as s:
        for a in accepted:
            parent = codes.get(a.get("parent") or "")
            if parent:
                a["parent_id"] = s.scalar(select(Hypothesis.id).where(Hypothesis.run_id == ctx.run.id, Hypothesis.code == parent["code"]))
    _prioritise(ctx, accepted)
    accepted = accepted[:platform().analysis.max_followups_per_round]
    keys = _persist(ctx, accepted, iteration=round_no + 1, round_key=ctx.task.key)
    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id)
        if keys and round_no + 1 < ctx.policy.max_iterations:
            add_task(s, run, key=f"followups:{round_no + 1}", agent="investigator",
                     title=f"Review results and propose follow-up hypotheses (round {round_no + 2})",
                     depends_on=["test:*"], optional=True, input={"round": round_no + 1}, seq=90 + round_no,
                     from_version=ctx.task.plan_version)
    ctx.say(f"Round {round_no + 1}: {len(keys)} follow-up hypotheses ({'llm:' + str(model) if props else 'deterministic drill-down'}).",
            kind="decision")
    return {"added": len(keys), "tasks": keys, "rejected": rejected}


def _method_attr(spec: dict, attr: str) -> Any:
    """An attribute of the spec's method (None for a name the registry does not know; validation rejects it later)."""
    name = spec.get("method")
    return getattr(methods.get(name), attr, None) if name in methods.names() else None


def _drilldowns(supported: list[dict], types, display_patterns: list[str] = ()) -> list[dict]:
    """Drill into each supported rate finding's top segment by the next categorical (enabled packs'
    display-name columns first)."""
    out = []
    for r in supported:
        spec = r["spec"]
        hl = (r.get("result") or {}).get("highlights") or {}
        top = hl.get("top_segment")
        seg = spec.get("segment") or {}
        if not _method_attr(spec, "drill_down") or top is None or seg.get("type") != "column":
            continue
        cats = [c for c, t in types.get(spec["asset"], {}).items() if t == "categorical" and c != seg.get("column")]
        cats = tmpl.prefer_display(cats, display_patterns)
        for other in cats[:1]:
            out.append({"question": f"Within {seg.get('label') or seg['column']} = {top}, does the outcome vary by {other}?",
                        "statement": f"Within {seg['column']} = {top}, the outcome rate differs across {other}.",
                        "priority": "medium", "parent": r["code"],
                        "spec": {**spec, "segment": {"type": "column", "column": other, "label": other.replace('_', ' ')},
                                 "filters": (spec.get("filters") or []) + [{"column": seg["column"], "op": "=", "value": top}]}})
    return out


_ROLE_WEIGHT = {"amount": 3, "duration": 3, "percent": 2, "flag": 2, "measure": 1}


def _matrix_continuations(results: list[dict], types, denied: list[str], roles: dict[str, str | None] | None = None) -> list[dict]:
    """For each outcome tested by segment, propose it by the next dimension it was not yet broken down by.
    One per outcome per round, least-explored outcomes first and business measures (amounts, durations,
    rates) before plain counts. Every test still goes through BH correction, so breadth adds no false positives."""
    roles = roles or {}
    tested: dict[tuple[str, str], set[str]] = {}
    template: dict[tuple[str, str], dict] = {}
    for r in results:
        spec = r["spec"]
        if not _method_attr(spec, "segment_matrix") or spec.get("filters"):
            continue
        out = spec.get("outcome") or {}
        key = (spec["asset"], stable_hash(out))
        tested.setdefault(key, set()).add((spec.get("segment") or {}).get("column"))
        template.setdefault(key, spec)
    proposals = []
    for key, spec in template.items():
        asset = spec["asset"]
        dims = [c for c, t in types.get(asset, {}).items()
                if t == "categorical" and c not in tested[key] and f"{asset}.{c}" not in set(denied) and not _TEXTY.search(c)]
        if not dims:
            continue
        nxt = dims[0]
        outcome_col = (spec.get("outcome") or {}).get("column") or ""
        label = (spec.get("outcome") or {}).get("label") or humanize(outcome_col or "outcome")
        weight = _ROLE_WEIGHT.get(roles.get(f"{asset}.{outcome_col}") or "", 1)
        proposals.append({"_order": (len(tested[key]), -weight),
                          "question": f"Does {label} differ by {humanize(nxt)}?",
                          "statement": f"{label[:1].upper() + label[1:]} differs materially across {humanize(nxt)}.",
                          "priority": "medium",
                          "spec": {**{k: v for k, v in spec.items() if k != "segment"},
                                   "segment": {"type": "column", "column": nxt, "label": humanize(nxt)}}})
    proposals.sort(key=lambda p: p.pop("_order"))
    return proposals

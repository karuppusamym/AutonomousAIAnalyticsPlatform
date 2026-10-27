"""Run lifecycle and human-in-the-loop controls (§39, §40, §43.3)."""
from __future__ import annotations

import json
import re
from typing import Any

from analystos.contracts.analysis import Filter
from analystos.contracts.events import RUN_TERMINAL
from analystos.contracts.policy import ExecutionIdentity
from analystos.core.errors import Conflict, ContextOverBudget, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Feedback, Hypothesis, Insight, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import (
    denied,
    evaluate,
    get_workspace,
    load_in_workspace,
    require_role,
    resolve_scope,
    scoped_loader,
)
from analystos.runtime.context import default_router, workspace_call_ctx
from analystos.runtime.engine import apply_replan
from analystos.workflows.orchestrator import signal_run, start_run

TERMINAL = RUN_TERMINAL


def create_run(user: User, workspace_id: str, *, objective: str | None, source_ids: list[str] | None = None,
               autonomy_level: int | None = None, origin: dict | None = None, playbook: str | None = None,
               definition: dict | str | None = None, pins: dict | None = None,
               analysis_context: dict | str | None = None) -> AnalysisRun:
    """`playbook` names a Playbook capability (default playbook.investigate) or a workspace playbook
    definition key; `definition` names an exact version ({key, version} or a definition id). Either
    must be runnable now (published; a draft only in a dev workspace; never retired), and enablement
    and certification are checked when the plan binds it. `pins` (a schedule fire) freezes the
    manifests, semantic versions and AnalysisSpecs the run binds (ADR-0021)."""
    return start_run_request(user, workspace_id, objective=objective, source_ids=source_ids, autonomy_level=autonomy_level,
                             origin=origin, playbook=playbook, definition=definition, pins=pins,
                             analysis_context=analysis_context)[0]


def _capabilities(s, workspace_id: str, *, playbook: str | None, definition: dict | str | None, pins: dict | None,
                  trigger: str) -> dict:
    from analystos.capabilities import registry
    from analystos.services import definitions

    if pins:
        keep = ("revision", "schedule_id", "baseline_run_id", "manifests", "semantic", "methods", "analyses")
        return {"playbook": pins.get("playbook") or "playbook.investigate", "definition": pins.get("definition") or {},
                "pinned": {k: pins[k] for k in keep if pins.get(k) is not None}}
    if definition is None and playbook is not None:
        manifest = registry.current().manifests.get(playbook)
        if manifest is not None and manifest.kind != "Playbook":
            raise InvalidInput(f"{playbook} is not a playbook")
        definition = {"key": playbook}
    if definition is None:
        return {}
    ref, spec = definitions.resolve_runnable(s, workspace_id, definition, trigger=trigger)
    if ref.kind != "playbook":
        raise InvalidInput(f"a {ref.kind} definition is not started as an analysis run")
    if ref.source == "workspace":
        return {"playbook": ref.key, "definition": {**ref.model_dump(), "manifest": spec, "dev": ref.status == "draft"}}
    return {"playbook": ref.key, "definition": ref.model_dump()}


def start_run_request(user: User, workspace_id: str, *, objective: str | None, source_ids: list[str] | None = None,
                      autonomy_level: int | None = None, origin: dict | None = None, playbook: str | None = None,
                      definition: dict | str | None = None, pins: dict | None = None,
                      analysis_context: dict | str | None = None,
                      idempotency: Any = None, limits: dict | None = None, constraints: dict | None = None,
                      on_created: Any = None) -> tuple[AnalysisRun, bool]:
    """Create a run and its dispatch-outbox row in one transaction (P4-06), then dispatch best-effort;
    the outbox relay retries whatever did not go out. With an idempotency key the claim is part of the
    same transaction: a duplicate returns the original run (`True` = replayed).

    Goal runs (S06) also pass `limits` (budget counters), `constraints` (the goal's scope filters, part
    of the plan hash) and `on_created(session, run, scope) -> dict`, which writes the goal row in the
    same transaction; what it returns is added to the idempotency record's response."""
    from analystos.services import dispatch
    from analystos.services.idempotency import begin_in, complete_in

    with session_scope() as s:
        require_role(s, user, workspace_id, "analyst")  # a replay is re-authorized too
        if idempotency is not None and (replay := begin_in(s, idempotency)) is not None:
            run = s.get(AnalysisRun, replay.resource_id)
            if run is None or run.workspace_id != workspace_id:
                raise NotFound("the run this Idempotency-Key created no longer exists")
            s.expunge(run)
            return run, True
        ws = get_workspace(s, workspace_id)
        context_snapshot = None
        if analysis_context is not None:
            from analystos.services import definitions

            context_ref, context_spec = definitions.resolve_runnable(s, workspace_id, analysis_context, trigger="analysis")
            if context_ref.kind != "analysis_context" or context_ref.status != "published":
                raise InvalidInput("select a published analysis context version")
            from analystos.semantic.service import approved_metrics

            if not set(context_spec["metric_names"]).issubset(approved_metrics(s, workspace_id)):
                raise InvalidInput("the analysis context references a metric that is no longer approved")
            context_sources = context_spec["source_ids"]
            if source_ids is not None and set(source_ids) != set(context_sources):
                raise InvalidInput("selected sources must match the analysis context version")
            source_ids = context_sources
            context_snapshot = {"definition": context_ref.model_dump(mode="json"), "spec": context_spec}
        objective = (objective or ws.objective or "").strip()
        if len(objective) < 10:
            raise InvalidInput("describe the business objective (at least 10 characters)")
        scope = resolve_scope(s, s.merge(user), workspace_id, source_ids=source_ids)
        if not scope.assets:
            raise InvalidInput("no selected, ready assets: add a source, discover it and select tables first")
        if len(set(scope.asset_sources.values())) > 1 and not source_ids:
            raise InvalidInput("the workspace has several sources: pass source_ids (one source, or several for a "
                               "cross-source run)")
        level = min(autonomy_level if autonomy_level is not None else ws.autonomy_level, ws.autonomy_level)
        identity = ExecutionIdentity(user_id=user.id, workspace_id=workspace_id, purpose="analysis")
        decision = evaluate(s, s.merge(user), identity, "run_analysis", autonomy_level=level)
        if decision.decision == "deny":
            raise denied(decision, "analysis run denied: " + ", ".join(decision.reasons))
        origin = origin or {"type": "user"}
        caps = _capabilities(s, workspace_id, playbook=playbook, definition=definition, pins=pins,
                             trigger=str(origin.get("type") or "api"))
        if context_snapshot is not None:
            caps["analysis_context"] = context_snapshot
        run = AnalysisRun(id=new_id("run"), workspace_id=workspace_id, objective=objective, status="NEW", autonomy_level=level,
                          policy_version=ws.policy_version, requested_by=user.id,
                          scope={**scope.model_dump(exclude={"abac"}), "hash": scope.scope_hash()}, instructions=[],
                          constraints=dict(constraints or {}), origin=origin, capabilities=caps, limits=dict(limits or {}))
        s.add(run)
        s.flush()
        extra = on_created(s, run, scope) if on_created is not None else None
        emit(workspace_id, "run.created", {"objective": objective, "autonomy_level": level, "assets": scope.assets,
                                           "policy": decision.model_dump(),
                                           **({"definition": {k: v for k, v in caps["definition"].items() if k != "manifest"}}
                                              if caps.get("definition") else {})},
             run_id=run.id, actor=f"user:{user.id}", session=s)
        outbox_id = dispatch.enqueue(s, run).id
        if idempotency is not None:
            complete_in(s, idempotency, response={"run_id": run.id, **(extra or {})}, resource_type="analysis_run",
                        resource_id=run.id)
        run_id = run.id
    dispatch.dispatch(outbox_id)  # best effort: a failure stays pending for the relay
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        s.expunge(run)
    return run, False


@scoped_loader
def get_run_for(session, user: User, run_id: str, minimum: str = "viewer", workspace_id: str | None = None) -> AnalysisRun:
    """A run the caller may see at `minimum`; with `workspace_id` (a path's) it must also belong there."""
    return load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, minimum=minimum, label="run")


@scoped_loader
def control(user: User, run_id: str, action: str, workspace_id: str | None = None) -> AnalysisRun:
    with session_scope() as s:
        run = get_run_for(s, user, run_id, "analyst", workspace_id)
        if run.status in TERMINAL:
            raise Conflict(f"run is {run.status}")
        if action == "pause":
            run.control = "pause"
        elif action == "resume":
            run.control = "run"
            if run.status == "PAUSED":
                run.status = "RUNNING"
        elif action == "cancel":
            run.control = "cancel"
        else:
            raise InvalidInput("action must be pause|resume|cancel")
        emit(run.workspace_id, "run.status", {"control": run.control, "requested_by": user.id}, run_id=run.id, session=s)
        audit(f"user:{user.id}", f"run.{action}", workspace_id=run.workspace_id, run_id=run.id, session=s)
        s.flush()  # persist changes before detaching (expunged objects are not flushed)
        s.expunge(run)
    signal_run(run_id)
    return run


FEEDBACK_KINDS = {"redirect": "Narrow or change what the analysis should focus on (filters, scope, exclusions)",
                  "add_context": "Adds business context or definitions without changing scope",
                  "reject_finding": "Says a finding is wrong or not useful",
                  "deeper_analysis": "Asks for deeper analysis or a new question to investigate",
                  "question": "Asks a question about the results"}


_EXCLUDE = re.compile(r"\b(exclude|excluding|ignore|ignoring|without|remove|drop|except|not)\b", re.I)
_INCLUDE = re.compile(r"\b(only|focus(?:ing)? on|just|restrict(?:ed)? to|limit(?:ed)? to|keep)\b", re.I)


def parse_redirect_rules(text: str, vocab: dict[tuple[str, str], list[str]]) -> list[dict[str, Any]]:
    """Deterministic redirect parsing: category values named in the instruction (from profiled
    vocabularies) become filters; the nearest preceding verb decides include vs exclude."""
    lowered = text.lower()
    hits: dict[tuple[str, str], dict[str, list]] = {}
    for (asset, column), values in vocab.items():
        for value in values:
            v = str(value)
            if len(v) < 3:
                continue
            m = re.search(rf"(?<![\w]){re.escape(v.lower())}(?![\w])", lowered)
            if not m:
                continue
            before = lowered[:m.start()]
            last_ex = max((x.end() for x in _EXCLUDE.finditer(before)), default=-1)
            last_in = max((x.end() for x in _INCLUDE.finditer(before)), default=-1)
            if last_ex < 0 and last_in < 0:
                continue
            bucket = hits.setdefault((asset, column), {"in": [], "ex": []})
            bucket["ex" if last_ex > last_in else "in"].append(value)
    filters = []
    for (asset, column), b in hits.items():
        if b["in"]:
            filters.append({"asset": asset, "column": column, "op": "=" if len(b["in"]) == 1 else "in",
                            "value": b["in"][0] if len(b["in"]) == 1 else b["in"]})
        for v in b["ex"]:
            filters.append({"asset": asset, "column": column, "op": "!=", "value": v})
    return filters


def _vocabulary(run: AnalysisRun) -> dict[tuple[str, str], list[str]]:
    from sqlalchemy import select

    from analystos.db.models import SourceAsset, SourceColumn

    denied = set(run.scope.get("denied_columns") or [])
    out: dict[tuple[str, str], list[str]] = {}
    with session_scope() as s:
        for fq in run.scope.get("assets") or []:
            schema, name = fq.split(".", 1)
            asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == run.workspace_id,
                                                       SourceAsset.schema_name == schema, SourceAsset.name == name))
            if not asset:
                continue
            for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id)):
                top = (c.profile or {}).get("top_values") or []
                if f"{fq}.{c.name}" in denied or "pii" in (c.tags or []) or not top or (c.profile or {}).get("distinct", 999) > 60:
                    continue
                out[(fq, c.name)] = [t.get("value") for t in top if isinstance(t.get("value"), str)]
    return out


def _feedback_messages(router: Any, ctx: Any, run: AnalysisRun, system: str, text: str,
                       catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compiled context (P4-T03) in the cache-stable layout (P4-T04): the instruction and objective
    are mandatory; the scope's column names and matching glossary entries fill the budget.
    Raises ContextOverBudget when the instruction alone is over budget."""
    from analystos.context.compiler import compile_context, load_knowledge
    from analystos.contracts.platform import PurposeProfile
    from analystos.services.platform_settings import get as platform

    settings = platform()
    profile = settings.context.profiles.get("feedback_interpretation") or PurposeProfile(sections=["catalog"])
    knowledge: list = []
    if settings.context.compiler_enabled and any(x != "catalog" for x in profile.sections):
        with session_scope() as s:
            knowledge = load_knowledge(s, run.workspace_id, profile.sections, run_id=run.id, query=f"{text} {run.objective}")
    compiled = compile_context("feedback_interpretation", profile, objective=f"{text} {run.objective}",
                               required={"instruction": text, "objective": run.objective}, catalog=catalog,
                               knowledge=knowledge, limit_chars=int(settings.llm.max_prompt_tokens * 3.6) - len(system) - 200,
                               min_relevance=settings.context.min_relevance)
    ctx.context_receipts = compiled.receipts
    return [{"role": "system", "content": system, "cache": True},
            {"role": "user", "content": json.dumps(compiled.body, separators=(",", ":"), default=str)}]


def _interpret_redirect(run: AnalysisRun, text: str) -> dict[str, Any]:
    """Rules first (profiled category vocabulary), model when rules find nothing or admin mode is
    `always`; either way filters are validated against the run scope. Invalid filters are dropped."""
    from analystos.agents.prompts import prompt, prompt_version_id

    router = default_router()
    system = prompt("feedback_interpretation.v1")
    ctx = workspace_call_ctx(run.workspace_id, run_id=run.id, agent_id="supervisor",
                             prompt_version=prompt_version_id("feedback_interpretation.v1", system))
    rule_filters = parse_redirect_rules(text, _vocabulary(run))
    mode = router.mode("feedback_interpretation")
    if rule_filters and mode in ("auto", "off"):
        router.record_skip("feedback_interpretation", ctx, estimated_tokens=1500, reason=f"mode={mode}: rule parse")
        return {"filters": rule_filters, "focus": [text], "summary": text, "interpreted_by": "rules"}
    catalog = [{"asset": a, "columns": [c for c in cols if f"{a}.{c}" not in set(run.scope.get("denied_columns") or [])]}
               for a, cols in (run.scope.get("columns") or {}).items()]
    out: dict[str, Any] = {"filters": rule_filters, "focus": [text], "summary": text,
                           "interpreted_by": "rules" if rule_filters else "none"}
    if not router.available("feedback_interpretation", ctx):
        return out
    try:
        messages = _feedback_messages(router, ctx, run, system, text, catalog)
    except ContextOverBudget as exc:  # visible refusal; the rule parse (if any) stands
        router.sink.record(ctx=ctx, purpose="feedback_interpretation", profile="-", provider="context_compiler", model="-",
                           status="refused", attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0,
                           request_hash=None, error=exc.message[:500])
        return {**out, "refused": exc.message}
    try:
        resp = router.complete("feedback_interpretation", messages, ctx=ctx, json_output=True, validate=_valid_feedback)
    except Exception:
        return out
    data = resp.data if isinstance(resp.data, dict) else {}
    if not data.get("filters") and rule_filters:
        return out
    valid = []
    for f in data.get("filters") or []:
        try:
            flt = Filter.model_validate({**f, "origin": "user_redirect"})
        except Exception:
            continue
        asset = f.get("asset") or next(iter(run.scope.get("assets") or []), None)
        cols = (run.scope.get("columns") or {}).get(asset, [])
        if flt.column in cols and f"{asset}.{flt.column}" not in set(run.scope.get("denied_columns") or []):
            valid.append({"asset": asset, "column": flt.column, "op": flt.op, "value": flt.value})
    return {"filters": valid, "focus": [str(x) for x in data.get("focus") or []] or [text],
            "summary": data.get("summary") or text, "interpreted_by": resp.model}


def _valid_feedback(resp: Any) -> str | None:
    """Escalation check (schema): an object whose filters, when present, validate as filters."""
    data = resp.data
    if not isinstance(data, dict):
        return "answer is not an object"
    filters = data.get("filters") or []
    if not isinstance(filters, list):
        return "filters is not a list"
    ok = 0
    for f in filters:
        try:
            Filter.model_validate({**f, "origin": "user_redirect"})
            ok += 1
        except Exception:
            continue
    return None if ok or not filters else f"none of {len(filters)} filters passed schema validation"


@scoped_loader
def submit_feedback(user: User, run_id: str, *, text: str, kind: str | None = None, target_type: str | None = None,
                    target_id: str | None = None, workspace_id: str | None = None) -> dict[str, Any]:
    if kind == "question":
        from analystos.services.run_questions import submit_saved_question

        return submit_saved_question(user, run_id, text=text, workspace_id=workspace_id, target_id=target_id)

    from analystos.decisions import Question, decision_service
    from analystos.llm.jev import CONSEQUENTIAL_INSTRUCTIONS

    with session_scope() as s:
        run = get_run_for(s, user, run_id, "analyst", workspace_id)
        s.flush()  # persist changes before detaching (expunged objects are not flushed)
        s.expunge(run)
    decisions = decision_service(default_router())
    ctx = workspace_call_ctx(run.workspace_id, run_id=run.id, agent_id="supervisor")
    fb_id = new_id("fb")
    classified_by, classification = "user", None
    if kind is None:
        # ADR-0015 route: pick a processing path among the valid feedback kinds (calibrated by user corrections).
        classification = decisions.decide(
            "feedback_classification", {"feedback": text, "objective": run.objective},
            Question.choice("What kind of feedback is `feedback` about an analysis of `objective`?", FEEDBACK_KINDS, default="redirect"),
            ctx=ctx, subject=f"feedback:{fb_id}")
        kind = classification.value
        classified_by = {"jev": f"jev:{classification.model}", "rules": "default", "default": "default"}.get(
            classification.backend, f"{classification.backend}:{classification.model}")
    if kind not in FEEDBACK_KINDS:
        raise InvalidInput(f"kind must be one of {sorted(FEEDBACK_KINDS)}")
    if kind == "question":
        from analystos.services.run_questions import submit_saved_question

        return submit_saved_question(user, run_id, text=text, workspace_id=workspace_id, target_id=target_id,
                                     classified_by=classified_by, feedback_id=fb_id,
                                     decision_ids=[classification.id] if classification is not None else [])
    # escalate_only: side-effect verbs (rule) or JEV may add the approval note; nothing removes it.
    risk = decisions.decide("risk_check", {"request": text},
                            Question.escalation(CONSEQUENTIAL_INSTRUCTIONS, key="consequential", levels=["read", "consequential"],
                                                baseline="read", escalate_to="consequential", escalate_at=0.5),
                            ctx=ctx, subject=f"feedback:{fb_id}")
    result: dict[str, Any] = {"kind": kind, "classified_by": classified_by, "consequential_p": risk.p,
                              "decision_ids": [d.id for d in (classification, risk) if d is not None]}
    interpretation = _interpret_redirect(run, text) if kind in ("redirect", "deeper_analysis") else None
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        was_completed = run.status == "COMPLETED"  # its workflow has returned; a replan needs a new one
        fb = Feedback(id=fb_id, workspace_id=run.workspace_id, run_id=run.id, user_id=user.id, kind=kind, text=text,
                      target_type=target_type, target_id=target_id, data={"interpretation": interpretation, **result})
        s.add(fb)
        run.instructions = [*run.instructions, {"kind": kind, "text": text, "at": utcnow().isoformat(), "feedback_id": fb.id}]
        if risk.value == "consequential":
            result["note"] = ("This instruction asks for an action with side effects; agents never execute it directly — "
                              "publication, scheduling and sending always go through an approval.")
        if kind in ("redirect", "deeper_analysis"):
            constraints = dict(run.constraints or {})
            constraints["filters"] = [*constraints.get("filters", []), *interpretation["filters"]]
            constraints["focus"] = [*constraints.get("focus", []), *interpretation["focus"]]
            run.constraints = constraints
            if run.status in TERMINAL - {"COMPLETED"}:
                raise Conflict(f"run is {run.status}")
            result["replan"] = apply_replan(s, run, f"user {kind}: {interpretation['summary'][:200]}", full=True)
            result["interpretation"] = interpretation
            fb.applied = True
        elif kind == "reject_finding":
            if target_type != "insight" or not target_id:
                raise InvalidInput("reject_finding needs target_type='insight' and target_id")
            ins = s.get(Insight, target_id)
            if ins is None or ins.run_id != run.id:
                raise NotFound("insight not found in this run")
            from analystos.evidence.verification import flag_wrong

            flag_wrong(s, ins, user_id=user.id, reason=text)  # P7-01: a rejection needs its reason, kept on the record
            ins.status = "rejected"
            from analystos.decisions.calibration import record_signal

            record_signal(s, "finding.reject", f"insight:{ins.id}", user_id=user.id, workspace_id=run.workspace_id)
            h = s.get(Hypothesis, ins.hypothesis_id)
            if h:
                h.status, h.conclusion = "rejected", (h.conclusion or "") + " — rejected by user feedback"
            result["replan"] = apply_replan(s, run, f"finding {ins.code} rejected by user", full=False)
            fb.applied = True
        elif kind == "add_context":
            from analystos.context.service import add_entry

            add_entry(s, workspace_id=run.workspace_id, kind="note", name=f"User context ({user.email})", body=text, origin="user")
            fb.applied = True
        from analystos.knowledge.learning import draft_from_feedback

        s.flush()
        draft = draft_from_feedback(s, fb, objective=run.objective,
                                    target=s.get(Insight, target_id) if kind == "reject_finding" and target_id else None)
        if draft is not None:
            result["knowledge_draft"] = draft.id  # P4-K08: corrections and redirects become drafts for review
        emit(run.workspace_id, "feedback.received", {"kind": kind, "text": text[:300], **{k: v for k, v in result.items() if k != "replan"}},
             run_id=run.id, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "feedback.submitted", workspace_id=run.workspace_id, run_id=run.id, details=result, session=s)
        result["feedback_id"] = fb.id
        if run.status == "COMPLETED" and result.get("replan"):
            run.status = "RUNNING"
    if result.get("replan"):
        if was_completed:
            start_run(run_id)
        else:
            signal_run(run_id)
    return result

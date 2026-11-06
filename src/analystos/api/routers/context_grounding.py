"""Mapping studio, grounded resolution and owner-authored golden questions."""
from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision
from analystos.api.serialize import row
from analystos.artifacts.registry import link, save_artifact
from analystos.contracts.grounding import BindingIn, GoldenEvaluateIn, GoldenIn, ResolveIn
from analystos.core.errors import Conflict, InvalidInput
from analystos.core.ids import new_id
from analystos.db.models import AnalysisRun, GoldenQuestion, GroundingEvaluation, RunTask, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope
from analystos.services import context_grounding as svc

router = APIRouter(prefix="/api/workspaces/{workspace_id}/grounding", tags=["context-grounding"])


@router.get("")
def studio(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    domains = svc.domains_for(session, scope)
    mappings = [{**row(b), "mapping_state": svc.binding_state(b.definition, scope)
                 if not b.definition.get("domain") or b.definition["domain"] in domains else "domain_mismatch"}
                for b in svc.scoped_bindings(session, scope)]
    groups = {}
    for mapping in mappings:
        if mapping["mapping_state"] == "resolved":
            groups.setdefault(" ".join(sorted(svc.terms(mapping["term"]))), []).append(mapping)
    for group in groups.values():
        if len({svc.meaning_identity(m["definition"]) for m in group}) > 1:
            for mapping in group:
                mapping["mapping_state"] = "ambiguous"
    known = svc.candidates(session, scope)
    cols = svc.catalog(session, scope)
    linked = {c for k in known for c in k["columns"]}
    return {"mappings": mappings, "catalog": cols, "domains": sorted(domains),
            "available_knowledge": known[:300], "knowledge_limit": 300,
            "coverage": {"columns": len(cols), "linked_columns": len(linked),
                         "unmapped_columns": [c["ref"] for c in cols if c["ref"] not in linked],
                         "note": "Link coverage is not a certification; technical columns may need no business term"}}


@router.post("/bindings")
def create_binding(workspace_id: str, body: BindingIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    return row(svc.save_binding(session, user, workspace_id, body))


@router.put("/bindings/{binding_id}")
def update_binding(workspace_id: str, binding_id: str, body: BindingIn, if_match: str | None = Header(default=None),
                   user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return row(svc.save_binding(session, user, workspace_id, body, binding_id, expected_revision(if_match, required=True)))


@router.post("/resolve")
def resolve_meaning(workspace_id: str, body: ResolveIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "analyst")
    return svc.resolve(session, user, workspace_id, body)


@router.post("/bindings/{binding_id}/check")
def check_mapping(workspace_id: str, binding_id: str, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    return svc.check_binding(session, user, workspace_id, binding_id)


@router.get("/runs/{run_id}")
def run_context(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    run = load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, label="run")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    from analystos.db.models import ModelCall
    tasks = list(session.scalars(select(RunTask).where(RunTask.run_id == run_id, RunTask.key.in_(["context", "hypotheses"]))))
    by_key = {t.key: t.output or {} for t in tasks}
    report = by_key.get("hypotheses", {}).get("grounding") or by_key.get("context", {}).get("grounding") or {}
    # A viewer whose access narrowed must not see the run owner's old mapped context.
    report = {**report, "resolutions": [r for r in report.get("resolutions", [])
                                        if all(svc.allowed_ref(c, scope) for c in r.get("columns", []))]}
    if report.get("meaning_review"):
        review = dict(report["meaning_review"])
        for part in ("questions", "not_asked", "resolved_without_asking"):
            review[part] = [{**q, "columns": [c for c in q.get("columns") or [] if svc.allowed_ref(c, scope)]}
                            for q in review.get(part) or []]
        report["meaning_review"] = review
    calls = list(session.scalars(select(ModelCall).where(ModelCall.workspace_id == workspace_id, ModelCall.run_id == run_id)))
    _, visible_receipts = svc.pinned_prompt_context(run, scope)
    receipt_ids = {r["id"] for r in visible_receipts}
    receipts = [{"purpose": c.purpose, "receipt": r} for c in calls for r in c.context_receipts or []
                if r.get("source") == "reviewed_brief" and r.get("id") in receipt_ids]
    from analystos.services.meaning_review import previous_rounds

    rounds = [{k: r.get(k) for k in ("round", "questions", "asked_at", "rechecked_at", "owner_minutes")}
              for r in previous_rounds(session, run.id)]
    return {"grounding": report, "brief_receipts": receipts, "rounds": rounds,
            "coverage": next(((t.output or {}).get("question_coverage") for t in tasks if t.key == "hypotheses"), None)}


@router.post("/runs/{run_id}/recheck")
def recheck(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "analyst")
    run = load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, label="run")
    session.refresh(run, with_for_update=True)
    tasks = list(session.scalars(select(RunTask).where(RunTask.run_id == run_id).with_for_update()))
    waiting = [t for t in tasks if t.status == "WAITING_USER" and (t.output or {}).get("meaning_questions")]
    if not waiting or run.control != "run" or run.status in {"COMPLETED", "CANCELLED", "REJECTED"}:
        raise Conflict("This run is not waiting for business meaning")
    from analystos.services.meaning_review import record_round

    # The round that was waiting becomes history: the next round asks only what is still open or an answer changed.
    record_round(session, run, waiting[0].output.get("grounding") or {}, user_id=user.id, asked_at=waiting[0].finished_at)
    for task in tasks:
        if task in waiting or task.key == "context":
            task.status, task.output, task.error, task.finished_at = "NEW", {}, None, None
    run.status = "RUNNING"
    emit(workspace_id, "context.meaning_recheck", {"run_id": run.id}, run_id=run.id, session=session)
    audit(f"user:{user.id}", "context.meaning_recheck", workspace_id=workspace_id, target=run.id, decision="allow")
    return {"status": "RUNNING"}


@router.get("/golden")
def golden(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    cases = [row(c) for c in session.scalars(select(GoldenQuestion).where(GoldenQuestion.workspace_id == workspace_id))
             if all(svc.allowed_ref(ref, scope) for ref in c.definition.get("expected_columns", []))]
    evaluations = list(session.scalars(select(GroundingEvaluation).where(GroundingEvaluation.workspace_id == workspace_id,
                                      GroundingEvaluation.reviewer_id == user.id).order_by(GroundingEvaluation.created_at.desc()).limit(10)))
    safe_evaluations = []
    for evaluation in evaluations:
        safe = row(evaluation)
        visible = [c for c in evaluation.result.get("cases", [])
                   if all(svc.allowed_ref(ref, scope) for ref in c.get("actual_columns", []) + c.get("expected", {}).get("expected_columns", []))]
        safe["result"] = {**evaluation.result, "cases": visible, "total": len(visible), "passed": sum(c["passed"] for c in visible)}
        safe_evaluations.append(safe)
    return {"questions": cases, "evaluations": safe_evaluations, "kind": "grounding_evaluation",
            "note": "Checks column grounding and clarification; not numerical-answer accuracy or real analyst pilot results"}


@router.post("/golden")
def create_golden(workspace_id: str, body: GoldenIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    if any(not svc.allowed_ref(c, scope) for c in body.expected_columns):
        raise InvalidInput("Expected columns must be authorized catalog references")
    if not body.expected_columns and not body.expect_clarification and not body.expected_query:
        raise InvalidInput("Choose expected columns, an expected verified query or expect clarification")
    if body.expected_query:
        from analystos.db.models import VerifiedQuery

        vq = session.get(VerifiedQuery, body.expected_query)
        if vq is None or vq.workspace_id != workspace_id:
            raise InvalidInput("The expected verified query is not in this workspace")
    case = GoldenQuestion(id=new_id("gold"), workspace_id=workspace_id, definition=body.model_dump(), created_by=user.id)
    session.add(case)
    session.flush()
    art = save_artifact(session, workspace_id=workspace_id, type_="context_package", name=f"Golden question {case.id}",
                        content=body.model_dump(), creator_user=user.id)
    link(session, workspace_id, ("golden_question", case.id), "defined_by", (art.type, art.id))
    emit(workspace_id, "context.golden_saved", {"id": case.id}, session=session)
    audit(f"user:{user.id}", "context.golden_saved", workspace_id=workspace_id, target=case.id, decision="allow")
    return row(case)


@router.post("/golden/evaluate")
def evaluate(workspace_id: str, body: GoldenEvaluateIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "analyst")
    results = svc.run_golden(session, user, workspace_id, use_model=body.use_model)
    evaluation = GroundingEvaluation(id=new_id("geval"), workspace_id=workspace_id, reviewer_id=user.id,
                                    result={"cases": results, "passed": sum(r["passed"] for r in results), "total": len(results),
                                            "use_model": body.use_model, "kind": "grounding_evaluation"})
    session.add(evaluation)
    session.flush()
    for result in results:
        link(session, workspace_id, ("grounding_evaluation", evaluation.id), "tests", ("golden_question", result["id"]))
    emit(workspace_id, "context.evaluation_saved", {"id": evaluation.id, "total": len(results)}, session=session)
    audit(f"user:{user.id}", "context.evaluation_saved", workspace_id=workspace_id, target=evaluation.id, decision="allow")
    return row(evaluation)

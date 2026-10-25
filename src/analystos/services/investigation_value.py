"""Guided investigations reuse reviewed facts; actions and pilot claims remain human-owned."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.artifacts.registry import link
from analystos.contracts.investigation import GuidedInvestigationIn, InvestigationActionIn, PilotReviewIn
from analystos.core.errors import Conflict, InvalidInput, PreconditionFailed
from analystos.core.ids import new_id
from analystos.db.models import AnalysisRun, Insight, InvestigationAction, PilotReview, User, WorkspaceMember
from analystos.events.bus import emit
from analystos.evidence.verification import insight_states
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope
from analystos.services import brief

TEMPLATES = [
    {"id": "sla_change", "version": 1, "title": "Explain an SLA increase", "description": "Compare breach rates, workload and resolution time; test likely drivers.",
     "question": "Investigate whether SLA breaches increased. Compare breach rates, incident volume and resolution times. Test differences by priority, assignment group and reassignment; separate associations from causes."},
    {"id": "team_comparison", "version": 1, "title": "Compare team performance", "description": "Compare teams while checking workload and priority mix.",
     "question": "Compare assignment-group performance: volume, resolution time and SLA breach rates. Check priority mix and sample sizes before ranking teams. Identify where comparisons are unfair or inconclusive."},
    {"id": "recurring_incidents", "version": 1, "title": "Find recurring incidents", "description": "Find concentration and repeated problems without assuming duplicate records are repeat incidents.",
     "question": "Investigate recurring incidents by business service, application and category. Distinguish repeated incidents from duplicate records. Compare concentration, severity and resolution time; identify evidence gaps."},
    {"id": "data_quality", "version": 1, "title": "Check data quality", "description": "Check completeness, duplicates, timestamps and joins before using the data.",
     "question": "Investigate data quality: missing values, duplicate entity keys, invalid timestamps, inconsistent categories and join coverage. Quantify recorded problems and state which conclusions they prevent."},
]


def reviewed_context(session: Session, user: User, workspace_id: str, source_ids: list[str] | None = None) -> dict:
    scope = resolve_scope(session, session.merge(user), workspace_id, source_ids=source_ids, minimum_role="viewer")
    document = brief.get(session, user, workspace_id)
    facts = visible_facts(document["assertions"], scope)
    return {"version": document["version"], "content_hash": document["content_hash"], "facts": facts,
            "open_questions": sum(a["review_state"] == "suggested" for a in document["assertions"]
                                  if brief.in_scope(a, scope))}


def visible_facts(assertions: list[dict], scope: Any) -> list[dict]:
    def allowed(a: dict) -> bool:
        subject = a.get("subject") or ""
        column = subject.rsplit(".", 1)[-1]
        return (a.get("review_state") in {"reviewed", "validated"} and brief.in_scope(a, scope)
                and subject not in scope.denied_columns and f"*.{column}" not in scope.denied_columns)
    return [a for a in assertions if allowed(a)]


def template_question(body: GuidedInvestigationIn) -> tuple[dict, str]:
    template = next((t for t in TEMPLATES if t["id"] == body.template_id), None)
    if template is None:
        raise InvalidInput("unknown investigation template")
    question = (f"{template['question']} Requested period: {body.period}. Comparison baseline: {body.comparison}. "
                f"Focus: {body.focus}. Decision this should support: {body.decision}. "
                "Use only available authorized data; explicitly report unavailable periods, measures or fields. "
                "Do not invent financial impact or make external changes.")
    return template, question


def start_guided(user: User, workspace_id: str, body: GuidedInvestigationIn) -> AnalysisRun:
    from analystos.db.base import session_scope
    from analystos.services.runs import create_run

    template, objective = template_question(body)
    with session_scope() as session:
        require_role(session, user, workspace_id, "analyst")
        context = reviewed_context(session, user, workspace_id, body.source_ids)
    return create_run(user, workspace_id, objective=objective, source_ids=body.source_ids,
                      origin={"type": "user", "guided_template": {**template, "inputs": body.model_dump(mode="json")},
                              "reviewed_context": context})


def action_list(session: Session, user: User, workspace_id: str, run_id: str | None = None) -> list[dict]:
    require_role(session, user, workspace_id, "viewer")
    stmt = select(InvestigationAction).where(InvestigationAction.workspace_id == workspace_id)
    if run_id:
        load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, label="run")
        stmt = stmt.where(InvestigationAction.run_id == run_id)
    from analystos.api.serialize import row

    actions = list(session.scalars(stmt.order_by(InvestigationAction.created_at.desc()).limit(500)))
    states = insight_states(session, [a.insight_id for a in actions])
    return [{**row(a), "verification_state": states[a.insight_id]} for a in actions]


def save_action(session: Session, user: User, workspace_id: str, insight_id: str, body: InvestigationActionIn,
                action_id: str | None = None, revision: int | None = None) -> InvestigationAction:
    insight = load_in_workspace(session, Insight, insight_id, workspace_id, user=user, minimum="analyst", label="finding")
    if body.owner_id and not session.scalar(select(WorkspaceMember).join(User, User.id == WorkspaceMember.user_id)
                                           .where(WorkspaceMember.workspace_id == workspace_id,
                                                  WorkspaceMember.user_id == body.owner_id, User.active.is_(True))):
        raise InvalidInput("choose an active member of this workspace as the owner")
    if body.status == "done":
        if not body.owner_id or not body.resolution_note:
            raise InvalidInput("a completed action needs an owner and a resolution note")
        if not insight.verified or insight.status in {"rejected", "superseded"} or insight_states(session, [insight.id])[insight.id]["badge"] != "verified":
            raise Conflict("the finding is not currently verified; review its evidence before completing this action")
    action = None
    if action_id:
        action = load_in_workspace(session, InvestigationAction, action_id, workspace_id, user=user, minimum="analyst", label="action")
        session.refresh(action, with_for_update=True)
        if action.insight_id != insight_id:
            raise InvalidInput("an action cannot be moved to another finding")
        if action.revision != revision:
            raise PreconditionFailed("this action changed; reload before saving")
    values = body.model_dump(mode="json")
    if action is None:
        action = InvestigationAction(id=new_id("act"), workspace_id=workspace_id, run_id=insight.run_id,
                                     insight_id=insight.id, created_by=user.id, revision=1, **values)
        session.add(action)
        session.flush()
        link(session, workspace_id, ("insight", insight.id), "motivates", ("investigation_action", action.id), run_id=insight.run_id)
        event = "investigation.action_created"
    else:
        for key, value in values.items():
            setattr(action, key, value)
        action.revision += 1
        event = "investigation.action_updated"
    emit(workspace_id, event, {"action_id": action.id, "insight_id": insight.id, "revision": action.revision},
         run_id=insight.run_id, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", event, workspace_id=workspace_id, target=action.id, details={"revision": action.revision}, session=session)
    return action


def save_review(session: Session, user: User, workspace_id: str, body: PilotReviewIn,
                review_id: str | None = None, revision: int | None = None) -> PilotReview:
    run = load_in_workspace(session, AnalysisRun, body.run_id, workspace_id, user=user, minimum="analyst", label="run")
    session.refresh(run, with_for_update=True)
    has_findings = bool(session.scalar(select(Insight.id).where(Insight.run_id == run.id).limit(1)))
    if run.status not in {"COMPLETED", "FAILED", "CANCELLED", "REJECTED"} and not (run.status == "WAITING_USER" and has_findings):
        raise Conflict("evaluate a finished investigation, or one with findings awaiting approval")
    review = None
    if review_id:
        review = load_in_workspace(session, PilotReview, review_id, workspace_id, user=user, minimum="analyst", label="pilot review")
        session.refresh(review, with_for_update=True)
        if review.reviewer_id != user.id:
            raise InvalidInput("only the original reviewer can change their assessment")
        if review.run_id != body.run_id or review.evidence_kind != body.evidence_kind:
            raise InvalidInput("the investigation and assessment type cannot change")
        if review.revision != revision:
            raise PreconditionFailed("this assessment changed; reload before saving")
    else:
        if session.scalar(select(PilotReview.id).where(PilotReview.run_id == run.id, PilotReview.reviewer_id == user.id,
                                                       PilotReview.evidence_kind == body.evidence_kind)):
            raise Conflict("you already assessed this investigation; edit that assessment instead")
    values = body.model_dump()
    if review is None:
        review = PilotReview(id=new_id("prv"), workspace_id=workspace_id, reviewer_id=user.id, revision=1, **values)
        session.add(review)
        session.flush()
        link(session, workspace_id, ("run", run.id), "evaluated_by", ("pilot_review", review.id), run_id=run.id)
    else:
        for key, value in values.items():
            setattr(review, key, value)
        review.revision += 1
    emit(workspace_id, "pilot.review_saved", {"review_id": review.id, "evidence_kind": review.evidence_kind, "revision": review.revision},
         run_id=run.id, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "pilot.review_saved", workspace_id=workspace_id, target=review.id, session=session)
    return review


def summarize_reviews(reviews: list[PilotReview]) -> dict:
    result = {}
    for kind in ("real_pilot", "rehearsal"):
        group = [r for r in reviews if r.evidence_kind == kind]
        reviewed = [r for r in group if r.accuracy != "not_reviewed"]
        baseline = sum(r.baseline_minutes for r in group)
        saved = sum(r.baseline_minutes - r.assisted_minutes for r in group)
        result[kind] = {"assessments": len(group), "reviewers": len({r.reviewer_id for r in group}),
                        "investigations": len({r.run_id for r in group}), "minutes_saved": saved,
                        "time_saved_fraction": saved / baseline if baseline else None,
                        "accuracy_reviewed": len(reviewed),
                        "correct_fraction": sum(r.accuracy == "correct" for r in reviewed) / len(reviewed) if reviewed else None,
                        "average_usefulness": sum(r.usefulness for r in group) / len(group) if group else None,
                        "would_use_again_fraction": sum(r.would_use_again for r in group) / len(group) if group else None}
    return result

"""Guided investigations reuse reviewed facts; actions and pilot claims remain human-owned."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.artifacts.registry import link
from analystos.contracts.investigation import (
    PILOT_SUBJECTS,
    GuidedInvestigationIn,
    InvestigationActionIn,
    PilotReviewIn,
)
from analystos.core.errors import Conflict, InvalidInput, NotFound, PreconditionFailed
from analystos.core.ids import new_id
from analystos.db.models import (
    AnalysisRun,
    Artifact,
    AskThread,
    AskTurn,
    Insight,
    InvestigationAction,
    PilotReview,
    Schedule,
    ScheduleRun,
    User,
    WorkspaceMember,
)
from analystos.events.bus import emit
from analystos.evidence.verification import insight_states
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.services import brief

# Domain-neutral guided investigations; domain packs add their own (`investigations:` in the pack's templates).
CORE_TEMPLATES = [
    {"id": "data_quality", "version": 1, "title": "Check data quality", "description": "Check completeness, duplicates, timestamps and joins before using the data.",
     "question": "Investigate data quality: missing values, duplicate entity keys, invalid timestamps, inconsistent categories and join coverage. Quantify recorded problems and state which conclusions they prevent."},
]


def templates(pack_list=None) -> list[dict]:
    """Guided investigation templates: the core ones, then each pack's (every installed pack by default;
    pass `packs.for_workspace(...)` for the ones that fit a workspace)."""
    from analystos.capabilities import packs

    out = list(CORE_TEMPLATES)
    seen = {t["id"] for t in out}
    for pack in packs.installed() if pack_list is None else pack_list:
        for t in pack.templates.get("investigations") or []:
            if t.get("id") and t["id"] not in seen:
                out.append({**t, "pack": pack.id})
                seen.add(t["id"])
    return out


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


def _ranges(body: GuidedInvestigationIn) -> tuple[tuple, tuple | None]:
    from analystos.services import periods

    current = (body.period.start, body.period.end)
    custom = (body.baseline.start, body.baseline.end) if body.baseline else None
    return current, periods.baseline_for(*current, body.comparison_mode, custom)


def template_question(body: GuidedInvestigationIn) -> tuple[dict, str]:
    from analystos.services import periods

    template = next((t for t in templates() if t["id"] == body.template_id), None)
    if template is None:
        raise InvalidInput("unknown investigation template")
    current, baseline = _ranges(body)
    names = periods.labels(current, *([baseline] if baseline else []))

    def said(rng: tuple, name: str) -> str:
        # Canonical label and ISO dates only: the reader's own label ("September 2026") stays in the constraint, so
        # nothing reads the dates back out of words. The platform applies them as filters (`services.periods`).
        return f"{name} ({rng[0].isoformat()} to {rng[1].isoformat()})"

    compare = f"Comparison baseline: {said(baseline, names[1])}." if baseline else "No comparison baseline."
    question = (f"{template['question']} Requested period: {said(current, names[0])}, applied as a date filter. {compare} "
                f"Focus: {body.focus}. Decision this should support: {body.decision}. "
                "Use only available authorized data; explicitly report unavailable periods, measures or fields. "
                "Do not invent financial impact or make external changes.")
    return template, question


def guided_sources(session: Session, user: User, workspace_id: str, template: dict, body: GuidedInvestigationIn) -> list[str] | None:
    """The source a guided investigation reads. The caller's choice when given; else the only source; else the
    one source whose tables the template's domain pack fits (with a date column for the period). Asks only when
    that still leaves several, or none."""
    from analystos.capabilities import packs
    from analystos.services import periods

    if body.source_ids:
        return body.source_ids
    scope = resolve_scope(session, session.merge(user), workspace_id)
    by_source: dict[str, list[str]] = {}
    for asset, source in scope.asset_sources.items():
        by_source.setdefault(source, []).append(asset)
    if len(by_source) <= 1:
        return None
    pack = next((p for p in packs.installed() if p.id == template.get("pack")), None)
    fitting = list(by_source)
    if pack is not None:
        fitting = [s for s in by_source if pack.applies_to([a.rsplit(".", 1)[-1] for a in by_source[s]],
                                                            [c for a in by_source[s] for c in scope.columns.get(a) or []])]
    if len(fitting) > 1:
        hints = packs.hints().event_start
        cols = periods.time_columns(session, scope, hints)
        dated = [s for s in fitting if any(cols.get(a, {}).get("column") for a in by_source[s])]
        fitting = dated or fitting
    if len(fitting) == 1:
        return fitting
    from analystos.db.models import Source

    names = sorted(str(session.get(Source, s).name if session.get(Source, s) else s) for s in (fitting or by_source))
    raise InvalidInput(f"this workspace has several sources that fit '{template['title']}' ({', '.join(names)}): "
                       "choose which one to read")


def start_guided(user: User, workspace_id: str, body: GuidedInvestigationIn) -> AnalysisRun:
    """The period and baseline become `run.constraints["period"]` (plan-hash bound; `services.periods`): every test
    reads only the requested period, and comparison tests read baseline and period."""
    from analystos.capabilities import packs
    from analystos.db.base import session_scope
    from analystos.services import periods
    from analystos.services.runs import start_run_request

    template, objective = template_question(body)
    current, baseline = _ranges(body)
    with session_scope() as session:
        require_role(session, user, workspace_id, "analyst")
        source_ids = guided_sources(session, user, workspace_id, template, body)
        context = reviewed_context(session, user, workspace_id, source_ids)
        scope = resolve_scope(session, session.merge(user), workspace_id, source_ids=source_ids)
        columns = periods.time_columns(session, scope, packs.hints().event_start)
    if scope.assets and not any(c.get("column") for c in columns.values()):
        raise InvalidInput("none of the selected tables has a date column, so the requested period cannot be applied")
    period = periods.constraint(*current, mode=body.comparison_mode, baseline=baseline, columns=columns,
                                period_label=body.period.label, baseline_label=body.baseline.label if body.baseline else None)
    inputs = body.model_dump(mode="json") | {"source_ids": source_ids}
    run, _ = start_run_request(user, workspace_id, objective=objective, source_ids=source_ids,
                               origin={"type": "user", "guided_template": {**template, "inputs": inputs},
                                       "reviewed_context": context},
                               constraints={"period": period})
    return run


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


@scoped_loader
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


FINISHED_RUN = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED"}
# Builder dashboards and report documents are artifacts; lineage keys them by their artifact type.
ARTIFACT_SUBJECTS = {"dashboard": "dashboard", "report": "report_document"}


def _pilot_subject(session: Session, user: User, workspace_id: str, body: PilotReviewIn) -> tuple[str, str]:
    """Load what the assessment measures (analyst role, this workspace) and return its lineage node.
    Failures and refusals count: only work still in progress is refused, so the score is not survivor-biased."""
    kind, ref = body.subject_type, body.subject_id
    if kind == "investigation":
        run = load_in_workspace(session, AnalysisRun, body.run_id, workspace_id, user=user, minimum="analyst", label="run")
        session.refresh(run, with_for_update=True)
        has_findings = bool(session.scalar(select(Insight.id).where(Insight.run_id == run.id).limit(1)))
        if run.status not in FINISHED_RUN and not (run.status == "WAITING_USER" and has_findings):
            raise Conflict("evaluate a finished investigation, or one with findings awaiting approval")
        return "run", run.id
    if kind == "ask":
        thread = load_in_workspace(session, AskThread, ref, workspace_id, user=user, minimum="analyst", label="conversation")
        if thread.user_id != user.id:  # Ask conversations are private to their creator
            raise NotFound("conversation not found")
        if not session.scalar(select(AskTurn.id).where(AskTurn.thread_id == thread.id, AskTurn.status != "running").limit(1)):
            raise Conflict("evaluate a conversation with at least one answered, refused or clarified question")
        return "ask_thread", thread.id
    if kind in ARTIFACT_SUBJECTS:
        label = "dashboard" if kind == "dashboard" else "report"
        art = load_in_workspace(session, Artifact, ref, workspace_id, user=user, minimum="analyst", label=label)
        if art.type != ARTIFACT_SUBJECTS[kind]:
            raise NotFound(f"{label} not found")
        return art.type, art.id
    schedule = load_in_workspace(session, Schedule, ref, workspace_id, user=user, minimum="analyst", label="schedule")
    if not session.scalar(select(ScheduleRun.id).where(ScheduleRun.schedule_id == schedule.id,
                                                       ScheduleRun.status.in_(("succeeded", "failed", "skipped"))).limit(1)):
        raise Conflict("evaluate a scheduled digest after it has run at least once (Run now is fine)")
    return "schedule", schedule.id


@scoped_loader
def save_review(session: Session, user: User, workspace_id: str, body: PilotReviewIn,
                review_id: str | None = None, revision: int | None = None) -> PilotReview:
    node = _pilot_subject(session, user, workspace_id, body)
    review = None
    if review_id:
        review = load_in_workspace(session, PilotReview, review_id, workspace_id, user=user, minimum="analyst", label="pilot review")
        session.refresh(review, with_for_update=True)
        if review.reviewer_id != user.id:
            raise InvalidInput("only the original reviewer can change their assessment")
        if (review.subject_type, review.subject_id, review.evidence_kind) != (body.subject_type, body.subject_id, body.evidence_kind):
            raise InvalidInput("what was assessed and the assessment type cannot change")
        if review.revision != revision:
            raise PreconditionFailed("this assessment changed; reload before saving")
    else:
        same = select(PilotReview.id).where(PilotReview.workspace_id == workspace_id, PilotReview.subject_type == body.subject_type,
                                            PilotReview.subject_id == body.subject_id, PilotReview.reviewer_id == user.id,
                                            PilotReview.evidence_kind == body.evidence_kind)
        if body.subject_type != "investigation":  # one conversation or dashboard may serve several task cards
            same = same.where(PilotReview.task == body.task)
        if session.scalar(same):
            raise Conflict("you already assessed this; edit that assessment instead")
    values = body.model_dump()
    if review is None:
        review = PilotReview(id=new_id("prv"), workspace_id=workspace_id, reviewer_id=user.id, revision=1, **values)
        session.add(review)
        session.flush()
        link(session, workspace_id, node, "evaluated_by", ("pilot_review", review.id), run_id=body.run_id)
    else:
        for key, value in values.items():
            setattr(review, key, value)
        review.revision += 1
    emit(workspace_id, "pilot.review_saved", {"review_id": review.id, "evidence_kind": review.evidence_kind,
                                              "subject_type": review.subject_type, "revision": review.revision},
         run_id=body.run_id, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "pilot.review_saved", workspace_id=workspace_id, target=review.id, session=session)
    return review


def pilot_subjects(session: Session, user: User, workspace_id: str, subject_type: str, limit: int = 100) -> list[dict]:
    """What an analyst may assess, newest first: {id, label, status}. Ask conversations are the caller's own."""
    require_role(session, user, workspace_id, "analyst")
    if subject_type == "investigation":
        runs = session.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace_id)
                               .order_by(AnalysisRun.created_at.desc()).limit(limit))
        return [{"id": r.id, "label": r.objective, "status": r.status.lower()} for r in runs]
    if subject_type == "ask":
        threads = list(session.scalars(select(AskThread).where(AskThread.workspace_id == workspace_id, AskThread.user_id == user.id,
                                                               AskThread.archived.is_(False))
                                       .order_by(AskThread.updated_at.desc()).limit(limit)))
        counts = dict(session.execute(select(AskTurn.thread_id, func.count()).where(AskTurn.thread_id.in_([t.id for t in threads]))
                                      .group_by(AskTurn.thread_id)).all()) if threads else {}
        return [{"id": t.id, "label": t.title, "status": f"{counts.get(t.id, 0)} questions"} for t in threads]
    if subject_type in ARTIFACT_SUBJECTS:
        arts = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id,
                                                      Artifact.type == ARTIFACT_SUBJECTS[subject_type])
                               .order_by(Artifact.updated_at.desc()).limit(limit))
        return [{"id": a.id, "label": str((a.content or {}).get("title") or a.name), "status": f"version {a.version}, {a.status}"}
                for a in arts]
    if subject_type == "digest":
        schedules = list(session.scalars(select(Schedule).where(Schedule.workspace_id == workspace_id)
                                         .order_by(Schedule.created_at.desc()).limit(limit)))
        last = {sid: status for sid, status in session.execute(
            select(ScheduleRun.schedule_id, ScheduleRun.status).where(ScheduleRun.schedule_id.in_([x.id for x in schedules]))
            .order_by(ScheduleRun.started_at))} if schedules else {}
        return [{"id": x.id, "label": x.name, "status": f"last run {last[x.id]}" if x.id in last else "not run yet"}
                for x in schedules]
    raise InvalidInput("unknown assessment subject")


def _measures(group: list) -> dict:
    reviewed = [r for r in group if r.accuracy != "not_reviewed"]
    baseline = sum(r.baseline_minutes for r in group)
    saved = sum(r.baseline_minutes - r.assisted_minutes for r in group)
    timed_review = [r.review_minutes for r in group if getattr(r, "review_minutes", None) is not None]
    return {"assessments": len(group), "reviewers": len({r.reviewer_id for r in group}),
            "minutes_saved": saved, "time_saved_fraction": saved / baseline if baseline else None,
            "accuracy_reviewed": len(reviewed),
            "correct_fraction": sum(r.accuracy == "correct" for r in reviewed) / len(reviewed) if reviewed else None,
            "average_usefulness": sum(r.usefulness for r in group) / len(group) if group else None,
            "would_use_again_fraction": sum(r.would_use_again for r in group) / len(group) if group else None,
            "review_minutes": sum(timed_review) if timed_review else None,
            "interventions": sum(getattr(r, "interventions", 0) or 0 for r in group)}


def _subject(r) -> str:
    return getattr(r, "subject_type", None) or "investigation"


def summarize_reviews(reviews: list[PilotReview]) -> dict:
    """Real pilot observations and rehearsals never mix; each is also split by what was assessed."""
    result = {}
    for kind in ("real_pilot", "rehearsal"):
        group = [r for r in reviews if r.evidence_kind == kind]
        result[kind] = {**_measures(group),
                        "investigations": len({r.run_id for r in group if r.run_id}),
                        "subjects": len({(_subject(r), getattr(r, "subject_id", None) or r.run_id) for r in group}),
                        "by_feature": {f: _measures(g) for f in PILOT_SUBJECTS if (g := [r for r in group if _subject(r) == f])}}
    return result

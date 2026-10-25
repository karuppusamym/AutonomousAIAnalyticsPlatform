from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from analystos.contracts.investigation import GuidedInvestigationIn, InvestigationActionIn, PilotReviewIn
from analystos.contracts.policy import DataScope
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound, PreconditionFailed
from analystos.db.models import (
    AnalysisRun,
    Insight,
    InvestigationAction,
    LineageEdge,
    PilotReview,
    User,
    VerificationRecord,
    Workspace,
    WorkspaceMember,
)
from analystos.services import investigation_value as svc


@pytest.fixture
def world(sqlite_db):
    with sqlite_db() as s:
        for table in (InvestigationAction.__table__, PilotReview.__table__):
            table.create(s.bind, checkfirst=True)
        s.add_all([User(id=u, email=f"{u}@x", name=u, password_hash="x", active=True, is_admin=False)
                   for u in ("owner", "analyst", "viewer", "other")])
        for ws in ("ws", "foreign"):
            s.add(Workspace(id=ws, name=ws, created_by="owner"))
        for u, role in (("owner", "owner"), ("analyst", "analyst"), ("viewer", "viewer")):
            s.add(WorkspaceMember(workspace_id="ws", user_id=u, role=role))
        s.add(WorkspaceMember(workspace_id="foreign", user_id="other", role="owner"))
        s.add(AnalysisRun(id="run", workspace_id="ws", objective="SLA drivers", status="COMPLETED", requested_by="owner"))
        s.add(AnalysisRun(id="foreign_run", workspace_id="foreign", objective="Private", status="COMPLETED", requested_by="other"))
        for iid, run, ws in (("ins", "run", "ws"), ("foreign_ins", "foreign_run", "foreign")):
            s.add(Insight(id=iid, workspace_id=ws, run_id=run, code="F1", title="Breach association", finding="Recorded association", verified=True))
        s.add(VerificationRecord(id="verification", workspace_id="ws", run_id="run", subject_type="insight", subject_id="ins",
                                 verdict="verified", verifier="test", fingerprint="fingerprint", state="ACTIVE"))
        s.commit()
    return sqlite_db


def action(**patch):
    return InvestigationActionIn(**{"title": "Review assignment patterns", "next_step": "Review routing with the team",
                                   "success_measure": "Compare next month's breach rate", **patch})


def review(**patch):
    return PilotReviewIn(**{"run_id": "run", "evidence_kind": "rehearsal", "task": "Explain SLA differences",
                           "baseline_minutes": 60, "assisted_minutes": 20, "accuracy": "correct", "usefulness": 4,
                           "would_use_again": True, **patch})


def test_each_guided_template_preserves_the_requested_question_and_does_not_invent_impact():
    for t in svc.TEMPLATES:
        template, question = svc.template_question(GuidedInvestigationIn(template_id=t["id"], period="Q3 2026",
                        comparison="Q2 2026", decision="Choose the team to review"))
        assert template["version"] == 1
        assert "Q3 2026" in question and "Q2 2026" in question
        assert "Do not invent financial impact" in question
    with pytest.raises(InvalidInput):
        svc.template_question(GuidedInvestigationIn(template_id="unknown", period="Q3 2026", comparison="Q2 2026", decision="Choose a team"))


def test_reviewed_context_excludes_suggestions_other_assets_and_denied_columns():
    scope = DataScope(workspace_id="ws", user_id="owner", role="owner", assets=["s.incident"],
                      columns={"s.incident": ["priority", "secret"]}, denied_columns=["s.incident.secret", "*.hidden"])
    assertions = [dict(key=str(i), subject=subject, review_state=state) for i, (subject, state) in enumerate([
        ("SLA", "reviewed"), ("s.incident.priority", "validated"), ("s.incident.secret", "reviewed"),
        ("s.incident.hidden", "reviewed"), ("foreign.table", "reviewed"), ("unreviewed", "suggested")])]
    assert [a["key"] for a in svc.visible_facts(assertions, scope)] == ["0", "1"]


def test_actions_preserve_lineage_owner_revision_and_prevent_stale_completion(world):
    with world() as s:
        user = s.get(User, "owner")
        saved = svc.save_action(s, user, "ws", "ins", action(owner_id="analyst"))
        s.commit()
        assert s.scalar(select(LineageEdge).where(LineageEdge.to_id == saved.id)).from_id == "ins"
        updated = svc.save_action(s, user, "ws", "ins", action(owner_id="analyst", status="in_progress"), saved.id, 1)
        s.commit()
        assert updated.revision == 2
        with pytest.raises(PreconditionFailed):
            svc.save_action(s, user, "ws", "ins", action(), saved.id, 1)
        s.get(VerificationRecord, "verification").state = "VOID"
        s.commit()
        with pytest.raises(Conflict):
            svc.save_action(s, user, "ws", "ins", action(owner_id="analyst", status="done", resolution_note="Reviewed with evidence"), saved.id, 2)


def test_only_scoped_active_members_can_own_actions_and_viewers_cannot_write(world):
    with world() as s:
        with pytest.raises(InvalidInput):
            svc.save_action(s, s.get(User, "owner"), "ws", "ins", action(owner_id="other"))
        with pytest.raises(Forbidden):
            svc.save_action(s, s.get(User, "viewer"), "ws", "ins", action())
        with pytest.raises(NotFound):
            svc.save_action(s, s.get(User, "owner"), "ws", "foreign_ins", action())
        with pytest.raises(InvalidInput):
            svc.save_action(s, s.get(User, "owner"), "ws", "ins", action(status="done"))


def test_pilot_measurements_do_not_mix_rehearsals_with_real_analyst_observations(world):
    with world() as s:
        user = s.get(User, "owner")
        rehearsal = svc.save_review(s, user, "ws", review())
        s.commit()
        result = svc.summarize_reviews([rehearsal])
        assert result["real_pilot"]["assessments"] == 0
        assert result["real_pilot"]["correct_fraction"] is None
        assert result["rehearsal"]["minutes_saved"] == 40
        with pytest.raises(Conflict):
            svc.save_review(s, user, "ws", review())
        with pytest.raises(InvalidInput):
            svc.save_review(s, s.get(User, "analyst"), "ws", review(), rehearsal.id, 1)
        svc.save_review(s, user, "ws", review(assisted_minutes=75, accuracy="not_reviewed"), rehearsal.id, 1)
        s.commit()
        result = svc.summarize_reviews([rehearsal])["rehearsal"]
        assert result["minutes_saved"] == -15
        assert result["correct_fraction"] is None
        with pytest.raises(PreconditionFailed):
            svc.save_review(s, user, "ws", review(), rehearsal.id, 1)


@pytest.mark.parametrize("patch", [{"baseline_minutes": 0}, {"assisted_minutes": -1}, {"baseline_minutes": float("nan")},
                                  {"usefulness": 6}, {"evidence_kind": "certified"}])
def test_pilot_contract_refuses_invalid_measurements(patch):
    with pytest.raises(ValidationError):
        review(**patch)


def test_accuracy_denominator_excludes_unreviewed_and_counts_each_reviewer_once():
    records = [SimpleNamespace(**{**review().model_dump(), "reviewer_id": "one"}),
               SimpleNamespace(**{**review(accuracy="not_reviewed").model_dump(), "reviewer_id": "two"})]
    result = svc.summarize_reviews(records)["rehearsal"]
    assert result["accuracy_reviewed"] == 1 and result["correct_fraction"] == 1
    assert result["reviewers"] == 2 and result["investigations"] == 1


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "REJECTED"])
def test_pilot_can_record_no_result_and_failed_runs_without_survivorship_bias(world, status):
    with world() as s:
        s.get(AnalysisRun, "run").status = status
        s.delete(s.get(Insight, "ins"))
        s.commit()
        saved = svc.save_review(s, s.get(User, "owner"), "ws", review(accuracy="incorrect", would_use_again=False))
        assert saved.accuracy == "incorrect"


def test_a_plan_awaiting_approval_is_not_a_finished_pilot_task(world):
    with world() as s:
        s.get(AnalysisRun, "run").status = "WAITING_USER"
        s.delete(s.get(Insight, "ins"))
        s.commit()
        with pytest.raises(Conflict):
            svc.save_review(s, s.get(User, "owner"), "ws", review())

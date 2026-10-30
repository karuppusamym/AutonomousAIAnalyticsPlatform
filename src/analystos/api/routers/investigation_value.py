from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision
from analystos.api.serialize import row, rows
from analystos.contracts.investigation import GuidedInvestigationIn, InvestigationActionIn, PilotReviewIn
from analystos.db.models import PilotReview, User
from analystos.governance.policy import require_role
from analystos.services import investigation_value as svc

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["investigation value"])


@router.get("/investigation-templates")
def templates(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    from analystos.capabilities import packs

    fitting = packs.for_workspace(session, workspace_id)
    return svc.templates(None if fitting is None else fitting)


@router.get("/reviewed-context")
def context(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return svc.reviewed_context(session, user, workspace_id)


@router.post("/guided-investigations")
def start(workspace_id: str, body: GuidedInvestigationIn, user: User = Depends(current_user)):
    return row(svc.start_guided(user, workspace_id, body))


@router.get("/investigation-actions")
def actions(workspace_id: str, run_id: str | None = None, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return svc.action_list(session, user, workspace_id, run_id)


@router.post("/findings/{insight_id}/actions")
def create_action(workspace_id: str, insight_id: str, body: InvestigationActionIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    return row(svc.save_action(session, user, workspace_id, insight_id, body))


@router.put("/findings/{insight_id}/actions/{action_id}")
def update_action(workspace_id: str, insight_id: str, action_id: str, body: InvestigationActionIn,
                  if_match: str | None = Header(default=None), user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    return row(svc.save_action(session, user, workspace_id, insight_id, body, action_id,
                               expected_revision(if_match, required=True)))


@router.get("/pilot-reviews")
def reviews(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    data = list(session.scalars(select(PilotReview).where(PilotReview.workspace_id == workspace_id)
                               .order_by(PilotReview.created_at.desc()).limit(1000)))
    return {"reviews": rows(data), "summary": svc.summarize_reviews(data), "self_reported": True,
            "limit": 1000, "at_limit": len(data) == 1000}


@router.get("/pilot-reviews/subjects")
def review_subjects(workspace_id: str, subject_type: str = "investigation", user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    """What the caller may assess for a pilot task card: investigations, own Ask conversations, dashboards,
    report documents or scheduled digests."""
    return svc.pilot_subjects(session, user, workspace_id, subject_type)


@router.post("/pilot-reviews")
def create_review(workspace_id: str, body: PilotReviewIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    return row(svc.save_review(session, user, workspace_id, body))


@router.put("/pilot-reviews/{review_id}")
def update_review(workspace_id: str, review_id: str, body: PilotReviewIn, if_match: str | None = Header(default=None),
                  user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return row(svc.save_review(session, user, workspace_id, body, review_id, expected_revision(if_match, required=True)))

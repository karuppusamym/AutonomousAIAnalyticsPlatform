from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows, standing, with_verification
from analystos.artifacts.registry import lineage_for, run_lineage
from analystos.contracts.policy import ApprovalSubject, ApprovalView
from analystos.core.errors import Conflict, InvalidInput
from analystos.core.ids import utcnow
from analystos.db.models import (
    AnalysisRun,
    Approval,
    Artifact,
    ArtifactVersion,
    Insight,
    Publication,
    QueryExecution,
    User,
)
from analystos.governance import approvals as approval_svc
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.semantic import service as semantic_svc
from analystos.workflows.orchestrator import signal_run

router = APIRouter(prefix="/api", tags=["artifacts"])


class ArtifactDecision(BaseModel):
    reason: str | None = None


INTERNAL_ARTIFACT_TYPES = ("step_result",)


@router.get("/workspaces/{workspace_id}/artifacts")
def list_artifacts(workspace_id: str, type: str | None = None, run_id: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Artifact).where(Artifact.workspace_id == workspace_id)
    # a Data Thread step's stored result belongs to its step, not to the Outputs list (ask for it by type)
    stmt = stmt.where(Artifact.type == type) if type else stmt.where(Artifact.type.not_in(INTERNAL_ARTIFACT_TYPES))
    if run_id:
        stmt = stmt.where(Artifact.run_id == run_id)
    return rows(session.scalars(stmt.order_by(Artifact.created_at.desc()).limit(500)))


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = load_in_workspace(session, Artifact, artifact_id, user=user, label="artifact")
    versions = session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id).order_by(ArtifactVersion.version))
    return {**row(art), "versions": rows(versions, exclude={"content"}),
            "lineage": lineage_for(session, art.workspace_id, (art.type, art.id))}


@router.get("/workspaces/{workspace_id}/lineage")
def lineage(workspace_id: str, node_type: str | None = None, node_id: str | None = None, run_id: str | None = None,
            user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Provenance around one node (`node_type` + `node_id`), or every edge one run recorded (`run_id`)."""
    require_role(session, user, workspace_id, "viewer")
    if run_id:
        return run_lineage(session, workspace_id, run_id)
    if not (node_type and node_id):
        raise InvalidInput("pass node_type and node_id, or run_id")
    return lineage_for(session, workspace_id, (node_type, node_id))


@router.get("/queries/{query_id}")
def get_query(query_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    q = load_in_workspace(session, QueryExecution, query_id, user=user, label="query")
    return row(q)


@router.get("/workspaces/{workspace_id}/insights")
def insights(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return with_verification(session, session.scalars(select(Insight).where(Insight.workspace_id == workspace_id,
                                                                             Insight.status != "superseded")
                                                       .order_by(Insight.created_at.desc())))


@router.get("/insights/{insight_id}")
def get_insight(insight_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Evidence behind a finding (DoD #20): experiments, queries (with SQL and result preview), lineage."""
    ins = load_in_workspace(session, Insight, insight_id, user=user, label="insight")
    q_ids = [e["id"] for e in ins.evidence if e.get("type") == "query"]
    from analystos.db.models import Experiment
    from analystos.evidence.verification import insight_view

    return {**row(ins), "verification_state": insight_view(session, ins), **standing(ins.evidence_bundle),
            "queries": rows(session.scalars(select(QueryExecution).where(
                QueryExecution.id.in_(q_ids), QueryExecution.workspace_id == ins.workspace_id))),
            "experiments": rows(session.scalars(select(Experiment).where(Experiment.hypothesis_id == ins.hypothesis_id))),
            "lineage": lineage_for(session, ins.workspace_id, ("insight", ins.id), depth=3)}


# ---------------------------------------------------------------------------------- approvals
@router.get("/workspaces/{workspace_id}/approvals")
def list_approvals(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Approval).where(Approval.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Approval.status == status)
    return rows(session.scalars(stmt.order_by(Approval.created_at.desc())))


@router.get("/approvals/{approval_id}", response_model=ApprovalView)
def get_approval(approval_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """One approval's status for anyone who sees the workspace's approvals inbox (a requester is a member);
    anyone else, including a requester who has left the workspace, gets the same 404 as an unknown id.
    Read only: no decision is possible here."""
    approval = load_in_workspace(session, Approval, approval_id, user=user, label="approval")
    # `expired` is persisted only when someone tries to act; the view says so as soon as it is true
    expires = approval.expires_at if approval.expires_at.tzinfo else approval.expires_at.replace(tzinfo=UTC)  # SQLite drops the zone
    status = "expired" if approval.status == "pending" and expires < utcnow() else approval.status

    def iso(d):  # noqa: ANN001, ANN202
        return d.isoformat() if d else None

    return ApprovalView(id=approval.id, workspace_id=approval.workspace_id, kind=approval.action, status=status,
                        risk_tier=approval.risk_tier, subject=ApprovalSubject(run_id=approval.run_id,
                                                                              destination=approval.destination,
                                                                              affected_assets=list(approval.affected_assets or [])),
                        payload_hash=approval.payload_hash, plan_hash=approval.plan_hash,
                        policy_version=approval.policy_version, requested_by=approval.requested_by,
                        decided_by=approval.decided_by, reason=approval.reason, created_at=iso(approval.created_at),
                        decided_at=iso(approval.decided_at), expires_at=iso(approval.expires_at))


@scoped_loader
def _decide(approval_id: str, user: User, session: Session, approve: bool, reason: str | None):
    pending = load_in_workspace(session, Approval, approval_id, user=user, label="approval")
    from analystos.semantic import ownership

    if pending.action == ownership.ACTION:  # P4-05: the named new owner accepts; an approver is not the recipient
        raise InvalidInput("an ownership transfer is accepted or declined by its recipient "
                           "(POST /api/workspaces/{workspace_id}/semantic/ownership/{approval_id}/accept)")
    approval = approval_svc.decide(session, approval_id, session.merge(user), approve=approve, reason=reason)
    if approval.action == semantic_svc.APPROVAL_ACTION:  # a KPI decided from the approvals inbox takes effect now
        semantic_svc.apply_decision(session, approval)
    else:
        from analystos.semantic import review

        if approval.action == review.MODEL_APPROVAL_ACTION:  # P4-05: a structure version
            review.apply_model_decision(session, approval)
        elif approval.action == review.RELATIONSHIP_APPROVAL_ACTION:  # P7-09: a measured relationship
            review.apply_candidate_decision(session, approval)
        else:
            from analystos.services import deliveries
            from analystos.triggers import packs, rules

            if approval.action == deliveries.APPROVAL_ACTION:  # N-3: an external destination's authorization
                deliveries.apply_decision(session, approval)
            elif approval.action == rules.APPROVAL_ACTION:  # S09: a monitor rule's standing approval
                rules.apply_decision(session, approval)
            elif approval.action == packs.REVIEW_ACTION:  # S09-T4: a recurring pack's brief, reviewed
                packs.apply_review(session, approval)
            else:
                from analystos.autonomy import playbook_signing

                if approval.action == playbook_signing.ACTION:  # S06-T6: a playbook draft's signature sign-off
                    playbook_signing.apply_decision(session, approval)
    session.flush()
    result = row(approval, exclude={"payload"})
    run_id = approval.run_id
    session.commit()
    if run_id:
        signal_run(run_id)
    return result


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: ArtifactDecision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return _decide(approval_id, user, session, True, body.reason if body else None)


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, body: ArtifactDecision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return _decide(approval_id, user, session, False, body.reason if body else None)


def _pending_for_artifact(session: Session, art: Artifact) -> Approval:
    approval = session.scalar(select(Approval).where(Approval.run_id == art.run_id, Approval.action == "publish_dashboard",
                                                     Approval.status == "pending").order_by(Approval.created_at.desc()))
    if approval is None:
        raise InvalidInput("no pending publication proposal for this artifact's run")
    return approval


@router.post("/artifacts/{artifact_id}/publish")
def publish_artifact(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Publishing is proposal-based: returns the pending, hash-bound proposal that includes this artifact."""
    art = load_in_workspace(session, Artifact, artifact_id, user=user, minimum="editor", label="artifact")
    return row(_pending_for_artifact(session, art), exclude={"payload"})


@router.post("/artifacts/{artifact_id}/approve")
def approve_artifact(artifact_id: str, body: ArtifactDecision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = load_in_workspace(session, Artifact, artifact_id, user=user, label="artifact")
    return _decide(_pending_for_artifact(session, art).id, user, session, True, body.reason if body else None)


# ---------------------------------------------------------------------------------- dashboards
@router.post("/workspaces/{workspace_id}/dashboards")
def create_dashboards(workspace_id: str, body: dict, user: User = Depends(current_user)):
    """Dashboards are produced by an analysis run (design -> approval -> publish). This starts one."""
    from analystos.services.runs import create_run

    run = create_run(user, workspace_id, objective=body.get("objective"), source_ids=body.get("source_ids"))
    return {"run_id": run.id, "note": "dashboards will be designed by the run and proposed for approval"}


@router.post("/dashboards/{artifact_id}/publish")
def publish_dashboard(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = load_in_workspace(session, Artifact, artifact_id, user=user, minimum="editor", label="artifact")
    return row(_pending_for_artifact(session, art), exclude={"payload"})


@router.post("/dashboards/{artifact_id}/schedule")
def schedule_dashboard(artifact_id: str, user: User = Depends(current_user)):
    raise InvalidInput("create a schedule with POST /api/workspaces/{id}/schedules (kind 'reanalysis' refreshes findings and "
                       "reports; publication of a refreshed dashboard always goes through a new approval)")


@router.post("/publications/{publication_id}/rollback")
def rollback(publication_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.core.config import get_settings
    from analystos.governance.audit import audit
    from analystos.publishing.base import get_publisher

    pub = load_in_workspace(session, Publication, publication_id, user=user, minimum="editor", label="publication",
                            for_update=True)
    if pub.status == "rolled_back":
        raise Conflict(f"publication {pub.id} is already rolled back")
    if pub.status not in ("succeeded", "partial"):
        raise Conflict(f"publication {pub.id} is {pub.status}; only a succeeded or partial publication can be rolled back")
    removed = get_publisher(pub.destination, get_settings()).rollback(pub.external_ids)
    pub.status = "rolled_back"
    if pub.artifact_id:  # S08 builder dashboard: the artifact this publication published
        published = select(Artifact).where(Artifact.id == pub.artifact_id, Artifact.status == "published")
    elif pub.run_id:
        published = select(Artifact).where(Artifact.run_id == pub.run_id, Artifact.status == "published")
    else:  # an Ask answer publication: only the charts this publication placed (never every run-less artifact)
        charts = [str(v) for v in ((pub.external_ids or {}).get("charts") or {}).values()]
        published = select(Artifact).where(Artifact.workspace_id == pub.workspace_id, Artifact.run_id.is_(None),
                                           Artifact.platform == pub.destination, Artifact.external_id.in_(charts),
                                           Artifact.status == "published")
    for art in session.scalars(published):
        art.status = "rolled_back"
    audit(f"user:{user.id}", "publication.rolled_back", workspace_id=pub.workspace_id, target=pub.id, details={"removed": removed},
          session=session)
    run = session.get(AnalysisRun, pub.run_id) if pub.run_id else None
    return {"removed": removed, "run_id": run.id if run else None}

"""Dashboard builder API v2 (spec S08): typed ops, conversation, versions, publish lifecycle, embedding, exports.
Every path is under /api/v2 (the web client's API base):

    GET  schemas/{chart|dashboard}                      the DSL JSON Schemas
    GET  workspaces/{ws}/dashboards                     builder dashboards
    POST workspaces/{ws}/dashboards                     {document} -> version 1 (editor)
    GET  dashboards/{id}                                current document and status
    POST dashboards/{id}/ops                            {ops, base_version} (If-Match: version)
    POST dashboards/{id}/messages                       {text, focus, base_version, apply}: dashboard_design ladder
    POST dashboards/{id}/revert                         {to_version, base_version}: undo = new version N+1
    GET  dashboards/{id}/versions[/{n}]                 history with each version's op diff
    POST dashboards/{id}/preview | validate             lifecycle steps
    POST dashboards/{id}/publish-request                {destination, audience} -> hash-bound approval
    POST dashboards/{id}/publish                        {approval_id}: verify, then publish (single use)
    POST dashboards/{id}/embed-token                    Superset guest token after an authorization recheck
    POST dashboards/{id}/exports                        {format} -> allowed | approval_required
    GET  dashboards/{id}/exports/{format}?approval_id=  the watermarked file
    POST workspaces/{ws}/publications/reconcile         {repair} (editor): drift report, optional repair
    GET  runs/{run}/collections                         an investigation's dashboards and collection requests
    POST runs/{run}/collections/publish-request         {keys, destination} -> approval bound to that selection
    POST collections/publish                            {approval_id}: verify, then publish only that selection
    GET  workspaces/{ws}/dashboard-grouping             existing Superset dashboards and their grouped titles
    POST workspaces/{ws}/dashboard-grouping/request     {dashboard_ids} -> approval bound to the exact renames
    POST workspaces/{ws}/dashboard-grouping/apply       {approval_id}: re-read titles, verify, rename
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.contracts.dashboard_dsl import chart_json_schema, dashboard_json_schema
from analystos.core.errors import InvalidInput, UpstreamUnavailable
from analystos.dashboards import service as dashboard_svc
from analystos.db.models import Artifact, User

router = APIRouter(prefix="/api/v2", tags=["dashboards-v2"])


class DashboardCreateIn(BaseModel):
    document: dict[str, Any]


class DashboardOpsIn(BaseModel):
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    base_version: int | None = None


class DashboardMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    focus: str | None = None
    base_version: int | None = None
    apply: bool = True


class DashboardRevertIn(BaseModel):
    to_version: int = Field(ge=1)
    base_version: int | None = None


class DashboardPublishRequestIn(BaseModel):
    destination: Literal["superset", "preview"] = "superset"
    audience: list[str] = Field(default_factory=lambda: ["role:viewer"])


class DashboardPublishIn(BaseModel):
    approval_id: str


class DashboardExportIn(BaseModel):
    format: Literal["csv", "xlsx", "pdf"]


class PublicationReconcileIn(BaseModel):
    repair: bool = False


class CollectionRequestIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=50)
    destination: Literal["superset", "preview"] | None = None


class CollectionPublishIn(BaseModel):
    approval_id: str


class GroupingRequestIn(BaseModel):
    dashboard_ids: list[int] = Field(min_length=1, max_length=200)


class GroupingApplyIn(BaseModel):
    approval_id: str


def _base(body_version: int | None, if_match: str | None) -> int | None:
    if if_match is None:
        return body_version
    try:
        return int(if_match.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError:
        raise InvalidInput("If-Match must be the dashboard version number") from None


@router.get("/schemas/{kind}")
def schema(kind: Literal["chart", "dashboard"], user: User = Depends(current_user)):
    return chart_json_schema() if kind == "chart" else dashboard_json_schema()


@router.get("/workspaces/{workspace_id}/dashboards")
def list_dashboards(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.governance.policy import require_role

    require_role(session, user, workspace_id, "viewer")
    rows = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "dashboard",
                                                  Artifact.run_id.is_(None)).order_by(Artifact.updated_at.desc()).limit(200))
    return [{k: v for k, v in dashboard_svc.view(a).items() if k != "document"} | {"title": (a.content or {}).get("title")}
            for a in rows if dashboard_svc.is_builder(a)]


@router.post("/workspaces/{workspace_id}/dashboards", status_code=201)
def create_dashboard(workspace_id: str, body: DashboardCreateIn, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    return dashboard_svc.view(dashboard_svc.create(session, user, workspace_id, body.document))


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return dashboard_svc.view(dashboard_svc.load(session, user, dashboard_id))


@router.post("/dashboards/{dashboard_id}/ops")
def apply_ops(dashboard_id: str, body: DashboardOpsIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
              if_match: str | None = Header(default=None, alias="If-Match")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    return dashboard_svc.apply(session, user, dashboard_id, body.ops, base_version=_base(body.base_version, if_match))


@router.post("/dashboards/{dashboard_id}/messages")
def message(dashboard_id: str, body: DashboardMessageIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
            if_match: str | None = Header(default=None, alias="If-Match")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    return dashboard_svc.converse(session, user, dashboard_id, body.text, focus=body.focus,
                                  base_version=_base(body.base_version, if_match), apply_now=body.apply)


@router.post("/dashboards/{dashboard_id}/revert")
def revert(dashboard_id: str, body: DashboardRevertIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
           if_match: str | None = Header(default=None, alias="If-Match")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    return dashboard_svc.revert(session, user, dashboard_id, body.to_version, base_version=_base(body.base_version, if_match))


@router.get("/dashboards/{dashboard_id}/versions")
def versions(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id)
    return dashboard_svc.history(session, user, dashboard_id)


@router.get("/dashboards/{dashboard_id}/versions/{n}")
def version(dashboard_id: str, n: int, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id)
    return dashboard_svc.version(session, user, dashboard_id, n)


@router.post("/dashboards/{dashboard_id}/preview")
def preview(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    return dashboard_svc.preview(session, user, dashboard_id)


@router.post("/dashboards/{dashboard_id}/validate")
def validate(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    return dashboard_svc.validate(session, user, dashboard_id)


@router.post("/dashboards/{dashboard_id}/publish-request", status_code=202)
def publish_request(dashboard_id: str, body: DashboardPublishRequestIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    approval = dashboard_svc.request_publish(session, user, dashboard_id, destination=body.destination, audience=body.audience)
    return {"approval_id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier,
            "payload_hash": approval.payload_hash, "expires_at": approval.expires_at.isoformat()}


@router.post("/dashboards/{dashboard_id}/publish")
def publish(dashboard_id: str, body: DashboardPublishIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    dashboard_svc.load(session, user, dashboard_id, minimum="editor")
    try:
        return dashboard_svc.execute_publish(session, user, dashboard_id, body.approval_id)
    except UpstreamUnavailable:
        session.commit()  # keep the FAILED publication and the restored approval: a retry reconciles
        raise


@router.post("/dashboards/{dashboard_id}/embed-token")
def embed_token(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.dashboards.access import guest_token

    dashboard_svc.load(session, user, dashboard_id)
    return guest_token(session, user, dashboard_id)


@router.post("/dashboards/{dashboard_id}/exports")
def request_export(dashboard_id: str, body: DashboardExportIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    from analystos.dashboards.exports import request_export as request

    dashboard_svc.load(session, user, dashboard_id)
    return request(session, user, dashboard_id, body.format)


@router.get("/dashboards/{dashboard_id}/exports/{fmt}")
def download_export(dashboard_id: str, fmt: Literal["csv", "xlsx", "pdf"], approval_id: str | None = None,
                    user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.dashboards.exports import export

    dashboard_svc.load(session, user, dashboard_id)
    data, media, name = export(session, user, dashboard_id, fmt, approval_id=approval_id)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                             "Cache-Control": "no-store"})


@router.post("/workspaces/{workspace_id}/publications/reconcile")
def reconcile(workspace_id: str, body: PublicationReconcileIn | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    from analystos.core.config import get_settings
    from analystos.dashboards.reconcile import latest_publications, reconcile_publication
    from analystos.governance.policy import require_role
    from analystos.publishing.base import get_publisher

    require_role(session, user, workspace_id, "editor")
    pubs = latest_publications(session, workspace_id)
    if not pubs:
        return []
    publisher = get_publisher("superset", get_settings())
    return [reconcile_publication(session, p, publisher=publisher, repair=bool(body and body.repair)) for p in pubs]


# ------------------------------------------------------------------- collections and grouping (BI-VIEWS-02)
@router.get("/runs/{run_id}/collections")
def run_collections(run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections
    from analystos.db.models import AnalysisRun
    from analystos.governance.policy import load_in_workspace

    load_in_workspace(session, AnalysisRun, run_id, user=user, label="investigation")
    return collections.overview(session, user, run_id)


@router.post("/runs/{run_id}/collections/publish-request", status_code=202)
def collection_publish_request(run_id: str, body: CollectionRequestIn, user: User = Depends(current_user),
                               session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections
    from analystos.db.models import AnalysisRun
    from analystos.governance.policy import load_in_workspace

    load_in_workspace(session, AnalysisRun, run_id, user=user, minimum="editor", label="investigation")
    approval = collections.request_collection(session, user, run_id, body.keys, destination=body.destination)
    return {"approval_id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier,
            "destination": approval.destination, "payload_hash": approval.payload_hash,
            "collection": list(approval.payload.get("collection") or []), "expires_at": approval.expires_at.isoformat()}


@router.post("/collections/publish")
def collection_publish(body: CollectionPublishIn, user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections

    try:
        return collections.execute_collection(session, user, body.approval_id)
    except UpstreamUnavailable:
        session.commit()  # keep the FAILED publication and the restored approval: a retry reconciles
        raise


@router.get("/workspaces/{workspace_id}/dashboard-grouping")
def dashboard_grouping(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections

    return collections.grouping_plan(session, user, workspace_id)


@router.post("/workspaces/{workspace_id}/dashboard-grouping/request", status_code=202)
def dashboard_grouping_request(workspace_id: str, body: GroupingRequestIn, user: User = Depends(current_user),
                               session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections

    approval = collections.request_grouping(session, user, workspace_id, body.dashboard_ids)
    return {"approval_id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier,
            "payload_hash": approval.payload_hash, "changes": list(approval.payload.get("changes") or []),
            "expires_at": approval.expires_at.isoformat()}


@router.post("/workspaces/{workspace_id}/dashboard-grouping/apply")
def dashboard_grouping_apply(workspace_id: str, body: GroupingApplyIn, user: User = Depends(current_user),
                             session: Session = Depends(db, scope="function")):
    from analystos.dashboards import collections

    try:
        return collections.apply_grouping(session, user, workspace_id, body.approval_id)
    except UpstreamUnavailable:
        session.commit()  # keep the audit of what was renamed (and a restored approval when nothing was)
        raise

"""Report documents and dashboard stories (CONV-04, CONV-05). Every path is under /api/v2:

    GET  schemas/report-document                          the ReportDoc JSON Schema
    GET  workspaces/{ws}/report-documents                 report documents
    POST workspaces/{ws}/report-documents                 {title, objective} -> version 1 (editor)
    GET  workspaces/{ws}/report-content                   verified content the caller may put in a report
    GET  report-documents/{id}                            current document and status
    POST report-documents/{id}/ops                        {ops, base_version} (If-Match: version)
    POST report-documents/{id}/messages                   {text, focus, base_version, apply}: report_design ladder
    POST report-documents/{id}/revert                     {to_version, base_version}: undo = new version N+1
    GET  report-documents/{id}/versions[/{n}]             history with each version's op diff
    GET  report-documents/{id}/preview                    in-platform HTML of the verified snapshot (no file)
    POST report-documents/{id}/exports                    {format} -> approval bound to version + snapshot hash
    GET  report-documents/{id}/exports/{fmt}?approval_id= the file, after verify_for_execution (single use)
    GET  dashboards/{id}/story                            what a builder dashboard shows and what changed
    POST dashboards/{id}/story/report                     {title}: the story as a new report document
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.contracts.report_doc import report_doc_json_schema
from analystos.core.errors import InvalidInput
from analystos.db.models import User
from analystos.services import report_docs as svc

router = APIRouter(prefix="/api/v2", tags=["report-documents"])


class ReportCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=2000)


class ReportOpsIn(BaseModel):
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    base_version: int | None = None


class ReportMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    focus: str | None = None
    base_version: int | None = None
    apply: bool = True


class ReportRevertIn(BaseModel):
    to_version: int = Field(ge=1)
    base_version: int | None = None


class ReportExportIn(BaseModel):
    format: Literal["pdf", "docx", "pptx", "html"]


class StoryReportIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)


def _base(body_version: int | None, if_match: str | None) -> int | None:
    if if_match is None:
        return body_version
    try:
        return int(if_match.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError:
        raise InvalidInput("If-Match must be the report version number") from None


@router.get("/schemas/report-document")
def schema(user: User = Depends(current_user)):
    return report_doc_json_schema()


@router.get("/workspaces/{workspace_id}/report-documents")
def list_documents(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return svc.list_documents(session, user, workspace_id)


@router.post("/workspaces/{workspace_id}/report-documents", status_code=201)
def create_document(workspace_id: str, body: ReportCreateIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    return svc.view(svc.create(session, user, workspace_id, title=body.title, objective=body.objective))


@router.get("/workspaces/{workspace_id}/report-content")
def report_content(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [{"ref": c.ref, "title": c.title, "kind": c.kind, "where": c.where}
            for c in svc.catalog(session, user, workspace_id)]


@router.get("/report-documents/{doc_id}")
def get_document(doc_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return svc.view(svc.load(session, user, doc_id))


@router.post("/report-documents/{doc_id}/ops")
def apply_ops(doc_id: str, body: ReportOpsIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
              if_match: str | None = Header(default=None, alias="If-Match")):
    svc.load(session, user, doc_id, minimum="editor")
    return svc.apply(session, user, doc_id, body.ops, base_version=_base(body.base_version, if_match))


@router.post("/report-documents/{doc_id}/messages")
def message(doc_id: str, body: ReportMessageIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
            if_match: str | None = Header(default=None, alias="If-Match")):
    svc.load(session, user, doc_id, minimum="editor")
    return svc.converse(session, user, doc_id, body.text, focus=body.focus, base_version=_base(body.base_version, if_match),
                        apply_now=body.apply)


@router.post("/report-documents/{doc_id}/revert")
def revert(doc_id: str, body: ReportRevertIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
           if_match: str | None = Header(default=None, alias="If-Match")):
    svc.load(session, user, doc_id, minimum="editor")
    return svc.revert(session, user, doc_id, body.to_version, base_version=_base(body.base_version, if_match))


@router.get("/report-documents/{doc_id}/versions")
def versions(doc_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    svc.load(session, user, doc_id)
    return svc.history(session, user, doc_id)


@router.get("/report-documents/{doc_id}/versions/{n}")
def version(doc_id: str, n: int, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    svc.load(session, user, doc_id)
    return svc.version(session, user, doc_id, n)


@router.get("/report-documents/{doc_id}/preview")
def preview(doc_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    svc.load(session, user, doc_id)
    return svc.preview(session, user, doc_id)


@router.post("/report-documents/{doc_id}/exports", status_code=202)
def request_export(doc_id: str, body: ReportExportIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    svc.load(session, user, doc_id, minimum="editor")
    return svc.request_export(session, user, doc_id, body.format)


@router.get("/report-documents/{doc_id}/exports/{fmt}")
def export(doc_id: str, fmt: Literal["pdf", "docx", "pptx", "html"], approval_id: str | None = Query(default=None),
           user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    svc.load(session, user, doc_id, minimum="editor")
    data, media, filename = svc.export(session, user, doc_id, fmt, approval_id=approval_id)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                                            "Cache-Control": "no-store"})


@router.get("/dashboards/{dashboard_id}/story")
def story(dashboard_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.dashboards import service as dashboards
    from analystos.services import stories

    dashboards.load(session, user, dashboard_id)
    return stories.story(session, user, dashboard_id)


@router.post("/dashboards/{dashboard_id}/story/report", status_code=201)
def story_report(dashboard_id: str, body: StoryReportIn, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    from analystos.dashboards import service as dashboards
    from analystos.services import stories

    dashboards.load(session, user, dashboard_id)
    return svc.view(stories.to_report(session, user, dashboard_id, title=body.title))

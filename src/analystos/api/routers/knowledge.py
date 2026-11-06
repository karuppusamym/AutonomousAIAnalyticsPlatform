"""Knowledge API (P4-K05, K07, K08, U04): the review queue for knowledge drafts, a preview of what the
context compiler would put in a prompt, the context receipts of a run's model calls, and the
knowledge studio — packs, documents (read, human edit -> revision), revision history, import,
export (a download to the caller; a push to a git remote is the K01 hash-bound approval) and the
semantic graph."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.core.errors import InvalidInput
from analystos.db.models import KnowledgePack, ModelCall, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.knowledge import bundle, remote, studio, suggestions

router = APIRouter(prefix="/api", tags=["knowledge"])


class ReviewDecision(BaseModel):
    id: str
    action: Literal["approve", "edit", "reject"]
    fields: dict[str, Any] = Field(default_factory=dict)  # edit: field name -> new value
    reason: str | None = Field(None, max_length=2000)


class ReviewIn(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=200)
    # Required only when the batch makes a passing golden question fail (knowledge/effect.py).
    override_reason: str | None = Field(None, max_length=2000)


class PreviewIn(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=200)


class DocumentSaveIn(BaseModel):
    path: str = Field(min_length=4, max_length=500)
    frontmatter: dict[str, Any]
    body: str = Field("", max_length=256 * 1024)
    base_sha256: str | None = Field(None, description="sha256 of the document as opened; omit to create a new path")
    mark_reviewed: bool = Field(False, description="add your own `verified: human:<you>` entry (the only entry you can add)")
    reason: str | None = Field(None, max_length=500)


class PushIn(BaseModel):
    approval_id: str


class GlossaryScanIn(BaseModel):
    use_model: bool = Field(True, description="let the glossary_suggestion model draft definitions the rules could not "
                                              "(its mode, availability and the workspace policy still decide)")
    include_ask: bool = Field(True, description="also suggest terms from recent unanswered Ask questions")
    include_descriptions: bool = Field(True, description="also ask about undescribed tables, views and columns")


class ContextPreviewIn(BaseModel):
    purpose: str = "hypothesis_generation"
    question: str = Field(min_length=1, max_length=4000)


@router.get("/workspaces/{workspace_id}/knowledge/suggestions")
def list_suggestions(workspace_id: str, status: str | None = "pending", kind: str | None = None, origin: str | None = None,
                     limit: int = 100, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The review queue: drafts with per-field value, confidence and provenance."""
    require_role(session, user, workspace_id, "viewer")
    return suggestions.queue(session, workspace_id, status=status or None, kind=kind, origin=origin, limit=limit)


@router.post("/workspaces/{workspace_id}/knowledge/suggestions/review")
def review_suggestions(workspace_id: str, body: ReviewIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Approve, edit-then-approve or reject drafts in one batch: one workspace-pack revision. A batch that
    makes a passing golden question fail is refused (409 `golden_regression`) unless `override_reason` is set."""
    from analystos.knowledge import effect

    require_role(session, user, workspace_id, "editor")
    return effect.review_checked(session, workspace_id, user, [d.model_dump() for d in body.decisions],
                                 override_reason=body.override_reason)


@router.post("/workspaces/{workspace_id}/knowledge/suggestions/preview")
def preview_suggestions(workspace_id: str, body: PreviewIn, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    """Golden-question scores now and with this batch applied; nothing is changed."""
    from analystos.knowledge import effect

    require_role(session, user, workspace_id, "editor")
    return effect.preview(session, workspace_id, user, [d.model_dump() for d in body.decisions])


@router.get("/workspaces/{workspace_id}/knowledge/suggestions/summary")
def suggestion_summary(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Pending drafts per kind, and `questions`: glossary terms and descriptions a person should answer."""
    from analystos.knowledge import glossary_scan

    require_role(session, user, workspace_id, "viewer")
    return glossary_scan.pending_counts(session, workspace_id)


@router.post("/workspaces/{workspace_id}/knowledge/glossary/scan")
def scan_glossary(workspace_id: str, body: GlossaryScanIn | None = None, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    """Scan the catalog and recent unanswered Ask questions for glossary terms and missing descriptions; queue
    them for review. Rules first; nothing is written to the glossary or the catalog until a person approves."""
    from analystos.core.ids import new_id
    from analystos.governance.audit import audit
    from analystos.knowledge import glossary_scan
    from analystos.runtime.context import default_router

    require_role(session, user, workspace_id, "editor")
    body = body or GlossaryScanIn()
    out = glossary_scan.scan(session, workspace_id, proposed_by=f"user:{user.id}", batch=new_id("gscan"),
                             router=default_router(), use_model=body.use_model, include_ask=body.include_ask,
                             include_descriptions=body.include_descriptions)
    audit(f"user:{user.id}", "knowledge.glossary_scanned", workspace_id=workspace_id, target=workspace_id, decision="allow",
          details={k: v for k, v in out.items() if isinstance(v, int)}, session=session)
    return out


@router.post("/workspaces/{workspace_id}/knowledge/context")
def preview_context(workspace_id: str, body: ContextPreviewIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    """What the context compiler would send for `purpose` about `question` from this workspace's
    knowledge (the catalog section is left out: it depends on a run's scope). No model is called."""
    from analystos.context.compiler import KNOWLEDGE_SECTIONS, compile_context, load_knowledge
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "viewer")
    settings = platform()
    profile = settings.context.profiles.get(body.purpose)
    if profile is None:
        raise InvalidInput(f"no context profile for purpose {body.purpose!r}")
    profile = profile.model_copy(update={"sections": [s for s in profile.sections if s in KNOWLEDGE_SECTIONS]})
    items = load_knowledge(session, workspace_id, profile.sections, query=body.question)
    compiled = compile_context(body.purpose, profile, objective=body.question, required={"question": body.question},
                               knowledge=items, min_relevance=settings.context.min_relevance)
    return {**compiled.summary(), "body": compiled.body}


@router.get("/runs/{run_id}/context-receipts")
def run_context_receipts(run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Per model call of a run: the purpose and the receipts of the context it carried."""
    from analystos.services.runs import get_run_for

    run = get_run_for(session, user, run_id, "viewer")
    calls = session.scalars(select(ModelCall).where(ModelCall.run_id == run.id, ModelCall.workspace_id == run.workspace_id)
                            .order_by(ModelCall.id))
    return [{"model_call_id": c.id, "purpose": c.purpose, "agent_id": c.agent_id, "status": c.status,
             "created_at": c.created_at, "receipts": c.context_receipts} for c in calls if c.context_receipts]


# ------------------------------------------------------------------------------------ studio (P4-U04)
def _can_edit(session: Session, user: User, workspace_id: str) -> bool:
    from analystos.security.auth import role_at_least

    return role_at_least(require_role(session, user, workspace_id, "viewer"), "editor")


@scoped_loader
def _pack(session: Session, user: User, workspace_id: str, pack_id: str, minimum: str = "viewer"):
    """The workspace's own pack or the platform pack (shared), for a caller with `minimum` there."""
    return load_in_workspace(session, KnowledgePack, pack_id, workspace_id, user=user, minimum=minimum,
                             label="knowledge pack", shared=True)


@router.get("/workspaces/{workspace_id}/knowledge/packs")
def list_packs(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The packs this workspace sees; `writable` is true only for its own pack and an editor."""
    return studio.packs(session, workspace_id, can_edit=_can_edit(session, user, workspace_id))


@router.get("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/documents")
def list_documents(workspace_id: str, pack_id: str, revision: int | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    return studio.documents(session, _pack(session, user, workspace_id, pack_id), revision)


@router.get("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document")
def read_document(workspace_id: str, pack_id: str, path: str, revision: int | None = None,
                  user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """One file: text, parsed frontmatter and body, trust fields (tier, verified, status, staleness)."""
    return studio.read(session, _pack(session, user, workspace_id, pack_id), path, revision)


@router.put("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document")
def save_document(workspace_id: str, pack_id: str, body: DocumentSaveIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    """A human edit (editor role, workspace pack only): one new revision, based on `base_sha256`."""
    pack = _pack(session, user, workspace_id, pack_id, "editor")
    return studio.save(session, pack, user, path=body.path, frontmatter=body.frontmatter, body=body.body,
                       base_sha256=body.base_sha256, mark_reviewed=body.mark_reviewed, reason=body.reason)


@router.delete("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document")
def delete_document(workspace_id: str, pack_id: str, path: str, base_sha256: str, reason: str | None = None,
                    user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    pack = _pack(session, user, workspace_id, pack_id, "editor")
    return studio.delete(session, pack, user, path=path, base_sha256=base_sha256, reason=reason)


@router.get("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/revisions")
def list_revisions(workspace_id: str, pack_id: str, path: str | None = None, limit: int = 50,
                   user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Revision history, newest first, with the paths each revision added, changed and removed."""
    return studio.revisions(session, _pack(session, user, workspace_id, pack_id), path=path, limit=limit)


@router.get("/workspaces/{workspace_id}/knowledge/locate")
def locate_document(workspace_id: str, document_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """A context receipt's `document_id` -> pack and path, so a receipt opens its document."""
    require_role(session, user, workspace_id, "viewer")
    return studio.locate(session, workspace_id, document_id)


@router.get("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/export")
def export_pack(workspace_id: str, pack_id: str, revision: int | None = None, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    """The pack as a deterministic OKF zip, returned to the caller (the publish policy must pass).
    A download to the requesting user's browser is not a write outside the platform; pushing to a
    git remote is, and goes through `push/request` -> approval -> `push`."""
    from analystos.governance.audit import audit

    pack = _pack(session, user, workspace_id, pack_id)
    data = bundle.export_zip(session, pack, revision=revision)
    number = revision or pack.head_revision
    audit(f"user:{user.id}", "knowledge.exported", workspace_id=workspace_id, target=pack.id, decision="allow",
          details={"revision": number, "bytes": len(data)}, session=session)
    name = f"{pack.slug}-r{number}.okf.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/workspaces/{workspace_id}/knowledge/import")
async def import_pack(workspace_id: str, file: UploadFile = File(...), slug: str = Form(...),
                      user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """An OKF or Atlas bundle (zip) into a read-only `imported` pack; hostile archives are refused."""
    require_role(session, user, workspace_id, "editor")
    data = await file.read(bundle.ARCHIVE_LIMITS.max_archive_bytes + 1)
    report = bundle.import_bundle(session, workspace_id, data, slug=slug, author=f"user:{user.id}",
                                  reason=f"import {file.filename or 'bundle.zip'}",
                                  origin={"filename": (file.filename or "")[:200], "by": f"user:{user.id}"})
    return report.as_dict()


@router.post("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push/request")
def request_push(workspace_id: str, pack_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Ask for the hash-bound approval a push to the pack's git remote needs (K01)."""
    from analystos.api.serialize import row

    pack = _pack(session, user, workspace_id, pack_id, "editor")
    return row(remote.request_push(session, pack, user))


@router.post("/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push")
def push_pack(workspace_id: str, pack_id: str, body: PushIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Push the head revision under an approved, single-use approval (verified immediately before)."""
    pack = _pack(session, user, workspace_id, pack_id, "editor")
    return remote.push(session, pack, user, body.approval_id)


@router.get("/workspaces/{workspace_id}/knowledge/graph")
def semantic_graph(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Tables, datasets, metrics, documents and pending suggestions; each edge governed or inferred."""
    require_role(session, user, workspace_id, "viewer")
    return studio.graph(session, workspace_id)

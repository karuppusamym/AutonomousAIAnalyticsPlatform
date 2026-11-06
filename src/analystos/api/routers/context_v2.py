"""Context Service REST interface (spec S03 §5, task S03-T4): search, get, compile and the change feed over the
Context Ledger, plus OKF export/import (S03-T6). Every read applies the caller's access before ranking
(`context/retrieval.py`); a workspace the caller cannot see is a 404.

`/lookup` (S03 §5, review P0-2) returns the context of up to ten named assets in one block (YAML or JSON).

Served under `/api/v2/context` (the spec's `/v2/context`): `/api/context/search` already names the v1 search.
An export is built for the requesting user's view and downloadable by that user only; like the workspace context
download it stays inside the platform, so it needs no approval and is audited (`context.exported`).

Federation (S03-T7): `/subscriptions` lets a workspace owner follow another workspace they own (same or another tenant
they belong to); `/feed` is the same change feed for an external subscriber, with the caller's access applied.
Subscribing copies context between workspaces of the platform, like an import, so it is audited rather than approved."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.context import federation, interchange, ledger, retrieval
from analystos.context import lookup as context_lookup
from analystos.core.errors import NotFound, UnsupportedCapability
from analystos.db.models import Artifact, User
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

router = APIRouter(prefix="/api/v2/context", tags=["context"])
EXPORT_FORMAT = "okf"
MAX_IMPORT_BYTES = 64 * 1024 * 1024


def _kinds(kinds: str | None) -> list[str] | None:
    out = [k.strip() for k in (kinds or "").split(",") if k.strip()]
    return out or None


@router.get("/search")
def search(workspace: str, q: str = "", kinds: str | None = None, include_suggested: bool = False,
           limit: int = Query(20, ge=1, le=200), user: User = Depends(current_user),
           session: Session = Depends(db, scope="function")):
    """`[{id, kind, title, summary, trust, version}]`, best first, of what the caller may see."""
    return [h.as_dict() for h in retrieval.search(session, user, workspace, q, kinds=_kinds(kinds),
                                                  include_suggested=include_suggested, limit=limit)]


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str, workspace: str | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """The full entry, its body read from the owner table."""
    return retrieval.get(session, user, entry_id, workspace_id=workspace)


class CompileIn(BaseModel):
    workspace: str
    purpose: str = Field(min_length=1, max_length=80)
    objective: str = Field(default="", max_length=4000)
    budget_tokens: int = Field(default=2000, ge=50, le=200_000)
    kinds: list[str] | None = None
    run_id: str | None = None  # pin this run's reviewed brief as the mandatory first section


@router.post("/compile")
def compile_context(body: CompileIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """`{bundle, receipts, identity_hash}`; 422 when a pinned brief does not fit, 409 on conflicting certified context."""
    out = retrieval.compile_bundle(session, user, body.workspace, purpose=body.purpose, objective=body.objective,
                                   budget_tokens=body.budget_tokens, kinds=body.kinds, run_id=body.run_id)
    audit(f"user:{user.id}", "context.compiled", workspace_id=body.workspace, target=out["identity_hash"][:40],
          decision="allow", details={"purpose": body.purpose, "receipts": len(out["receipts"])}, session=session)
    return out


class LookupIn(BaseModel):
    workspace: str
    assets: list[str] = Field(min_length=1, max_length=context_lookup.MAX_ASSETS)
    format: Literal["yaml", "json"] = "yaml"
    budget_chars: int = Field(default=context_lookup.DEFAULT_BUDGET_CHARS, ge=context_lookup.MIN_BUDGET_CHARS,
                              le=context_lookup.MAX_BUDGET_CHARS)


@router.post("/lookup")
def lookup_context(body: LookupIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """`{content, format, assets, omissions, receipts, identity_hash, budget}` for the named `schema.table` assets
    the caller may see (others are absent and not counted); 422 when the identities alone exceed the budget."""
    out = context_lookup.lookup(session, user, body.workspace, body.assets, format=body.format,
                                budget_chars=body.budget_chars)
    audit(f"user:{user.id}", "context.looked_up", workspace_id=body.workspace, target=out["identity_hash"][:40],
          decision="allow", details={"assets": out["assets"], "omissions": len(out["omissions"]), "format": body.format},
          session=session)
    return out


@router.get("/changes")
def changes(workspace: str, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
            user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Ledger changes after `since` (pass back the returned `cursor`)."""
    return retrieval.changes(session, user, workspace, since=since, limit=limit)


class ExportIn(BaseModel):
    workspace: str
    kinds: list[str] | None = None
    format: Literal["okf", "openlineage", "odcs"] = "okf"


@router.post("/export", status_code=202)
def export_context(body: ExportIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Build the OKF pack of what the caller may see; 202 with a handle the same user downloads."""
    from analystos.artifacts.registry import save_artifact
    if body.format != EXPORT_FORMAT:
        raise UnsupportedCapability(f"context export format {body.format} is not available yet (okf is)",
                                    details={"supported": [EXPORT_FORMAT]})
    pack = interchange.export_pack(session, user, body.workspace, kinds=body.kinds)
    art = save_artifact(session, workspace_id=body.workspace, type_="export", creator_user=user.id,
                        name=f"Context ledger export {pack['digest'][:12]} for {user.id}",
                        content={"format": "okf.context_ledger", "for_user": user.id, "digest": pack["digest"],
                                 "files": {p: b.decode("utf-8") for p, b in pack["files"].items()}})
    audit(f"user:{user.id}", "context.exported", workspace_id=body.workspace, target=art.id, decision="allow",
          details={"format": body.format, "documents": pack["documents"], "omitted": pack["omitted"],
                   "digest": pack["digest"]}, session=session)
    return {"artifact_id": art.id, "status": "ready", "format": body.format, "documents": pack["documents"],
            "omitted": pack["omitted"], "digest": pack["digest"], "download": f"/api/v2/context/exports/{art.id}"}


@scoped_loader
def _export_artifact(session: Session, user: User, artifact_id: str) -> Artifact:
    art = load_in_workspace(session, Artifact, artifact_id, user=user, label="export")
    content = art.content or {}
    if art.type != "export" or content.get("format") != "okf.context_ledger" or content.get("for_user") != user.id:
        raise NotFound("export not found")  # built for another user's view: theirs alone
    return art


@router.get("/exports/{artifact_id}", response_class=Response)
def download_export(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = _export_artifact(session, user, artifact_id)
    files = {p: t.encode("utf-8") for p, t in (art.content.get("files") or {}).items()}
    return Response(interchange.zip_files(files), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="context-ledger-{art.content["digest"][:12]}.okf.zip"'})


@router.post("/import")
async def import_context(workspace: str, file: UploadFile = File(...), user: User = Depends(current_user),
                         session: Session = Depends(db, scope="function")):
    """An OKF pack (zip) into the review queue as suggestions (`source=import:okf`). Never certified by import."""
    require_role(session, user, workspace, "editor")
    data = await file.read(MAX_IMPORT_BYTES + 1)
    return interchange.import_pack(session, user, workspace, data)


class ReconcileIn(BaseModel):
    workspace: str


@router.post("/reconcile")
def reconcile(body: ReconcileIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Catch the workspace's ledger up with its owners (owner role) and report the consistency check."""
    require_role(session, user, body.workspace, "owner")
    counts = ledger.reconcile(session, body.workspace)
    return {"changes": counts, "consistency": ledger.check_consistency(session, body.workspace)}


# ------------------------------------------------------------------------------------------- federation (S03-T7)
@router.get("/feed")
def feed(workspace: str, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), kinds: str | None = None,
         user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The federation feed after `since`: `upsert` items (the full shareable entry, keyed by `key`, with `version`)
    and `remove` items (`key`, `version`). Apply an upsert when its version is newer than yours, a remove when it is
    not older; pass the returned `cursor` back as `since`. Delivery is at least once."""
    access = retrieval.access_for(session, user, workspace)
    return federation.feed(session, access, since=since, limit=limit, kinds=_kinds(kinds))


class SubscribeIn(BaseModel):
    workspace: str
    source_workspace: str
    kinds: list[str] | None = None


@router.get("/subscriptions")
def list_subscriptions(workspace: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return federation.list_subscriptions(session, user, workspace)


@router.post("/subscriptions", status_code=201)
def subscribe(body: SubscribeIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Follow `source_workspace`'s ledger (owner of both). The first sync runs before the reply."""
    sub = federation.subscribe(session, user, body.workspace, body.source_workspace, kinds=body.kinds)
    session.commit()  # the sync below reads the subscription in its own transactions
    return {"subscription": federation.as_dict(sub), "sync": federation.sync_subscription(sub.id)}


@router.post("/subscriptions/{subscription_id}/sync")
def sync_subscription(subscription_id: str, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    federation.load(session, user, subscription_id, federation.SUBSCRIBER_ROLE)
    session.commit()
    return federation.sync_subscription(subscription_id)


@router.post("/subscriptions/{subscription_id}/resync")
def resync_subscription(subscription_id: str, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    """Replay the source's feed from the start; resumes a suspended subscription if the subject's access is back."""
    federation.reset(session, user, federation.load(session, user, subscription_id, federation.SUBSCRIBER_ROLE))
    session.commit()
    return federation.sync_subscription(subscription_id)


@router.delete("/subscriptions/{subscription_id}")
def revoke_subscription(subscription_id: str, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    return federation.revoke(session, user, federation.load(session, user, subscription_id, federation.SUBSCRIBER_ROLE))

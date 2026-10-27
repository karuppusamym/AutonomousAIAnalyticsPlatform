"""API v2 conversations (spec S04 §4.6): conversations and messages over Ask threads, branches, T1 state,
summaries, typed corrections and personal memory, legal hold, deletion and tenant retention.

Raw messages and state are for the conversation's participant only; other apps and agents read summaries and
approved knowledge. Every route binds the conversation (or correction) to its workspace through a scoped loader.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.conversations import corrections as corr_svc
from analystos.conversations import retention, summaries
from analystos.conversations import service as conv_svc
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import User

router = APIRouter(prefix="/v2", tags=["conversations"])


class ConversationIn(BaseModel):
    workspace_id: str
    title: str | None = Field(default=None, max_length=300)
    channel: Literal["ui", "api", "mcp", "teams", "slack", "email"] = "api"


class MessageAttachment(BaseModel):
    type: Literal["message", "artifact"]
    id: str


class ConversationMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[MessageAttachment] | None = None
    mode: Literal["ask", "investigate", "edit"] = "ask"
    analysis: Literal["quick", "analyst"] = "quick"  # ask: one governed query, or a plan of <= 4 governed steps
    edit_message_id: str | None = None  # edit: the message this one replaces (in a new branch)


class ConversationBranchIn(BaseModel):
    from_message_id: str | None = None


class ConversationStatePatch(BaseModel):
    filters: list[dict[str, Any]] | None = None
    sources: list[str] | None = None
    metric_bindings: dict[str, str] | None = None
    time_window: str | dict[str, Any] | None = None


class ConversationCorrectionIn(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    meaning: str = Field(min_length=1, max_length=2000)
    scope: Literal["session", "personal", "workspace_proposed"] = "session"
    message_id: str | None = None


class CorrectionPromoteIn(BaseModel):
    to: Literal["personal", "workspace_proposed"]


class LegalHoldIn(BaseModel):
    hold: bool
    reason: str | None = Field(default=None, max_length=500)


class ConversationRetentionIn(BaseModel):
    messages_days: int | None = None
    results_days: int | None = None


# ------------------------------------------------------------------------------------ conversations
@router.get("/conversations")
def list_conversations(mine: bool = True, workspace_id: str | None = None, channel: str | None = None,
                       archived: bool = False, limit: int = Query(50, ge=1, le=200), user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    """The caller's own conversations. ``mine=false`` is refused: nobody lists other users' conversations."""
    if not mine:
        raise InvalidInput("only your own conversations are listed (mine=true); other apps read summaries")
    return conv_svc.list_mine(session, user, workspace_id=workspace_id, channel=channel, archived=archived, limit=limit)


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationIn, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    conv = conv_svc.create(session, user, body.workspace_id, title=body.title, channel=body.channel)
    return conv_svc.view(session, conv)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    return conv_svc.detail(session, user, conversation_id)


@router.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: str, body: ConversationMessageIn, user: User = Depends(current_user)):
    """``ask`` answers through the governed Ask path; ``investigate`` starts a run; ``edit`` branches before
    ``edit_message_id`` and asks there. Returns the conversation (the branch, for an edit) and the message."""
    return conv_svc.send(user, conversation_id, body.text, mode=body.mode, analysis=body.analysis,
                         attachments=[a.model_dump() for a in body.attachments or []],
                         edit_message_id=body.edit_message_id)


@router.post("/conversations/{conversation_id}/branch", status_code=201)
def branch_conversation(conversation_id: str, body: ConversationBranchIn, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    return conv_svc.branch(session, user, conversation_id, body.from_message_id)


@router.get("/conversations/{conversation_id}/state")
def get_state(conversation_id: str, response: Response, version: int | None = Query(None, ge=1),
              user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The latest state (or ``version``); its ETag is the state version, for ``If-Match`` on PATCH."""
    from analystos.api.http import set_etag

    out = conv_svc.get_state(session, user, conversation_id, version)
    set_etag(response, out["version"])
    return out


@router.patch("/conversations/{conversation_id}/state")
def patch_state(conversation_id: str, body: ConversationStatePatch, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """Set filters, sources (within your scope), metric bindings or the time window. ``If-Match`` (the state
    version) makes a stale edit 412."""
    from analystos.api.http import expected_revision

    changes = body.model_dump(exclude_unset=True)
    return conv_svc.patch_state(session, user, conversation_id, changes,
                                expected_version=expected_revision(if_match, required=False))


@router.post("/conversations/{conversation_id}/close")
def close_conversation(conversation_id: str, user: User = Depends(current_user)):
    """Close the conversation and write its summary (a new message reopens it)."""
    from analystos.db.base import session_scope

    with session_scope() as s:
        conv_svc.close(s, s.merge(user), conversation_id)
    return summaries.summarize(user, conversation_id, "close")


# ------------------------------------------------------------------------------------ summaries
@router.get("/conversations/{conversation_id}/summary")
def get_summary(conversation_id: str, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    conv = conv_svc.load(session, user, conversation_id)
    row = summaries.latest(session, conv.id)
    if row is None:
        raise NotFound("no summary yet: close the conversation or POST /summary")
    from analystos.conversations import state as st

    return {**summaries.view(row), "stale": row.through_seq < st.last_seq(session, conv)}


@router.post("/conversations/{conversation_id}/summary", status_code=201)
def summarize_now(conversation_id: str, user: User = Depends(current_user)):
    return summaries.summarize(user, conversation_id, "manual")


# ------------------------------------------------------------------------------------ corrections and memory
@router.post("/conversations/{conversation_id}/corrections", status_code=201)
def add_correction(conversation_id: str, body: ConversationCorrectionIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """``session`` applies here only; ``personal`` remembers it for you (180 days); ``workspace_proposed`` applies
    here and asks the workspace's knowledge reviewers to make it the team definition."""
    conv = conv_svc.load(session, user, conversation_id)
    return corr_svc.correct(session, user, conv, term=body.term, meaning=body.meaning, scope=body.scope,
                            message_id=body.message_id)


@router.get("/conversations/{conversation_id}/corrections")
def list_corrections(conversation_id: str, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    return corr_svc.in_conversation(session, conv_svc.load(session, user, conversation_id))


@router.post("/conversations/{conversation_id}/corrections/{correction_id}/promote", status_code=201)
def promote_correction(conversation_id: str, correction_id: str, body: CorrectionPromoteIn, user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    conv = conv_svc.load(session, user, conversation_id)
    row = corr_svc.load_correction(session, user, correction_id, conv.workspace_id)
    return corr_svc.promote(session, user, conv, row, body.to)


@router.get("/conversations/{conversation_id}/memory")
def effective_memory(conversation_id: str, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    """What the next message will use: session clarifications, your personal memory, approved team corrections."""
    conv = conv_svc.load(session, user, conversation_id)
    return corr_svc.memory(session, user.id, conv.workspace_id, conv.id)


@router.get("/memory/corrections")
def my_memory(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return corr_svc.mine(session, user, workspace_id)


@router.delete("/memory/corrections/{correction_id}")
def forget_correction(correction_id: str, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    return corr_svc.revoke(session, user, correction_id)


# ------------------------------------------------------------------------------------ hold, delete, retention
@router.put("/conversations/{conversation_id}/legal-hold")
def legal_hold(conversation_id: str, body: LegalHoldIn, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    return conv_svc.set_legal_hold(session, user, conversation_id, body.hold, body.reason)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, reason: str | None = Query(None, max_length=300),
                        user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """A deletion request: 409 under legal hold; the audit log keeps a tombstone with counts, never content."""
    return conv_svc.delete(session, user, conversation_id, reason=reason)


def _this_tenant(tenant_id: str) -> None:
    from analystos.tenancy import current_tenant

    if tenant_id != current_tenant():
        raise NotFound("tenant not found")


@router.get("/tenants/{tenant_id}/conversation-retention")
def get_retention(tenant_id: str, user: User = Depends(current_user)):
    _this_tenant(tenant_id)
    return retention.get_policy(tenant_id)


@router.put("/tenants/{tenant_id}/conversation-retention")
def put_retention(tenant_id: str, body: ConversationRetentionIn, user: User = Depends(current_user)):
    _this_tenant(tenant_id)
    return retention.set_policy(user, tenant_id, body.model_dump(exclude_none=True))

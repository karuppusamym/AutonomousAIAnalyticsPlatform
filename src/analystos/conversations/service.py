"""API v2 conversations over Ask threads (spec S04 §4.1, §4.6).

A conversation is an ``ask_thread`` and a message is an ``ask_turn``; nothing is copied. What v2 adds: the
channel and Desk request a conversation came from, branches (a branch inherits its parent's messages up to the
branch point by reference and starts from the parent's state at that message), and participant-only access.

Access: a conversation's raw messages and state are readable by its participant (its owner) only; there is no
admin bypass in v2. Other apps and agents read *summaries* and *approved knowledge* (``summaries.py``,
``corrections.py``), never other users' messages. Every load goes through ``load_in_workspace``, so the caller's
workspace role applies and another tenant's or workspace's conversation is the same 404 as an unknown id.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Artifact, AskThread, AskTurn, TenantMember, User, Workspace, WorkspaceMember
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

CHANNELS = ("ui", "api", "mcp", "teams", "slack", "email")
MODES = ("ask", "investigate", "edit")
ANALYSIS_MODES = ("quick", "analyst")
MAX_TEXT = 4000
MAX_ATTACHMENTS = 10
HOLD_ROLES = ("tenant_admin", "tenant_auditor")


def actor_of(user: User) -> str:
    return f"user:{user.id}"


# ------------------------------------------------------------------------------------ access
@scoped_loader
def load(session: Session, user: User, conversation_id: str, workspace_id: str | None = None, *,
         minimum: str = "viewer", for_update: bool = False) -> AskThread:
    """The caller's own conversation (participant), checked in its workspace; anything else is a 404."""
    conv = load_in_workspace(session, AskThread, conversation_id, workspace_id, user=user, minimum=minimum,
                             label="conversation", for_update=for_update)
    if conv.user_id != user.id:
        raise NotFound("conversation not found")
    return conv


def _tenant_of(session: Session, workspace_id: str) -> str | None:
    ws = session.get(Workspace, workspace_id)
    return getattr(ws, "tenant_id", None) if ws is not None else None


def view(session: Session, conv: AskThread) -> dict[str, Any]:
    from analystos.conversations import state as st

    head = st.latest(session, conv.id)
    own = session.scalar(select(func.count()).select_from(AskTurn).where(AskTurn.thread_id == conv.id)) or 0
    return {"id": conv.id, "workspace_id": conv.workspace_id, "tenant_id": _tenant_of(session, conv.workspace_id),
            "channel": conv.channel or "ui", "title": conv.title, "owner_id": conv.user_id,
            "participants": [conv.user_id], "request_id": conv.request_id,
            "parent_conversation_id": conv.parent_conversation_id, "branch_from_message_id": conv.branch_from_message_id,
            "legal_hold": bool(conv.legal_hold), "archived": bool(conv.archived),
            "closed_at": _iso(conv.closed_at), "summarized_at": _iso(conv.summarized_at),
            "created_at": _iso(conv.created_at), "updated_at": _iso(conv.updated_at), "revision": conv.revision or 1,
            "state_version": head.version if head is not None else 0, "message_count": int(own)}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def message_view(session: Session, turn: AskTurn, *, inherited: bool = False) -> dict[str, Any]:
    from analystos.services.ask import turn_out

    answer = turn_out(session, turn)
    for k in ("thread_id", "question", "user_id", "seq", "workspace_id", "id", "created_at"):
        answer.pop(k, None)
    mode = "investigate" if turn.route == "investigate" else "ask"
    return {"id": turn.id, "conversation_id": turn.thread_id, "seq": turn.seq, "inherited": inherited,
            "author_id": turn.user_id, "text": turn.question, "mode": mode, "status": turn.status,
            "created_at": _iso(turn.created_at), "answer": answer}


# ------------------------------------------------------------------------------------ conversations
def create(session: Session, user: User, workspace_id: str, *, title: str | None = None, channel: str = "api",
           request_id: str | None = None) -> AskThread:
    from analystos.services import ask as ask_svc

    if channel not in CHANNELS:
        raise InvalidInput(f"channel must be one of {', '.join(CHANNELS)}")
    out = ask_svc.create_thread(session, user, workspace_id, title, channel=channel, request_id=request_id)
    conv = session.get(AskThread, out["id"])
    emit(workspace_id, "conversation.created", {"conversation_id": conv.id, "channel": channel, "request_id": request_id},
         actor=actor_of(user), session=session)
    return conv


def list_mine(session: Session, user: User, *, workspace_id: str | None = None, channel: str | None = None,
              archived: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    """The caller's conversations, newest activity first, in one workspace or every workspace they belong to."""
    if workspace_id:
        require_role(session, user, workspace_id, "viewer")
        spaces = [workspace_id]
    else:
        spaces = list(session.scalars(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)))
    if channel is not None and channel not in CHANNELS:
        raise InvalidInput(f"channel must be one of {', '.join(CHANNELS)}")
    if not spaces:
        return []
    q = select(AskThread).where(AskThread.user_id == user.id, AskThread.workspace_id.in_(spaces),
                                AskThread.archived.is_(archived))
    if channel:
        q = q.where(AskThread.channel == channel)
    convs = session.scalars(q.order_by(AskThread.updated_at.desc(), AskThread.id).limit(min(max(limit, 1), 200)))
    return [view(session, c) for c in convs]


@scoped_loader
def detail(session: Session, user: User, conversation_id: str) -> dict[str, Any]:
    from analystos.conversations import state as st

    conv = load(session, user, conversation_id)
    return {**view(session, conv),
            "messages": [message_view(session, t, inherited=inh) for t, inh in st.history(session, conv)]}


def previous_answer(session: Session, conv: AskThread, seq: int) -> AskTurn | None:
    """The latest answered message before ``seq`` that ``conv`` can see (its own or inherited): follow-ups in a
    branch build on the branch point's answer."""
    from analystos.conversations import state as st

    answered = [t for t, _ in st.history(session, conv) if t.seq < seq and t.status == "answered"]
    return answered[-1] if answered else None


# ------------------------------------------------------------------------------------ messages
def _clean_text(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) < 1:
        raise InvalidInput("say something")
    if len(text) > MAX_TEXT:
        raise InvalidInput(f"a message is at most {MAX_TEXT} characters")
    return text


def _attach(session: Session, user: User, conv: AskThread, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attachments are references the caller can already read: a message of their own conversations, or an
    artifact of this workspace. They join the state's result handles; their content is never copied."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise InvalidInput(f"at most {MAX_ATTACHMENTS} attachments")
    out = []
    for a in attachments:
        kind, ref = str((a or {}).get("type") or ""), str((a or {}).get("id") or "")
        if kind == "message":
            from analystos.services.ask import _turn_for

            turn = _turn_for(session, user, ref, conv.workspace_id)
            out.append({"attachment": "message", "message_id": turn.id, "query_id": (turn.result or {}).get("query_id"),
                        "result_hash": (turn.result or {}).get("result_hash")})
        elif kind == "artifact":
            art = load_in_workspace(session, Artifact, ref, conv.workspace_id, user=user, label="artifact")
            out.append({"attachment": "artifact", "artifact_id": art.id, "type": art.type})
        else:
            raise InvalidInput("an attachment is {type: message|artifact, id}")
    return out


@scoped_loader
def send(user: User, conversation_id: str, text: str, *, mode: str = "ask", analysis: str = "quick",
         attachments: list[dict[str, Any]] | None = None, edit_message_id: str | None = None) -> dict[str, Any]:
    """Post a message. ``ask`` answers it through the governed Ask path; ``investigate`` starts a run and records
    the message with the run handle; ``edit`` replaces an earlier message by branching just before it and asking
    there (the original conversation is unchanged)."""
    from analystos.conversations import state as st
    from analystos.services import ask as ask_svc

    if mode not in MODES:
        raise InvalidInput(f"mode must be one of {', '.join(MODES)}")
    if analysis not in ANALYSIS_MODES:
        raise InvalidInput(f"analysis must be one of {', '.join(ANALYSIS_MODES)}")
    text = _clean_text(text)
    with session_scope() as s:
        me = s.merge(user)
        conv = load(s, me, conversation_id, minimum="analyst" if mode == "investigate" else "viewer")
        if mode == "edit":
            if not edit_message_id:
                raise InvalidInput("edit needs edit_message_id")
            edited = st.require_message(s, conv, edit_message_id)
            prior = [t for t, _ in st.history(s, conv) if t.seq < edited.seq]
            target = branch(s, me, conv.id, prior[-1].id if prior else None)
            conversation_id = target["id"]
            conv = load(s, me, conversation_id)
        handles = _attach(s, me, conv, list(attachments or []))
        if handles:
            state = st.current(s, conv.id)
            state["result_handles"] = [*state["result_handles"], *handles][-st.MAX_HANDLES:]
            st.write(s, conv, state, cause="edit", actor=actor_of(me))
        if conv.closed_at is not None:
            conv.closed_at = None  # a new message reopens it
        ws = conv.workspace_id
    if mode == "investigate":
        message = _investigate(user, conversation_id, ws, text)
    else:
        message = ask_svc.ask_in_thread(user, conversation_id, text, mode=analysis)
    with session_scope() as s:
        me = s.merge(user)
        conv = load(s, me, conversation_id)
        turn = s.get(AskTurn, message["id"])
        out = {"conversation": view(s, conv), "message": message_view(s, turn)}
    return out


def _investigate(user: User, conversation_id: str, workspace_id: str, text: str) -> dict[str, Any]:
    from analystos.conversations import state as st
    from analystos.services.runs import create_run

    run = create_run(user, workspace_id, objective=text, origin={"type": "conversation", "conversation_id": conversation_id})
    with session_scope() as s:
        conv = s.get(AskThread, conversation_id)
        seq = st.last_seq(s, conv) + 1
        turn = AskTurn(id=new_id("askt"), thread_id=conv.id, workspace_id=workspace_id, user_id=user.id, seq=seq,
                       question=text, parameters={}, status="delegated", route="investigate", attempts=[], stages=[
                           {"key": "done", "text": "Started an investigation", "at_ms": 0}],
                       decisions=[], provenance={"run_id": run.id}, promotions=[
                           {"target": "investigate", "run_id": run.id, "status": run.status}])
        s.add(turn)
        conv.updated_at = utcnow()
        s.flush()
        st.on_turn(s, turn, actor_of(user))
        emit(workspace_id, "conversation.message", {"conversation_id": conv.id, "message_id": turn.id,
                                                    "mode": "investigate", "run_id": run.id},
             actor=actor_of(user), session=s)
        return {"id": turn.id}


# ------------------------------------------------------------------------------------ branches
@scoped_loader
def branch(session: Session, user: User, conversation_id: str, from_message_id: str | None) -> dict[str, Any]:
    """A new conversation that continues this one after ``from_message_id``: it inherits the messages up to that
    point (by reference) and starts from the state as it stood there. ``None`` branches before the first message."""
    from analystos.conversations import state as st

    parent = load(session, user, conversation_id)
    point = st.require_message(session, parent, from_message_id) if from_message_id else None
    child = AskThread(id=new_id("ask"), workspace_id=parent.workspace_id, user_id=user.id,
                      title=f"{parent.title[:280]} (branch)", archived=False, revision=1, channel=parent.channel or "ui",
                      request_id=None, parent_conversation_id=parent.id,
                      branch_from_message_id=point.id if point is not None else None, legal_hold=False)
    session.add(child)
    session.flush()
    if point is not None:
        source = parent.id if point.thread_id == parent.id else point.thread_id
        row, origin = st.branch_copy(session, source, child, point.seq, actor_of(user))
        restored = {"conversation_id": source, "version": origin.version if origin is not None else 0}
    else:
        restored = {"conversation_id": parent.id, "version": 0}
    emit(parent.workspace_id, "conversation.branched", {"conversation_id": child.id, "parent_conversation_id": parent.id,
                                                       "from_message_id": from_message_id, "restored_state": restored},
         actor=actor_of(user), session=session)
    return {**view(session, child), "restored_state": restored}


# ------------------------------------------------------------------------------------ state
@scoped_loader
def get_state(session: Session, user: User, conversation_id: str, version: int | None = None) -> dict[str, Any]:
    from analystos.conversations import state as st
    from analystos.conversations.models import ConversationState

    conv = load(session, user, conversation_id)
    if version is None:
        return {"conversation_id": conv.id, **st.view(st.latest(session, conv.id))}
    row = session.scalar(select(ConversationState).where(ConversationState.conversation_id == conv.id,
                                                         ConversationState.version == version))
    if row is None:
        raise NotFound(f"state version {version} not found")
    return {"conversation_id": conv.id, **st.view(row)}


@scoped_loader
def patch_state(session: Session, user: User, conversation_id: str, changes: dict[str, Any],
                expected_version: int | None = None) -> dict[str, Any]:
    from analystos.conversations import state as st
    from analystos.governance.policy import resolve_scope

    conv = load(session, user, conversation_id)
    allowed: set[str] = set()
    if "sources" in changes:
        scope = resolve_scope(session, user, conv.workspace_id)
        allowed = set(scope.assets) | set(scope.source_ids)
    row = st.patch(session, conv, changes, actor=actor_of(user), allowed_sources=allowed,
                   expected_version=expected_version)
    return {"conversation_id": conv.id, **st.view(row)}


# ------------------------------------------------------------------------------------ close, hold, delete
@scoped_loader
def close(session: Session, user: User, conversation_id: str) -> AskThread:
    conv = load(session, user, conversation_id)
    conv.closed_at = conv.closed_at or utcnow()
    return conv


def _hold_officer(session: Session, user: User, workspace_id: str) -> None:
    if user.is_admin:
        return
    tenant = _tenant_of(session, workspace_id)
    role = session.scalar(select(TenantMember.role).where(TenantMember.tenant_id == tenant, TenantMember.user_id == user.id))
    if role not in HOLD_ROLES:
        raise Forbidden("a legal hold is set by a tenant admin or auditor",
                        details={"rule": "legal_hold", "remedy": "ask a tenant admin"})


@scoped_loader
def set_legal_hold(session: Session, user: User, conversation_id: str, hold: bool, reason: str | None) -> dict[str, Any]:
    """A tenant admin or auditor holds (or releases) a conversation: retention and deletion skip it while held.
    They need not be its participant; they see its metadata, never its messages, through this call."""
    conv = load_in_workspace(session, AskThread, conversation_id, None, user=user, label="conversation")
    _hold_officer(session, user, conv.workspace_id)
    if hold and not (reason or "").strip():
        raise InvalidInput("a legal hold needs a reason")
    conv.legal_hold = bool(hold)
    audit(actor_of(user), "conversation.legal_hold_set" if hold else "conversation.legal_hold_released",
          workspace_id=conv.workspace_id, target=conv.id, decision="allow",
          details={"reason": (reason or "")[:500]}, session=session)
    emit(conv.workspace_id, "conversation.legal_hold", {"conversation_id": conv.id, "hold": bool(hold)},
         actor=actor_of(user), session=session)
    return {"id": conv.id, "workspace_id": conv.workspace_id, "legal_hold": bool(conv.legal_hold)}


@scoped_loader
def delete(session: Session, user: User, conversation_id: str, *, reason: str | None = None) -> dict[str, Any]:
    """A deletion request: messages, state, summaries, session and personal corrections and every memory entry
    (with its embedding) derived from the conversation go, in one transaction; an audit tombstone records counts,
    never content. Refused while a legal hold is set. Its participant asks; a platform admin may ask for them."""
    from analystos.conversations import retention

    if user.is_admin:
        conv = load_in_workspace(session, AskThread, conversation_id, None, user=user, label="conversation")
    else:
        conv = load(session, user, conversation_id)
    if conv.legal_hold:
        raise Conflict("this conversation is under legal hold and cannot be deleted",
                       details={"rule": "legal_hold", "remedy": "a tenant admin releases the hold first"})
    counts = retention.purge_conversation(session, conv)
    audit(actor_of(user), "conversation.deleted", workspace_id=counts["workspace_id"], target=conversation_id,
          decision="allow", details={"tombstone": True, "reason": (reason or "deletion request")[:300],
                                     "owner_id": counts.pop("owner_id"), **{k: v for k, v in counts.items()
                                                                            if k != "workspace_id"}},
          session=session)
    emit(counts["workspace_id"], "conversation.deleted", {"conversation_id": conversation_id}, actor=actor_of(user),
         session=session)
    return {"id": conversation_id, "deleted": True, **counts}


def detach_request(session: Session, conversation_id: str) -> None:
    from analystos.desk.models import Request

    session.execute(update(Request).where(Request.conversation_id == conversation_id).values(conversation_id=None))

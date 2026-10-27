"""Typed corrections and the memory tiers they feed (spec S04 §3, §4.4; AT-18).

``{term, meaning, scope}``:

* ``session`` — T1: the conversation's state gets the clarification at once; nothing outlives the conversation.
* ``personal`` ("remember this") — T1 plus T2: the author's own memory in this workspace for 180 days, applied in
  their later conversations and visible to nobody else.
* ``workspace_proposed`` ("make this the team definition") — T1 for the author's session plus a knowledge
  suggestion (``knowledge/suggestions.py``) an editor decides. Other users get it only once it is approved.

Promotion moves a correction up one tier (session → personal → workspace_proposed) as a new row that names the one
it came from. Personal memory never changes a team definition silently: when it differs from an approved one, the
memory lists the override and Ask says so in its stages. Text is data, never instructions (CLAUDE.md rule 7).
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.models import AskThread, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

SCOPES = ("session", "personal", "workspace_proposed")
PROMOTIONS = {"session": ("personal", "workspace_proposed"), "personal": ("workspace_proposed",)}
PERSONAL_TTL = timedelta(days=180)
MAX_TERM = 200
MAX_MEANING = 2000
ORIGIN = "conversation.correction"
LAYER_ORDER = ("session", "personal", "workspace")  # precedence, highest first
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def term_key(term: str) -> str:
    return " ".join(str(term or "").lower().split())[:MAX_TERM]


def _clean(value: str, limit: int, what: str) -> str:
    text = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    if not text:
        raise InvalidInput(f"a correction needs a {what}")
    if len(text) > limit:
        raise InvalidInput(f"a correction's {what} is at most {limit} characters")
    return text


def view(row: Any) -> dict[str, Any]:
    return {"id": row.id, "conversation_id": row.conversation_id, "message_id": row.message_id, "user_id": row.user_id,
            "term": row.term, "meaning": row.meaning, "scope": row.scope, "status": row.status,
            "promoted_from": row.promoted_from, "suggestion_id": row.suggestion_id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}



# ------------------------------------------------------------------------------------ apply
def correct(session: Session, user: User, conv: AskThread, *, term: str, meaning: str, scope: str,
            message_id: str | None = None, promoted_from: str | None = None) -> dict[str, Any]:
    """Apply a correction in ``conv`` (the caller's own, already loaded). Returns the correction and the new state
    version."""
    from analystos.conversations import state as st
    from analystos.conversations.models import ConversationCorrection

    if scope not in SCOPES:
        raise InvalidInput(f"scope must be one of {', '.join(SCOPES)}")
    term, meaning = _clean(term, MAX_TERM, "term"), _clean(meaning, MAX_MEANING, "meaning")
    key = term_key(term)
    if message_id:
        message_id = st.require_message(session, conv, message_id).id
    now = utcnow()
    row = ConversationCorrection(id=new_id("corr"), workspace_id=conv.workspace_id, conversation_id=conv.id,
                                 message_id=message_id, user_id=user.id, term=term, term_key=key, meaning=meaning,
                                 scope=scope, status="proposed" if scope == "workspace_proposed" else "active",
                                 promoted_from=promoted_from,
                                 expires_at=now + PERSONAL_TTL if scope == "personal" else None)
    session.add(row)
    session.flush()
    state = st.set_clarification(st.current(session, conv.id), term=term, term_key=key, meaning=meaning,
                                 correction_id=row.id, scope=scope)
    version = st.write(session, conv, state, cause="correction", actor=f"user:{user.id}", message_id=message_id)
    if scope == "personal":
        for old in session.scalars(select(ConversationCorrection).where(
                ConversationCorrection.workspace_id == conv.workspace_id, ConversationCorrection.user_id == user.id,
                ConversationCorrection.scope == "personal", ConversationCorrection.status == "active",
                ConversationCorrection.term_key == key, ConversationCorrection.id != row.id)):
            old.status = "superseded"
            _forget(session, [old.id])  # an older personal meaning leaves the ledger with its embedding
    elif scope == "workspace_proposed":
        _propose(session, user, conv, row)
    audit(f"user:{user.id}", "conversation.correction_applied", workspace_id=conv.workspace_id, target=row.id,
          decision="allow", details={"conversation_id": conv.id, "scope": scope, "term": term,
                                     "suggestion_id": row.suggestion_id, "promoted_from": promoted_from},
          session=session)
    emit(conv.workspace_id, "conversation.correction_applied", {"conversation_id": conv.id, "correction_id": row.id,
                                                                "scope": scope, "status": row.status},
         actor=f"user:{user.id}", session=session)
    return {"correction": view(row), "state_version": version.version}


def _propose(session: Session, user: User, conv: AskThread, row: Any) -> None:
    from analystos.knowledge.suggestions import field, propose

    prov = {"source": "conversation", "correction_id": row.id, "conversation_id": conv.id, "by": f"user:{user.id}"}
    suggestion = propose(session, conv.workspace_id, kind="definition", subject=f"term:{row.term_key}"[:200],
                         title=row.term, fields={"body": field(f"{row.term}: {row.meaning}", 0.8, **prov)},
                         origin=ORIGIN, proposed_by=f"user:{user.id}", batch=conv.id)
    if suggestion is None:  # already decided for this term, rejected before, or owner content exists
        row.status = "not_proposed"
        return
    row.suggestion_id = suggestion.id  # the pending suggestion is in the ledger as `suggested` (S03 projector)


@scoped_loader
def load_correction(session: Session, user: User, correction_id: str, workspace_id: str | None = None) -> Any:
    """The caller's own correction, checked in its workspace; anyone else's is a 404."""
    from analystos.conversations.models import ConversationCorrection

    row = load_in_workspace(session, ConversationCorrection, correction_id, workspace_id, user=user, label="correction")
    if row.user_id != user.id:
        raise NotFound("correction not found")
    return row


def promote(session: Session, user: User, conv: AskThread, row: Any, to: str) -> dict[str, Any]:
    """"Remember this" (session → personal) or "make this the team definition" (→ workspace_proposed)."""
    if row.conversation_id != conv.id or row.user_id != user.id:
        raise NotFound("correction not found")
    if to not in PROMOTIONS.get(row.scope, ()):
        raise InvalidInput(f"a {row.scope} correction can be promoted to: {', '.join(PROMOTIONS.get(row.scope, ())) or 'nothing'}")
    if row.status not in ("active",):
        raise InvalidInput(f"a {row.status} correction cannot be promoted")
    return correct(session, user, conv, term=row.term, meaning=row.meaning, scope=to, message_id=row.message_id,
                   promoted_from=row.id)


@scoped_loader
def revoke(session: Session, user: User, correction_id: str) -> dict[str, Any]:
    """Forget a personal correction (the author only)."""
    row = load_correction(session, user, correction_id)
    if row.scope != "personal":
        raise NotFound("correction not found")
    row.status = "revoked"
    _forget(session, [row.id])
    audit(f"user:{user.id}", "conversation.correction_revoked", workspace_id=row.workspace_id, target=row.id,
          decision="allow", session=session)
    return view(row)


def _forget(session: Session, ids: list[str]) -> None:
    from analystos.context.ledger import forget

    forget(session, "conversation_correction", ids)


def mine(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    """The caller's personal memory in a workspace (active, unexpired)."""
    require_role(session, user, workspace_id, "viewer")
    return [view(r) for r in _personal(session, user.id, workspace_id)]


def _personal(session: Session, user_id: str, workspace_id: str) -> list[Any]:
    from analystos.conversations.models import ConversationCorrection as C

    now = utcnow()
    return list(session.scalars(select(C).where(C.workspace_id == workspace_id, C.user_id == user_id,
                                                C.scope == "personal", C.status == "active",
                                                or_(C.expires_at.is_(None), C.expires_at > now))
                                .order_by(C.created_at)))


# ------------------------------------------------------------------------------------ approval hook
def on_suggestions_decided(session: Session, decided: list[tuple[Any, str]]) -> None:
    """Called by the knowledge review when it decides suggestions: a team correction becomes approved (applies to
    everyone in the workspace, ledger trust ``approved``) or rejected (its suggested projection is withdrawn)."""
    from analystos.conversations.models import ConversationCorrection as C

    ids = {row.id: status for row, status in decided if getattr(row, "origin", None) == ORIGIN}
    if not ids:
        return
    for corr in session.scalars(select(C).where(C.suggestion_id.in_(list(ids)), C.scope == "workspace_proposed")):
        status = ids[corr.suggestion_id]
        corr.status = "approved" if status == "approved" else "rejected"  # the ledger hook projects an approval


# ------------------------------------------------------------------------------------ effective memory
def memory(session: Session, user_id: str, workspace_id: str, conversation_id: str | None) -> dict[str, Any]:
    """What applies to ``user_id`` in this conversation: session clarifications, then the user's own personal
    memory, then approved team corrections — one meaning per term, highest layer first. Where a session or
    personal meaning differs from an approved team one, ``overrides`` says so (never silent)."""
    from analystos.conversations import state as st
    from analystos.conversations.models import ConversationCorrection as C

    layers: dict[str, list[dict[str, Any]]] = {"session": [], "personal": [], "workspace": []}
    if conversation_id:
        for c in st.current(session, conversation_id).get("clarifications") or []:
            layers["session"].append({"term": c["term"], "term_key": c["term_key"], "meaning": c["meaning"],
                                      "correction_id": c.get("correction_id"), "scope": "session",
                                      "saved_as": c.get("scope") or "session"})  # how it was applied here
    for r in _personal(session, user_id, workspace_id):
        layers["personal"].append({"term": r.term, "term_key": r.term_key, "meaning": r.meaning, "correction_id": r.id,
                                   "scope": "personal"})
    for r in session.scalars(select(C).where(C.workspace_id == workspace_id, C.scope == "workspace_proposed",
                                             C.status == "approved").order_by(C.updated_at, C.id)):
        layers["workspace"].append({"term": r.term, "term_key": r.term_key, "meaning": r.meaning, "correction_id": r.id,
                                    "scope": "workspace"})
    chosen: dict[str, dict[str, Any]] = {}
    for layer in LAYER_ORDER:
        for item in layers[layer]:
            chosen.setdefault(item["term_key"], item)
    team = {i["term_key"]: i for i in layers["workspace"]}
    overrides = [{"term": i["term"], "used": i["scope"], "meaning": i["meaning"],
                  "team_meaning": team[k]["meaning"], "team_correction_id": team[k]["correction_id"]}
                 for k, i in chosen.items() if i["scope"] != "workspace" and k in team
                 and term_key(team[k]["meaning"]) != term_key(i["meaning"])]
    return {"items": sorted(chosen.values(), key=lambda i: (LAYER_ORDER.index(i["scope"]), i["term_key"])),
            "overrides": overrides}


def for_prompt(mem: dict[str, Any]) -> list[dict[str, str]]:
    """The model sees meanings as labelled data: term, meaning, and which layer said so."""
    return [{"term": i["term"], "meaning": i["meaning"], "from": i["scope"]} for i in mem.get("items") or []]


def in_conversation(session: Session, conv: AskThread) -> list[dict[str, Any]]:
    from analystos.conversations.models import ConversationCorrection as C

    return [view(r) for r in session.scalars(select(C).where(C.conversation_id == conv.id).order_by(C.created_at, C.id))]

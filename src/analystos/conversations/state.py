"""T1 session state (spec S04 §3, §4.2): ``{filters, sources, metric_bindings, time_window, clarifications,
result_handles}``, one immutable version per change.

Each version records ``at_seq``, the last message it saw. A branch from message N copies the latest version with
``at_seq <= N`` byte for byte, so the branch resumes from exactly the state the parent had after N (corrections made
after N but before N+1 included). Result handles are references (query id, result hash), never rows (ADR-014).
"""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound, PreconditionFailed
from analystos.core.ids import new_id
from analystos.db.models import AskThread, AskTurn

FIELDS = ("filters", "sources", "metric_bindings", "time_window", "clarifications", "result_handles")
EDITABLE = ("filters", "sources", "metric_bindings", "time_window")
MAX_HANDLES = 20
MAX_FILTERS = 50
FILTER_OPS = ("=", "!=", "<", "<=", ">", ">=", "in", "not in", "between", "like", "is null", "is not null")
CAUSES = ("created", "message", "correction", "edit", "branch")


def empty() -> dict[str, Any]:
    return {"filters": [], "sources": [], "metric_bindings": {}, "time_window": None, "clarifications": [],
            "result_handles": []}


def latest(session: Session, conversation_id: str):
    from analystos.conversations.models import ConversationState

    return session.scalar(select(ConversationState).where(ConversationState.conversation_id == conversation_id)
                          .order_by(ConversationState.version.desc()).limit(1))


def current(session: Session, conversation_id: str) -> dict[str, Any]:
    row = latest(session, conversation_id)
    return {**empty(), **copy.deepcopy(row.state)} if row is not None else empty()


def last_seq(session: Session, conv: AskThread) -> int:
    """The last message seq visible in the conversation (its own, else the branch point it continues from)."""
    own = session.scalar(select(func.max(AskTurn.seq)).where(AskTurn.thread_id == conv.id))
    if own:
        return int(own)
    if conv.branch_from_message_id:
        point = session.get(AskTurn, conv.branch_from_message_id)
        return int(point.seq) if point is not None else 0
    return 0


def write(session: Session, conv: AskThread, state: dict[str, Any], *, cause: str, actor: str,
          message_id: str | None = None, at_seq: int | None = None):
    from analystos.conversations.models import ConversationState

    if cause not in CAUSES:
        raise ValueError(f"state cause {cause!r}")
    version = (session.scalar(select(func.max(ConversationState.version))
                              .where(ConversationState.conversation_id == conv.id)) or 0) + 1
    row = ConversationState(id=new_id("cst"), conversation_id=conv.id, workspace_id=conv.workspace_id, version=version,
                            at_seq=last_seq(session, conv) if at_seq is None else at_seq, message_id=message_id,
                            cause=cause, state={k: copy.deepcopy(state.get(k, empty()[k])) for k in FIELDS},
                            created_by=actor[:80])
    session.add(row)
    session.flush()
    return row


def at_message(session: Session, conversation_id: str, seq: int):
    from analystos.conversations.models import ConversationState

    return session.scalar(select(ConversationState).where(ConversationState.conversation_id == conversation_id,
                                                          ConversationState.at_seq <= seq)
                          .order_by(ConversationState.version.desc()).limit(1))


def view(row: Any) -> dict[str, Any]:
    if row is None:
        return {"version": 0, "at_seq": 0, "cause": None, "message_id": None, "state": empty(), "created_at": None}
    return {"version": row.version, "at_seq": row.at_seq, "cause": row.cause, "message_id": row.message_id,
            "state": {**empty(), **row.state}, "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


# ------------------------------------------------------------------------------------ changes
def on_turn(session: Session, turn: AskTurn, actor: str) -> None:
    """A finished message moves the state on: its result handle (reference only) and the tables it read."""
    conv = session.get(AskThread, turn.thread_id)
    if conv is None:
        return
    state = current(session, conv.id)
    result = turn.result or {}
    if turn.status == "answered" and result.get("query_id"):
        handle = {"message_id": turn.id, "seq": turn.seq, "query_id": result.get("query_id"),
                  "result_hash": result.get("result_hash"), "row_count": result.get("row_count"),
                  "columns": list(result.get("columns") or [])[:50]}
        state["result_handles"] = [*state["result_handles"], handle][-MAX_HANDLES:]
    for fq in result.get("referenced_assets") or []:
        if fq not in state["sources"]:
            state["sources"] = [*state["sources"], fq]
    if turn.status == "answered":
        _narrowing(state, turn.provenance or {})
    write(session, conv, state, cause="message", actor=actor, message_id=turn.id, at_seq=turn.seq)


def _narrowing(state: dict[str, Any], prov: dict[str, Any]) -> None:
    """PILOT-FIX-02: the state says what the latest answer is restricted to. A follow-up that narrowed the previous
    answer adds its filter (replacing an earlier one on the same column) or sets the period; a new question starts
    over with its own period. The answer's SQL is what applies them; the state shows them."""
    followup = prov.get("followup") or {}
    period = prov.get("period") if isinstance(prov.get("period"), dict) else None
    window = {**period, "origin": "answer"} if period else None
    tw = state.get("time_window")
    by_answer = isinstance(tw, dict) and tw.get("origin") == "answer"  # one a person set stays until they change it
    if not followup:
        state["filters"] = [f for f in state.get("filters") or [] if f.get("origin") != "followup"]
        if by_answer or tw is None:
            state["time_window"] = window
        return
    if followup.get("period"):
        if by_answer or tw is None:
            state["time_window"] = {**followup["period"], "origin": "answer"}
        return
    column, values = followup.get("column"), list(followup.get("values") or [])
    if not column or not values:
        return
    op = "not in" if followup.get("mode") == "exclude" else "in"
    kept = [f for f in state.get("filters") or []
            if not (op == "in" and f.get("column") == column and f.get("op") == "in" and f.get("origin") == "followup")]
    state["filters"] = [*kept, {"column": column, "op": op, "value": values, "origin": "followup"}][-MAX_FILTERS:]
    if window and tw is None:
        state["time_window"] = window


def _check_filters(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_FILTERS:
        raise InvalidInput(f"filters must be a list of at most {MAX_FILTERS} {{column, op, value}} items")
    out = []
    for f in value:
        if not isinstance(f, dict) or not isinstance(f.get("column"), str) or not f["column"].strip():
            raise InvalidInput("each filter needs a column")
        op = str(f.get("op") or "=").lower()
        if op not in FILTER_OPS:
            raise InvalidInput(f"filter op must be one of {', '.join(FILTER_OPS)}")
        out.append({"column": f["column"].strip()[:200], "op": op, "value": f.get("value")})
    return out


def patch(session: Session, conv: AskThread, changes: dict[str, Any], *, actor: str, allowed_sources: set[str],
          expected_version: int | None = None) -> Any:
    """Set editable fields (filters, sources, metric bindings, time window). Sources must be in the caller's scope:
    state never widens what a later message may read (the gateway checks again anyway)."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise InvalidInput(f"not editable: {', '.join(unknown)} (editable: {', '.join(EDITABLE)})")
    head = latest(session, conv.id)
    if expected_version is not None and expected_version != (head.version if head else 0):
        raise PreconditionFailed(f"state is at version {head.version if head else 0}, not {expected_version}",
                                 details={"current_version": head.version if head else 0})
    state = current(session, conv.id)
    if "filters" in changes:
        state["filters"] = _check_filters(changes["filters"])
    if "sources" in changes:
        sources = changes["sources"]
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise InvalidInput("sources must be a list of source ids or schema.table names")
        outside = [s for s in sources if s not in allowed_sources]
        if outside:
            raise NotFound(f"not in your scope: {', '.join(outside[:5])}")
        state["sources"] = list(dict.fromkeys(sources))
    if "metric_bindings" in changes:
        mb = changes["metric_bindings"]
        if not isinstance(mb, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mb.items()):
            raise InvalidInput("metric_bindings maps a phrase to a metric name")
        state["metric_bindings"] = {k[:200]: v[:200] for k, v in mb.items()}
    if "time_window" in changes:
        tw = changes["time_window"]
        if tw is not None and not isinstance(tw, (str, dict)):
            raise InvalidInput("time_window is a phrase or {start, end, grain}")
        state["time_window"] = tw
    return write(session, conv, state, cause="edit", actor=actor)


def set_clarification(state: dict[str, Any], *, term: str, term_key: str, meaning: str, correction_id: str,
                      scope: str) -> dict[str, Any]:
    kept = [c for c in state.get("clarifications") or [] if c.get("term_key") != term_key]
    state["clarifications"] = [*kept, {"term": term, "term_key": term_key, "meaning": meaning,
                                       "correction_id": correction_id, "scope": scope}]
    return state


def branch_copy(session: Session, source_id: str, child: AskThread, seq: int, actor: str) -> Any:
    """The branch's first state: the source conversation's state as it stood at message ``seq``."""
    source = at_message(session, source_id, seq)
    state = copy.deepcopy(source.state) if source is not None else empty()
    row = write(session, child, state, cause="branch", actor=actor, at_seq=seq)
    if source is not None:
        row.message_id = source.message_id
    return row, source


def require_message(session: Session, conv: AskThread, message_id: str) -> AskTurn:
    """A message visible in ``conv``: its own, or one it inherited up to its branch point."""
    turn = session.get(AskTurn, message_id) if message_id else None
    if turn is not None:
        for c in lineage(session, conv):
            if turn.thread_id == c.id and (c.id == conv.id or turn.seq <= _limit(session, conv, c)):
                return turn
    raise NotFound("message not found")


def lineage(session: Session, conv: AskThread, depth: int = 20) -> list[AskThread]:
    """The conversation, then each ancestor it branched from (bounded)."""
    out, cur = [conv], conv
    while cur.parent_conversation_id and len(out) < depth:
        cur = session.get(AskThread, cur.parent_conversation_id)
        if cur is None:
            break
        out.append(cur)
    return out


def _limit(session: Session, conv: AskThread, ancestor: AskThread) -> int:
    """The last seq of ``ancestor`` that ``conv`` inherits (the smallest branch point on the way up)."""
    limit, cur = 10**9, conv
    while cur is not None and cur.id != ancestor.id:
        point = session.get(AskTurn, cur.branch_from_message_id) if cur.branch_from_message_id else None
        limit = min(limit, point.seq if point is not None else 0)
        cur = session.get(AskThread, cur.parent_conversation_id) if cur.parent_conversation_id else None
    return limit


def history(session: Session, conv: AskThread) -> list[tuple[AskTurn, bool]]:
    """Every message visible in ``conv`` in order, each with ``inherited`` (from an ancestor up to the branch point)."""
    out: list[tuple[AskTurn, bool]] = []
    chain = lineage(session, conv)
    for c in reversed(chain):
        q = select(AskTurn).where(AskTurn.thread_id == c.id).order_by(AskTurn.seq)
        if c.id != conv.id:
            q = q.where(AskTurn.seq <= _limit(session, conv, c))
        out.extend((t, c.id != conv.id) for t in session.scalars(q))
    return out

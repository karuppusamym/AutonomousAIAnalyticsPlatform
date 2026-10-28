"""Conversation summaries (spec S04 §4.3).

``{questions[], answers[{claim, evidence_ref, label}], corrections[], open_questions[]}``, written on idle (30 min,
a platform sweep), on close, or on request. Rules first: each answered message gives one claim built from its
stored result. The ``summarization`` purpose may reword claims (``model_gate``: in ``auto`` mode the rules result
is good enough, so no call is made). Code decides: every claim, model or rules, passes the numbers guard against
the computed result of the message it cites — a claim whose number matches nothing there is dropped (and listed in
``guard.dropped``) and the rules claim stands in for it.

The S03 context ledger projects the latest summary as ``conversation_summary`` (``context/ledger.py``) with trust
``workspace`` when the workspace policy sets ``share_conversation_summaries``, otherwise ``personal`` (the participant
only). The model sees questions,
explanations and single-row aggregates only, never result rows.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.core.errors import NotFound
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AskThread, AskTurn, User
from analystos.events.bus import emit
from analystos.governance.policy import scoped_loader
from analystos.tenancy.background import as_owner_of, platform_loop

log = get_logger(__name__)
IDLE_AFTER = timedelta(minutes=30)
TRIGGERS = ("idle", "close", "manual", "mcp")
PURPOSE = "summarization"
PROMPT = "conversation_summary.v1"
MAX_FACTS = 6
OPEN_STATUSES = ("clarify", "needs_input", "refused")
_WORD = re.compile(r"[A-Za-z0-9_]+")

Llm = Callable[[dict[str, Any]], tuple[Any, Any]]


# ------------------------------------------------------------------------------------ facts of a message
def _numbers(result: dict[str, Any]) -> list[float]:
    """Every numeric cell of the stored result (already capped at 500 rows) and its row count."""
    from analystos.skills.result_facts import num

    vals = [float(v) for row in (result.get("rows") or []) for v in (row or []) if num(v) is not None]
    if result.get("row_count") is not None:
        vals.append(float(result["row_count"]))
    return vals


def _headline(result: dict[str, Any]) -> dict[str, Any] | None:
    """A single-row result's numeric cells (an aggregate); None for anything wider."""
    from analystos.skills.result_facts import num

    rows, cols = result.get("rows") or [], list(result.get("columns") or [])
    if len(rows) != 1 or not cols:
        return None
    facts = {c: v for c, v in zip(cols, rows[0], strict=False) if num(v) is not None}
    return dict(list(facts.items())[:MAX_FACTS]) or None


def _label(turn: AskTurn) -> str:
    prov = turn.provenance or {}
    if prov.get("governance") == "governed":
        return "governed"
    return "verified" if turn.answered_by == "registry" else "ad_hoc"


def _ref(turn: AskTurn) -> dict[str, Any]:
    result = turn.result or {}
    return {"message_id": turn.id, "seq": turn.seq, "query_id": result.get("query_id"),
            "result_hash": result.get("result_hash")}


def _question(turn: AskTurn) -> str:
    return turn.question.strip().rstrip("?").strip()


def rules_claim(turn: AskTurn) -> str:
    from analystos.skills.result_facts import fmt_num

    result = turn.result or {}
    head = _headline(result)
    if head:
        return f"{_question(turn)}: " + ", ".join(f"{c} = {fmt_num(v)}" for c, v in head.items())
    n = result.get("row_count")
    return f"{_question(turn)}: {n} rows" if n is not None else f"{_question(turn)}: answered"


def check_claim(claim: str, turn: AskTurn) -> dict[str, Any]:
    """The numbers guard (skills/result_facts.py): each number in ``claim`` must equal a value the cited message
    computed. The question and column names are masked first (their digits are labels, not claims)."""
    from analystos.skills.result_facts import numbers_bound

    result = turn.result or {}
    # Codes that mix letters and digits ("P1", "Q3") are labels; a bare number in the question is not.
    codes = [w for w in _WORD.findall(turn.question) if any(ch.isdigit() for ch in w) and any(ch.isalpha() for ch in w)]
    labels = [turn.question, _question(turn), *codes, *[str(c) for c in result.get("columns") or []]]
    body = claim.strip().rstrip(".!?")  # the citation must sit inside the claim's own sentence
    return numbers_bound(f"{body} (step {turn.seq}).", _numbers(result), labels=labels, steps={turn.seq})


# ------------------------------------------------------------------------------------ build
def build(turns: list[AskTurn], corrections: list[dict[str, Any]], *, llm: Llm | None = None,
          title: str = "") -> tuple[dict[str, Any], dict[str, Any], str]:
    """(summary, guard, produced_by) for the visible messages ``turns``."""
    answered = {t.seq: t for t in turns if t.status == "answered" and t.result}
    questions = [{"seq": t.seq, "message_id": t.id, "text": t.question} for t in turns]
    open_questions = [{"seq": t.seq, "message_id": t.id, "text": t.question, "status": t.status,
                       "reason": (t.refusal or {}).get("kind")} for t in turns if t.status in OPEN_STATUSES]
    proposed: dict[int, str] = {}
    produced_by, guard = "rules", {"checked": 0, "dropped": [], "model": None}
    if llm is not None and answered:
        payload = {"title": title, "messages": [
            {"seq": t.seq, "question": t.question, "explanation": (t.explanation or "")[:600],
             "facts": _headline(t.result or {}), "row_count": (t.result or {}).get("row_count")}
            for t in answered.values()]}
        data, model = llm(payload)
        if isinstance(data, dict) and isinstance(data.get("answers"), list):
            guard["model"] = str(model)
            for a in data["answers"]:
                if not isinstance(a, dict) or not isinstance(a.get("claim"), str):
                    continue
                try:
                    seq = int(a.get("seq"))
                except (TypeError, ValueError):
                    seq = None
                turn = answered.get(seq) if seq is not None else None
                claim = " ".join(a["claim"].split())[:600]
                if turn is None:
                    guard["dropped"].append({"claim": claim, "problems": ["cites no answered message"]})
                    continue
                check = check_claim(claim, turn)
                guard["checked"] += 1
                if check["ok"]:
                    proposed.setdefault(seq, claim)
                else:
                    guard["dropped"].append({"claim": claim, "seq": seq, "problems": check["problems"]})
    answers = []
    for seq, turn in answered.items():
        claim, by = (proposed[seq], "model") if seq in proposed else (rules_claim(turn), "rules")
        if by == "rules":
            check = check_claim(claim, turn)
            guard["checked"] += 1
            if not check["ok"]:  # a rules claim always binds; if it ever does not, say less rather than wrong
                guard["dropped"].append({"claim": claim, "seq": seq, "problems": check["problems"]})
                claim = f"{_question(turn)}: answered"
        answers.append({"claim": claim, "evidence_ref": _ref(turn), "label": _label(turn), "by": by})
    if proposed:
        produced_by = f"model:{guard['model']}"
    guard["ok"] = True  # every published claim passed; dropped ones are listed, never shown as answers
    tables = sorted({a for t in answered.values() for a in (t.result or {}).get("referenced_assets") or []})
    summary = {"questions": questions, "answers": answers, "corrections": corrections, "open_questions": open_questions,
               "tables": tables}  # the ledger's refs: retrieval withholds the summary from callers outside them
    return summary, guard, produced_by


def _model_llm(owner: User, workspace_id: str) -> Llm | None:
    """The ``summarization`` purpose through the router, or None when the rules result is to be used."""
    from analystos.agents.common import llm_json, model_gate
    from analystos.services.ask import adhoc_context

    with session_scope() as s:
        ctx = adhoc_context(s, s.merge(owner), workspace_id)

    def call(payload: dict[str, Any]) -> tuple[Any, Any]:
        if not model_gate(ctx, PURPOSE, payload, deterministic_ok=True):
            return None, "deterministic"
        return llm_json(ctx, PURPOSE, PROMPT, payload)

    return call


def text_of(summary: dict[str, Any]) -> str:
    lines = [f"- {a['claim']} [{a['label']}, message {a['evidence_ref']['seq']}]" for a in summary.get("answers") or []]
    lines += [f"- Correction ({c['scope']}): {c['term']} = {c['meaning']}" for c in summary.get("corrections") or []]
    lines += [f"- Open: {q['text']}" for q in summary.get("open_questions") or []]
    return "\n".join(lines) or "- Nothing answered yet."


# ------------------------------------------------------------------------------------ write
def _shared(session: Session, workspace_id: str) -> bool:
    from analystos.governance.policy import get_workspace, load_policy

    return bool(load_policy(session, get_workspace(session, workspace_id, allow_disabled=True)).share_conversation_summaries)


def write(session: Session, conv: AskThread, trigger: str, *, llm: Llm | None = None) -> Any:
    from analystos.conversations import state as st
    from analystos.conversations.models import ConversationCorrection as C
    from analystos.conversations.models import ConversationSummary

    if trigger not in TRIGGERS:
        raise ValueError(f"summary trigger {trigger!r}")
    turns = [t for t, _ in st.history(session, conv)]
    corrections = [{"term": r.term, "meaning": r.meaning, "scope": r.scope, "status": r.status}
                   for r in session.scalars(select(C).where(C.conversation_id == conv.id).order_by(C.created_at, C.id))]
    summary, guard, produced_by = build(turns, corrections, llm=llm, title=conv.title)
    version = (session.scalar(select(func.max(ConversationSummary.version))
                              .where(ConversationSummary.conversation_id == conv.id)) or 0) + 1
    trust = "workspace" if _shared(session, conv.workspace_id) else "personal"
    row = ConversationSummary(id=new_id("csum"), conversation_id=conv.id, workspace_id=conv.workspace_id,
                              user_id=conv.user_id, version=version, through_seq=max((t.seq for t in turns), default=0),
                              trigger=trigger, trust=trust, summary=summary, guard=guard, produced_by=produced_by[:120])
    session.add(row)
    session.flush()
    conv.summarized_at = utcnow()
    emit(conv.workspace_id, "conversation.summarized", {"conversation_id": conv.id, "version": version, "trigger": trigger,
                                                        "trust": trust, "dropped": len(guard["dropped"])},
         actor="process:conversation_summary", session=session)
    return row


def view(row: Any) -> dict[str, Any]:
    return {"id": row.id, "conversation_id": row.conversation_id, "version": row.version, "through_seq": row.through_seq,
            "trigger": row.trigger, "trust": row.trust, "produced_by": row.produced_by, "summary": row.summary,
            "guard": row.guard, "created_at": row.created_at.isoformat() if row.created_at else None}


def latest(session: Session, conversation_id: str) -> Any:
    from analystos.conversations.models import ConversationSummary as S

    return session.scalar(select(S).where(S.conversation_id == conversation_id).order_by(S.version.desc()).limit(1))


@scoped_loader
def summarize(user: User, conversation_id: str, trigger: str = "manual", *, llm: Llm | None = None,
              use_model: bool = True) -> dict[str, Any]:
    """Write a new summary of the caller's conversation (participant only)."""
    from analystos.conversations.service import load

    with session_scope() as s:
        conv = load(s, s.merge(user), conversation_id)
        ws = conv.workspace_id
    if llm is None and use_model:
        try:
            llm = _model_llm(user, ws)
        except Exception:  # noqa: BLE001 - no model context (no scope, no router): the rules summary stands
            log.info("conversation %s: summary without a model", conversation_id, exc_info=True)
    with session_scope() as s:
        conv = load(s, s.merge(user), conversation_id)
        return view(write(s, conv, trigger, llm=llm))


@scoped_loader
def current(user: User, conversation_id: str, *, refresh_if_stale: bool = False) -> dict[str, Any]:
    """The latest summary; with ``refresh_if_stale`` a new one when messages arrived since (MCP)."""
    from analystos.conversations import state as st
    from analystos.conversations.service import load

    with session_scope() as s:
        conv = load(s, s.merge(user), conversation_id)
        row = latest(s, conv.id)
        stale = row is None or row.through_seq < st.last_seq(s, conv)
        if row is not None and not (refresh_if_stale and stale):
            return {**view(row), "stale": stale}
        if row is None and not refresh_if_stale:
            raise NotFound("no summary yet")
    return {**summarize(user, conversation_id, "mcp" if refresh_if_stale else "manual", use_model=False), "stale": False}


# ------------------------------------------------------------------------------------ idle sweep
@platform_loop
def summarize_idle(now: Any = None, *, idle_after: timedelta = IDLE_AFTER, limit: int = 50) -> list[str]:
    """Conversations quiet for ``idle_after`` with messages newer than their last summary get one. Selection runs
    in system scope; each summary is written as the conversation's own tenant. Only active workspaces: a disabled or
    archived one cannot be summarized, and its oldest conversations would otherwise fill every batch forever."""
    from analystos.db.models import Workspace

    now = now or utcnow()
    with session_scope() as s:
        latest_turn = (select(AskTurn.thread_id, func.max(AskTurn.created_at).label("last"))
                       .group_by(AskTurn.thread_id).subquery())
        ids = list(s.scalars(select(AskThread.id).join(latest_turn, latest_turn.c.thread_id == AskThread.id)
                             .join(Workspace, Workspace.id == AskThread.workspace_id)
                             .where(Workspace.status == "active", Workspace.deleted_at.is_(None))
                             .where(latest_turn.c.last < now - idle_after,
                                    (AskThread.summarized_at.is_(None)) | (AskThread.summarized_at < latest_turn.c.last))
                             .order_by(latest_turn.c.last).limit(limit)))
    done = []
    for conv_id in ids:
        try:
            _summarize_owned(conv_id)
            done.append(conv_id)
        except Exception:  # noqa: BLE001 - one conversation never stops the sweep
            log.exception("idle summary of conversation %s failed", conv_id)
    return done


def _summarize_owned(conversation_id: str) -> None:
    with as_owner_of("ask_thread", conversation_id), session_scope() as s:
        conv = s.get(AskThread, conversation_id)
        if conv is not None:
            write(s, conv, "idle")


def summarize_idle_quietly() -> list[str]:
    try:
        return summarize_idle()
    except Exception:  # noqa: BLE001
        log.exception("idle conversation summaries failed")
        return []

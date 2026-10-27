"""Escalation (spec S13 §3.4): SLA by priority, assignment by workspace rota, and the data pack prepared for
the analyst so a human starts from the context, the candidate SQL and the partial evidence, not from zero.

Workspace settings (``workspace.settings["desk"]``): ``rota`` (user ids, in order), ``sla_hours``
({priority: hours}), ``review_ad_hoc`` (ad hoc auto answers go to the queue first)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from analystos.db.models import AskTurn, ContextEntry, SourceAsset, Workspace, WorkspaceMember
from analystos.desk.models import Request
from analystos.desk.states import OPEN, QUEUE_STATES, sla_due
from analystos.desk.triage import content_words

ANALYST_ROLES = ("analyst", "editor", "owner")
MAX_RECEIPTS = 10


def desk_settings(session: Session, workspace_id: str) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    return dict(((ws.settings or {}) if ws is not None else {}).get("desk") or {})


def pick_assignee(session: Session, workspace_id: str, exclude: str | None = None) -> str | None:
    """The rota's (else the workspace analysts') member with the fewest open assigned requests; ties keep
    rota order (else user id). The requester is skipped when someone else can take it."""
    analysts = list(session.scalars(select(WorkspaceMember.user_id).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role.in_(ANALYST_ROLES)).order_by(WorkspaceMember.user_id)))
    rota = [u for u in desk_settings(session, workspace_id).get("rota") or [] if u in analysts] or analysts
    if exclude in rota and len(rota) > 1:
        rota = [u for u in rota if u != exclude]
    if not rota:
        return None
    load = dict(session.execute(select(Request.assignee_id, func.count()).where(
        Request.workspace_id == workspace_id, Request.assignee_id.in_(rota), Request.state.in_(sorted(OPEN)))
        .group_by(Request.assignee_id)).all())
    return min(rota, key=lambda u: (load.get(u, 0), rota.index(u)))


def build_data_pack(session: Session, req: Request, *, turn: AskTurn | None = None, reason: str,
                    refusal: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the analyst gets with an escalated request. Every entry is a reference or text the platform already
    holds; nothing in it is an instruction (context is data, CLAUDE.md rule 7)."""
    terms = [w for w in content_words(req.text) if len(w) > 2][:12]
    receipts: list[dict[str, Any]] = []
    if terms:
        like = [ContextEntry.name.ilike(f"%{t}%") for t in terms]
        for e in session.scalars(select(ContextEntry).where(ContextEntry.workspace_id == req.workspace_id, or_(*like))
                                 .order_by(ContextEntry.created_at.desc()).limit(MAX_RECEIPTS)):
            receipts.append({"kind": "context_entry", "id": e.id, "entry_kind": e.kind, "name": e.name,
                             "trusted": bool(e.trusted), "excerpt": (e.body or "")[:300]})
        names = [SourceAsset.name.ilike(f"%{t}%") for t in terms] + [SourceAsset.business_name.ilike(f"%{t}%") for t in terms]
        for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == req.workspace_id,
                                                           SourceAsset.selected.is_(True), or_(*names)).limit(MAX_RECEIPTS)):
            receipts.append({"kind": "asset", "id": a.id, "asset": f"{a.schema_name}.{a.name}",
                             "business_name": a.business_name, "excerpt": (a.description or "")[:300]})
    candidate_sql: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if turn is not None:
        if turn.sql:
            candidate_sql.append({"sql": turn.sql, "from": f"ask_turn:{turn.id}", "status": turn.status})
        for attempt in (turn.attempts or [])[:5]:
            if isinstance(attempt, dict) and attempt.get("sql") and attempt.get("sql") != turn.sql:
                candidate_sql.append({"sql": attempt["sql"], "from": f"ask_turn:{turn.id}", "status": attempt.get("status"),
                                      "error": (attempt.get("error") or "")[:300] or None})
        # The inspector holds the turn's model calls and decisions, including SQL the gateway refused (a refusal
        # raised by the Ask path does not keep its attempts on the turn).
        evidence.append({"kind": "ask_turn", "id": turn.id, "thread_id": turn.thread_id, "status": turn.status,
                         "inspector": f"/api/ask/turns/{turn.id}/inspector"})
        query_id = ((turn.result or {}) if isinstance(turn.result, dict) else {}).get("query_id")
        if query_id:
            evidence.append({"kind": "query", "id": query_id})
    evidence += [{"kind": "run", "id": r} for r in req.run_ids or []]
    return {"reason": reason, "triage": dict(req.triage or {}), "intent": dict(req.normalized_intent or {}),
            "missing": list((req.triage or {}).get("missing") or []) + list((refusal or {}).get("missing") or []),
            "refusal": {k: refusal.get(k) for k in ("kind", "title", "message", "remedy") if refusal.get(k)} if refusal else None,
            "context_receipts": receipts, "candidate_sql": candidate_sql, "evidence": evidence}


def apply_escalation(session: Session, req: Request, now: datetime, *, turn: AskTurn | None = None, reason: str,
                     refusal: dict[str, Any] | None = None) -> None:
    """Fields an escalated request carries; the state change itself is the caller's (service.move)."""
    settings = desk_settings(session, req.workspace_id)
    if req.sla_due_at is None:
        req.sla_due_at = sla_due(now, req.priority, settings.get("sla_hours"))
    if req.assignee_id is None:
        req.assignee_id = pick_assignee(session, req.workspace_id, exclude=req.requester_id)
    req.data_pack = build_data_pack(session, req, turn=turn, reason=reason, refusal=refusal)


def overdue(session: Session, now: datetime, workspace_id: str | None = None, limit: int = 200) -> list[Request]:
    """Queue requests past their SLA that have not been flagged yet (bounded per sweep)."""
    stmt = select(Request).where(Request.state.in_(sorted(QUEUE_STATES)), Request.sla_due_at.is_not(None),
                                 Request.sla_due_at < now, Request.sla_breached_at.is_(None))
    if workspace_id:
        stmt = stmt.where(Request.workspace_id == workspace_id)
    return list(session.scalars(stmt.order_by(Request.sla_due_at).limit(limit)))

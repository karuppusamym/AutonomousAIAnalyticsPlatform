"""Request Desk service (spec S13): intake, dedupe, triage, the auto paths, escalation, actions, views, value.

Sessions are short (CLAUDE.md): triage and the Ask path may call a model, so each runs between
transactions, never inside one. Every state change goes through ``move`` (the state machine) and writes a
``request_event`` plus a ``request.*`` run event for the UI stream. Answers come only from the existing
governed paths — Ask (gateway, scope, numbers from executed SQL), investigation runs, reviewed context — so
the desk adds routing and accountability, never a new way to produce a number.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.api.serialize import row
from analystos.core.errors import AnalystOSError, Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    AskTurn,
    ContextEntry,
    ModelCall,
    SemanticMetric,
    User,
    VerifiedQuery,
    Workspace,
    WorkspaceMember,
)
from analystos.desk import dedupe, escalation, triage, value
from analystos.desk.models import ClassBaseline, Request, RequestEvent
from analystos.desk.states import (
    ANSWERED_STATES,
    CHANNELS,
    CLASSES,
    PRIORITIES,
    QUEUE_STATES,
    TERMINAL,
    check_transition,
    resolution_for,
    sla_due,
)
from analystos.events.bus import emit
from analystos.governance.policy import load_in_workspace, member_role, require_role, scoped_loader
from analystos.security.auth import role_at_least

log = get_logger(__name__)
MAX_TEXT = 4000
ANALYST = "analyst"
ASK_CLASSES = ("R1", "R2")
RUN_CLASSES = ("R3", "R6")
CONTEXT_CLASSES = ("R7",)
GOVERNED_BY = ("registry", "semantic")  # verified query or certified metric: no review needed
ACTIONS = ("accept", "reopen", "assign", "escalate", "answer", "decline", "mark_duplicate", "certify")
RUN_DONE, RUN_FAILED = {"COMPLETED"}, {"FAILED", "REJECTED", "CANCELLED"}
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
VALUE_MAX_ROWS = 50_000


# ------------------------------------------------------------------------------------------- events
def _event(session: Session, req: Request, type_: str, actor: str, *, from_state: str | None = None,
           to_state: str | None = None, payload: dict[str, Any] | None = None) -> None:
    seq = (session.scalar(select(func.max(RequestEvent.seq)).where(RequestEvent.request_id == req.id)) or 0) + 1
    session.add(RequestEvent(id=new_id("rqe"), request_id=req.id, workspace_id=req.workspace_id, seq=seq, type=type_,
                             from_state=from_state, to_state=to_state, actor=actor, payload=payload or {}))
    emit(req.workspace_id, f"request.{type_}", {"request_id": req.id, "from": from_state, "to": to_state,
                                                "class": req.request_class, **(payload or {})},
         actor=actor, session=session)
    session.flush()


def move(session: Session, req: Request, dst: str, actor: str, *, type_: str = "state_changed",
         payload: dict[str, Any] | None = None, now: datetime | None = None) -> None:
    """The only way a request changes state."""
    src = req.state
    check_transition(src, dst)
    now = now or utcnow()
    req.state, req.revision = dst, (req.revision or 0) + 1
    if dst in ANSWERED_STATES and req.first_answer_at is None:
        req.first_answer_at = now
        from analystos.observability.metrics import inc

        inc("analystos_desk_resolutions_total", mode=(req.answer_ref or {}).get("via") or dst)
    if dst in TERMINAL:
        req.closed_at = now
        req.resolution = req.resolution if req.resolution == "duplicate" else \
            resolution_for(dst, (req.answer_ref or {}).get("via"))
    _event(session, req, type_, actor, from_state=src, to_state=dst, payload=payload)


# ------------------------------------------------------------------------------------------- helpers
def _actor(user: User) -> str:
    return f"user:{user.id}"


def _is_analyst(session: Session, user: User, workspace_id: str) -> bool:
    return role_at_least(member_role(session, user, workspace_id), ANALYST)


def catalog(session: Session, workspace_id: str) -> dict[str, Any]:
    """What R1 can be answered from: approved metric names and active verified-query phrasings (names only)."""
    metrics: set[str] = set()
    for name, display in session.execute(select(SemanticMetric.name, SemanticMetric.display_name).where(
            SemanticMetric.workspace_id == workspace_id, SemanticMetric.status == "approved").limit(200)):
        metrics |= {n for n in (name, display) if n}
    verified: list[str] = []
    for name, patterns in session.execute(select(VerifiedQuery.name, VerifiedQuery.patterns).where(
            VerifiedQuery.workspace_id == workspace_id, VerifiedQuery.status == "active").limit(200)):
        verified += [p for p in (patterns or []) if isinstance(p, str)] or [name]
    return {"metrics": sorted(metrics), "verified": verified[:400]}


def _call_ctx(session: Session, workspace_id: str, request_id: str) -> Any:
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.llm.router import CallContext

    workspace = get_workspace(session, workspace_id)
    policy = load_policy(session, workspace)
    return CallContext.for_policy(policy, workspace_id=workspace_id, task_id=request_id, agent_id="desk",
                                  prompt_version="request_triage.v1", tenant_id=workspace.tenant_id,
                                  policy_version=workspace.policy_version)


def _router() -> Any:
    from analystos.runtime import context as runtime_context

    return runtime_context.default_services().router


def _cost(session: Session, task_ids: list[str]) -> float:
    ids = [t for t in task_ids if t]
    if not ids:
        return 0.0
    return float(session.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(ModelCall.task_id.in_(ids))) or 0.0)


# ------------------------------------------------------------------------------------------- intake
def submit(user: User, workspace_id: str, text: str, *, channel: str = "ui", priority: str = "normal",
           process: bool = True, conversation_id: str | None = None) -> dict[str, Any]:
    """Create a request, link it to an open duplicate or triage it, and (``process``) run its auto path.
    Returns the request as its requester sees it. ``conversation_id`` (S04, the requester's own conversation in this
    workspace) makes an Ask answer a message of that conversation instead of a new one."""
    text = " ".join((text or "").split())
    if len(text) < 3:
        raise InvalidInput("say what you need (at least a few words)")
    if len(text) > MAX_TEXT:
        raise InvalidInput(f"a request is at most {MAX_TEXT} characters")
    if channel not in CHANNELS:
        raise InvalidInput(f"channel must be one of {', '.join(CHANNELS)}")
    if priority not in PRIORITIES:
        raise InvalidInput(f"priority must be one of {', '.join(PRIORITIES)}")
    now = utcnow()
    with session_scope() as s:
        require_role(s, user, workspace_id, "viewer")
        if conversation_id is not None:
            from analystos.conversations.service import load as load_conversation

            load_conversation(s, s.merge(user), conversation_id, workspace_id)
        cat = catalog(s, workspace_id)
        rules = triage.classify_rules(text, cat)
        intent = triage.normalize_intent(text, rules.request_class, cat)
        req = Request(id=new_id("rq"), workspace_id=workspace_id, requester_id=user.id, channel=channel, text=text,
                      normalized_intent=intent, state="new", priority=priority, run_ids=[], triage={}, followers=[],
                      cost_usd=0.0, revision=1, conversation_id=conversation_id)
        s.add(req)
        s.flush()
        _event(s, req, "created", _actor(user), to_state="new", payload={"channel": channel, "priority": priority})
        original = dedupe.find_original(s, workspace_id, text, intent.get("key"), now, exclude_id=req.id)
        if original is not None:
            orig, score = original
            req.duplicate_of, req.resolution = orig.id, "duplicate"
            if user.id != orig.requester_id and user.id not in (orig.followers or []):
                orig.followers = [*(orig.followers or []), user.id]
            move(s, req, "declined", _actor(user), type_="duplicate_linked",
                 payload={"duplicate_of": orig.id, "similarity": round(score, 4)}, now=now)
            _event(s, orig, "follower_added", _actor(user), payload={"request_id": req.id, "similarity": round(score, 4)})
            return view(s, user, req)
        call_ctx = _call_ctx(s, workspace_id, req.id)
        request_id = req.id
    result = triage.triage(text, cat, router=_router(), call_ctx=call_ctx)
    with session_scope() as s:
        req = s.get(Request, request_id)
        req.request_class = result.request_class
        req.triage = result.as_dict()
        req.normalized_intent = {**(req.normalized_intent or {}), "outcome": result.intent.get("outcome")}
        move(s, req, "triaged", f"desk:{result.answered_by}", type_="triaged", payload=result.as_dict())
    if process:
        return process_request(user, request_id)
    with session_scope() as s:
        return view(s, user, s.get(Request, request_id))


def process_request(user: User, request_id: str) -> dict[str, Any]:
    """Run a triaged request's path: R1/R2 Ask, R3/R6 an investigation, R7 a context lookup, the rest escalate.
    ``user`` is the requester: every governed path runs under their scope, never a wider one."""
    with session_scope() as s:
        req = _load(s, user, request_id)
        if req.state != "triaged":
            return view(s, user, req)
        cls, ws, flags = req.request_class, req.workspace_id, set((req.triage or {}).get("flags") or [])
        text = req.text
        requester = s.get(User, req.requester_id)
        s.expunge(requester)
    try:
        if cls in ASK_CLASSES:
            return _ask_path(requester, request_id, ws, text, flags)
        if cls in RUN_CLASSES:
            return _run_path(requester, request_id, ws, text)
        if cls in CONTEXT_CLASSES:
            return _context_path(requester, request_id, ws, text)
    except AnalystOSError as exc:
        log.info("request %s auto path stopped: %s", request_id, exc.message)
        return _escalate_by_desk(requester, request_id, f"the auto path stopped: {exc.message}",
                                 refusal={"kind": exc.code, "message": exc.message})
    from analystos.desk.proposals import propose

    proposal = propose(cls, text)  # R4/R5: a draft schedule or monitor for the analyst, never created here
    return _escalate_by_desk(requester, request_id, triage.CLASS_LABELS.get(cls or "", "unclassified") +
                             (" — a draft is prepared; an analyst completes it and it goes through approval" if proposal
                              else " — a person handles this class"), proposal=proposal)


def _escalate_by_desk(user: User, request_id: str, reason: str, *, turn_id: str | None = None,
                      refusal: dict[str, Any] | None = None, proposal: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utcnow()
    with session_scope() as s:
        req = s.get(Request, request_id)
        turn = s.get(AskTurn, turn_id) if turn_id else None
        escalation.apply_escalation(s, req, now, turn=turn, reason=reason, refusal=refusal)
        if proposal is not None:
            req.data_pack = {**(req.data_pack or {}), "proposal": proposal}
        req.cost_usd = _cost(s, [req.id, turn_id or ""])
        move(s, req, "escalated", "desk", type_="escalated",
             payload={"reason": reason[:500], "assignee_id": req.assignee_id,
                      "sla_due_at": req.sla_due_at.isoformat() if req.sla_due_at else None}, now=now)
        return view(s, user, req)


def _ask_path(user: User, request_id: str, ws: str, text: str, flags: set[str]) -> dict[str, Any]:
    from analystos.services import ask as ask_svc

    with session_scope() as s:
        req = s.get(Request, request_id)
        if req.conversation_id:  # S04: a message in the requester's existing conversation
            thread = {"id": req.conversation_id}
        else:
            thread = ask_svc.create_thread(s, s.merge(user), ws, title=text[:300], channel=req.channel, request_id=req.id)
            req.conversation_id = thread["id"]
        move(s, req, "auto_answering", "desk", payload={"path": "ask", "conversation_id": thread["id"]})
    turn = ask_svc.ask_in_thread(user, thread["id"], text)
    if turn.get("status") != "answered":
        return _escalate_by_desk(user, request_id, "Ask did not answer: " + str((turn.get("refusal") or {}).get("title")
                                                                               or turn.get("status")),
                                 turn_id=turn["id"], refusal={**(turn.get("refusal") or {}), "status": turn.get("status")})
    governed = turn.get("answered_by") in GOVERNED_BY or turn.get("governance") == "governed"
    with session_scope() as s:
        req = s.get(Request, request_id)
        review = "sensitive" in flags or (not governed and escalation.desk_settings(s, ws).get("review_ad_hoc"))
        result = turn.get("result") or {}
        req.answer_ref = {"via": "auto_with_review" if review else "auto", "kind": "ask_turn", "turn_id": turn["id"],
                          "thread_id": thread["id"], "answered_by": turn.get("answered_by"),
                          "governance": "governed" if governed else "ad_hoc", "query_id": result.get("query_id"),
                          "explanation": turn.get("explanation")}
        req.human_minutes = 0
        req.cost_usd = _cost(s, [req.id, turn["id"]])
        if review:
            settings = escalation.desk_settings(s, ws)
            req.sla_due_at = sla_due(utcnow(), req.priority, settings.get("sla_hours"))
            req.assignee_id = req.assignee_id or escalation.pick_assignee(s, ws, exclude=req.requester_id)
            move(s, req, "auto_with_review", "desk", payload={"turn_id": turn["id"], "why": sorted(flags & {"sensitive"})
                                                              or ["review_ad_hoc"], "assignee_id": req.assignee_id})
        else:
            move(s, req, "auto_answered", "desk", payload={"turn_id": turn["id"], "answered_by": turn.get("answered_by")})
        return view(s, user, req)


def _run_path(user: User, request_id: str, ws: str, text: str) -> dict[str, Any]:
    from analystos.services.runs import create_run

    run = create_run(user, ws, objective=text, origin={"type": "request", "request_id": request_id})
    with session_scope() as s:
        req = s.get(Request, request_id)
        req.run_ids = [*(req.run_ids or []), run.id]
        move(s, req, "in_investigation", "desk", payload={"path": "investigation", "run_id": run.id})
        return view(s, user, req)


def _context_path(user: User, request_id: str, ws: str, text: str) -> dict[str, Any]:
    """R7: a reviewed definition the workspace already holds answers it; otherwise its owner decides."""
    terms = [w for w in triage.content_words(text) if len(w) > 2 and w not in ("counts", "mean", "defined", "definition")]
    with session_scope() as s:
        req = s.get(Request, request_id)
        move(s, req, "auto_answering", "desk", payload={"path": "context"})
        found = None
        if terms:
            entries = list(s.scalars(select(ContextEntry).where(
                ContextEntry.workspace_id == ws, ContextEntry.trusted.is_(True),
                ContextEntry.kind.in_(("term", "definition", "metric"))).limit(500)))
            phrase = " ".join(terms)
            for e in entries:
                names = [e.name, *(e.synonyms or [])]
                if any(" ".join(triage.content_words(n)) and " ".join(triage.content_words(n)) in phrase for n in names):
                    found = e
                    break
        if found is not None:
            req.answer_ref = {"via": "auto", "kind": "context_entry", "entry_id": found.id, "name": found.name,
                              "definition": (found.body or "")[:2000]}
            req.human_minutes = 0
            move(s, req, "auto_answered", "desk", payload={"entry_id": found.id})
            return view(s, user, req)
    return _escalate_by_desk(user, request_id, "no reviewed definition matches this term; the glossary owner decides")


# ------------------------------------------------------------------------------------------- reads
@scoped_loader
def _load(session: Session, user: User, request_id: str, *, for_update: bool = False) -> Request:
    """A request is visible to its requester, its followers and the workspace's analysts; anyone else
    (another workspace, another tenant) gets the same 404."""
    req = load_in_workspace(session, Request, request_id, user=user, label="request", for_update=for_update)
    if req.requester_id != user.id and user.id not in (req.followers or []) and \
            not _is_analyst(session, user, req.workspace_id):
        raise NotFound("request not found")
    return req


def reconcile(session: Session, req: Request) -> None:
    """An investigation request follows its run: completed → the analyst reviews the findings before they are
    shared (product spec §4 R3); failed → escalated."""
    if req.state != "in_investigation" or not req.run_ids:
        return
    run = session.get(AnalysisRun, req.run_ids[-1])
    if run is None or run.status not in RUN_DONE | RUN_FAILED:
        return
    now = utcnow()
    if run.status in RUN_DONE:
        req.answer_ref = {"via": "auto_with_review", "kind": "run", "run_id": run.id}
        req.sla_due_at = req.sla_due_at or sla_due(now, req.priority,
                                                              escalation.desk_settings(session, req.workspace_id).get("sla_hours"))
        req.assignee_id = req.assignee_id or escalation.pick_assignee(session, req.workspace_id, exclude=req.requester_id)
        move(session, req, "auto_with_review", "desk", payload={"run_id": run.id}, now=now)
    else:
        escalation.apply_escalation(session, req, now, reason=f"the investigation ended {run.status}")
        move(session, req, "escalated", "desk", type_="escalated", payload={"run_id": run.id, "run_status": run.status},
             now=now)


def view(session: Session, user: User, req: Request, *, timeline: bool = False) -> dict[str, Any]:
    analyst = _is_analyst(session, user, req.workspace_id)
    out = row(req, exclude={"request_class", "data_pack", "answer_ref"})
    out["class"] = req.request_class
    out["answer"] = req.answer_ref if (analyst or req.state in ANSWERED_STATES) else None
    if analyst:
        out["data_pack"] = req.data_pack
    if req.duplicate_of:
        orig = session.get(Request, req.duplicate_of)
        out["original"] = {"id": orig.id, "state": orig.state,
                           "answer": orig.answer_ref if orig.state in ANSWERED_STATES else None} if orig else None
    out["sla_breached"] = req.sla_breached_at is not None
    if timeline:
        out["timeline"] = [{k: e[k] for k in ("seq", "type", "from_state", "to_state", "actor", "payload", "created_at")}
                           for e in (row(x) for x in session.scalars(select(RequestEvent).where(
                               RequestEvent.request_id == req.id).order_by(RequestEvent.seq)))]
    return out


@scoped_loader
def get_request(session: Session, user: User, request_id: str) -> dict[str, Any]:
    req = _load(session, user, request_id)
    reconcile(session, req)
    return view(session, user, req, timeline=True)


def _workspaces(session: Session, user: User, minimum: str) -> list[str]:
    if user.is_admin:
        return list(session.scalars(select(Workspace.id).where(Workspace.deleted_at.is_(None))))
    return [ws for ws, role in session.execute(select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
        WorkspaceMember.user_id == user.id)) if role_at_least(role, minimum)]


def list_requests(session: Session, user: User, *, view_name: str = "mine", workspace_id: str | None = None,
                  state: str | None = None, request_class: str | None = None, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """mine = what I asked; queue = work waiting on an analyst (escalated, review, reopened), mine first, by SLA;
    all = every request of the workspaces I am an analyst in."""
    if view_name not in ("mine", "queue", "all"):
        raise InvalidInput("view must be mine, queue or all")
    if request_class is not None and request_class not in CLASSES:
        raise InvalidInput("class must be one of R1..R9")
    minimum = "viewer" if view_name == "mine" else ANALYST
    if workspace_id is not None:
        require_role(session, user, workspace_id, minimum)
        spaces = [workspace_id]
    else:
        spaces = _workspaces(session, user, minimum)
    if not spaces:
        return []
    stmt = select(Request).where(Request.workspace_id.in_(spaces))
    if view_name == "mine":
        stmt = stmt.where(Request.requester_id == user.id)
    elif view_name == "queue":
        sweep_sla(session, workspace_id=workspace_id)
        stmt = stmt.where(Request.state.in_(sorted(QUEUE_STATES)))
    if state:
        stmt = stmt.where(Request.state == state)
    if request_class:
        stmt = stmt.where(Request.request_class == request_class)
    if view_name == "queue":
        mine_first = (Request.assignee_id == user.id).desc()
        stmt = stmt.order_by(mine_first, Request.sla_due_at.asc().nulls_last(), Request.created_at)
    else:
        stmt = stmt.order_by(Request.created_at.desc())
    reqs = list(session.scalars(stmt.limit(min(max(limit, 1), MAX_LIMIT))))
    return [view(session, user, r) for r in reqs]


def sweep_sla(session: Session, *, workspace_id: str | None = None, now: datetime | None = None) -> list[str]:
    """Flag queue requests past their SLA once: an event on the timeline and a notification to the assignee
    (or the workspace when nobody is assigned)."""
    from analystos.services.notifications import notify

    now = now or utcnow()
    flagged = []
    for req in escalation.overdue(session, now, workspace_id):
        req.sla_breached_at = now
        _event(session, req, "sla_breached", "desk", payload={"sla_due_at": req.sla_due_at.isoformat() if req.sla_due_at else None,
                                                              "assignee_id": req.assignee_id, "priority": req.priority})
        notify(session, req.workspace_id, kind="request_sla", title=f"Request past its SLA: {req.text[:120]}",
               body=f"Priority {req.priority}; state {req.state}.", link={"type": "request", "id": req.id},
               user_id=req.assignee_id)
        flagged.append(req.id)
    return flagged


# ------------------------------------------------------------------------------------------- actions
@scoped_loader
def act(session: Session, user: User, request_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise InvalidInput(f"action must be one of {', '.join(ACTIONS)}")
    payload = dict(payload or {})
    req = _load(session, user, request_id, for_update=True)
    reconcile(session, req)
    analyst = _is_analyst(session, user, req.workspace_id)
    actor = _actor(user)
    now = utcnow()
    if action in ("accept", "reopen"):
        if req.requester_id != user.id:
            raise Forbidden("only the requester accepts or reopens a request")
        if action == "accept":
            move(session, req, "accepted", actor, type_="accepted", now=now)
        else:
            reason = str(payload.get("reason") or "").strip()[:1000]
            move(session, req, "reopened", actor, type_="reopened", payload={"reason": reason}, now=now)
            escalation.apply_escalation(session, req, now, reason=f"the requester reopened it: {reason or 'no reason given'}",
                                        turn=session.get(AskTurn, (req.answer_ref or {}).get("turn_id") or "") or None)
            move(session, req, "escalated", "desk", type_="escalated", payload={"reason": "reopened"}, now=now)
        return view(session, user, req, timeline=True)
    if not analyst:
        raise Forbidden(f"'{action}' is an analyst action")
    if action == "assign":
        assignee = str(payload.get("assignee_id") or user.id)
        role = session.scalar(select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == req.workspace_id,
                                                                 WorkspaceMember.user_id == assignee))
        target = session.get(User, assignee)
        if not (role_at_least(role, ANALYST) or (target is not None and target.is_admin)):
            raise InvalidInput("the assignee must be an analyst of this workspace")
        if req.state in TERMINAL:
            raise Conflict("a closed request cannot be assigned")
        previous, req.assignee_id = req.assignee_id, assignee
        _event(session, req, "assigned", actor, payload={"assignee_id": assignee, "previous": previous})
    elif action == "escalate":
        reason = str(payload.get("reason") or "an analyst took it over")[:1000]
        escalation.apply_escalation(session, req, now, reason=reason,
                                    turn=session.get(AskTurn, (req.answer_ref or {}).get("turn_id") or "") or None)
        move(session, req, "escalated", actor, type_="escalated", payload={"reason": reason}, now=now)
    elif action == "answer":
        _answer(session, req, user, payload, now)
    elif action == "certify":  # S13-T5: the analyst's answer becomes reusable context in the S03 ledger
        from analystos.desk.certify import certify

        record = certify(session, req, user, payload)
        _event(session, req, "certified", actor, payload=record)
    elif action == "decline":
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise InvalidInput("say why the request is declined")
        move(session, req, "declined", actor, type_="declined", payload={"reason": reason[:1000]}, now=now)
    elif action == "mark_duplicate":
        orig_id = str(payload.get("of") or "")
        orig = session.get(Request, orig_id) if orig_id else None
        if orig is None or orig.workspace_id != req.workspace_id or orig.id == req.id or orig.state in TERMINAL:
            raise InvalidInput("`of` must be another open request of this workspace")
        req.duplicate_of, req.resolution = orig.id, "duplicate"
        if req.requester_id != orig.requester_id and req.requester_id not in (orig.followers or []):
            orig.followers = [*(orig.followers or []), req.requester_id]
        move(session, req, "declined", actor, type_="duplicate_linked", payload={"duplicate_of": orig.id}, now=now)
        _event(session, orig, "follower_added", actor, payload={"request_id": req.id})
    return view(session, user, req, timeline=True)


def _answer(session: Session, req: Request, user: User, payload: dict[str, Any], now: datetime) -> None:
    """An analyst answers an escalated request, or approves/edits the prepared answer of one in review. The
    minutes they spent are recorded: they are the human cost the value metrics weigh against the baseline."""
    try:
        minutes = int(payload.get("human_minutes"))
    except (TypeError, ValueError):
        raise InvalidInput("human_minutes (the minutes you spent) is required") from None
    if minutes < 0 or minutes > 60 * 24 * 30:
        raise InvalidInput("human_minutes must be between 0 and 43200")
    reviewed = req.state == "auto_with_review"
    text = str(payload.get("text") or "").strip()
    if not reviewed and not text:
        raise InvalidInput("an answer needs text")
    evidence = [e for e in payload.get("evidence") or [] if isinstance(e, dict)][:20]
    prior = dict(req.answer_ref or {})
    req.answer_ref = {**(prior if reviewed else {}), "via": "auto_with_review" if reviewed else "human",
                      "answered_by_user": user.id, **({"text": text[:20000]} if text else {}),
                      **({"evidence": evidence} if evidence else {}), "edited": bool(reviewed and text)}
    req.human_minutes = (req.human_minutes or 0) + minutes
    move(session, req, "answered", _actor(user), type_="answered",
         payload={"via": req.answer_ref["via"], "human_minutes": minutes}, now=now)


# ------------------------------------------------------------------------------------------- value
def baselines(session: Session, workspace_id: str) -> dict[str, float]:
    return {c: float(m) for c, m in session.execute(select(ClassBaseline.request_class, ClassBaseline.baseline_minutes)
                                                     .where(ClassBaseline.workspace_id == workspace_id))}


def value_report(session: Session, user: User, workspace_id: str, *, start: datetime | None = None,
                 end: datetime | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "viewer")
    end = end or utcnow()
    start = start or end - timedelta(days=30)
    if start >= end:
        raise InvalidInput("`from` must be before `to`")
    cols = (Request.request_class, Request.state, Request.resolution, Request.created_at, Request.first_answer_at,
            Request.closed_at, Request.human_minutes, Request.cost_usd)
    rows_ = session.execute(select(*cols).where(Request.workspace_id == workspace_id, Request.created_at >= start,
                                                Request.created_at < end).limit(VALUE_MAX_ROWS)).all()
    data = [{"class": r[0], "state": r[1], "resolution": r[2], "created_at": r[3], "first_answer_at": r[4],
             "closed_at": r[5], "human_minutes": r[6], "cost_usd": r[7]} for r in rows_]
    from analystos.desk.rollup import daily_series

    daily = daily_series(session, workspace_id, start.date(), end.date())
    return {"workspace_id": workspace_id, "from": start.isoformat(), "to": end.isoformat(),
            "truncated": len(data) >= VALUE_MAX_ROWS, **value.compute(data, baselines(session, workspace_id)),
            # The nightly rollup (desk_daily): the trend by day the requests were created, and when it last ran.
            "daily": daily, "rollup_computed_at": max((d["computed_at"] for d in daily if d["computed_at"]), default=None)}


EXPORT_COLUMNS = ("day", "class", "n", "duplicates", "closed", "accepted", "auto_resolved", "auto_resolution_rate",
                  "accepted_rate", "tta_p50_seconds", "tta_p95_seconds", "human_minutes", "est_minutes_saved",
                  "unbaselined", "cost_usd", "computed_at")
MAX_RANGE_DAYS = 366


def _days(start: datetime | None, end: datetime | None) -> tuple[Any, Any]:
    end_d = (end or utcnow()).date()
    start_d = start.date() if start else end_d - timedelta(days=29)
    if start_d > end_d:
        raise InvalidInput("`from` must be before `to`")
    if (end_d - start_d).days >= MAX_RANGE_DAYS:
        raise InvalidInput(f"a range is at most {MAX_RANGE_DAYS} days")
    return start_d, end_d


def value_export(session: Session, user: User, workspace_id: str, *, start: datetime | None = None,
                 end: datetime | None = None) -> str:
    """The rollup as CSV (FR-053 "exportable"): aggregates about the desk's own work, one row per day and class.
    No request text or answer leaves through it."""
    import csv
    import io

    require_role(session, user, workspace_id, "viewer")
    start_d, end_d = _days(start, end)
    from analystos.desk.rollup import daily_series

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in daily_series(session, workspace_id, start_d, end_d, request_class=None):
        writer.writerow(r)
    return buf.getvalue()


def refresh_value(session: Session, user: User, workspace_id: str, *, start: datetime | None = None,
                  end: datetime | None = None) -> dict[str, Any]:
    """Recompute the rollup for a range now (an analyst's "refresh"), instead of waiting for the nightly job."""
    from analystos.desk.rollup import rollup_in

    require_role(session, user, workspace_id, ANALYST)
    start_d, end_d = _days(start, end)
    rows_written = rollup_in(session, workspace_id, start_d, end_d)
    return {"workspace_id": workspace_id, "from": start_d.isoformat(), "to": end_d.isoformat(), "rows": rows_written}


def set_baseline(session: Session, user: User, workspace_id: str, request_class: str, minutes: float,
                 source: str = "practitioner_protocol") -> dict[str, Any]:
    require_role(session, user, workspace_id, "owner")
    if request_class not in CLASSES:
        raise InvalidInput("class must be one of R1..R9")
    if not 0 < float(minutes) <= 60 * 24 * 30:
        raise InvalidInput("baseline minutes must be positive")
    b = session.scalar(select(ClassBaseline).where(ClassBaseline.workspace_id == workspace_id,
                                                   ClassBaseline.request_class == request_class))
    if b is None:
        b = ClassBaseline(id=new_id("cb"), workspace_id=workspace_id, request_class=request_class,
                          baseline_minutes=float(minutes), source=source)
        session.add(b)
    else:
        b.baseline_minutes, b.source, b.measured_at = float(minutes), source, utcnow()
    session.flush()
    return {**row(b, exclude={"request_class"}), "class": request_class}


def list_baselines(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    return [{**row(b, exclude={"request_class"}), "class": b.request_class}
            for b in session.scalars(select(ClassBaseline).where(ClassBaseline.workspace_id == workspace_id)
                                     .order_by(ClassBaseline.request_class))]

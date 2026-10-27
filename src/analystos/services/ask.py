"""Ask threads (P4-U02): persisted conversations over the governed Ask path.

A turn runs `agents/sql_agent.ask` unchanged in what it may execute (route and clarify decisions,
registry or generation, then gateway validation and scope); this module adds what the UI needs
around it: the thread, the plain-language stages streamed while it runs, one refusal per kind with
a remedy, provenance and staleness, the inspector (decisions and model-call receipts recorded with
`task_id = turn id`), and promotions of an answer into platform objects.

Promotions that stay inside the platform (verified query, metric, monitor, "Investigate why") are
role-checked and validated through the gateway (a metric is proposed to the semantic layer, where an
approver approves it); adding an answer to a dashboard is meant for a BI
audience, so it is an approval bound to the payload hash and runs only after `verify_for_execution`
(then it is published through the BI publisher; services/ask_outputs.py). "Save as report" renders the
stored answer as an HTML report. A pending promotion stays on the turn with its approval's status.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from analystos.api.serialize import row, rows
from analystos.core.errors import (
    AnalystOSError,
    BudgetExceeded,
    Forbidden,
    InvalidInput,
    ModelRouteUnavailable,
    ModelUnavailable,
    NotAnswerable,
    NotFound,
    PolicyDenied,
    QueryTimeout,
    SpendCapReached,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import (
    Approval,
    Artifact,
    AskThread,
    AskTurn,
    DecisionRecord,
    ModelCall,
    QueryExecution,
    Source,
    SourceAsset,
    User,
)
from analystos.events.bus import emit
from analystos.events.stream import REAUTH_SECONDS, StreamGuard, terminal_frame
from analystos.governance.audit import audit
from analystos.governance.policy import (
    get_workspace,
    load_in_workspace,
    load_policy,
    require_role,
    resolve_scope,
    scoped_loader,
)
from analystos.services.citations import record_turn_quietly

log = get_logger(__name__)
AGING_AFTER = timedelta(hours=24)
STALE_AFTER = timedelta(days=7)
PROMOTE_TARGETS = ("verified_query", "metric", "monitor", "dashboard", "investigate", "report")
# What a caller sees when the Ask path failed in a way no refusal kind describes (the cause is only logged).
UNEXPECTED_FAILURE = "The question stopped because of an unexpected error. Nothing was answered from a guess."
MODES = ("quick", "analyst")
DASHBOARD_ACTION = "ask.add_to_dashboard"
NEW_THREAD_TITLE = "New question"

# One refusal state per kind, each with the remedy the UI shows next to it.
REFUSALS: dict[str, dict[str, str]] = {
    "needs_input": {"title": "A required input is missing",
                    "remedy": "Choose a value for each missing input and ask again. Nothing was guessed."},
    "clarify": {"title": "The question needs more detail",
                "remedy": "Say what to measure (a count, a rate, an average), over which records and period, and how to group it."},
    "not_answerable": {"title": "The data in scope cannot answer this",
                       "remedy": "Nothing was guessed. Ask about the tables and columns you can see, or add a source that "
                                 "holds this data. Ask only reads data: changes go through the owning system."},
    "sql_rejected": {"title": "The query gateway refused the SQL",
                     "remedy": "Rephrase the question around the tables you can see, or paste the SQL into Explain to see why."},
    "policy_denied": {"title": "Your access does not cover this",
                      "remedy": "Ask a workspace owner for access to this data or to the SQL tool."},
    "budget_exceeded": {"title": "The Ask budget is used up",
                        "remedy": "Wait for the hourly budget to reset, or ask a workspace owner to raise it."},
    "spend_cap": {"title": "The model spend cap is reached",
                  "remedy": "No model was called. Model use resumes when the cap period resets (00:00 UTC for the daily "
                            "platform cap, the 1st of the month for the workspace cap), or a platform administrator raises "
                            "it (Admin > Settings: llm.daily_spend_cap_usd; workspace policy: workspace_monthly_cost_budget_usd). "
                            "Meanwhile write the SQL yourself or use a verified query."},
    "no_model": {"title": "No verified answer, and no model may write SQL here",
                 "remedy": "Write the SQL yourself (Explain checks it first), or save a verified query for this question."},
    # Why generation got no SQL: one kind per cause (the router's reason, not a generic "no model route").
    "mode_off": {"title": "SQL writing by a model is turned off",
                 "remedy": "An administrator set the sql_generation purpose to off (Admin > Models). Ask for it to be set to "
                           "auto, write the SQL yourself (Explain checks it first), or save a verified query."},
    "no_api_key": {"title": "No model provider key is set for the API",
                   "remedy": "Set {env} for the api and worker containers (docker compose reads it from your shell or the "
                             ".env file at `docker compose up`), then restart them: `docker compose up -d api worker`. A key "
                             "exported after the containers started is not seen by them."},
    "provider_cooldown": {"title": "The model provider refused for credits",
                          "remedy": "The provider answered HTTP 402: the account's credits were exhausted. After topping up, "
                                    "Ask retries the provider automatically in {retry_in_s} s (the cooldown after a 402); "
                                    "ask again then. Nothing was answered from a guess."},
    "policy_blocked": {"title": "The workspace policy does not allow this model provider",
                       "remedy": "The workspace allowed_providers (or the air-gapped install) excludes the provider of the "
                                 "sql_generation profile. A workspace owner can allow it, or an administrator can route the "
                                 "purpose to an allowed provider in Admin > Models."},
    "residency_blocked": {"title": "No model meets the workspace data residency",
                          "remedy": "No model of the sql_generation profile has a known region matching the workspace "
                                    "data_residency ({data_residency}); unknown regions fail closed. Add a model with that "
                                    "region to the profile, or change the residency rule."},
    "approval_required": {"title": "This model call needs an approval first",
                          "remedy": "The estimated cost is above the workspace approval threshold, or the model has no "
                                    "price yet (unpriced models fail closed). Price the model in Admin > Models, choose a "
                                    "cheaper one, or raise expensive_model_approval_usd."},
    "model_budget": {"title": "The model budget is used up",
                     "remedy": "The workspace monthly (or run) model budget is spent. A workspace owner can raise it; "
                               "verified queries and rule answers still work without a model."},
    "cap_reached": {"title": "The per-purpose model cap is reached",
                    "remedy": "SQL generation used its per-run cap ({cap}: {used} of {limit}). An administrator can raise "
                              "the cap in Admin > Models."},
    "context_over_budget": {"title": "The question's context is too large for the model budget",
                            "remedy": "Select fewer tables for this workspace, or narrow the question to one table; an "
                                      "administrator can raise the prompt limit in Admin > Models."},
    "invalid_output": {"title": "The model did not return usable SQL",
                       "remedy": "Ask again, rephrase the question around one table, or write the SQL yourself "
                                 "(Explain checks it first)."},
    "no_scope": {"title": "Nothing in scope to answer from",
                 "remedy": "Add a source, discover it and select its tables in Sources."},
    "timeout": {"title": "The query took too long",
                "remedy": "Narrow the question: a shorter period, fewer groups or a filter."},
    "unavailable": {"title": "A source or model is unavailable right now",
                    "remedy": "Try again in a minute. Nothing was answered from a guess."},
    "failed": {"title": "The question could not be answered",
               "remedy": "Rephrase it, or paste SQL into Explain to check it against the gateway."},
}


# ModelOutcome codes (agents/common.py) -> refusal kind.
MODEL_REFUSALS = {"mode_off": "mode_off", "no_api_key": "no_api_key", "provider_cooldown": "provider_cooldown",
                  "policy_blocked": "policy_blocked", "residency_blocked": "residency_blocked",
                  "approval_required": "approval_required", "budget_exceeded": "model_budget", "cap_reached": "cap_reached",
                  "context_over_budget": "context_over_budget", "invalid_output": "invalid_output",
                  "upstream_unavailable": "unavailable"}
_REMEDY_DEFAULTS = {"env": "OPENROUTER_API_KEY", "retry_in_s": "about 60", "data_residency": "the workspace rule",
                    "cap": "calls", "used": "all", "limit": "the cap"}


class _RemedyValues(dict):
    def __missing__(self, key: str) -> str:
        return _REMEDY_DEFAULTS.get(key, "")


def refusal(kind: str, message: str, **details: Any) -> dict[str, Any]:
    spec = REFUSALS[kind]
    values = _RemedyValues({k: v for k, v in details.items() if v is not None})
    return {"kind": kind, "title": spec["title"], "message": message, "remedy": spec["remedy"].format_map(values),
            "details": details}


def refusal_for(exc: AnalystOSError) -> dict[str, Any]:
    """The refusal kind of a failed Ask, from the error class (not its wording where a class exists)."""
    if isinstance(exc, ModelUnavailable):
        reason = str(exc.details.get("reason") or "")
        return refusal(MODEL_REFUSALS.get(reason, "no_model"), exc.message, **{**exc.details, "code": exc.code})
    if isinstance(exc, NotAnswerable):
        kind = "not_answerable"
    elif isinstance(exc, SQLRejected):
        kind = "sql_rejected"
    elif isinstance(exc, SpendCapReached):
        kind = "spend_cap"
    elif isinstance(exc, BudgetExceeded):
        kind = "budget_exceeded"
    elif isinstance(exc, (PolicyDenied, Forbidden)):
        kind = "policy_denied"
    elif isinstance(exc, QueryTimeout):
        kind = "timeout"
    elif isinstance(exc, (UpstreamUnavailable, ModelRouteUnavailable)):
        kind = "unavailable"
    elif isinstance(exc, InvalidInput) and "SQL generation unavailable" in exc.message:
        kind = "no_model"
    else:
        kind = "failed"
    return refusal(kind, exc.message, code=exc.code, **({k: v for k, v in exc.details.items() if k in ("scope", "used", "limit", "cap", "spent_usd", "limit_usd", "resets_at", "reason", "by")}))


# ------------------------------------------------------------------------------ ad hoc context
@dataclass
class AdhocContext:
    """The SQL agent's context outside a run (Ask, the query console, MCP `ask`). With a `turn_id`
    every model call and decision it makes is recorded against that Ask turn (`task_id`)."""

    user: User
    workspace: Any
    scope: Any
    policy: Any
    agent: Any
    services: Any
    run: Any = None
    task: Any = None
    turn_id: str | None = None
    thread_id: str | None = None  # compiled contexts are reused within a thread (CTX-005)
    on_stage: Callable[[str, str, dict[str, Any]], None] | None = None
    semantic_catalog: dict[str, Any] | None = None

    @property
    def router(self):
        return self.services.router

    @property
    def jev(self):
        return self.services.jev

    @property
    def subject(self) -> str | None:
        return f"ask:{self.turn_id}" if self.turn_id else None

    def call_ctx(self, exclude_families=None):
        from analystos.llm.router import CallContext

        return CallContext.for_policy(self.policy, workspace_id=self.workspace.id, task_id=self.turn_id, agent_id="sql",
                                      prompt_version="sql.v1", exclude_families=exclude_families or [],
                                      tenant_id=getattr(self.workspace, "tenant_id", None),
                                      scope_hash=self.scope.scope_hash() if hasattr(self.scope, "scope_hash") else None,
                                      policy_version=getattr(self.scope, "policy_version", None))

    def say(self, *a, **k):
        return None


def adhoc_context(session: Session, user: User, workspace_id: str) -> AdhocContext:
    from analystos.runtime.context import default_services
    from analystos.semantic.compiler import load_catalog
    from analystos.tools.registry import get_agent_spec

    scope = resolve_scope(session, session.merge(user), workspace_id)
    ws = get_workspace(session, workspace_id)
    ctx = AdhocContext(user=user, workspace=ws, scope=scope, policy=load_policy(session, ws), agent=get_agent_spec(session, "sql"),
                       services=default_services(), semantic_catalog=load_catalog(session, workspace_id))
    session.expunge_all()
    return ctx


# ------------------------------------------------------------------------------ threads
@scoped_loader
def _thread_for(session: Session, user: User, thread_id: str, workspace_id: str | None = None) -> AskThread:
    """Threads are private to their author (a question can reveal intent); the workspace role still applies."""
    thread = load_in_workspace(session, AskThread, thread_id, workspace_id, user=user, label="thread")
    if thread.user_id != user.id and not user.is_admin:
        raise NotFound("thread not found")
    return thread


@scoped_loader
def _turn_for(session: Session, user: User, turn_id: str, workspace_id: str | None = None) -> AskTurn:
    """`workspace_id` (a saved-analysis schedule's) additionally requires the turn to belong there."""
    turn = load_in_workspace(session, AskTurn, turn_id, workspace_id, user=user, label="question")
    try:
        _thread_for(session, user, turn.thread_id, turn.workspace_id)
    except NotFound:
        raise NotFound("question not found") from None
    return turn


def create_thread(session: Session, user: User, workspace_id: str, title: str | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "viewer")
    get_workspace(session, workspace_id)
    thread = AskThread(id=new_id("ask"), workspace_id=workspace_id, user_id=user.id,
                       title=(title or "").strip()[:300] or NEW_THREAD_TITLE, archived=False, revision=1)
    session.add(thread)
    session.flush()
    return {**row(thread), "turns": []}


def list_threads(session: Session, user: User, workspace_id: str, q: str | None = None, *, archived: bool = False,
                 limit: int = 50) -> list[dict[str, Any]]:
    """The caller's threads, newest activity first; `q` searches titles and every question asked."""
    require_role(session, user, workspace_id, "viewer")
    stmt = select(AskThread).where(AskThread.workspace_id == workspace_id, AskThread.user_id == user.id,
                                   AskThread.archived.is_(archived))
    if q and q.strip():
        like = f"%{q.strip()}%"
        asked = select(AskTurn.thread_id).where(AskTurn.workspace_id == workspace_id, AskTurn.question.ilike(like))
        stmt = stmt.where(or_(AskThread.title.ilike(like), AskThread.id.in_(asked)))
    threads = list(session.scalars(stmt.order_by(AskThread.updated_at.desc()).limit(min(max(limit, 1), 200))))
    counts = dict(session.execute(select(AskTurn.thread_id, func.count()).where(AskTurn.thread_id.in_([t.id for t in threads]))
                                  .group_by(AskTurn.thread_id)).all()) if threads else {}
    return [{**row(t), "turn_count": counts.get(t.id, 0)} for t in threads]


@scoped_loader
def thread_detail(session: Session, user: User, thread_id: str) -> dict[str, Any]:
    thread = _thread_for(session, user, thread_id)
    turns = list(session.scalars(select(AskTurn).where(AskTurn.thread_id == thread.id).order_by(AskTurn.seq)))
    return {**row(thread), "turns": [turn_out(session, t) for t in turns]}


@scoped_loader
def update_thread(session: Session, user: User, thread_id: str, *, title: str | None = None,
                  archived: bool | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    """`expected_revision` (from an optional If-Match, P4-06) makes a stale edit 412; old clients omit it."""
    from analystos.core.errors import PreconditionFailed

    thread = _thread_for(session, user, thread_id)
    if expected_revision is not None:
        session.refresh(thread, with_for_update=True)
        if expected_revision != (thread.revision or 1):
            raise PreconditionFailed(f"thread {thread.id} is at revision {thread.revision or 1}, not {expected_revision}",
                                     details={"current_revision": thread.revision or 1})
    changed = False
    if title is not None and title.strip():
        thread.title, changed = title.strip()[:300], True
    if archived is not None:
        thread.archived, changed = archived, True
    if changed:
        thread.revision = (thread.revision or 1) + 1
    session.flush()
    return row(thread)


# ------------------------------------------------------------------------------ provenance and staleness
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def provenance(session: Session, workspace_id: str, out: dict[str, Any]) -> dict[str, Any]:
    """Where an answer came from: the tables the gateway saw, their source and how fresh they were,
    and which rung answered (verified query, or generated SQL by which model)."""
    result = out.get("result") or {}
    assets = []
    for fq in result.get("referenced_assets") or []:
        schema, _, name = fq.partition(".")
        asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        source = session.get(Source, asset.source_id) if asset else None
        assets.append({"asset": fq, "asset_id": asset.id if asset else None,
                       "business_name": asset.business_name if asset else None,
                       "source_id": source.id if source else None, "source_name": source.name if source else None,
                       "source_kind": source.kind if source else None,
                       "execution_mode": source.execution_mode if source else None,
                       "freshness_at": _iso(asset.freshness_at) if asset else None,
                       "row_count": asset.row_count if asset else None})
    return {"assets": assets, "answered_by": out.get("answered_by"), "verified_query": out.get("verified_query"),
            "governance": out.get("governance", "ad_hoc"), "semantic": out.get("semantic"),
            "parent_turn_id": out.get("parent_turn_id"),
            "model": out.get("model"), "query_id": result.get("query_id"), "cache_hit": bool(result.get("cache_hit")),
            "result_hash": result.get("result_hash"), "repairs": len(out.get("attempts") or []),
            "suggestions": list(out.get("suggestions") or []), **({"rules": out["rules"]} if out.get("rules") else {})}


def staleness(session: Session, turn: AskTurn, now: datetime | None = None) -> dict[str, Any]:
    """How current the answer is, read now: `changed` when a table was refreshed after the answer,
    else by the age of its oldest table (fresh < 24 h <= aging < 7 days <= stale). Unknown stays unknown."""
    assets = [a for a in (turn.provenance or {}).get("assets") or [] if a.get("asset_id")]
    if turn.status != "answered" or not assets:
        return {"state": "unknown", "label": "Data freshness unknown", "data_as_of": None}
    now = now or utcnow()
    current = {a.id: a.freshness_at for a in session.scalars(select(SourceAsset).where(
        SourceAsset.id.in_([a["asset_id"] for a in assets])))}
    recorded = [datetime.fromisoformat(a["freshness_at"]) for a in assets if a.get("freshness_at")]
    if any(current.get(a["asset_id"]) and a.get("freshness_at")
           and current[a["asset_id"]] > datetime.fromisoformat(a["freshness_at"]) for a in assets):
        return {"state": "changed", "label": "Data refreshed since this answer: ask again for current numbers",
                "data_as_of": _iso(min(recorded)) if recorded else None}
    if not recorded:
        return {"state": "unknown", "label": "Data freshness unknown", "data_as_of": None}
    oldest = min(recorded)
    age = now - oldest
    state = "stale" if age >= STALE_AFTER else "aging" if age >= AGING_AFTER else "fresh"
    hours = int(age.total_seconds() // 3600)
    when = f"{hours // 24} days" if hours >= 48 else f"{hours} hours" if hours >= 1 else "under an hour"
    return {"state": state, "label": f"Data as of {when} ago", "data_as_of": _iso(oldest)}


def turn_out(session: Session, turn: AskTurn) -> dict[str, Any]:
    from analystos.semantic.evidence import evidence_status

    fresh = staleness(session, turn)
    prov = turn.provenance or {}
    semantic = prov.get("semantic") or {}
    governance = {"governance": prov.get("governance", "ad_hoc")}  # ADR-0019: on every answer
    if governance["governance"] == "governed":
        governance |= {"semantic_model_version": semantic.get("semantic_model_version", semantic.get("model_version")),
                       "compiler_version": semantic.get("compiler_version")}
    return {**row(turn), **governance, "staleness": fresh, "evidence_status": evidence_status(session, turn, fresh),
            "promotions": promotions_view(session, list(turn.promotions or []))}


def approval_state(approval: Approval | None) -> str:
    """The status a person acts on: an open request past its expiry reads `expired` before anyone tries it."""
    if approval is None:
        return "missing"
    expires = approval.expires_at if approval.expires_at.tzinfo else approval.expires_at.replace(tzinfo=UTC)
    return "expired" if approval.status in ("pending", "approved") and expires < utcnow() else approval.status


def promotions_view(session: Session, promotions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each promotion still waiting on an approval carries that approval's current status, so the thread can
    offer "Complete" once it is approved (and say when it was rejected or expired) after a reload."""
    waiting = [p["approval_id"] for p in promotions if p.get("status") == "approval_required" and p.get("approval_id")]
    if not waiting:
        return promotions
    states = {a.id: approval_state(a) for a in session.scalars(select(Approval).where(Approval.id.in_(waiting)))}
    return [{**p, "approval_status": states.get(p["approval_id"], "missing")}
            if p.get("status") == "approval_required" and p.get("approval_id") else p for p in promotions]


def record_promotion(promotions: list[dict[str, Any]] | None, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Append a promotion; completing one that waited on an approval replaces that pending entry."""
    out = list(promotions or [])
    approval_id = record.get("approval_id")
    for i, p in enumerate(out):
        if approval_id and p.get("approval_id") == approval_id and p.get("target") == record.get("target") \
                and p.get("status") == "approval_required":
            out[i] = {**record, "requested_at": p.get("requested_at") or p.get("at")}
            return out
    return [*out, record]


# ------------------------------------------------------------------------------ asking
def _finish(out: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    status = out.get("status")
    if status == "answered":
        return "answered", None
    if status == "needs_input":
        return "needs_input", refusal("needs_input", out.get("explanation") or "", missing=out.get("missing") or [],
                                      verified_query=out.get("verified_query"), parameters=out.get("parameters") or {})
    if status == "clarify":
        extra = {k: out[k] for k in ("assumption", "clarify_step") if out.get(k) is not None}  # analyst mode
        return "clarify", refusal("clarify", out.get("explanation") or "", missing=out.get("missing") or [],
                                  suggestions=list(out.get("suggestions") or []), **extra)
    return "refused", out.get("refusal") or refusal("failed", "The question could not be answered.")


def _step_provenance(session: Session, workspace_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return provenance(session, workspace_id, {**entry, "attempts": []})


def _analysis_provenance(session: Session, workspace_id: str, out: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """The headline step's provenance with the tables of every answered step, so staleness and "why this
    number" see each table the analysis read; each answered step keeps its own provenance too."""
    prov = provenance(session, workspace_id, out)
    seen = {a["asset"] for a in prov["assets"]}
    for entry in analysis.get("steps") or []:
        if entry.get("status") != "answered" or not entry.get("result"):
            continue
        entry["provenance"] = _step_provenance(session, workspace_id, entry)
        for a in entry["provenance"]["assets"]:
            if a["asset"] not in seen:
                seen.add(a["asset"])
                prov["assets"].append(a)
    prov["mode"] = "analyst"
    return prov


@scoped_loader
def ask_in_thread(user: User, thread_id: str, question: str, parameters: dict[str, Any] | None = None, *,
                  on_stage: Callable[[dict[str, Any]], None] | None = None,
                  ask_fn: Callable[..., dict[str, Any]] | None = None, mode: str = "quick") -> dict[str, Any]:
    """Ask one question in a thread; returns the persisted turn. Errors of the Ask path become the
    turn's refusal (one kind each, with a remedy); only an unknown thread or empty question raise.

    `mode="analyst"` plans the question into at most four steps, runs each one through the same
    single-question Ask (`ask_fn`, default `agents.sql_agent.ask`), and persists the plan, per-step
    results, facts, checks and the cited synthesis as `analysis` (agents/analyst.py); the turn's
    sql/result/chart are the headline step's."""
    from analystos.agents.sql_agent import ask as sql_ask

    question = (question or "").strip()
    if not question:
        raise InvalidInput("ask a question")
    if mode not in MODES:
        raise InvalidInput(f"mode must be one of {', '.join(MODES)}")
    started = time.perf_counter()
    with session_scope() as s:
        thread = _thread_for(s, s.merge(user), thread_id)
        seq = (s.scalar(select(func.max(AskTurn.seq)).where(AskTurn.thread_id == thread.id)) or 0) + 1
        turn = AskTurn(id=new_id("askt"), thread_id=thread.id, workspace_id=thread.workspace_id, user_id=user.id, seq=seq,
                       question=question, parameters=dict(parameters or {}), status="running", attempts=[], stages=[],
                       decisions=[], provenance={}, promotions=[])
        s.add(turn)
        if seq == 1 and thread.title == NEW_THREAD_TITLE:
            thread.title = question[:300]
        thread.updated_at = utcnow()
        workspace_id, turn_id = thread.workspace_id, turn.id
    stages: list[dict[str, Any]] = []

    def stage(key: str, text: str, data: dict[str, Any] | None = None) -> None:
        entry = {"key": key, "text": text, "at_ms": int((time.perf_counter() - started) * 1000), **({"data": data} if data else {})}
        stages.append(entry)
        if on_stage is not None:
            on_stage({"turn_id": turn_id, **entry})

    try:
        with session_scope() as s:
            ctx = adhoc_context(s, user, workspace_id)
        ctx.turn_id, ctx.thread_id, ctx.on_stage = turn_id, thread_id, stage
        with session_scope() as s:
            previous = s.scalar(select(AskTurn).where(AskTurn.thread_id == thread_id, AskTurn.seq < seq,
                                                     AskTurn.status == "answered").order_by(AskTurn.seq.desc()).limit(1))
            ctx.previous_answer = ({"id": previous.id, "sql": previous.sql,
                                    "rules": (previous.provenance or {}).get("rules", {})} if previous else None)
        if not ctx.scope.assets:
            out = {"status": "refused", "refusal": refusal("no_scope", "No selected, ready tables are in your scope.")}
        elif mode == "analyst":
            from analystos.agents import analyst

            out = analyst.run(ctx, question, parameters=parameters, ask_fn=ask_fn or sql_ask, finish=_finish,
                              refuse=refusal_for)
        else:
            out = (ask_fn or sql_ask)(ctx, question, parameters=parameters)
    except AnalystOSError as exc:
        out = {"status": "refused", "refusal": refusal_for(exc)}
    except Exception:  # noqa: BLE001 - any other failure still ends the turn; it must never stay "running"
        log.exception("ask turn %s failed unexpectedly", turn_id)
        out = {"status": "refused", "refusal": refusal("failed", UNEXPECTED_FAILURE)}
    status, refused = _finish(out)
    stage("done", "Answered" if status == "answered" else (refused or {}).get("title", "Not answered"))
    try:
        out_turn = _persist_turn(user, thread_id, workspace_id, turn_id, out, status, refused, stages, started, mode)
    except Exception:
        _mark_failed(turn_id, stages)
        raise
    if status == "answered":
        from analystos.services.steps import record_quietly

        record_quietly("ask_thread", workspace_id, thread_id, user.id)
    return out_turn


def _mark_failed(turn_id: str, stages: list[dict[str, Any]] | None = None) -> None:
    """Best effort: a turn whose result could not be recorded ends as a `failed` refusal, not `running`."""
    try:
        with session_scope() as s:
            turn = s.get(AskTurn, turn_id)
            if turn is not None and turn.status == "running":
                turn.status, turn.refusal = "refused", refusal("failed", UNEXPECTED_FAILURE)
                if stages is not None:
                    turn.stages = stages
    except Exception:  # noqa: BLE001 - the stale-work sweep ends it later
        log.exception("could not mark ask turn %s failed", turn_id)


def _persist_turn(user: User, thread_id: str, workspace_id: str, turn_id: str, out: dict[str, Any], status: str,
                  refused: dict[str, Any] | None, stages: list[dict[str, Any]], started: float, mode: str) -> dict[str, Any]:
    with session_scope() as s:
        turn = s.get(AskTurn, turn_id)
        turn.status, turn.refusal = status, refused
        turn.route = out.get("route")
        turn.answered_by = out.get("answered_by") if status == "answered" else None
        turn.sql, turn.explanation = out.get("sql"), out.get("explanation")
        turn.chart = out.get("chart") if isinstance(out.get("chart"), dict) else None
        turn.result = out.get("result")
        turn.verified_query = out.get("verified_query")
        turn.model = out.get("model")
        turn.attempts = list(out.get("attempts") or [])
        turn.decisions = list(out.get("decisions") or [])
        analysis = out.get("analysis") if isinstance(out.get("analysis"), dict) else None
        if analysis is not None and status == "answered":
            turn.provenance = _analysis_provenance(s, workspace_id, out, analysis)
        else:
            turn.provenance = provenance(s, workspace_id, out) if status == "answered" else {}
        turn.analysis = analysis
        turn.stages = stages
        turn.latency_ms = int((time.perf_counter() - started) * 1000)
        emit(workspace_id, "ask.answered", {"thread_id": thread_id, "turn_id": turn_id, "status": status,
                                            "answered_by": turn.answered_by, "refusal": (refused or {}).get("kind"),
                                            "mode": mode, "steps": len((analysis or {}).get("steps") or []) or None},
             actor=f"user:{user.id}", session=s)
        s.flush()
        if status != "answered":
            _suggest_terms(s, workspace_id, turn)
        else:  # N-8: measured results and cited documents as separate citation kinds; conflicts flagged
            record_turn_quietly(s, turn, actor=f"user:{user.id}")
        return turn_out(s, turn)


def _suggest_terms(session: Session, workspace_id: str, turn: AskTurn) -> None:
    """Words of an unanswered question that match nothing known become "Define X?" glossary drafts for a
    person (knowledge/glossary_scan.py; rules only, a few at most). Never fails the turn."""
    from analystos.knowledge.glossary_scan import from_ask_turn

    try:
        with session.begin_nested():
            from_ask_turn(session, workspace_id, turn)
    except Exception:  # noqa: BLE001 - a suggestion is best effort; the answer (or refusal) stands
        import logging

        logging.getLogger(__name__).warning("glossary suggestion after ask turn %s failed", turn.id, exc_info=True)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream_turn(user: User, thread_id: str, question: str, parameters: dict[str, Any] | None = None, *,
                      ask: Callable[..., dict[str, Any]] = ask_in_thread, guard: StreamGuard | None = None,
                      mode: str = "quick") -> AsyncIterator[str]:
    """SSE frames for one Ask turn: `stage` per plain-language step as it happens, then `turn` (the
    persisted turn) or `error`, then `end`. The Ask itself runs in a worker thread. With a guard the
    caller is re-authorized before every frame and while waiting: an expired token or lost access
    ends the stream with `expired` / `revoked` and no stage or answer after it (P4-01)."""
    import anyio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_stage(entry: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, entry)

    async def denied(payload: bool = True) -> str | None:
        verdict = await guard.verdict(payload=payload) if guard is not None else None
        return terminal_frame(*verdict) if verdict is not None else None

    extra = {"mode": mode} if mode != "quick" else {}
    task = asyncio.ensure_future(anyio.to_thread.run_sync(lambda: ask(user, thread_id, question, parameters, on_stage=on_stage,
                                                                      **extra)))
    getter: asyncio.Future | None = None
    try:
        while True:
            getter = getter if getter is not None and not getter.done() else asyncio.ensure_future(queue.get())
            timeout = guard.wait_limit(REAUTH_SECONDS) if guard is not None else None
            done, _ = await asyncio.wait({getter, task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if frame := await denied(payload=False):
                    yield frame
                    return
                continue
            if getter in done:
                if frame := await denied():
                    yield frame
                    return
                yield _sse("stage", getter.result())
                continue
            getter.cancel()
            while not queue.empty():
                if frame := await denied():
                    yield frame
                    return
                yield _sse("stage", queue.get_nowait())
            break
        if frame := await denied():
            yield frame
            return
        try:
            yield _sse("turn", task.result())
        except AnalystOSError as exc:
            yield _sse("error", {"error": exc.to_dict()})
        except Exception:  # noqa: BLE001 - the stream always closes with `end`; the cause is only logged
            log.exception("ask stream for thread %s failed", thread_id)
            yield _sse("error", {"error": AnalystOSError(UNEXPECTED_FAILURE).to_dict()})
        yield _sse("end", {"status": "done"})
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# ------------------------------------------------------------------------------ inspector
@scoped_loader
def inspector(session: Session, user: User, turn_id: str) -> dict[str, Any]:
    """The four inspector tabs' data: Result and SQL (the turn and its gateway audit row), Evidence
    (provenance, context receipts), Decision (decision rows and model-call receipts of the turn)."""
    turn = _turn_for(session, user, turn_id)
    decisions = rows(session.scalars(select(DecisionRecord).where(DecisionRecord.task_id == turn.id)
                                     .order_by(DecisionRecord.created_at)))
    calls = list(session.scalars(select(ModelCall).where(ModelCall.task_id == turn.id, ModelCall.workspace_id == turn.workspace_id)
                                 .order_by(ModelCall.id)))
    query_id = (turn.result or {}).get("query_id")
    query = session.get(QueryExecution, query_id) if query_id else None
    receipts: list[dict[str, Any]] = []
    for c in calls:
        for r in c.context_receipts or []:
            if r not in receipts:
                receipts.append(r)
    return {"turn": turn_out(session, turn), "decisions": decisions, "model_calls": rows(calls),
            "query": row(query, exclude={"result_preview"}) if query is not None and query.workspace_id == turn.workspace_id else None,
            "receipts": receipts}


# ------------------------------------------------------------------------------ explain pasted SQL
@scoped_loader
def rerun_turn(user: User, turn_id: str, sql: str | None = None) -> dict[str, Any]:
    """A fresh immutable turn, with a link to the old result and current access checks."""
    from analystos.agents.sql_agent import _authorize_ask, _check_budget, _result
    from analystos.artifacts.registry import link
    from analystos.core.ids import stable_hash
    from analystos.governance.budgets import ASK_PURPOSE, ask_actor

    with session_scope() as session:
        old = _turn_for(session, session.merge(user), turn_id)
        if old.status != "answered" or not old.sql:
            raise InvalidInput("Only an answered query can be rerun.")
        thread_id, question, saved_sql = old.thread_id, old.question, old.sql
        semantic = (old.provenance or {}).get("semantic")
    edited = sql is not None and sql != saved_sql
    statement = sql if sql is not None else saved_sql
    if not statement.strip():
        raise InvalidInput("SQL cannot be empty.")

    def execute(ctx, _question, parameters=None):
        _authorize_ask(ctx)
        _check_budget(ctx)
        pinned = None
        if semantic and not edited:
            catalog = ctx.semantic_catalog
            if not catalog or catalog["id"] != semantic["model_id"] or catalog["hash"] != semantic["model_hash"]:
                raise InvalidInput("The saved semantic model changed. Ask the question again to use the current definition.")
            for metric in semantic["metrics"]:
                current = catalog["metrics"].get(metric["name"])
                if not current or current["id"] != metric["id"] or current["hash"] != metric["hash"]:
                    raise InvalidInput("A pinned metric changed. Ask again to explicitly use its current definition.")
            if stable_hash(statement) != semantic["sql_hash"]:
                raise InvalidInput("The saved SQL does not match its compiled definition.")
            pinned = {**semantic, "policy_hash": stable_hash(ctx.policy.model_dump(mode="json")),
                      "policy_version": ctx.scope.policy_version, "scope_hash": ctx.scope.scope_hash()}
        result = ctx.services.gateway.execute(ctx.scope, statement, actor=ask_actor(user.id), purpose=ASK_PURPOSE,
                                              run_id=None, task_id=ctx.turn_id, use_cache=False)
        return {"status": "answered", "answered_by": "semantic" if pinned else "sql", "sql": statement,
                "governance": "governed" if pinned else "ad_hoc", "semantic": pinned, "parent_turn_id": turn_id,
                "explanation": "Fresh result from the saved calculation." if not edited else "Fresh result from your edited SQL.",
                "result": _result(result), "model": None, "attempts": [], "decisions": [], "route": "rerun"}

    result = ask_in_thread(user, thread_id, question, ask_fn=execute)
    with session_scope() as session:
        link(session, result["workspace_id"], ("ask_turn", result["id"]), "derived_from", ("ask_turn", turn_id))
    return result


# ------------------------------------------------------------------------------ analyst mode: steps
def _analysis_of(turn: AskTurn) -> dict[str, Any]:
    if not isinstance(turn.analysis, dict):
        raise InvalidInput("This question was answered in quick mode; it has no analysis steps.")
    return turn.analysis


def _entry(analysis: dict[str, Any], n: int) -> dict[str, Any]:
    entry = next((e for e in analysis.get("steps") or [] if e.get("n") == n), None)
    if entry is None:
        raise NotFound(f"step {n} not found in this analysis")
    return entry


@scoped_loader
def rerun_step(user: User, turn_id: str, n: int, sql: str | None = None) -> dict[str, Any]:
    """Re-run one analyst step with its saved or edited SQL through the gateway, with the current access
    and Ask budget checks. The step's facts and checks are recomputed in place (the previous headline
    value feeds the magnitude check), the synthesis is marked `stale` until re-synthesized, and the turn's
    sql/result/chart follow the headline step. A rerun answer is labelled ad hoc: it was not compiled."""
    import copy

    from analystos.agents import analyst
    from analystos.agents.sql_agent import _authorize_ask, _check_budget, _result
    from analystos.governance.budgets import ASK_PURPOSE, ask_actor

    with session_scope() as s:
        turn = _turn_for(s, s.merge(user), turn_id)
        analysis = _analysis_of(turn)
        if turn.status != "answered":
            raise InvalidInput("Only an answered analysis can have a step rerun.")
        old = copy.deepcopy(_entry(analysis, n))
        ws_id, thread_id = turn.workspace_id, turn.thread_id
    statement = sql if sql is not None else old.get("sql")
    if not statement or not statement.strip():
        raise InvalidInput("SQL cannot be empty." if sql is not None else "This step has no SQL to rerun: send the SQL to run.")
    edited = sql is not None and sql != old.get("sql")
    with session_scope() as s:
        ctx = adhoc_context(s, user, ws_id)
    ctx.turn_id, ctx.thread_id = turn_id, thread_id
    _authorize_ask(ctx)
    _check_budget(ctx)
    result = _result(ctx.services.gateway.execute(ctx.scope, statement, actor=ask_actor(user.id), purpose=ASK_PURPOSE,
                                                  run_id=None, task_id=turn_id, use_cache=False))
    dialect = next(iter(ctx.scope.source_dialects.values()), "postgres")
    at = utcnow().isoformat()
    entry = analyst.rerun_entry(old, statement, result, dialect, edited=edited, at=at)
    with session_scope() as s:
        turn = s.get(AskTurn, turn_id, with_for_update=True)
        analysis = copy.deepcopy(turn.analysis)
        analysis["steps"] = [entry if e.get("n") == n else e for e in analysis["steps"]]
        if analysis.get("synthesis"):
            stale = sorted(set(analysis["synthesis"].get("stale_steps") or []) | {n})
            analysis["synthesis"] = {**analysis["synthesis"], "stale": True, "stale_since": at, "stale_steps": stale}
        head = analysis.get("headline_step")
        if head == n:
            turn.sql, turn.result, turn.chart = statement, result, entry.get("chart")
            turn.answered_by, turn.model = entry["answered_by"], None
        headline = next(e for e in analysis["steps"] if e.get("n") == head)
        mirror = {**headline, "result": turn.result, "attempts": []}
        turn.provenance = _analysis_provenance(s, ws_id, mirror, analysis)
        turn.analysis = analysis
        emit(ws_id, "ask.step_rerun", {"thread_id": thread_id, "turn_id": turn_id, "step": n, "edited": edited,
                                       "query_id": result.get("query_id")}, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ask.step_rerun", workspace_id=ws_id, target=turn_id,
              details={"step": n, "edited": edited, "query_id": result.get("query_id"), "sql": statement[:2000]}, session=s)
        s.flush()
        record_turn_quietly(s, turn, actor=f"user:{user.id}")
        return turn_out(s, turn)


@scoped_loader
def resynthesize(user: User, turn_id: str) -> dict[str, Any]:
    """Write the synthesis again from the steps' current facts (after a step rerun): the same template
    path, and the synthesis model only where its purpose allows and its numbers bind."""
    import copy

    from analystos.agents import analyst

    with session_scope() as s:
        turn = _turn_for(s, s.merge(user), turn_id)
        analysis = copy.deepcopy(_analysis_of(turn))
        if turn.status != "answered" or not analysis.get("headline_step"):
            raise InvalidInput("Only an answered analysis can be synthesized.")
        ws_id, thread_id, question = turn.workspace_id, turn.thread_id, turn.question
    with session_scope() as s:
        ctx = adhoc_context(s, user, ws_id)
    ctx.turn_id, ctx.thread_id = turn_id, thread_id
    synthesis = analyst.synthesize(ctx, question, analysis["plan"], analysis["steps"], analysis["headline_step"])
    with session_scope() as s:
        turn = s.get(AskTurn, turn_id, with_for_update=True)
        current = copy.deepcopy(turn.analysis)
        current["synthesis"] = {**synthesis, "synthesized_at": utcnow().isoformat()}
        turn.analysis = current
        turn.explanation = synthesis["text"]
        s.flush()
        record_turn_quietly(s, turn, actor=f"user:{user.id}")
        return turn_out(s, turn)


def why_step(session: Session, user: User, turn_id: str, n: int, *, number: str | None = None, column: str | None = None,
             row: int | None = None) -> dict[str, Any]:
    """Why this number, for one analyst step: the headline step is the turn itself; another step is
    explained from its own stored result and provenance (its first 50 rows)."""
    from types import SimpleNamespace

    from analystos.evidence.why import explain_ask_turn

    turn = _turn_for(session, user, turn_id)
    analysis = _analysis_of(turn)
    entry = _entry(analysis, n)
    if entry.get("status") != "answered" or not entry.get("result"):
        raise NotFound(f"step {n} has no answer to explain ({entry.get('status')})")
    if analysis.get("headline_step") == n:
        out = explain_ask_turn(session, turn, number=number, column=column, row=row)
    else:
        proxy = SimpleNamespace(id=f"{turn.id}:s{n}", thread_id=turn.thread_id, question=entry["question"],
                                answered_by=entry.get("answered_by"), provenance=entry.get("provenance") or {},
                                status="answered", result=entry["result"], workspace_id=turn.workspace_id, sql=entry.get("sql"))
        out = explain_ask_turn(session, proxy, number=number, column=column, row=row)
    out["subject"] = {**out["subject"], "id": turn.id, "step": n, "step_question": entry["question"]}
    return out


def explain_sql(session: Session, user: User, workspace_id: str, sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Paste-SQL explain: the deterministic explanation, the validator's verdict for this caller and,
    when accepted, the source's plan through the gateway (EXPLAIN, nothing executed)."""
    from analystos.gateway.validator import validate_sql
    from analystos.runtime.context import default_gateway
    from analystos.skills.sqlexplain import explain_sql as explain

    scope = resolve_scope(session, session.merge(user), workspace_id)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    out = explain(sql, dialect)
    try:
        validate_sql(scope, sql, max_rows=max_rows or scope.max_rows)
        out["gateway"] = {"accepted": True}
    except AnalystOSError as exc:
        out["gateway"] = {"accepted": False, "code": exc.code, "reason": exc.message}
        # A rejected probe is audited like a rejected console query: explain must not be a silent scope oracle.
        audit(f"user:{user.id}", "query.explain_rejected", workspace_id=workspace_id, decision="deny",
              details={"code": exc.code, "reason": exc.message[:300], "sql": sql[:2000]}, session=session)
        return out
    try:
        out["plan"] = default_gateway().explain(scope, sql, actor=f"user:{user.id}", purpose="console.explain")
    except AnalystOSError as exc:
        out["plan"] = {"available": False, "reason": exc.message}
    return out


# ------------------------------------------------------------------------------ promotions
def measure_of(sql: str | None, dialect: str = "postgres") -> str | None:
    """The first aggregate in an answer's SELECT, unqualified, as a metric expression (COUNT(*), AVG(x))."""
    import sqlglot
    from sqlglot import exp

    if not sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    select_ = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select_ is None:
        return None
    for projection in select_.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.AggFunc) or node.find(exp.AggFunc) is not None:
            node = node.copy()
            for col in node.find_all(exp.Column):
                col.set("table", None)
            return node.sql(dialect=dialect)
    return None


def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:n] or "ask"


def _measure(session: Session, user: User, turn: AskTurn, body: dict[str, Any]) -> tuple[str, Artifact, Any]:
    """A metric expression checked through the gateway on the workspace's analytical dataset."""
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, user, turn.workspace_id)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    expression = (body.get("sql_expression") or measure_of(turn.sql, dialect) or "").strip()
    if not expression:
        raise InvalidInput("this answer has no aggregate to measure: give sql_expression (e.g. COUNT(*))")
    if ";" in expression:
        raise InvalidInput("sql_expression is one expression, not a statement")
    dataset = session.scalar(select(Artifact).where(Artifact.workspace_id == turn.workspace_id, Artifact.type == "dataset")
                             .order_by(Artifact.created_at.desc()))
    if dataset is None:
        raise InvalidInput("no analytical dataset in this workspace yet: run an investigation first, then promote")
    sql = f'SELECT {expression} AS value FROM ({dataset.content["sql"]}) d'
    try:
        result = default_gateway().execute(scope, sql, actor=f"user:{user.id}", purpose="metric.validate", max_rows=1)
    except AnalystOSError as exc:
        raise InvalidInput(f"{expression} does not measure the analytical dataset {dataset.name}: {exc.message}") from None
    value = result.rows[0][0] if result.rows else None
    return expression, dataset, {"value": value, "query_id": result.query_id}


@scoped_loader
def promote(user: User, turn_id: str, target: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn an answered question into a platform object; returns the promotion record."""
    from analystos.artifacts.registry import link
    from analystos.governance.approvals import request_approval, verify_for_execution
    from analystos.registries import verified_queries as vq_svc
    from analystos.services.monitors import create_monitor

    body = dict(body or {})
    if target not in PROMOTE_TARGETS:
        raise InvalidInput(f"target must be one of {', '.join(PROMOTE_TARGETS)}")
    with session_scope() as s:
        me = s.merge(user)
        turn = _turn_for(s, me, turn_id)
        if turn.status != "answered" or not turn.result:
            raise InvalidInput("only an answered question can be promoted")
        if turn_out(s, turn)["evidence_status"]["state"] == "changed":
            raise InvalidInput("The answer's evidence changed. Refresh or ask again before promoting it.")
        ws_id, question, query_id = turn.workspace_id, turn.question, turn.result.get("query_id")
    record: dict[str, Any]
    if target == "investigate":
        from analystos.services.runs import create_run

        objective = (body.get("objective") or f"Investigate why: {question}").strip()
        run = create_run(user, ws_id, objective=objective, origin={"type": "ask", "turn_id": turn_id, "query_id": query_id})
        record = {"target": target, "id": run.id, "status": "started", "objective": objective}
    with session_scope() as s:
        me = s.merge(user)
        turn = _turn_for(s, me, turn_id)
        if target == "verified_query":
            vq = vq_svc.promote(s, me, ws_id, question=body.get("question") or question, query_id=query_id, name=body.get("name"))
            record = {"target": target, "id": vq.id, "name": vq.name, "status": "created"}
        elif target == "metric":
            # A KPI is proposed to the semantic layer (P4-K03): it becomes usable once an approver
            # approves it there (separation of duties); the expression is checked through the gateway first.
            from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
            from analystos.semantic.service import propose_metric

            require_role(s, me, ws_id, "editor")
            expression, dataset, check = _measure(s, me, turn, body)
            name = _slug(body.get("name") or question)
            defn = SemanticMetricDef(name=name, expressions=[DialectExpression(expression=expression)],
                                     description=f"Promoted from Ask: {question}"[:500],
                                     display_name=body.get("display_name") or question[:200], format=body.get("format"),
                                     dataset=dataset.name)
            metric, created = propose_metric(s, ws_id, defn, proposed_by=user.id, via="user", source=("ask_turn", turn_id))
            record = {"target": target, "id": metric.id, "name": metric.name, "version": metric.version,
                      "status": metric.status if not created else "proposed", "approval_id": metric.approval_id,
                      "value": check["value"]}
        elif target == "monitor":
            expression, dataset, check = _measure(s, me, turn, body)
            kind = body.get("kind") or "metric_drift"
            config: dict[str, Any] = {"sql_expression": expression, "dataset_artifact_id": dataset.id,
                                      "grain": body.get("grain") or "week"}
            if kind == "metric_threshold":
                config.update(op=body.get("op"), value=body.get("value"))
            name = (body.get("name") or question)[:200]
            m = create_monitor(s, me, ws_id, name=name, kind=kind, config=config, auto_investigate=bool(body.get("auto_investigate")))
            s.flush()
            record = {"target": target, "id": m.id, "name": m.name, "status": "created", "kind": kind, "value": check["value"]}
        elif target == "dashboard":
            record = _dashboard(s, me, turn, body, request_approval, verify_for_execution)
        elif target == "report":
            from analystos.services.ask_outputs import answer_report

            require_role(s, me, ws_id, "analyst")
            record = answer_report(s, me, turn)
        node = {"investigate": "run", "metric": "semantic_metric", "report": "artifact",
                "dashboard": "approval" if record["status"] == "approval_required" else "chart"}.get(target, target)
        link(s, ws_id, ("ask_turn", turn_id), "promoted_to", (node, record["id"]))
        record["at"] = utcnow().isoformat()
        turn.promotions = record_promotion(turn.promotions, record)
        emit(ws_id, "ask.promoted", {"turn_id": turn_id, **{k: record.get(k) for k in ("target", "id", "status")}},
             actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ask.promoted", workspace_id=ws_id, target=turn_id,
              details={k: record.get(k) for k in ("target", "id", "status", "approval_id")}, session=s)
    return record


def _dashboard_payload(turn: AskTurn, *, chart: Any, dashboard: str, destination: str) -> dict[str, Any]:
    """What an approver approves: the answer's own question and SQL (read from the turn, never the request)
    and the requester's choices of chart, dashboard and destination."""
    return {"kind": "ask_chart", "turn_id": turn.id, "question": turn.question, "sql": turn.sql, "chart": chart,
            "dashboard": dashboard, "destination": destination}


def _dashboard(s: Session, me: User, turn: AskTurn, body: dict[str, Any], request_approval, verify_for_execution) -> dict[str, Any]:
    """Adding an answer to a dashboard is for a BI audience: an approval over the exact chart payload
    first; with the approved request (`approval_id`), verified just before the side effect, the chart is
    published to the approved destination through the BI publisher (services/ask_outputs.py)."""
    require_role(s, me, turn.workspace_id, "editor")
    ws = get_workspace(s, turn.workspace_id)
    policy = load_policy(s, ws)
    approval_id = body.get("approval_id")
    if approval_id:
        from analystos.services.ask_outputs import publish_to_dashboard

        apr = s.get(Approval, approval_id, with_for_update=True)
        if apr is None or apr.workspace_id != turn.workspace_id or apr.action != DASHBOARD_ACTION \
                or (apr.payload or {}).get("turn_id") != turn.id:
            raise InvalidInput("the approval does not cover adding this answer to a dashboard")
        # The choices come from the approved request; the question and SQL are recomputed from the turn, so a
        # changed answer no longer matches the approved hash.
        approved = apr.payload or {}
        payload = _dashboard_payload(turn, chart=approved.get("chart"), dashboard=approved.get("dashboard") or "Ask answers",
                                     destination=approved.get("destination") or "preview")
        verify_for_execution(s, approval_id, payload=payload, plan_hash=None)
        return publish_to_dashboard(s, me, turn, payload, apr)
    from analystos.services.ask_outputs import usable_destination

    destination, note = usable_destination(policy.publish_destinations, body.get("destination"))
    payload = _dashboard_payload(turn, chart=body.get("chart") or turn.chart, dashboard=(body.get("dashboard") or "Ask answers")[:200],
                                 destination=destination)
    apr = request_approval(s, workspace_id=turn.workspace_id, run_id=None, action=DASHBOARD_ACTION, payload=payload,
                           plan_hash=None, policy_version=ws.policy_version, requested_by=me.id, risk_tier="medium",
                           destination=destination, affected_assets=[a["asset"] for a in (turn.provenance or {}).get("assets", [])],
                           evidence={"query_id": (turn.result or {}).get("query_id"), "answered_by": turn.answered_by})
    return {"target": "dashboard", "id": apr.id, "status": "approval_required", "approval_id": apr.id,
            "payload_hash": apr.payload_hash, "dashboard": payload["dashboard"], "destination": destination,
            "expires_at": apr.expires_at.isoformat(), **({"note": note} if note else {})}

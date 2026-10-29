from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import StreamAuth, current_user, db, stream_guard, streaming_auth
from analystos.api.serialize import row, rows, standing, with_verification
from analystos.core.errors import InvalidInput
from analystos.db.base import session_scope
from analystos.db.models import (
    AgentMessage,
    AnalysisRun,
    Approval,
    Experiment,
    Hypothesis,
    Insight,
    ModelCall,
    QueryExecution,
    RunEvent,
    RunTask,
    ToolExecution,
    User,
)
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.services import runs as run_svc
from analystos.services.ask import AdhocContext, adhoc_context, explain_sql

router = APIRouter(prefix="/api", tags=["analysis"])


class RunIn(BaseModel):
    objective: str | None = None
    source_ids: list[str] | None = None
    autonomy_level: int | None = None
    playbook: str | None = None  # a Playbook capability id or workspace playbook key; default playbook.investigate
    definition: dict | str | None = None  # an exact definition version: {key, version} or a definition id (P7-03)
    analysis_context: dict | str | None = None  # published, versioned business purpose and source binding


class FeedbackIn(BaseModel):
    text: str
    kind: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class AskIn(BaseModel):
    question: str
    parameters: dict | None = None  # values for a verified query's parameters (answers a "needs_input" decline)
    use_registry: bool = True


class SqlIn(BaseModel):
    sql: str
    max_rows: int | None = None


class HypothesisPatch(BaseModel):
    statement: str | None = None
    priority: str | None = None
    status: str | None = None


@router.post("/workspaces/{workspace_id}/analysis")
def start(workspace_id: str, body: RunIn, response: Response, user: User = Depends(current_user),
          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Start a run. With `Idempotency-Key` a retried request returns the run the first one created
    (`Idempotent-Replayed: true`); the same key with a different body is 409 (P4-06)."""
    from analystos.services.idempotency import Key

    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="run.create",
                 request=body.model_dump())
    run, replayed = run_svc.start_run_request(user, workspace_id, **body.model_dump(), idempotency=key)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return row(run)


@router.get("/workspaces/{workspace_id}/analysis")
def list_runs(workspace_id: str, limit: int | None = None, cursor: str | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """Newest runs first. With `limit` or `cursor`: a `{items, next_cursor}` page; without: the old array of 50."""
    from analystos.api.http import page, wants_page

    require_role(session, user, workspace_id, "viewer")
    stmt = select(AnalysisRun).where(AnalysisRun.workspace_id == workspace_id)
    if wants_page(limit, cursor):
        return page(session, stmt, AnalysisRun, limit=limit, cursor=cursor, scope={"workspace": workspace_id, "list": "runs"},
                    render=lambda rs: rows(rs, exclude={"scope", "plan"}))
    return rows(session.scalars(stmt.order_by(AnalysisRun.created_at.desc()).limit(50)), exclude={"scope", "plan"})


def _run_detail(session: Session, run: AnalysisRun) -> dict:
    tasks = [t for t in session.scalars(select(RunTask).where(RunTask.run_id == run.id).order_by(RunTask.seq, RunTask.key))
             if not t.key.startswith("_")]
    hyps = list(session.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id).order_by(Hypothesis.created_at)))
    exps = {e.hypothesis_id: e for e in session.scalars(select(Experiment).where(Experiment.run_id == run.id, Experiment.role == "primary"))}
    insights = list(session.scalars(select(Insight).where(Insight.run_id == run.id).order_by(Insight.code)))
    approvals = list(session.scalars(select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at)))
    held = {i.hypothesis_id: standing(i.evidence_bundle)["holdout"] for i in insights if i.hypothesis_id}  # P8-15
    scope = dict(run.scope or {})
    return {**row(run), "scope": {k: scope.get(k) for k in ("assets", "denied_columns", "max_rows", "timeout_seconds", "policy_version", "hash")},
            "tasks": rows(tasks),
            "hypotheses": [{**row(h), "result": {k: (exps[h.id].result or {}).get(k) for k in (
                "test", "n", "p_value", "p_adjusted", "effect_size", "effect_label", "highlights", "groups", "warnings",
                "strength")} if h.id in exps else None, "experiment_id": exps[h.id].id if h.id in exps else None,
                "holdout": held.get(h.id)} for h in hyps],
            "insights": with_verification(session, insights), "approvals": rows(approvals, exclude={"payload"}),
            "model_fallbacks": model_fallbacks(session, run.id)}


def model_fallbacks(session: Session, run_id: str) -> list[dict]:
    """N-14: the purposes this run answered by rules because no model route was allowed, one per purpose and
    cause (llm.fallback.summarize of the run's `model.fallback` events)."""
    from analystos.llm.fallback import summarize

    events = session.scalars(select(RunEvent.payload).where(RunEvent.run_id == run_id, RunEvent.type == "model.fallback")
                             .order_by(RunEvent.id).limit(500))
    return summarize([p for p in events if isinstance(p, dict)])


@router.get("/workspaces/{workspace_id}/analysis/{run_id}")
def get_run(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return _run_detail(session, run_svc.get_run_for(session, user, run_id, workspace_id=workspace_id))


@router.get("/agent-runs/{task_id}")
def agent_run(task_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    task = load_in_workspace(session, RunTask, task_id, user=user, label="agent run", workspace_of=_task_workspace)
    return {**row(task), "messages": rows(session.scalars(select(AgentMessage).where(AgentMessage.task_id == task_id))),
            "tool_calls": rows(session.scalars(select(ToolExecution).where(ToolExecution.task_id == task_id))),
            "model_calls": rows(session.scalars(select(ModelCall).where(ModelCall.task_id == task_id))),
            "queries": rows(session.scalars(select(QueryExecution).where(QueryExecution.task_id == task_id)))}


def _task_workspace(session: Session, task: RunTask) -> str | None:
    return session.scalar(select(AnalysisRun.workspace_id).where(AnalysisRun.id == task.run_id))


@scoped_loader
def _control(workspace_id: str, run_id: str, action: str, user: User):
    return row(run_svc.control(user, run_id, action, workspace_id), exclude={"scope", "plan"})


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/pause")
def pause(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(workspace_id, run_id, "pause", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/resume")
def resume(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(workspace_id, run_id, "resume", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/cancel")
def cancel(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(workspace_id, run_id, "cancel", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/feedback")
def feedback(workspace_id: str, run_id: str, body: FeedbackIn, user: User = Depends(current_user)):
    return run_svc.submit_feedback(user, run_id, **body.model_dump(), workspace_id=workspace_id)


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/console")
def console(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Agent console (§52.7): messages, tool calls, model calls, queries, cost."""
    run = run_svc.get_run_for(session, user, run_id, workspace_id=workspace_id)
    calls = list(session.scalars(select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)))
    return {"messages": rows(session.scalars(select(AgentMessage).where(AgentMessage.run_id == run_id).order_by(AgentMessage.id))),
            "tool_calls": rows(session.scalars(select(ToolExecution).where(ToolExecution.run_id == run_id).order_by(ToolExecution.id))),
            "model_calls": rows(calls),
            "queries": rows(session.scalars(select(QueryExecution).where(QueryExecution.run_id == run_id)
                                            .order_by(QueryExecution.created_at)), exclude={"result_preview"}),
            "cost": {"usd": run.cost_usd, "tokens": run.tokens,
                     "model_calls": sum(1 for c in calls if c.status in ("ok", "error")),
                     "jev_calls": sum(1 for c in calls if c.provider == "typesafe" and c.status in ("ok", "error")),
                     "failed_calls": sum(1 for c in calls if c.status == "error"),
                     "cache_hits": sum(1 for c in calls if c.status == "cache_hit"),
                     "deterministic_skips": sum(1 for c in calls if c.status == "skipped"),
                     "tokens_saved": sum(c.tokens_saved or 0 for c in calls),
                     "by_rung": _spend_by(calls, lambda c: c.answered_by or "llm_large"),
                     "by_model": _spend_by([c for c in calls if c.status in ("ok", "error", "cache_hit")], lambda c: c.model)}}


def _spend_by(calls: list, key) -> dict:
    """Per rung or model: provider requests, tokens spent and avoided, cost (the Operate breakdown, per run)."""
    out: dict[str, dict] = {}
    for c in calls:
        b = out.setdefault(key(c) or "unknown", {"calls": 0, "tokens_used": 0, "tokens_saved": 0, "cost_usd": 0.0})
        b["calls"] += 1 if c.status in ("ok", "error") else 0
        b["tokens_used"] += (c.input_tokens or 0) + (c.output_tokens or 0)
        b["tokens_saved"] += c.tokens_saved or 0
        b["cost_usd"] += float(c.cost_usd or 0.0)
    return out


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/events")
async def events(workspace_id: str, run_id: str, request: Request, after_id: int = 0,
                 auth: StreamAuth = Depends(streaming_auth)):
    """Persisted event stream as Server-Sent Events. Reconnect with ?after_id=<last id> (or Last-Event-ID).
    Database reads run in worker threads; new events arrive by Redis nudge (polling only as a fallback).
    The caller is re-authorized while the stream is open: when the token expires or access is lost the
    stream ends with an `expired` or `revoked` event and sends nothing after it."""
    from analystos.events.stream import run_blocking, run_event_stream

    def load(session: Session, user: User) -> AnalysisRun:
        return run_svc.get_run_for(session, user, run_id, workspace_id=workspace_id)

    await run_blocking(_authorize_stream, auth.user, load)
    last = int(request.headers.get("last-event-id") or after_id)
    stream = run_event_stream(run_id, last, is_disconnected=request.is_disconnected, guard=stream_guard(auth, load))
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _authorize_stream(user: User, load: Callable[[Session, User], object]) -> None:
    with session_scope() as s:
        load(s, user)


@router.patch("/hypotheses/{hypothesis_id}")
def edit_hypothesis(hypothesis_id: str, body: HypothesisPatch, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    h = load_in_workspace(session, Hypothesis, hypothesis_id, user=user, minimum="analyst", label="hypothesis")
    if h.status not in ("proposed", "approved") and body.statement:
        raise InvalidInput("only untested hypotheses can be edited; submit feedback to redirect the analysis instead")
    if body.statement:
        h.statement = body.statement
    if body.priority in ("high", "medium", "low"):
        h.priority = body.priority
    if body.status == "rejected" and h.status in ("proposed", "approved"):
        h.status = "rejected"
        task = session.scalar(select(RunTask).where(RunTask.run_id == h.run_id, RunTask.key == f"test:{h.code}"))
        if task and task.status == "NEW":
            task.status, task.error = "SKIPPED", "rejected by user"
    return row(h)


# --------------------------------------------------------------------------------------- ad hoc
def _adhoc(session: Session, user: User, workspace_id: str) -> AdhocContext:
    """The SQL agent's context outside a run (Ask box, console, MCP `ask`): services/ask.py."""
    return adhoc_context(session, user, workspace_id)


@router.post("/workspaces/{workspace_id}/ask")
def ask(workspace_id: str, body: AskIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.agents.sql_agent import ask as sql_ask

    return sql_ask(_adhoc(session, user, workspace_id), body.question, parameters=body.parameters, use_registry=body.use_registry)


@router.post("/workspaces/{workspace_id}/query")
def query(workspace_id: str, body: SqlIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Manual SQL console: same gateway, same scope and audit as agents (no bypass)."""
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, session.merge(user), workspace_id)
    r = default_gateway().execute(scope, body.sql, actor=f"user:{user.id}", purpose="console", max_rows=body.max_rows)
    return r.model_dump()


@router.post("/workspaces/{workspace_id}/query/explain")
def explain_query(workspace_id: str, body: SqlIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Deterministic explanation (no model, no execution), the gateway validator's verdict for this
    caller and, when accepted, the source's plan through the gateway (EXPLAIN only)."""
    return explain_sql(session, user, workspace_id, body.sql, body.max_rows)

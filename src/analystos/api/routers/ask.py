"""Ask threads API (P4-U02): threads, turns (JSON or streamed stages over SSE), the answer
inspector, promotions and the paste-SQL explain (on the analysis router)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import StreamAuth, current_user, db, stream_guard, streaming_auth
from analystos.db.base import session_scope
from analystos.db.models import User
from analystos.services import ask as ask_svc
from analystos.services import idempotency as idem
from analystos.services.idempotency import Key

router = APIRouter(prefix="/api", tags=["ask"])


class AskThreadIn(BaseModel):
    title: str | None = None


class AskThreadPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


class AskTurnIn(BaseModel):
    question: str
    parameters: dict | None = None  # values for a verified query's parameters (answers a "needs_input" refusal)
    # quick = one governed query (unchanged); analyst = a plan of <= 4 governed steps, facts, checks and a cited synthesis
    mode: Literal["quick", "analyst"] = "quick"


class AskRerunIn(BaseModel):
    sql: str | None = Field(default=None, max_length=100_000)


class AskScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cron: str = "0 9 * * *"
    timezone: str = "UTC"
    approval_id: str | None = None


class AskPromoteIn(BaseModel):
    target: Literal["verified_query", "metric", "monitor", "dashboard", "draft", "investigate", "report"]
    name: str | None = None
    question: str | None = None  # verified query: the phrasing it answers (default: the question asked)
    sql_expression: str | None = None  # metric / monitor: default = the answer's first aggregate
    display_name: str | None = None
    format: str | None = None
    kind: Literal["metric_drift", "metric_threshold", "change_point", "forecast_deviation"] | None = None
    op: str | None = None
    value: float | None = None
    grain: Literal["day", "week", "month"] | None = None
    auto_investigate: bool = False
    dashboard: str | None = None
    destination: str | None = None
    chart: dict | None = None
    objective: str | None = None  # investigate: default "Investigate why: <question>"
    approval_id: str | None = None  # dashboard: an approved request, to add the chart
    dashboard_id: str | None = None  # draft: an existing builder dashboard (else a new draft)
    title: str | None = None  # draft: the new draft's title (default: the question)
    extra: dict = Field(default_factory=dict)


@router.get("/workspaces/{workspace_id}/ask/threads")
def list_threads(workspace_id: str, q: str | None = None, archived: bool = False, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    return ask_svc.list_threads(session, user, workspace_id, q, archived=archived)


@router.post("/workspaces/{workspace_id}/ask/threads")
def create_thread(workspace_id: str, body: AskThreadIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.create_thread(session, user, workspace_id, body.title)


@router.get("/ask/threads/{thread_id}")
def get_thread(thread_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.thread_detail(session, user, thread_id)


@router.patch("/ask/threads/{thread_id}")
def patch_thread(thread_id: str, body: AskThreadPatch, response: Response, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """Rename or archive a thread. `If-Match` (its ETag) makes a stale edit 412; old clients may omit it (P4-06)."""
    from analystos.api.http import expected_revision, set_etag

    out = ask_svc.update_thread(session, user, thread_id, title=body.title, archived=body.archived,
                                expected_revision=expected_revision(if_match, required=False))
    set_etag(response, out.get("revision") or 1)
    return out


@router.post("/ask/threads/{thread_id}/turns")
async def ask_turn(thread_id: str, body: AskTurnIn, request: Request, auth: StreamAuth = Depends(streaming_auth),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Ask in a thread. With `Accept: text/event-stream` the plain-language stages stream as `stage`
    events, then `turn` (the persisted answer or refusal) and `end`; otherwise the turn is returned.
    A streamed turn ends with `expired` or `revoked` (and nothing after) if the caller loses access.
    With `Idempotency-Key` a retry of the same question returns the turn the first request produced
    (`Idempotent-Replayed: true`), 409 while that one still runs, 409 for a different body (P4-06)."""
    from analystos.events.stream import run_blocking

    user = auth.user

    def load(session: Session, caller: User) -> object:
        return ask_svc._thread_for(session, caller, thread_id)

    def check() -> str:
        with session_scope() as s:
            return load(s, s.merge(user)).workspace_id

    workspace_id = await run_blocking(check)  # an unknown thread is a 404 before any stream opens
    streamed = "text/event-stream" in request.headers.get("accept", "")
    request_body = {"thread_id": thread_id, "question": body.question, "parameters": body.parameters}
    if body.mode != "quick":
        request_body["mode"] = body.mode  # a quick request hashes as before
    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="ask.turn", request=request_body)
    if key is not None and (replay := await run_blocking(lambda: idem.claim(key))) is not None:
        turn = await run_blocking(lambda: _turn_json(user, replay.resource_id))
        headers = {"Idempotent-Replayed": "true"}
        if streamed:
            frames = [ask_svc._sse("turn", turn), ask_svc._sse("end", {"status": "done", "replayed": True})]
            return StreamingResponse(iter(frames), media_type="text/event-stream", headers={"Cache-Control": "no-cache", **headers})
        return JSONResponse(content=turn, headers=headers)
    if streamed:
        stream = ask_svc.stream_turn(user, thread_id, body.question, body.parameters, guard=stream_guard(auth, load),
                                     mode=body.mode)
        return StreamingResponse(_settle_stream(stream, key) if key is not None else stream,
                                 media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    import anyio

    try:
        out = await anyio.to_thread.run_sync(lambda: ask_svc.ask_in_thread(user, thread_id, body.question, body.parameters,
                                                                           mode=body.mode))
    except BaseException:
        if key is not None:
            await run_blocking(lambda: idem.release(key))
        raise
    if key is not None:
        await run_blocking(lambda: idem.complete(key, response={"turn_id": out["id"]}, resource_type="ask_turn", resource_id=out["id"]))
    return out


def _turn_json(user: User, turn_id: str) -> dict:
    with session_scope() as s:
        return ask_svc.turn_out(s, ask_svc._turn_for(s, s.merge(user), turn_id))


async def _settle_stream(stream, key):
    """Pass the SSE frames through; the `turn` frame completes the idempotency claim, anything else
    (an error, a revoked stream, a disconnect) releases it so a retry runs the question again."""
    import json

    from analystos.events.stream import run_blocking

    turn_id = None
    try:
        async for frame in stream:
            if frame.startswith("event: turn\n"):
                data = json.loads(frame.split("data: ", 1)[1])
                turn_id = data.get("id") if isinstance(data, dict) else None
            yield frame
    finally:
        if turn_id:
            await run_blocking(lambda: idem.complete(key, response={"turn_id": turn_id}, resource_type="ask_turn",
                                                     resource_id=turn_id))
        else:
            await run_blocking(lambda: idem.release(key))


@router.get("/ask/turns/{turn_id}/inspector")
def inspect_turn(turn_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.inspector(session, user, turn_id)


@router.get("/ask/turns/{turn_id}/why")
def why_turn_number(turn_id: str, number: str | None = None, column: str | None = None, row: int | None = None,
                    step: int | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """"Why this number?" (P7-08) for an Ask answer: each numeric cell (or the one given by `number` text,
    `column` and/or `row`) resolved fact -> step -> query receipt -> data version -> semantic version -> verdict,
    every link with its current state; broken and voided links are returned, never dropped. For an analyst
    turn `step` explains that step's numbers (default: the headline step, which the turn mirrors)."""
    from analystos.evidence.why import explain_ask_turn

    if step is not None:
        return ask_svc.why_step(session, user, turn_id, step, number=number, column=column, row=row)
    return explain_ask_turn(session, ask_svc._turn_for(session, user, turn_id), number=number, column=column, row=row)


@router.get("/ask/turns/{turn_id}/citations")
def turn_citations(turn_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The answer's evidence as two citation kinds (N-8): each governed query result, and the knowledge
    documents its model calls were given (with sha256 receipts); a number found only in a document is
    labelled document-sourced; document claims that disagree with the result are flagged."""
    from analystos.services.citations import for_turn

    return for_turn(session, ask_svc._turn_for(session, user, turn_id))


@router.post("/ask/turns/{turn_id}/rerun")
def rerun(turn_id: str, body: AskRerunIn, user: User = Depends(current_user)):
    return ask_svc.rerun_turn(user, turn_id, body.sql)


@router.post("/ask/turns/{turn_id}/steps/{n}/rerun")
def rerun_step(turn_id: str, n: int, body: AskRerunIn, user: User = Depends(current_user)):
    """Analyst turn: run step `n` again with its saved SQL (or `sql`, edited) through the gateway; the step's
    facts and checks are recomputed and the synthesis is marked stale. Returns the updated turn."""
    return ask_svc.rerun_step(user, turn_id, n, body.sql)


@router.post("/ask/turns/{turn_id}/synthesize")
def synthesize_turn(turn_id: str, user: User = Depends(current_user)):
    """Analyst turn: write the synthesis again from the steps' current facts (clears `stale`)."""
    return ask_svc.resynthesize(user, turn_id)


@router.post("/ask/turns/{turn_id}/schedule")
def schedule(turn_id: str, body: AskScheduleIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.services.saved_analysis import request_schedule

    return request_schedule(session, user, turn_id, **body.model_dump())


@router.post("/ask/turns/{turn_id}/promote")
def promote_turn(turn_id: str, body: AskPromoteIn, user: User = Depends(current_user)):
    """Promote an answer: verified query, metric, monitor, "Investigate why" (starts a run), a report (an
    HTML document of the stored answer, listed in Outputs), a dashboard chart (202 with an approval
    request first; again with the approved `approval_id` publishes it to the approved destination), or a
    recommended chart in a draft builder dashboard (`draft`: new, or `dashboard_id`; no publication)."""
    fields = body.model_dump(exclude={"target", "extra"}, exclude_none=True)
    out = ask_svc.promote(user, turn_id, body.target, {**body.extra, **fields})
    return JSONResponse(status_code=202, content=out) if out.get("status") == "approval_required" else out

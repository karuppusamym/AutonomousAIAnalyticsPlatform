"""Goal API v2 (S06 §3.1): goals, run signals, run state, the event stream, evidence and plan versions.

    POST /v2/goals                      Idempotency-Key required -> 202 {goal_id, run_id, state, events_url}
    POST /v2/runs/{id}/signals          {type: pause|resume|cancel|redirect|approve_step, payload}
    GET  /v2/runs/{id}                  state, steps, complexity decision, remaining budget, limitations
    GET  /v2/runs/{id}/events           Server-Sent Events (contract below)
    GET  /v2/runs/{id}/evidence         findings with verification standing, statement counts, Evidence v2 objects
    GET  /v2/evidence/{evidence_id}     one finding as the Evidence v2 object (S07-T4)
    GET  /v2/runs/{id}/plan?version=    the current plan, or a superseded version (immutable)

SSE contract (`docs/20-contracts/05-goal-api-v2.md`): one frame per persisted `run_event`,
`id: <event id>`, `event: <type>`, `data: {"v": 2, "id", "run_id", "type", "payload", "actor",
"created_at"}`; resume with `Last-Event-ID` or `?after_id=`; the stream ends with `event: end`
(`{"status"}`) once the run is terminal, or `event: expired|revoked` when the caller loses access.
`run.budget` frames carry `{limits, used, remaining, exhausted}` after every step of a goal run.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from analystos.api.deps import StreamAuth, current_user, db, stream_guard, streaming_auth
from analystos.autonomy import goals as goal_svc
from analystos.contracts.goals import GoalIn, SignalIn
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, User
from analystos.services import runs as run_svc

router = APIRouter(prefix="/v2", tags=["goals-v2"])


@router.post("/goals", status_code=202)
def create_goal(body: GoalIn, response: Response, user: User = Depends(current_user),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Accept a goal. A retried request with the same key and body returns the same goal and run
    (`Idempotent-Replayed: true`); the same key with another body is 409; no key is 422."""
    out, replayed = goal_svc.create_goal(user, body, idempotency_key)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    response.headers["Location"] = f"/v2/runs/{out['run_id']}"
    return out


@router.post("/runs/{run_id}/signals")
def signal(run_id: str, body: SignalIn, user: User = Depends(current_user)):
    return goal_svc.signal(user, run_id, body)


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return goal_svc.run_view(session, run_svc.get_run_for(session, user, run_id))


@router.get("/runs/{run_id}/evidence")
def evidence(run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return goal_svc.evidence_view(session, run_svc.get_run_for(session, user, run_id))


@router.get("/evidence/{evidence_id}")
def evidence_object(evidence_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """One finding as `EvidenceV2` (spec S07 §3.3): label, dataset and metric versions, statements, method,
    effect with its interval, sample and coverage, limitations, review status, fingerprint, void reason."""
    from analystos.db.models import Insight
    from analystos.evidence.api_v2 import for_insight
    from analystos.governance.policy import load_in_workspace

    return for_insight(session, load_in_workspace(session, Insight, evidence_id, user=user, label="evidence"))


@router.get("/runs/{run_id}/plan")
def plan(run_id: str, version: int | None = None, user: User = Depends(current_user),
         session: Session = Depends(db, scope="function")):
    from analystos.autonomy.replan import plan_version

    return plan_version(session, run_svc.get_run_for(session, user, run_id), version)


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after_id: int = 0, auth: StreamAuth = Depends(streaming_auth)):
    from analystos.events.stream import Batch, fetch_batch, run_blocking, run_event_stream

    def load(session: Session, user: User) -> AnalysisRun:
        return run_svc.get_run_for(session, user, run_id)

    def authorize(user: User) -> None:
        with session_scope() as s:
            load(s, user)

    def fetch_v2(rid: str, after: int) -> Batch:
        batch = fetch_batch(rid, after)
        return Batch(events=[{"v": 2, "run_id": rid, **e} for e in batch.events], status=batch.status)

    await run_blocking(authorize, auth.user)
    last = int(request.headers.get("last-event-id") or after_id)
    stream = run_event_stream(run_id, last, is_disconnected=request.is_disconnected, fetch=fetch_v2,
                              guard=stream_guard(auth, load))
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

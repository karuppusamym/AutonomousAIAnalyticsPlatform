"""Analyst Request Desk API (spec S13 §4). Intake answers 201 once the request is triaged; its auto path
(Ask, an investigation, a context lookup or escalation) runs after the response unless ``wait`` is set."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import User
from analystos.desk import service as desk

router = APIRouter(prefix="/api", tags=["desk"])


class RequestIn(BaseModel):
    workspace_id: str
    text: str = Field(min_length=3, max_length=4000)
    channel: Literal["ui", "api"] = "ui"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class RequestActionIn(BaseModel):
    type: Literal["accept", "reopen", "assign", "escalate", "answer", "decline", "mark_duplicate", "certify"]
    payload: dict = Field(default_factory=dict)


class BaselineIn(BaseModel):
    workspace_id: str
    request_class: Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"] = Field(alias="class")
    baseline_minutes: float = Field(gt=0, le=43200)
    source: Literal["practitioner_protocol", "pilot_review", "assumed"] = "practitioner_protocol"


@router.post("/requests", status_code=201)
def create_request(body: RequestIn, background: BackgroundTasks, wait: bool = False, user: User = Depends(current_user)):
    out = desk.submit(user, body.workspace_id, body.text, channel=body.channel, priority=body.priority, process=wait)
    if not wait and out["state"] == "triaged":
        background.add_task(desk.process_request, user, out["id"])
    return JSONResponse(status_code=201, content={"request": out, "conversation_id": out.get("conversation_id")})


@router.get("/requests")
def list_requests(view: Literal["mine", "queue", "all"] = "mine", workspace_id: str | None = None, state: str | None = None,
                  request_class: str | None = Query(default=None, alias="class"), limit: int = 50,
                  user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return desk.list_requests(session, user, view_name=view, workspace_id=workspace_id, state=state,
                              request_class=request_class, limit=limit)


@router.get("/requests/{request_id}")
def get_request(request_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return desk.get_request(session, user, request_id)


@router.post("/requests/{request_id}/actions")
def request_action(request_id: str, body: RequestActionIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    return desk.act(session, user, request_id, body.type, body.payload)


@router.get("/value")
def value(workspace_id: str = Query(alias="workspace"), start: datetime | None = Query(default=None, alias="from"),
          end: datetime | None = Query(default=None, alias="to"), user: User = Depends(current_user),
          session: Session = Depends(db, scope="function")):
    return desk.value_report(session, user, workspace_id, start=start, end=end)


@router.put("/value/baselines")
def put_baseline(body: BaselineIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return desk.set_baseline(session, user, body.workspace_id, body.request_class, body.baseline_minutes, body.source)

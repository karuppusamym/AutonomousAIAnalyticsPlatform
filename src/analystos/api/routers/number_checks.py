"""Number check (S07-T8): check a number quoted elsewhere against the workspace's governed path.

    POST /v2/number-checks   {workspace_id, question, claimed, label?, column?, tolerance?, source?}
                             -> 200 {verdict: confirmed|contradicted|unverifiable, authority, claimed, computed,
                                     comparison, reason, evidence{turn_id, sql, query_id, result_hash, assets, ...}}
    POST /v2/number-checks/handoff  {workspace_id, turn_id, claimed, label?, column?, tolerance?, source?}
                             -> 201 {request_id, state: escalated, assignee_id, sla_due_at, verdict}

Mounted under `/api` too (`/api/v2/number-checks`) for the web client. The claimed value never reaches a model.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from analystos.api.deps import current_user
from analystos.db.models import User
from analystos.evidence import number_check

router = APIRouter(prefix="/v2", tags=["number-checks"])


class NumberCheckIn(BaseModel):
    workspace_id: str
    question: str = Field(min_length=3, max_length=number_check.MAX_QUESTION)
    claimed: str | float = Field(description="One number as written: 1204, 1,204.5 or 12.5%")
    label: str | None = Field(default=None, max_length=200)
    column: str | None = Field(default=None, max_length=200)
    tolerance: float = Field(default=0.0, ge=0.0, le=number_check.MAX_TOLERANCE)
    source: str | None = Field(default=None, max_length=number_check.MAX_SOURCE)


@router.post("/number-checks")
def create_number_check(body: NumberCheckIn, user: User = Depends(current_user)):
    return number_check.check(user, body.workspace_id, body.question, body.claimed, label=body.label,
                              column=body.column, tolerance=body.tolerance, source=body.source, channel="api")


class NumberCheckHandoffIn(BaseModel):
    workspace_id: str
    turn_id: str = Field(min_length=1, max_length=40)
    claimed: str | float
    label: str | None = Field(default=None, max_length=200)
    column: str | None = Field(default=None, max_length=200)
    tolerance: float = Field(default=0.0, ge=0.0, le=number_check.MAX_TOLERANCE)
    source: str | None = Field(default=None, max_length=number_check.MAX_SOURCE)


@router.post("/number-checks/handoff", status_code=201)
def hand_off_number_check(body: NumberCheckHandoffIn, user: User = Depends(current_user)):
    """Send a check to the Request Desk (an escalated R7 request); the verdict is recomputed from the stored answer."""
    return number_check.handoff(user, body.workspace_id, body.turn_id, body.claimed, label=body.label,
                                column=body.column, tolerance=body.tolerance, source=body.source)

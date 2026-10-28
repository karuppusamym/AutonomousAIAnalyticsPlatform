"""Triggers, alert groups and conversation channels (spec S09).

    POST /v2/triggers/webhook/{rule_id}                  signed event (HMAC over timestamp.body) -> 202 after enqueue
    POST /v2/workspaces/{workspace_id}/trigger-rules     create a webhook / Kafka rule for a monitor (editor)
    GET  /v2/workspaces/{workspace_id}/trigger-rules
    POST /v2/workspaces/{workspace_id}/trigger-rules/{rule_id}/enabled
    GET  /v2/workspaces/{workspace_id}/trigger-events    recent accepted events and their outcomes
    POST /v2/workspaces/{workspace_id}/monitors/{monitor_id}/rule-approval   (re)request a rule monitor's approval
    GET  /v2/workspaces/{workspace_id}/alert-groups
    POST /v2/workspaces/{workspace_id}/channel-bindings  inbound Slack / Teams / email channel (editor)
    GET  /v2/workspaces/{workspace_id}/channel-bindings
    POST /v2/channels/{kind}/{binding_id}                signed inbound platform message -> Request Desk request

The webhook and inbound channel routes carry no user session: the signature with the rule's or binding's own
secret is the authentication, and every write runs as the tenant that owns the rule or binding.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import Monitor, User
from analystos.governance.policy import load_in_workspace, require_role
from analystos.triggers import channels, groups, ingress, rules
from analystos.triggers.models import ChannelBinding, TriggerEvent, TriggerRule

router = APIRouter(prefix="/v2", tags=["triggers-v2"])


class TriggerRuleIn(BaseModel):
    monitor_id: str
    name: str
    kind: str = Field(description="webhook | kafka")
    action: str = "evaluate"
    secret_ref: str | None = None
    payload_schema: dict[str, Any] = {"type": "object"}
    topic: str | None = None
    rule_set: str = "default"
    transform: dict[str, str] = {}


class EnabledIn(BaseModel):
    enabled: bool


class ChannelBindingIn(BaseModel):
    kind: str = Field(description="slack | teams | email")
    name: str
    secret_ref: str
    external_key: str | None = None
    default_requester_id: str | None = None


@router.post("/triggers/webhook/{rule_id}", status_code=202)
async def webhook(rule_id: str, request: Request):
    """Accept one signed event: 202 once it is durably queued, 200 for a replayed event id (nothing processed)."""
    from starlette.concurrency import run_in_threadpool

    from analystos.tenancy.background import carry_context

    body = await request.body()
    accepted = await run_in_threadpool(carry_context(ingress.accept_webhook), rule_id, dict(request.headers), body)
    return JSONResponse(accepted.body(), status_code=accepted.status_code)


@router.post("/workspaces/{workspace_id}/trigger-rules", status_code=201)
def create_rule(workspace_id: str, body: TriggerRuleIn, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    rule = rules.create_rule(session, session.merge(user), workspace_id, **body.model_dump())
    out = rules.out(rule)
    session.commit()
    return out


@router.get("/workspaces/{workspace_id}/trigger-rules")
def list_rules(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, session.merge(user), workspace_id, "viewer")
    return [rules.out(r) for r in session.scalars(select(TriggerRule).where(TriggerRule.workspace_id == workspace_id)
                                                  .order_by(TriggerRule.created_at.desc()))]


@router.post("/workspaces/{workspace_id}/trigger-rules/{rule_id}/enabled")
def set_rule_enabled(workspace_id: str, rule_id: str, body: EnabledIn, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    rule = load_in_workspace(session, TriggerRule, rule_id, workspace_id, user=user, minimum="editor", label="trigger rule")
    out = rules.out(rules.set_enabled(session, session.merge(user), rule, body.enabled))
    session.commit()
    return out


@router.get("/workspaces/{workspace_id}/trigger-events")
def list_events(workspace_id: str, rule_id: str | None = None, limit: int = 100, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    require_role(session, session.merge(user), workspace_id, "viewer")
    stmt = select(TriggerEvent).where(TriggerEvent.workspace_id == workspace_id)
    if rule_id:
        stmt = stmt.where(TriggerEvent.rule_id == rule_id)
    return [{c: getattr(e, c) for c in ("id", "rule_id", "event_id", "source", "status", "attempts", "result", "error",
                                        "received_at", "processed_at")}
            for e in session.scalars(stmt.order_by(TriggerEvent.received_at.desc()).limit(min(limit, 500)))]


@router.post("/workspaces/{workspace_id}/monitors/{monitor_id}/rule-approval")
def request_rule_approval(workspace_id: str, monitor_id: str, user: User = Depends(current_user),
                          session: Session = Depends(db, scope="function")):
    monitor = load_in_workspace(session, Monitor, monitor_id, workspace_id, user=user, minimum="editor", label="monitor")
    if not rules.requires_approval(monitor.config):
        from analystos.core.errors import InvalidInput

        raise InvalidInput("this monitor neither notifies a channel nor starts work: it has no rule to approve")
    approval = rules.request_rule_approval(session, session.merge(user), monitor)
    out = {"monitor_id": monitor.id, "approval_id": approval.id, "rule_hash": monitor.rule_hash, "status": approval.status}
    session.commit()
    return out


@router.get("/workspaces/{workspace_id}/alert-groups")
def list_groups(workspace_id: str, status: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    return groups.list_groups(session, session.merge(user), workspace_id, status=status)


@router.post("/workspaces/{workspace_id}/channel-bindings", status_code=201)
def create_binding(workspace_id: str, body: ChannelBindingIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    b = channels.create_binding(session, session.merge(user), workspace_id, **body.model_dump())
    out = channels.binding_out(b)
    session.commit()
    return out


@router.get("/workspaces/{workspace_id}/channel-bindings")
def list_bindings(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, session.merge(user), workspace_id, "viewer")
    return [channels.binding_out(b) for b in session.scalars(select(ChannelBinding)
                                                             .where(ChannelBinding.workspace_id == workspace_id))]


@router.post("/channels/{kind}/{binding_id}")
async def inbound(kind: str, binding_id: str, request: Request):
    """One signed Slack event, Teams outgoing-webhook message or relayed email -> a Desk request."""
    from starlette.concurrency import run_in_threadpool

    from analystos.tenancy.background import carry_context

    body = await request.body()
    result = await run_in_threadpool(carry_context(channels.accept_inbound), kind, binding_id, dict(request.headers), body)
    return JSONResponse(result.body, status_code=result.status_code)

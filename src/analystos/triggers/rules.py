"""Monitor rules and trigger rules (spec S09 §3.1, §3.3).

A **rule monitor** is a monitor whose config says what happens when it fires: `notify` (a channel such as
`teams:payments-ops`, i.e. a delivery destination by kind and name) and/or `on_trigger` (a *pinned* playbook
version with limits), over a frozen `scope`. It is approved once: the approval's payload is the rule's scope,
recipients, playbook and limits, so changing any of them gives a new hash, invalidates the old approval and
requests a new one. Every side effect it causes (an outbound message, a goal run) calls
`verify_for_execution` against that approval immediately before acting (CLAUDE.md rule 6).

A **trigger rule** says how outside events reach a monitor: a signed webhook (`secret_ref`) or a Kafka topic,
each with a typed payload schema. It never widens what the monitor may do.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from analystos.core.errors import ApprovalRequired, Conflict, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Approval, DeliveryDestination, Monitor, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role
from analystos.triggers.models import TriggerRule

APPROVAL_ACTION = "monitor_rule.approve"
RULE_KEYS = ("notify", "on_trigger")
KINDS = ("webhook", "kafka")
ACTIONS = ("evaluate", "observe")
NOTIFY_MODES = ("request_approval", "auto")
# Inbound signing secrets live in their own namespace, so a rule cannot be pointed at another secret.
TRIGGER_SECRET_REF = re.compile(r"^env:ANALYSTOS_TRIGGER_[A-Z0-9_]+$")
_POINTER = re.compile(r"^(/[^/]*)*$")
LIMIT_KEYS = ("max_cost_usd", "max_steps", "max_duration_s", "max_replans", "max_retries", "max_scan_bytes")


def _settings():  # noqa: ANN202
    from analystos.core.config import get_settings

    return get_settings()


# ------------------------------------------------------------------------------------ rule monitors
def requires_approval(config: dict[str, Any] | None) -> bool:
    return any((config or {}).get(k) for k in RULE_KEYS)


def parse_playbook(ref: Any) -> dict[str, Any]:
    """`investigate.v1@3`-style `key@version`, or `{key, version}`: a trigger runs a pinned version only."""
    if isinstance(ref, str):
        key, sep, version = ref.partition("@")
        ref = {"key": key.strip(), "version": version.strip()} if sep else {"key": key.strip()}
    if not isinstance(ref, dict) or not ref.get("key"):
        raise InvalidInput("on_trigger.playbook must be key@version or {key, version}")
    if ref.get("version") in (None, ""):
        raise InvalidInput("on_trigger.playbook must pin a version (key@version): a monitor runs a fixed playbook")
    version = ref["version"]
    if isinstance(version, int) or str(version).isdigit():  # a workspace definition version
        return {"key": str(ref["key"]), "version": int(version)}
    return {"key": str(ref["key"]), "version": str(version), "source": "builtin"}  # an installed manifest's semver


def validate_rule_config(session: Session, workspace_id: str, config: dict[str, Any]) -> None:
    """Shape of `scope`, `notify` and `on_trigger`; the playbook must be runnable now."""
    scope = config.get("scope")
    if scope is not None and (not isinstance(scope, dict) or set(scope) - {"source", "filters", "assets"}):
        raise InvalidInput("scope must be {source, filters, assets}")
    if scope and scope.get("filters") is not None and not isinstance(scope["filters"], dict):
        raise InvalidInput("scope.filters must be an object of column: value")
    notify = config.get("notify")
    if notify is not None:
        if not isinstance(notify, dict) or set(notify) - {"channel", "channels", "mode"}:
            raise InvalidInput("notify must be {channel: <kind>:<destination name>, mode}")
        if notify.get("mode", "request_approval") not in NOTIFY_MODES:
            raise InvalidInput(f"notify.mode must be one of {list(NOTIFY_MODES)}")
        if not _channel_refs(notify):
            raise InvalidInput("notify needs a channel (<kind>:<destination name>)")
        for ref in _channel_refs(notify):
            _destination(session, workspace_id, ref)
    on_trigger = config.get("on_trigger")
    if on_trigger is not None:
        if not isinstance(on_trigger, dict) or set(on_trigger) - {"playbook", "limits", "objective"}:
            raise InvalidInput("on_trigger must be {playbook: key@version, limits, objective}")
        ref = parse_playbook(on_trigger.get("playbook"))
        limits = on_trigger.get("limits") or {}
        if not isinstance(limits, dict) or set(limits) - set(LIMIT_KEYS) or \
                not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in limits.values()):
            raise InvalidInput(f"on_trigger.limits takes positive numbers for {list(LIMIT_KEYS)}")
        from analystos.services.definitions import resolve_runnable

        resolve_runnable(session, workspace_id, ref, trigger="monitor")


def _channel_refs(notify: dict[str, Any]) -> list[str]:
    refs = notify.get("channels") or ([notify["channel"]] if notify.get("channel") else [])
    if not isinstance(refs, list) or not all(isinstance(r, str) and ":" in r for r in refs):
        raise InvalidInput("notify.channel must look like <kind>:<destination name>, e.g. teams:payments-ops")
    return refs


def _destination(session: Session, workspace_id: str, ref: str) -> DeliveryDestination:
    kind, _, name = ref.partition(":")
    dest = session.scalar(select(DeliveryDestination).where(DeliveryDestination.workspace_id == workspace_id,
                                                            DeliveryDestination.kind == kind.strip(),
                                                            DeliveryDestination.name == name.strip()))
    if dest is None:
        raise InvalidInput(f"notify channel {ref}: no {kind} destination named {name!r} in this workspace")
    if dest.status == "revoked":
        raise InvalidInput(f"notify channel {ref} is revoked")
    if "alert" not in (dest.content_kinds or []):
        raise InvalidInput(f"notify channel {ref} does not accept alerts")
    return dest


def recipients(session: Session, monitor: Monitor) -> list[str]:
    """Destination ids an alert of this monitor goes to: `deliver_to` plus the `notify` channels."""
    cfg = monitor.config or {}
    ids = list(cfg.get("deliver_to") or [])
    if cfg.get("notify"):
        for ref in _channel_refs(cfg["notify"]):
            try:
                ids.append(_destination(session, monitor.workspace_id, ref).id)
            except InvalidInput:
                continue  # a channel renamed away is not a recipient; the approval no longer matches either
    return list(dict.fromkeys(ids))


def rule_payload(session: Session, monitor: Monitor) -> dict[str, Any]:
    """What an approver signs, rebuilt from the monitor's current row at every check: what it reads (kind,
    metric, scope), who hears about it (each recipient with its destination hash) and what it may start."""
    cfg = monitor.config or {}
    dests = []
    for dest_id in recipients(session, monitor):
        dest = session.get(DeliveryDestination, dest_id)
        if dest is not None:
            dests.append({"id": dest.id, "kind": dest.kind, "destination_hash": dest.destination_hash})
    on_trigger = cfg.get("on_trigger") or None
    return {"monitor_id": monitor.id, "workspace_id": monitor.workspace_id, "owner": monitor.created_by,
            "kind": monitor.kind, "metric": cfg.get("metric"), "sql_expression": cfg.get("sql_expression"),
            "scope": cfg.get("scope") or {}, "recipients": sorted(dests, key=lambda d: d["id"]),
            "notify_mode": (cfg.get("notify") or {}).get("mode", "request_approval") if cfg.get("notify") else None,
            "on_trigger": None if on_trigger is None else {"playbook": parse_playbook(on_trigger.get("playbook")),
                                                           "limits": on_trigger.get("limits") or {},
                                                           "objective": on_trigger.get("objective")}}


def request_rule_approval(session: Session, user: User, monitor: Monitor) -> Approval:
    from analystos.governance.approvals import request_approval

    ws = get_workspace(session, monitor.workspace_id)
    payload = rule_payload(session, monitor)
    current = session.get(Approval, monitor.approval_id) if monitor.approval_id else None
    if current is not None and current.status in ("pending", "approved") and current.payload_hash == stable_hash(payload)             and _aware(current.expires_at) > utcnow():
        return current  # asking again for the same rule keeps the open or standing approval
    if monitor.approval_id:
        _invalidate(session, monitor.approval_id, "monitor rule changed; a new approval was requested")
    approval = request_approval(session, workspace_id=monitor.workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=payload, plan_hash=None, policy_version=ws.policy_version,
                                requested_by=user.id, risk_tier="high", destination=f"monitor:{monitor.name}"[:120],
                                affected_assets=list((payload["scope"] or {}).get("assets") or []),
                                evidence={"summary": _describe(payload)})
    monitor.approval_id, monitor.rule_hash = approval.id, stable_hash(payload)
    audit(f"user:{user.id}", "monitor.rule_approval_requested", workspace_id=monitor.workspace_id, target=monitor.id,
          decision="approval_required", details={"approval_id": approval.id, "rule_hash": monitor.rule_hash}, session=session)
    return approval


def _aware(value):  # noqa: ANN001, ANN202
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _describe(payload: dict[str, Any]) -> str:
    who = ", ".join(f"{r['kind']}:{r['id']}" for r in payload["recipients"]) or "nobody outside the platform"
    run = payload["on_trigger"]
    what = f"; starts {run['playbook']['key']}@{run['playbook']['version']} with limits {run['limits']}" if run else ""
    return f"monitor {payload['monitor_id']} over {payload['scope'] or 'the workspace scope'} notifies {who}{what}"


def _invalidate(session: Session, approval_id: str, reason: str) -> None:
    session.execute(update(Approval).where(Approval.id == approval_id, Approval.status.in_(["pending", "approved"]))
                    .values(status="invalidated", reason=reason))


def after_config_change(session: Session, user: User, monitor: Monitor) -> Approval | None:
    """Changing scope, recipients, playbook or limits needs re-approval; anti-noise settings do not."""
    if not requires_approval(monitor.config):
        if monitor.approval_id:
            _invalidate(session, monitor.approval_id, "the monitor no longer notifies or starts work")
            monitor.approval_id, monitor.rule_hash = None, None
        return None
    if monitor.approval_id and monitor.rule_hash == stable_hash(rule_payload(session, monitor)):
        return None
    return request_rule_approval(session, user, monitor)


def apply_decision(session: Session, approval: Approval) -> Monitor | None:
    """After an approver approves a rule, the approval is standing (like a destination authorization)."""
    if approval.action != APPROVAL_ACTION:
        return None
    monitor = session.scalar(select(Monitor).where(Monitor.approval_id == approval.id))
    if monitor is None:
        return None
    if approval.status == "approved":
        if stable_hash(rule_payload(session, monitor)) != approval.payload_hash:
            _invalidate(session, approval.id, "monitor rule changed while its approval was pending")
            raise Conflict("the monitor rule changed while its approval was pending; a new approval is required")
        until = utcnow() + timedelta(days=int(_settings().delivery_authorization_days))
        session.execute(update(Approval).where(Approval.id == approval.id).values(expires_at=until))
        session.expire(approval)
        emit(monitor.workspace_id, "monitor.rule_approved", {"monitor_id": monitor.id, "approval_id": approval.id},
             actor=f"user:{approval.decided_by}", session=session)
    return monitor


def verify_rule(session: Session, monitor: Monitor) -> Approval:
    """Immediately before a side effect: the rule's approval still binds exactly this scope, these recipients,
    this playbook and these limits, and its requester and approver still hold their roles."""
    from analystos.governance.approvals import verify_for_execution

    if not monitor.approval_id:
        raise ApprovalRequired(f"monitor {monitor.name} has no approved rule; request its approval first")
    return verify_for_execution(session, monitor.approval_id, payload=rule_payload(session, monitor), plan_hash=None)


def start_goal(monitor_id: str, alert_id: str) -> dict[str, Any]:
    """Rule -> goal: start the monitor's pinned playbook with its frozen scope and limits. Refused (and
    recorded on the alert) unless the rule's approval verifies now."""
    from analystos.db.base import session_scope
    from analystos.db.models import Alert, Workspace
    from analystos.services.runs import start_run_request

    with session_scope() as s:
        monitor = s.get(Monitor, monitor_id)
        alert = s.get(Alert, alert_id)
        cfg = monitor.config or {}
        on_trigger = cfg.get("on_trigger") or {}
        try:
            approval = verify_rule(s, monitor)
        except (ApprovalRequired, PolicyDenied) as exc:
            alert.data = {**(alert.data or {}), "goal": {"refused": exc.message}}
            audit(f"monitor:{monitor.id}", "monitor.goal_refused", workspace_id=monitor.workspace_id, target=alert.id,
                  decision="deny", reasons=[exc.message], session=s)
            return {"refused": exc.message}
        owner = s.get(User, monitor.created_by)
        ws = s.get(Workspace, monitor.workspace_id)
        objective = (on_trigger.get("objective") or
                     f"Investigate this monitored change and identify its drivers: {alert.message} "
                     f"Business objective: {ws.objective}")[:2000]
        ref = parse_playbook(on_trigger.get("playbook"))
        limits, scope, ws_id, approval_id = dict(on_trigger.get("limits") or {}), cfg.get("scope") or {}, ws.id, approval.id
        s.expunge(owner)
    run, _ = start_run_request(owner, ws_id, objective=objective, definition=ref, limits=limits or None,
                               origin={"type": "alert", "alert_id": alert_id, "monitor_id": monitor_id,
                                       "rule_approval_id": approval_id, "scope": scope, "publish": "skip",
                                       "automatic": True})
    with session_scope() as s:
        a = s.get(Alert, alert_id)
        a.investigation_run_id = run.id
        a.data = {**(a.data or {}), "goal": {"run_id": run.id, "playbook": ref, "limits": limits}}
    return {"run_id": run.id}


# ------------------------------------------------------------------------------------ trigger rules
def _check_schema(schema: Any) -> dict[str, Any]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise InvalidInput("payload_schema must be a JSON Schema with type: object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidInput(f"payload_schema is not a valid JSON Schema: {exc.message}") from None
    return schema


def validate_payload(rule: TriggerRule, payload: Any) -> dict[str, Any]:
    """The typed payload contract: an event that does not satisfy the rule's schema is refused (422)."""
    from jsonschema import Draft202012Validator

    if not isinstance(payload, dict):
        raise InvalidInput("the event payload must be a JSON object")
    errors = sorted(Draft202012Validator(rule.payload_schema or {"type": "object"}).iter_errors(payload),
                    key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(root)"
        raise InvalidInput(f"payload does not match the rule's schema at {where}: {e.message}",
                           details={"errors": len(errors)})
    return payload


def _check_transform(transform: Any) -> dict[str, str]:
    transform = transform or {}
    if not isinstance(transform, dict) or not all(isinstance(k, str) and isinstance(v, str) and _POINTER.match(v)
                                                  for k, v in transform.items()):
        raise InvalidInput("transform maps payload field names to JSON pointers, e.g. {observed: /metrics/rate}")
    return transform


def create_rule(session: Session, user: User, workspace_id: str, *, monitor_id: str, name: str, kind: str,
                action: str = "evaluate", secret_ref: str | None = None, payload_schema: dict[str, Any] | None = None,
                topic: str | None = None, rule_set: str = "default", transform: dict[str, str] | None = None) -> TriggerRule:
    require_role(session, user, workspace_id, "editor")
    monitor = session.get(Monitor, monitor_id)
    if monitor is None or monitor.workspace_id != workspace_id:
        raise NotFound("monitor not found in this workspace")
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {list(KINDS)}")
    if action not in ACTIONS:
        raise InvalidInput(f"action must be one of {list(ACTIONS)}")
    if action == "observe":
        from analystos.triggers import noise

        if not noise.parse(monitor.config).enter:
            raise InvalidInput("an observe rule needs the monitor's condition.enter: the event's fields are the reading")
    name = (name or "").strip()
    if not name:
        raise InvalidInput("a trigger rule needs a name")
    if session.scalar(select(TriggerRule.id).where(TriggerRule.workspace_id == workspace_id, TriggerRule.name == name)):
        raise Conflict(f"a trigger rule named {name!r} already exists in this workspace")
    if kind == "webhook":
        if not TRIGGER_SECRET_REF.match(str(secret_ref or "")):
            raise InvalidInput("a webhook rule needs secret_ref = env:ANALYSTOS_TRIGGER_<NAME>, the environment "
                               "variable holding its HMAC signing secret")
        topic = None
    else:
        from analystos.triggers.kafka import topic_allowed

        if not topic or not re.match(r"^[A-Za-z0-9._-]{1,249}$", topic):
            raise InvalidInput("a kafka rule needs a topic name")
        if not topic_allowed(get_workspace(session, workspace_id).tenant_id, topic):
            raise PolicyDenied(f"topic {topic} is not in this tenant's Kafka topic ACL (ANALYSTOS_KAFKA_TOPIC_ACL)")
        secret_ref = None
    if not re.match(r"^[a-z0-9_-]{1,80}$", rule_set or ""):
        raise InvalidInput("rule_set must be a short lowercase name")
    rule = TriggerRule(id=new_id("trg"), workspace_id=workspace_id, monitor_id=monitor.id, name=name[:200], kind=kind,
                       action=action, secret_ref=secret_ref, payload_schema=_check_schema(payload_schema or {"type": "object"}),
                       topic=topic, rule_set=rule_set, transform=_check_transform(transform), enabled=True,
                       created_by=user.id, revision=1)
    session.add(rule)
    session.flush()
    audit(f"user:{user.id}", "trigger.rule_created", workspace_id=workspace_id, target=rule.id,
          details={"kind": kind, "monitor_id": monitor.id, "action": action, "topic": topic}, session=session)
    return rule


def set_enabled(session: Session, user: User, rule: TriggerRule, enabled: bool) -> TriggerRule:
    require_role(session, user, rule.workspace_id, "editor")
    rule.enabled, rule.revision = enabled, (rule.revision or 1) + 1
    audit(f"user:{user.id}", "trigger.rule_updated", workspace_id=rule.workspace_id, target=rule.id,
          details={"enabled": enabled}, session=session)
    return rule


def out(rule: TriggerRule) -> dict[str, Any]:
    return {"id": rule.id, "workspace_id": rule.workspace_id, "monitor_id": rule.monitor_id, "name": rule.name,
            "kind": rule.kind, "action": rule.action, "secret_ref": rule.secret_ref, "payload_schema": rule.payload_schema,
            "topic": rule.topic, "rule_set": rule.rule_set, "transform": rule.transform, "enabled": rule.enabled,
            "revision": rule.revision, "webhook_url": f"/v2/triggers/webhook/{rule.id}" if rule.kind == "webhook" else None}

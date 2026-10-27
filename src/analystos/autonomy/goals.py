"""Goal API v2 service (S06 §3.1): a goal -> complexity router -> one run, created atomically.

`create_goal` requires an `Idempotency-Key`. The goal row, its run, the run's dispatch-outbox row and
the idempotency record commit in one transaction (services/runs.start_run_request), so a retried
POST returns the same goal and run, and the same key with a different body is 409.

Mode -> autonomy: `goal_supervised` = autonomy 2 (the plan waits for approval before any step);
`goal_autonomous` = autonomy 3, capped by the workspace's level. A `clarify_needed` escalation from
the router makes the run supervised whatever was asked. Approvals are never waived: publish and
notify are always `required` (contracts.goals.ApprovalPolicy)."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select

from analystos.autonomy.complexity import route
from analystos.contracts.analysis import Filter
from analystos.contracts.goals import ComplexityDecision, ComplexityFeatures, GoalIn, RunLimits, SignalIn
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    Approval,
    IdempotencyRecord,
    Relationship,
    RunTask,
    SourceAsset,
    SourceColumn,
    User,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role, resolve_scope, scoped_loader

OPERATION = "goal.create"
_TEMPLATE = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<major>\d+)$")


def parse_playbook(value: str | None) -> tuple[str | None, int | None]:
    """`investigate.v1@3` -> ("playbook.investigate", 3). A capability id or a workspace playbook key
    passes through; `@N` names an exact published definition version."""
    if not value:
        return None, None
    name, _, version = value.partition("@")
    if version and not version.isdigit():
        raise InvalidInput("playbook version must be an integer: name@N")
    m = _TEMPLATE.match(name)
    if m:
        from analystos.capabilities import registry

        pid = f"playbook.{m.group('name')}"
        manifest = registry.current().manifests.get(pid)
        if manifest is None or manifest.kind != "Playbook":
            raise InvalidInput(f"unknown playbook template {name}")
        if manifest.version.split(".")[0] != m.group("major"):
            raise InvalidInput(f"{name}: this installation has {pid}@{manifest.version}")
        name = pid
    return name, int(version) if version else None


def _features(s: Any, user: User, body: GoalIn, scope: Any, policy: Any) -> tuple[ComplexityFeatures, list[str], list[str]]:
    """Deterministic features of the goal over its authorized scope."""
    from analystos.autonomy.complexity import ambiguity_flags, policy_tier

    fq = set(scope.assets)
    assets = [a for a in s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == body.workspace_id))
              if f"{a.schema_name}.{a.name}" in fq]
    ids = {a.id for a in assets}
    edges = {(r.from_asset_id, r.to_asset_id) for r in s.scalars(select(Relationship).where(
        Relationship.workspace_id == body.workspace_id, Relationship.validated.is_(True)))
        if r.from_asset_id in ids and r.to_asset_id in ids and r.from_asset_id != r.to_asset_id}
    joined = {a for e in edges for a in e}
    join_depth = max(0, len(joined) - 1) if edges else 0  # a spanning chain over the related tables
    tags = [c.tags or [] for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(ids)))] if ids else []
    sensitive = sum(1 for t in tags if {"pii", "sensitive"} & set(t))
    restricted = sum(1 for t in tags if "restricted" in t)
    autonomous = body.mode == "goal_autonomous"
    tier, policy_reasons = policy_tier(denied_columns=len(scope.denied_columns), sensitive_columns=sensitive,
                                       restricted_columns=restricted, autonomous=autonomous,
                                       external_side_effect="dashboard_draft" in body.requested_outputs)
    flags = ambiguity_flags(body.question)
    return ComplexityFeatures(table_count=len(assets), join_depth=join_depth,
                              estimated_scan_rows=int(sum(a.row_count or 0 for a in assets)), ambiguity_flags=flags,
                              policy_tier=tier, requested_artifacts=list(body.requested_outputs)), policy_reasons, flags


def classify(user: User, body: GoalIn, *, services: Any = None) -> tuple[ComplexityDecision, int]:
    """The router's decision for this goal, and the autonomy level its run gets."""
    from analystos.capabilities import enablement, registry

    requested, _version = parse_playbook(body.playbook)
    snapshot = registry.current()
    with session_scope() as s:
        require_role(s, user, body.workspace_id, "analyst")
        ws = get_workspace(s, body.workspace_id)
        scope = resolve_scope(s, s.merge(user), body.workspace_id, source_ids=body.data_scope.source_ids or None)
        if not scope.assets:
            raise InvalidInput("no selected, ready assets: add a source, discover it and select tables first")
        _check_filters(scope, body.data_scope.filters)
        features, policy_reasons, _ = _features(s, user, body, scope, None)
        explicit = enablement.overrides(s, body.workspace_id)
        level = 3 if body.mode == "goal_autonomous" else 2
        level = min(level, ws.autonomy_level)

        def runnable(pid: str) -> str | None:
            m = snapshot.manifests.get(pid)
            if m is None:
                return "not installed"
            return enablement.usable(m, snapshot, explicit, autonomous_run=level >= 3)

    decide = None
    if services is not None and getattr(services, "router", None) is not None:
        from analystos.runtime.context import workspace_call_ctx

        def decide(purpose: str, state: dict, question: Any, facts: dict) -> Any:
            return services.decisions.decide(purpose, state, question, facts=facts,
                                             ctx=workspace_call_ctx(body.workspace_id, agent_id="goal_router"))

    decision = route(features, question=body.question, snapshot=snapshot, runnable=runnable, requested_tier=body.tier,
                     requested_playbook=requested, policy_reasons=policy_reasons, decide=decide)
    if decision.clarify.get("value") == "clarify":
        level = min(level, 2)  # escalate_only: a person approves the plan before any step runs
    return decision, level


def _check_filters(scope: Any, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    columns = {c for cols in scope.columns.values() for c in cols}
    for raw in filters:
        f = Filter.model_validate({**raw, "origin": "user_redirect"})
        if f.via is None and f.column not in columns:
            raise InvalidInput(f"data_scope filter column {f.column!r} is not in the authorized scope")
        out.append(f.model_dump())
    return out


def _completed_replay(key: Any) -> bool:
    """A retried POST: skip routing (and its decision rung) and let the transaction replay it."""
    with session_scope() as s:
        rec = s.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.principal == key.principal, IdempotencyRecord.workspace_id == key.workspace_id,
            IdempotencyRecord.operation == key.operation, IdempotencyRecord.key == key.key))
        return rec is not None and rec.status == "completed"


def create_goal(user: User, body: GoalIn, idempotency_key: str | None, *, services: Any = None) -> tuple[dict, bool]:
    from analystos.autonomy.models import Goal
    from analystos.services import runs as run_svc
    from analystos.services.idempotency import Key

    key = Key.of(idempotency_key, principal=user.id, workspace_id=body.workspace_id, operation=OPERATION,
                 request=body.model_dump(mode="json"))
    if key is None:
        raise InvalidInput("POST /v2/goals requires an Idempotency-Key header")
    if _completed_replay(key):
        run, replayed = run_svc.start_run_request(user, body.workspace_id, objective=body.question, idempotency=key)
        return _goal_out(run.id), replayed

    decision, level = classify(user, body, services=services)
    limits = RunLimits.model_validate({**decision.limits, "max_parallel": _max_parallel(body.workspace_id)})         .tightened(body.limits).as_dict()
    playbook, version = parse_playbook(body.playbook) if body.playbook else (decision.playbook, None)
    goal_id = new_id("goal")
    origin = {"type": "goal", "goal_id": goal_id, "mode": body.mode, "requested_outputs": list(body.requested_outputs),
              "publish": "propose" if "dashboard_draft" in body.requested_outputs else "skip",
              **({"request_id": body.request_id} if body.request_id else {})}

    def on_created(s: Any, run: AnalysisRun, scope: Any) -> dict:
        run.limits = {**limits, "source_slots": _source_slots(s, sorted(set(scope.asset_sources.values())))}
        filters = _check_filters(scope, body.data_scope.filters)
        if filters:
            run.constraints = {"filters": filters}
        s.add(Goal(id=goal_id, workspace_id=body.workspace_id, run_id=run.id, question=body.question, mode=body.mode,
                   data_scope=body.data_scope.model_dump(), requested_outputs=list(body.requested_outputs),
                   playbook=body.playbook, limits=limits, approval_policy=body.approval_policy.model_dump(),
                   complexity=decision.model_dump(mode="json"), request_id=body.request_id, created_by=user.id))
        emit(body.workspace_id, "goal.created", {"goal_id": goal_id, "tier": decision.tier, "floor": decision.floor,
                                                 "playbook": decision.playbook if not body.playbook else playbook,
                                                 "limits": limits, "mode": body.mode, "autonomy_level": run.autonomy_level},
             run_id=run.id, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "goal.created", workspace_id=body.workspace_id, run_id=run.id, target=goal_id,
              details={"tier": decision.tier, "playbook": playbook}, session=s)
        return {"goal_id": goal_id}

    kwargs: dict[str, Any] = {"definition": {"key": playbook, "version": version}} if version else {"playbook": playbook}
    if playbook == "playbook.investigate" and not version:
        kwargs = {}  # the default playbook: no definition lookup (same binding as a v1 run)
    run, replayed = run_svc.start_run_request(user, body.workspace_id, objective=body.question,
                                              source_ids=body.data_scope.source_ids or None, autonomy_level=level,
                                              origin=origin, idempotency=key, limits=limits, on_created=on_created, **kwargs)
    return _goal_out(run.id), replayed


def _max_parallel(workspace_id: str) -> int:
    from analystos.governance.policy import load_policy

    with session_scope() as s:
        return load_policy(s, get_workspace(s, workspace_id)).max_parallel_steps


def _source_slots(s: Any, source_ids: list[str]) -> dict[str, int]:
    """Each source's step slots, fixed for the run: `config.max_concurrent_steps`, else the platform default."""
    from analystos.core.config import get_settings
    from analystos.db.models import Source

    default = get_settings().source_max_concurrent_steps
    out = {}
    for src in s.scalars(select(Source).where(Source.id.in_(source_ids))) if source_ids else []:
        value = (src.config or {}).get("max_concurrent_steps")
        out[src.id] = max(1, int(value)) if isinstance(value, int) and not isinstance(value, bool) else default
    return out


def _goal_out(run_id: str) -> dict:
    from analystos.autonomy.models import Goal

    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.run_id == run_id))
        run = s.get(AnalysisRun, run_id)
        if goal is None or run is None:
            raise NotFound("the goal this Idempotency-Key created no longer exists")
        return {"goal_id": goal.id, "run_id": run.id, "state": run.status, "events_url": f"/v2/runs/{run.id}/events",
                "tier": goal.complexity.get("tier"), "playbook": goal.complexity.get("playbook") if not goal.playbook
                else goal.playbook, "limits": goal.limits}


# ------------------------------------------------------------------------------ run views
def run_view(session: Any, run: AnalysisRun) -> dict:
    from analystos.autonomy import budget
    from analystos.autonomy.models import Goal

    tasks = list(session.scalars(select(RunTask).where(RunTask.run_id == run.id).order_by(RunTask.seq, RunTask.key)))
    goal = session.scalar(select(Goal).where(Goal.run_id == run.id))
    used = budget.usage(run, tasks, session=session) if run.limits else {}
    return {"run_id": run.id, "goal_id": goal.id if goal else None, "workspace_id": run.workspace_id, "state": run.status,
            "control": run.control, "objective": run.objective, "autonomy_level": run.autonomy_level,
            "plan_version": run.plan_version, "plan_hash": run.plan_hash,
            "playbook": (run.capabilities or {}).get("playbook"), "error": run.error,
            "complexity": goal.complexity if goal else None,
            "budget": budget.snapshot(run.limits, used) if run.limits else None,
            "limitations": (run.summary or {}).get("limitations"),
            "steps": [{"key": t.key, "agent": t.agent_id, "title": t.title, "status": t.status, "attempts": t.attempts,
                       "error": t.error} for t in tasks if not t.key.startswith("_")],
            "events_url": f"/v2/runs/{run.id}/events",
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}


def evidence_view(session: Any, run: AnalysisRun) -> dict:
    """Findings with their verification standing, and the statements that produced them."""
    from analystos.api.serialize import with_verification
    from analystos.db.models import Insight, QueryExecution

    insights = list(session.scalars(select(Insight).where(Insight.run_id == run.id).order_by(Insight.code)))
    queries = session.execute(select(QueryExecution.status, func.count(), func.coalesce(func.sum(QueryExecution.row_count), 0))
                              .where(QueryExecution.run_id == run.id).group_by(QueryExecution.status)).all()
    return {"run_id": run.id, "state": run.status, "findings": with_verification(session, insights),
            "verified": sum(1 for i in insights if i.status == "verified"),
            "queries": {st: {"count": n, "rows": int(r)} for st, n, r in queries},
            "data_manifest": run.data_manifest or {}}


# ------------------------------------------------------------------------------ signals
@scoped_loader
def signal(user: User, run_id: str, sig: SignalIn) -> dict:
    """pause | resume | cancel | redirect on a run the caller may operate (analyst); approve_step needs
    approval rights (approver or owner), and `governance/approvals.decide` applies separation of duties."""
    from analystos.services import runs as run_svc

    with session_scope() as s:
        run = run_svc.get_run_for(s, user, run_id, "approver" if sig.type == "approve_step" else "analyst")
        workspace_id = run.workspace_id
    if sig.type in ("pause", "resume", "cancel"):
        run = run_svc.control(user, run_id, sig.type, workspace_id)
        result: dict[str, Any] = {"control": run.control}
    elif sig.type == "redirect":
        result = run_svc.submit_feedback(user, run_id, text=str(sig.payload["text"]), kind="redirect")
    else:
        result = _approve_step(user, run_id, sig.payload)
    emit(workspace_id, "goal.signal", {"type": sig.type, "result": _brief(result)}, run_id=run_id, actor=f"user:{user.id}")
    with session_scope() as s:
        state = s.get(AnalysisRun, run_id).status
    return {"run_id": run_id, "signal": sig.type, "state": state, "result": result}


def _brief(result: Any) -> Any:
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool)) or v is None}
    return None


def _approve_step(user: User, run_id: str, payload: dict[str, Any]) -> dict:
    """Decide the approval a step waits for. The caller must hold approval rights (governance/approvals)."""
    from analystos.governance.approvals import decide
    from analystos.workflows.orchestrator import signal_run

    approve = payload.get("approve", True) is not False
    with session_scope() as s:
        approval_id = payload.get("approval_id")
        if not approval_id:
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == str(payload["step"])))
            approval_id = (task.input or {}).get("approval_id") if task else None
            if approval_id is None and task is not None:  # a gate whose approval its side-effect step carries
                approval_id = s.scalar(select(Approval.id).where(Approval.run_id == run_id, Approval.status == "pending")
                                       .order_by(Approval.created_at.desc()))
        approval = s.get(Approval, approval_id) if approval_id else None
        if approval is None or approval.run_id != run_id:
            raise NotFound("no approval is waiting for that step")
        decided = decide(s, approval.id, s.merge(user), approve=approve, reason=payload.get("reason"))
        out = {"approval_id": decided.id, "status": decided.status}
    signal_run(run_id)
    return out

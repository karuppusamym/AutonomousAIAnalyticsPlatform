"""Autonomy Center reads (S11-T5 over S06): goal runs, queues, exceptions, budgets, schedules, monitors and
alert noise of one workspace, in a bounded number of statements (no per-run query)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from analystos.contracts.events import RUN_TERMINAL
from analystos.core.ids import utcnow
from analystos.db.models import Alert, AnalysisRun, Monitor, RunTask, Schedule

EXCEPTION_STATES = ("FAILED", "COMPLETED_WITH_LIMITATIONS", "EXPIRED")
QUEUE_STATES = ("NEW", "PLANNING", "READY", "RUNNING", "WAITING_USER", "PAUSED")


def _iso(ts: Any) -> str | None:
    return ts.isoformat() if ts else None


def list_goals(session: Any, workspace_id: str, *, limit: int = 50, state: str | None = None) -> list[dict]:
    """Newest goals first, each with its run's state, tier, playbook and budget use (steps, cost, time)."""
    from analystos.autonomy.models import Goal

    q = (select(Goal, AnalysisRun).join(AnalysisRun, AnalysisRun.id == Goal.run_id)
         .where(Goal.workspace_id == workspace_id).order_by(Goal.created_at.desc()).limit(max(1, min(limit, 200))))
    if state:
        q = q.where(AnalysisRun.status == state)
    rows = session.execute(q).all()
    run_ids = [r.id for _, r in rows]
    steps = dict(session.execute(select(RunTask.run_id, func.count()).where(
        RunTask.run_id.in_(run_ids), RunTask.attempts > 0, ~RunTask.key.startswith("_", autoescape=True)).group_by(RunTask.run_id)).all()) \
        if run_ids else {}
    now = utcnow()
    out = []
    for goal, run in rows:
        limits = run.limits or {}
        end = run.finished_at or now
        start = run.started_at or run.created_at
        duration = (end - (start if start.tzinfo else start.replace(tzinfo=end.tzinfo))).total_seconds() if start else 0.0
        used = {"steps": int(steps.get(run.id, 0)), "cost_usd": float(run.cost_usd or 0.0), "duration_s": round(duration, 1),
                "replans": max(0, (run.plan_version or 1) - 1)}
        out.append({"goal_id": goal.id, "run_id": run.id, "question": goal.question, "mode": goal.mode, "state": run.status,
                    "tier": (goal.complexity or {}).get("tier"), "playbook": (run.capabilities or {}).get("playbook")
                    or (goal.complexity or {}).get("playbook"), "plan_version": run.plan_version,
                    "limits": {k: v for k, v in limits.items() if k != "source_slots"}, "used": used,
                    "limitation": ((run.summary or {}).get("limitations") or {}).get("reason"), "error": run.error,
                    "created_at": _iso(goal.created_at), "finished_at": _iso(run.finished_at)})
    return out


def summary(session: Any, workspace_id: str, *, days: int = 30) -> dict:
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.runtime.usage import _month_cost_sql

    since = utcnow() - timedelta(days=days)
    by_state = dict(session.execute(select(AnalysisRun.status, func.count()).where(
        AnalysisRun.workspace_id == workspace_id, AnalysisRun.created_at >= since).group_by(AnalysisRun.status)).all())
    active = dict(session.execute(select(AnalysisRun.status, func.count()).where(
        AnalysisRun.workspace_id == workspace_id, AnalysisRun.status.not_in(sorted(RUN_TERMINAL)))
        .group_by(AnalysisRun.status)).all())
    exceptions = [{"run_id": r.id, "objective": r.objective, "state": r.status, "origin": (r.origin or {}).get("type"),
                   "reason": ((r.summary or {}).get("limitations") or {}).get("reason") or r.error,
                   "finished_at": _iso(r.finished_at)}
                  for r in session.scalars(select(AnalysisRun).where(
                      AnalysisRun.workspace_id == workspace_id, AnalysisRun.created_at >= since,
                      AnalysisRun.status.in_(EXCEPTION_STATES)).order_by(AnalysisRun.created_at.desc()).limit(20))]
    waiting = int(active.get("WAITING_USER", 0))
    schedules = session.execute(select(func.count(), func.count().filter(Schedule.enabled.is_(True)), func.min(
        Schedule.next_run_at).filter(Schedule.enabled.is_(True))).where(Schedule.workspace_id == workspace_id)).one()
    monitors = session.execute(select(func.count(), func.count().filter(Monitor.enabled.is_(True)))
                               .where(Monitor.workspace_id == workspace_id)).one()
    alerts = dict(session.execute(select(Alert.status, func.count()).where(
        Alert.workspace_id == workspace_id, Alert.created_at >= since).group_by(Alert.status)).all())
    raised = sum(alerts.values())
    quiet = session.scalar(select(func.count()).select_from(Alert).where(
        Alert.workspace_id == workspace_id, Alert.created_at >= since, Alert.status == "resolved",
        Alert.investigation_run_id.is_(None))) or 0
    goal_spend = session.scalar(select(func.coalesce(func.sum(AnalysisRun.cost_usd), 0.0)).where(
        AnalysisRun.workspace_id == workspace_id, AnalysisRun.created_at >= since,
        AnalysisRun.origin["type"].as_string() == "goal")) or 0.0
    policy = load_policy(session, get_workspace(session, workspace_id))
    month = _month_cost_sql(session, workspace_id)
    return {"workspace_id": workspace_id, "window_days": days,
            "runs": {"by_state": by_state, "total": sum(by_state.values())},
            "queues": {s: int(active.get(s, 0)) for s in QUEUE_STATES},
            "exceptions": {"count": len(exceptions) + waiting, "waiting_for_people": waiting, "runs": exceptions},
            "budgets": {"month_spend_usd": round(float(month), 4), "month_budget_usd": policy.workspace_monthly_cost_budget_usd,
                        "goal_spend_usd": round(float(goal_spend), 4), "run_cost_budget_usd": policy.run_cost_budget_usd,
                        "max_parallel_steps": policy.max_parallel_steps},
            "schedules": {"total": int(schedules[0]), "enabled": int(schedules[1]), "next_run_at": _iso(schedules[2])},
            "monitors": {"total": int(monitors[0]), "enabled": int(monitors[1])},
            "alerts": {"raised": raised, "by_status": alerts, "resolved_without_investigation": int(quiet),
                       "noise_rate": round(quiet / raised, 3) if raised else None}}

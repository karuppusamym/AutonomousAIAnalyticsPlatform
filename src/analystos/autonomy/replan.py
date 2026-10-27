"""Replan contract (S06 §3.5): `replan(reason_code, detail)` -> plan_version + 1.

* The reason is machine-readable (`contracts.goals.REPLAN_REASONS`); what it resets comes from the
  playbook's `replan_boundary` for that code, else the fallback trigger (`playbook.REASON_TRIGGER`).
* Prior steps and evidence stay immutable: the superseded version is copied to `run_plan_version`
  (plan, hash, every task's status and output) before anything is reset, and superseded findings are
  marked, never deleted.
* Approvals bound to the old plan hash become invalid: the engine invalidates the run's pending and
  approved approvals, and `verify_for_execution` refuses any approval whose plan hash is not the run's.
* `max_replans` (a goal limit) bounds how often a run may replan.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from analystos.contracts.goals import REPLAN_REASONS
from analystos.core.errors import BudgetExceeded, Conflict, InvalidInput
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, RunTask


def check_replan_budget(run: AnalysisRun) -> None:
    cap = (run.limits or {}).get("max_replans")
    if cap is not None and max(0, run.plan_version - 1) >= int(cap):
        raise BudgetExceeded(f"replan budget exhausted: {max(0, run.plan_version - 1)} of {cap} replans used",
                             details={"cap": "max_replans", "used": run.plan_version - 1, "limit": cap})


def record_superseded(session: Any, run: AnalysisRun, tasks: list[RunTask], *, reason_code: str, detail: str | None,
                      actor: str | None) -> None:
    """Copy the plan version being replaced (with what its tasks produced) before the replan changes it."""
    from analystos.autonomy.models import RunPlanVersion

    if session.scalar(select(RunPlanVersion.id).where(RunPlanVersion.run_id == run.id,
                                                      RunPlanVersion.version == run.plan_version)):
        return
    session.add(RunPlanVersion(
        id=new_id("rpv"), workspace_id=run.workspace_id, run_id=run.id, version=run.plan_version, plan=run.plan or {},
        plan_hash=run.plan_hash, reason_code=reason_code, detail=(detail or "")[:2000] or None, actor=actor,
        tasks=[{"key": t.key, "agent": t.agent_id, "status": t.status, "attempts": t.attempts, "plan_version": t.plan_version,
                "error": t.error, "output": t.output or {}} for t in sorted(tasks, key=lambda t: (t.seq, t.key))]))


def replan(run_id: str, reason_code: str, detail: str, *, actor: str) -> dict[str, Any]:
    """Typed replan of a live run. The caller has authorized the actor on the run."""
    from analystos.contracts.events import RUN_TERMINAL
    from analystos.runtime.engine import apply_replan

    if reason_code not in REPLAN_REASONS:
        raise InvalidInput(f"reason_code must be one of {', '.join(REPLAN_REASONS)}")
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run is None:
            raise InvalidInput("unknown run")
        if run.status in RUN_TERMINAL:
            raise Conflict(f"run is {run.status}")
        if run.plan_version < 1:
            raise Conflict("the run has no plan yet")
        return apply_replan(s, run, detail, reason_code=reason_code, actor=actor)


def plan_version(session: Any, run: AnalysisRun, version: int | None) -> dict[str, Any]:
    """The plan of `version` (default: current) with its tasks, from the live rows or the snapshot."""
    from analystos.autonomy.models import RunPlanVersion

    if version is None or version == run.plan_version:
        tasks = session.scalars(select(RunTask).where(RunTask.run_id == run.id).order_by(RunTask.seq, RunTask.key))
        return {"run_id": run.id, "version": run.plan_version, "current": True, "plan": run.plan, "plan_hash": run.plan_hash,
                "tasks": [{"key": t.key, "agent": t.agent_id, "status": t.status, "attempts": t.attempts,
                           "depends_on": t.depends_on, "error": t.error} for t in tasks if not t.key.startswith("_")]}
    row = session.scalar(select(RunPlanVersion).where(RunPlanVersion.run_id == run.id, RunPlanVersion.version == version))
    if row is None:
        from analystos.core.errors import NotFound

        raise NotFound(f"plan version {version} of this run does not exist")
    return {"run_id": run.id, "version": row.version, "current": False, "plan": row.plan, "plan_hash": row.plan_hash,
            "superseded_by": {"reason_code": row.reason_code, "detail": row.detail, "actor": row.actor,
                              "at": row.superseded_at.isoformat() if row.superseded_at else None},
            "tasks": [{k: v for k, v in t.items() if k != "output"} | {"output_keys": sorted((t.get("output") or {}).keys())}
                      for t in row.tasks or [] if not str(t.get("key", "")).startswith("_")]}

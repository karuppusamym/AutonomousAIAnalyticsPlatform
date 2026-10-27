"""Orchestrator-agnostic run engine. Temporal (production) and the local runner (tests, laptops)
drive the same four operations: plan_run, get_state, execute_task, finish_run. All state is in
Postgres; each task has a stable key (idempotency) and a plan_version (stale results are dropped).

The engine knows no step keys: approval gates, side effects, skips, dynamic expansions and what a
replan resets are read from the run's bound playbook (capabilities/playbook.py, P4-X02)."""
from __future__ import annotations

import traceback
from datetime import UTC
from typing import Any

from sqlalchemy import func, select, update

from analystos.artifacts.registry import producing_plan
from analystos.autonomy import budget
from analystos.capabilities.binding import bind_run, bound_refs, run_playbook
from analystos.capabilities.playbook import Step, evaluate
from analystos.contracts.events import RUN_TERMINAL
from analystos.core.errors import AnalystOSError, RunCancelled
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger, run_id_var
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Hypothesis, Insight, RunTask, Workspace
from analystos.events.bus import emit
from analystos.governance.approvals import invalidate_run_approvals
from analystos.runtime.context import RunContext, Services, default_services
from analystos.runtime.plan import dep_satisfied, plan_hash
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
CLAIM_TTL_SECONDS = 30 * 60  # equals the Temporal start_to_close timeout for execute_task
NON_RETRYABLE = ("Forbidden", "PolicyDenied", "SQLRejected", "ApprovalRequired", "InvalidInput", "BudgetExceeded",
                 "RunCancelled", "NotFound", "Conflict")


def set_run_status(session, run: AnalysisRun, status: str, **extra: Any) -> None:
    if run.status == status and not extra:
        return
    run.status = status
    for k, v in extra.items():
        setattr(run, k, v)
    if status in RUN_TERMINAL and run.finished_at is None:
        run.finished_at = utcnow()
    emit(run.workspace_id, "run.status", {"status": status, **{k: v for k, v in extra.items() if k == "error"}},
         run_id=run.id, session=session)


def add_task(session, run: AnalysisRun, *, key: str, agent: str, title: str, depends_on: list[str],
             optional: bool = False, input: dict | None = None, seq: int = 0, from_version: int | None = None) -> RunTask:
    """Add a task to the run's current plan. `from_version` is the plan version of the task doing the
    adding; a task running under a superseded plan may not extend the new one."""
    if from_version is not None and from_version != run.plan_version:
        raise RunCancelled("plan changed while this task was running; not extending the new plan")
    existing = session.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == key))
    if existing:
        return existing
    task = RunTask(id=new_id("tsk"), run_id=run.id, key=key, agent_id=agent, title=title, depends_on=depends_on,
                   input={**(input or {}), "optional": optional}, plan_version=run.plan_version, seq=seq, status="NEW")
    session.add(task)
    return task


def run_hash(run: AnalysisRun) -> str:
    return plan_hash(run.plan, constraints=run.constraints, scope_hash=run.scope.get("hash", ""),
                     plan_version=run.plan_version, capabilities=bound_refs(run))


def materialize_plan(session, run: AnalysisRun) -> None:
    playbook = run_playbook(run)
    ns = {"run": {"autonomy_level": run.autonomy_level, "origin": run.origin or {}}}
    skipped = (run.capabilities or {}).get("skipped") or {}
    for i, step in enumerate(run.plan["steps"]):
        task = add_task(session, run, key=step["key"], agent=step["agent"], title=step["title"], depends_on=step["depends_on"],
                        optional=step.get("optional", False), seq=i * 10)
        spec = playbook.step(step["key"])
        if spec is not None and spec.skip_when and evaluate(spec.skip_when.if_, ns):
            # e.g. scheduled re-analyses and alert investigations refresh findings and reports; they do
            # not propose publication unless the schedule asks for it.
            task.status, task.error = "SKIPPED", spec.skip_when.reason
        elif step["key"] in skipped:  # an optional step whose capability is disabled or not certified here
            task.status, task.error = "SKIPPED", skipped[step["key"]]
    run.plan_hash = run_hash(run)


def _workspace_disabled(s, workspace_id: str) -> bool:
    """A disabled or deleted workspace stops its runs. A missing row (never in Postgres, where the
    foreign key holds; possible in unit fixtures) is not treated as disabled."""
    ws = s.get(Workspace, workspace_id)
    return ws is not None and (ws.status != "active" or ws.deleted_at is not None)


@owned_by("analysis_run", "run_id")
def plan_run(run_id: str, services: Services | None = None) -> dict:
    """Activity 1: bind the playbook and its capabilities, the supervisor frames the plan (idempotent).
    A binding failure (a required step's capability disabled, deprecated or not certified for an
    autonomous run) fails the run with the reason instead of starting it."""
    from analystos.agents.supervisor import build_plan

    services = services or default_services()
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        if run.plan_version > 0 or run.status in RUN_TERMINAL:
            return {"plan_version": run.plan_version}
        if _workspace_disabled(s, run.workspace_id):
            run.control = "cancel"
            set_run_status(s, run, "CANCELLED", error="workspace disabled")
            return {"plan_version": 0, "cancelled": "workspace disabled"}
        set_run_status(s, run, "PLANNING", started_at=utcnow())
        try:
            binding, failure = bind_run(s, run), None
        except AnalystOSError as exc:
            failure = f"{exc.code}: {exc.message}"
    if failure:
        finish_run(run_id, "FAILED", failure)  # schedules and alerts hear about it like any failed run
        return {"plan_version": 0, "failed": failure}
    plan = build_plan(run_id, services, binding.playbook)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run.plan_version > 0:
            return {"plan_version": run.plan_version}
        run.plan = plan
        run.capabilities = binding.to_json()
        run.plan_version = 1
        materialize_plan(s, run)
        set_run_status(s, run, "READY")
        emit(run.workspace_id, "run.replanned", {"plan_version": 1, "plan_hash": run.plan_hash, "steps": len(plan["steps"]),
                                                 "playbook": binding.playbook.ref, "capabilities": len(binding.refs),
                                                 "skipped": binding.skipped}, run_id=run.id, session=s)
        for st in plan["steps"]:
            spec = binding.playbook.step(st["key"])
            if spec is None or spec.type != "approval_gate" or spec.payload != "plan":
                continue
            _request_plan_approval(s, run, s.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == st["key"])))
    return {"plan_version": 1}


def _request_plan_approval(s, run: AnalysisRun, task: RunTask) -> None:
    """A plan approval bound to the run's current plan hash, handed to the gate task that waits for it."""
    from analystos.governance.approvals import request_approval

    approval = request_approval(s, workspace_id=run.workspace_id, run_id=run.id, action="execute_plan",
                                payload={"plan": run.plan, "constraints": run.constraints}, plan_hash=run.plan_hash,
                                policy_version=run.policy_version, requested_by=run.requested_by, risk_tier="medium",
                                destination=None, affected_assets=run.scope.get("assets", []))
    task.input = {**task.input, "approval_id": approval.id}


def _approval_gate(session, task: RunTask, step: Step | None) -> str | None:
    """For approval-gated tasks: 'ready' | 'waiting' | 'skip'."""
    approval_id = task.input.get("approval_id")
    if step is None or not step.waits_for_approval or not approval_id:
        return None
    approval = session.get(Approval, approval_id)
    if approval is None:
        return "skip"
    if approval.status in ("approved", "executed"):
        return "ready"
    if approval.status == "pending":
        return "waiting"
    return "skip"


class _NeedsLock(Exception):
    """Raised by an unlocked `get_state` pass that reached a state change."""


@owned_by("analysis_run", "run_id")
def get_state(run_id: str) -> dict:
    """Activity: what should the orchestrator do next?

    The loop polls often and nearly every poll changes nothing, so the run and its tasks are read
    without locks (P4-S02). A poll that has to change state (pause, a run status, parking or skipping
    a task) takes the run's row lock and decides again on fresh rows, which serializes it with
    replans, controls and finish_run as before."""
    with session_scope() as s:
        try:
            run, disabled = _run_and_workspace(s, run_id, lock=False)
            return _decide(s, run, locked=False, workspace_disabled=disabled)
        except _NeedsLock:
            s.expire_all()
            run, disabled = _run_and_workspace(s, run_id, lock=True)
            return _decide(s, run, locked=True, workspace_disabled=disabled)


def _run_and_workspace(s, run_id: str, *, lock: bool) -> tuple[AnalysisRun, bool]:
    """The run and whether its workspace is disabled, in one statement (the loop's query budget,
    P4-S02). A missing workspace row (unit fixtures) is not disabled."""
    q = (select(AnalysisRun, Workspace.status, Workspace.deleted_at)
         .outerjoin(Workspace, Workspace.id == AnalysisRun.workspace_id).where(AnalysisRun.id == run_id))
    if lock:
        q = q.with_for_update(of=AnalysisRun)
    row = s.execute(q).one_or_none()
    if row is None:
        return None, False  # type: ignore[return-value]
    run, status, deleted_at = row
    return run, status is not None and (status != "active" or deleted_at is not None)


def _decide(s, run: AnalysisRun, *, locked: bool, workspace_disabled: bool = False) -> dict:
    def change() -> None:
        if not locked:
            raise _NeedsLock

    run_id = run.id
    if run.status in RUN_TERMINAL:
        return {"terminal": True, "status": run.status}
    if workspace_disabled:
        return {"control": "cancel"}
    if run.control == "cancel":
        return {"control": "cancel"}
    if run.control == "pause":
        if run.status != "PAUSED":
            change()
            set_run_status(s, run, "PAUSED")
        return {"control": "pause"}
    tasks = {t.key: t for t in s.scalars(select(RunTask).where(RunTask.run_id == run_id))}
    if not tasks:
        return {"needs_plan": True}
    approval_ids = [t.input["approval_id"] for t in tasks.values()
                    if t.status in ("NEW", "WAITING_USER") and t.input.get("approval_id")]
    if len(approval_ids) > 1:  # one read, not one per gated task: _approval_gate then hits the identity map
        list(s.scalars(select(Approval).where(Approval.id.in_(approval_ids))))
    playbook = run_playbook(run)
    ready, waiting, running = [], [], []
    for key, task in sorted(tasks.items(), key=lambda kv: (kv[1].seq, kv[0])):
        if task.status == "RUNNING":
            running.append(key)
            continue
        if task.status not in ("NEW", "WAITING_USER"):
            continue
        if task.status == "WAITING_USER" and (task.output or {}).get("meaning_questions"):
            waiting.append(key)
            continue
        if not all(dep_satisfied(d, tasks, key) for d in task.depends_on):
            continue
        step = playbook.step(key)
        gate = _approval_gate(s, task, step)
        if step is not None and step.type == "side_effect" and not task.input.get("approval_id") and task.status == "NEW":
            # its approval_gate finished without creating an approval (nothing publishable or denied)
            change()
            task.status, task.finished_at = "SKIPPED", utcnow()
            continue
        if gate == "waiting":
            if task.status != "WAITING_USER":
                change()
                task.status = "WAITING_USER"
            waiting.append(key)
        elif gate == "skip":
            change()
            task.status, task.finished_at, task.error = "SKIPPED", utcnow(), "approval rejected, expired or invalidated"
            emit(run.workspace_id, "task.updated", {"task": key, "status": "SKIPPED"}, run_id=run_id, session=s)
        else:
            ready.append(key)
    limits = run.limits or {}
    refused = [k for k, t in tasks.items() if limits and t.status == "FAILED" and budget.is_budget_failure(t.error)]
    failed_required = [k for k, t in tasks.items() if t.status == "FAILED" and not t.input.get("optional") and k not in refused]
    if failed_required:
        # the task's own error carries the cause and remedy (e.g. a policy refusal); the run shows it
        causes = "; ".join(f"{k}: {tasks[k].error}" for k in failed_required if tasks[k].error)
        return {"fail": f"required task(s) failed: {', '.join(failed_required)}" + (f" — {causes}" if causes else "")}
    done = all(t.status in ("COMPLETED", "SKIPPED", "FAILED", "CANCELLED", "INVALIDATED") for t in tasks.values())
    if done and not refused:
        return {"done": True}
    if limits:  # a goal run's budget counters (S06 §3.4): no extra statement for runs without limits
        return _decide_within_budget(s, run, tasks, limits, refused, ready, waiting, running, change)
    if ready:
        if run.status not in ("RUNNING",):
            change()
            set_run_status(s, run, "RUNNING")
        return {"ready": ready[:4]}
    if waiting and not running:
        if run.status != "WAITING_USER":
            change()
            set_run_status(s, run, "WAITING_USER")
        return {"waiting_user": waiting}
    if running:
        return {"running": running}
    return {"fail": "no runnable task (dependency deadlock)"}


def _decide_within_budget(s, run: AnalysisRun, tasks: dict, limits: dict, refused: list[str], ready: list[str],
                          waiting: list[str], running: list[str], change) -> dict:
    """`_decide` for a run with goal limits: dispatch no more than the step budget allows; once a
    counter is used up (or a step was refused by a budget), let running steps finish, then stop."""
    used = budget.usage(run, tasks.values(), session=s)
    snap = budget.snapshot(limits, used)
    stops = budget.stops(limits, used) + [f"{k}: {tasks[k].error}" for k in refused]
    steps_left = None if limits.get("max_steps") is None else int(limits["max_steps"] - used["steps"])
    if steps_left is not None and steps_left <= 0 and (ready or waiting) and not stops:
        stops.append(f"max_steps ({used['steps']:g} of {float(limits['max_steps']):g})")
    if stops:
        if running:
            return {"running": running, "budget": snap, "draining": stops}
        expired = bool(waiting) and not ready and any(x.startswith("max_duration_s") for x in stops)
        return {"limited": "; ".join(stops), "expired": expired, "budget": snap}
    if ready and steps_left is not None:
        ready = ready[:max(steps_left, 0)]
    wanted = list(ready)
    ready = _within_parallel(run, ready, running, limits)
    if wanted and not ready and not running:  # every ready step waits for a source slot another run holds
        return {"throttled": wanted, "budget": snap}
    if ready:
        if run.status != "RUNNING":
            change()
            set_run_status(s, run, "RUNNING")
        return {"ready": ready[:4], "budget": snap}
    if waiting and not running:
        if run.status != "WAITING_USER":
            change()
            set_run_status(s, run, "WAITING_USER")
        return {"waiting_user": waiting, "budget": snap}
    if running:
        return {"running": running, "budget": snap}
    return {"fail": "no runnable task (dependency deadlock)"}


DEFAULT_PARALLEL = 4


def _within_parallel(run: AnalysisRun, ready: list[str], running: list[str], limits: dict) -> list[str]:
    """Parallel branches (S06 §3.6): at most `max_parallel` steps at once, and a step that queries a
    source only while that source has a free slot (`limits.source_slots`, shared by every run)."""
    from analystos.runtime import source_slots as ss

    room = int(limits.get("max_parallel") or DEFAULT_PARALLEL) - len(running)
    slots = ss.source_limits(run)
    free = {src: lim - ss.default_slots().holders(src) for src, lim in slots.items()
            if src in ss.run_sources(run)} if slots else {}
    out = []
    for key in ready:
        if len(out) >= room:
            break
        if free and ss.queries_sources(run, key):
            if any(n <= 0 for n in free.values()):
                continue
            free = {src: n - 1 for src, n in free.items()}
        out.append(key)
    return out


def _acquire_source_slots(run_id: str, key: str, claim: int) -> list[str] | None:
    """Take a slot on every limited source the step queries; None (and the claim handed back) when one
    is full. Returns the sources held, released when the step ends."""
    from analystos.runtime import source_slots as ss

    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        slots = ss.source_limits(run)
        if not slots or not ss.queries_sources(run, key):
            return []
        wanted = [src for src in ss.run_sources(run) if src in slots]
    held: list[str] = []
    for src in wanted:
        if not ss.default_slots().acquire(src, ss.holder(run_id, key), slots[src], CLAIM_TTL_SECONDS):
            for h in held:
                ss.default_slots().release(h, ss.holder(run_id, key))
            with session_scope() as s:  # back to NEW under the same claim: the next poll dispatches it again
                s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.key == key, RunTask.claim_version == claim)
                          .values(status="NEW", started_at=None, attempts=RunTask.attempts - 1,
                                  error=f"waiting for a slot on source {src}"))
            return None
        held.append(src)
    return held


def _release_source_slots(run_id: str, key: str, held: list[str]) -> None:
    from analystos.runtime import source_slots as ss

    for src in held:
        ss.default_slots().release(src, ss.holder(run_id, key))


def _claim(s, run_id: str, key: str) -> dict:
    """Optimistic claim (P4-S02): read the task unlocked, then compare-and-set on the claim version,
    status and plan version it was read with. Of two concurrent claimers exactly one updates the row;
    the other reports `in_progress`. A RUNNING claim younger than CLAIM_TTL_SECONDS belongs to a live
    worker; an older one (or one released by `release_timed_out_claim`) is retaken."""
    row = s.execute(select(RunTask.id, RunTask.status, RunTask.started_at, RunTask.attempts, RunTask.plan_version,
                           RunTask.claim_version, RunTask.agent_id, AnalysisRun.workspace_id, AnalysisRun.limits,
                           Workspace.status.label("workspace_status"),
                           Workspace.deleted_at.label("workspace_deleted_at"))
                    .join(AnalysisRun, AnalysisRun.id == RunTask.run_id)
                    .outerjoin(Workspace, Workspace.id == AnalysisRun.workspace_id)
                    .where(RunTask.run_id == run_id, RunTask.key == key)).one_or_none()
    if row is None:
        return {"status": "missing"}
    if row.workspace_status is not None and (row.workspace_status != "active" or row.workspace_deleted_at is not None):
        return {"status": "CANCELLED", "error": "workspace disabled"}
    if row.status in ("COMPLETED", "SKIPPED"):
        return {"status": row.status, "cached": True}
    if row.status == "RUNNING" and row.started_at and _age_seconds(row.started_at) < CLAIM_TTL_SECONDS:
        return {"status": "in_progress"}
    claim = row.claim_version + 1
    won = s.execute(update(RunTask).where(RunTask.id == row.id, RunTask.claim_version == row.claim_version,
                                          RunTask.status == row.status, RunTask.plan_version == row.plan_version)
                    .values(status="RUNNING", attempts=RunTask.attempts + 1, started_at=utcnow(), error=None,
                            claim_version=claim)
                    .execution_options(synchronize_session=False)).rowcount
    if not won:
        return {"status": "in_progress"}  # another claimer moved the row first
    emit(row.workspace_id, "agent.started", {"task": key, "agent": row.agent_id, "attempt": row.attempts + 1},
         run_id=run_id, session=s)
    return {"claim": claim, "plan_version": row.plan_version, "slotted": bool((row.limits or {}).get("source_slots"))}


@owned_by("analysis_run", "run_id")
def execute_task(run_id: str, key: str, services: Services | None = None) -> dict:
    """Activity: run one task. Idempotent by (run_id, key, plan_version)."""
    from analystos.agents.dispatch import dispatch

    services = services or default_services()
    token = run_id_var.set(run_id)
    held: list[str] | None = []
    try:
        with session_scope() as s:
            claimed = _claim(s, run_id, key)
        if "claim" not in claimed:
            return claimed
        claim, version = claimed["claim"], claimed["plan_version"]
        held = _acquire_source_slots(run_id, key, claim) if claimed.get("slotted") else []
        if held is None:
            return {"status": "throttled"}
        plan_token = producing_plan.set((run_id, version))
        try:
            ctx = RunContext.load(run_id, key, services)
            output = dispatch(ctx) or {}
            status, error = "COMPLETED", None
        except RunCancelled as exc:
            output, status, error = {}, "CANCELLED", str(exc)
        except AnalystOSError as exc:
            from analystos.core.errors import MeaningRequired

            if isinstance(exc, MeaningRequired):
                output, status, error = {"meaning_questions": exc.details.get("questions", []),
                                         "grounding": exc.details.get("grounding", {})}, "WAITING_USER", exc.message
            else:
                output, status, error = {}, "FAILED", f"{exc.code}: {exc.message}"
            log.warning("task %s failed: %s", key, error)
        except Exception as exc:  # unexpected: keep the trace for the agent console
            output, status, error = {}, "FAILED", f"{type(exc).__name__}: {exc}"
            log.error("task %s crashed\n%s", key, traceback.format_exc())
            if not _is_last_attempt(run_id, key):
                _reset(run_id, key, error, claim)
                raise
        finally:
            producing_plan.reset(plan_token)
        with session_scope() as s:
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key).with_for_update())
            run, ws_disabled = _run_and_workspace(s, run_id, lock=False)  # one statement, as before (P4-S02)
            if task is None:
                return {"status": "discarded"}
            if task.claim_version != claim:
                # a later attempt retook the claim (this one timed out or was released); its result stands
                return {"status": "superseded"}
            if ws_disabled:
                task.status, task.output, task.error, task.finished_at = "CANCELLED", {}, "workspace disabled", utcnow()
                return {"status": "CANCELLED", "error": "workspace disabled"}
            if run.plan_version != version or task.plan_version != version:
                # replanned while running: discard. Dynamic tasks of the old plan are removed; base
                # tasks run again under the new plan version.
                if run_playbook(run).is_dynamic(key):
                    s.delete(task)
                else:
                    task.status, task.output, task.error = "NEW", {}, "discarded: plan changed while running"
                return {"status": "discarded"}
            if status == "CANCELLED" and run.control != "cancel":
                task.status, task.error = "NEW", None
                return {"status": "requeued"}
            task.status, task.output, task.error, task.finished_at = status, output, error, utcnow()
            emit(run.workspace_id, "task.updated" if status == "WAITING_USER" else "agent.completed" if status == "COMPLETED" else "agent.failed",
                 {"task": key, "agent": task.agent_id, "status": status, "error": error}, run_id=run_id, session=s)
            if run.limits:  # remaining budget for the Autonomy Center, after every step of a goal run
                used = budget.usage(run, s.scalars(select(RunTask).where(RunTask.run_id == run_id)), session=s)
                emit(run.workspace_id, "run.budget", {"task": key, **budget.snapshot(run.limits, used)}, run_id=run_id,
                     session=s)
        return {"status": status, "error": error}
    finally:
        if held:
            _release_source_slots(run_id, key, held)
        run_id_var.reset(token)


def _age_seconds(ts) -> float:
    """Seconds since `ts`; a naive timestamp (a store without time zones) is read as UTC."""
    return (utcnow() - (ts if ts.tzinfo else ts.replace(tzinfo=UTC))).total_seconds()


def _is_last_attempt(run_id: str, key: str, max_attempts: int = 3) -> bool:
    """No retry after `max_attempts`, nor once a goal run has used its `max_retries` (S06 §3.4)."""
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))
        if task is None or task.attempts >= max_attempts:
            return True
        cap = (s.scalar(select(AnalysisRun.limits).where(AnalysisRun.id == run_id)) or {}).get("max_retries")
        if cap is None:
            return False
        retries = s.scalar(select(func.coalesce(func.sum(RunTask.attempts - 1), 0)).where(
            RunTask.run_id == run_id, RunTask.attempts > 1)) or 0
        return retries >= cap


def _reset(run_id: str, key: str, error: str, claim: int) -> None:
    """Hand a crashed attempt's task back for the retry, unless a later attempt already holds it."""
    with session_scope() as s:
        s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.key == key, RunTask.claim_version == claim)
                  .values(status="NEW", error=error))


@platform_loop
def nonterminal_runs() -> list[str]:
    """Runs a local orchestrator should be driving (oldest first)."""
    with session_scope() as s:
        return list(s.scalars(select(AnalysisRun.id).where(AnalysisRun.status.not_in(sorted(RUN_TERMINAL)))
                              .order_by(AnalysisRun.created_at)))


@platform_loop
def release_orphaned_claims(run_ids: list[str] | None = None) -> list[str]:
    """Local orchestrator restart (ADR-0025): the process that held these RUNNING claims is gone, so
    each goes back to NEW under a new claim version (a late result of the old attempt, should one ever
    land, is `superseded`). Returns the non-terminal runs to re-drive. Only the local orchestrator calls
    this, once per start of the single process that drives local runs; Temporal re-dispatches its own.
    `run_ids` narrows it to those runs (still only the non-terminal ones)."""
    live = nonterminal_runs()
    run_ids = live if run_ids is None else [r for r in live if r in set(run_ids)]
    if not run_ids:
        return []
    with session_scope() as s:
        released = s.execute(update(RunTask).where(RunTask.run_id.in_(run_ids), RunTask.status == "RUNNING")
                             .values(status="NEW", started_at=None, claim_version=RunTask.claim_version + 1,
                                     error="claim released: the local orchestrator restarted while this task ran")
                             .execution_options(synchronize_session=False)).rowcount
    if released:
        log.warning("released %d task claim(s) held by a previous local orchestrator process", released)
    return run_ids


@owned_by("analysis_run", "run_id")
def finish_run(run_id: str, outcome: str, error: str | None = None) -> None:
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run.status in RUN_TERMINAL:
            return
        notify_needed = False
        if outcome == "CANCELLED":
            s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.status.in_(["NEW", "READY", "WAITING_USER"]))
                      .values(status="CANCELLED"))
            invalidate_run_approvals(s, run_id, "run cancelled")
            side_effects = [k for k, st in run_playbook(run).steps.items() if st.type == "side_effect"]
            published = bool(side_effects) and s.scalar(select(func.count()).select_from(RunTask).where(
                RunTask.run_id == run_id, RunTask.key.in_(side_effects), RunTask.status == "COMPLETED")) > 0
            run.summary = {**(run.summary or {}), "cancel_outcome": "cancelled_after_publication" if published else "cancelled_before_side_effects"}
        if outcome in LIMITED:
            _close_with_limitations(s, run, outcome, error or "budget exhausted")
        set_run_status(s, run, outcome, **({"error": error} if error else {}))
        notify_needed = (run.origin or {}).get("type") in ("schedule", "alert")
        workspace_id, requested_by = run.workspace_id, run.requested_by
    if notify_needed:
        from analystos.services.schedules import complete_from_run

        complete_from_run(run_id)
    if outcome == "COMPLETED":  # last: the schedule's result must not wait on (or race with) recording the steps
        from analystos.services.steps import record_quietly

        record_quietly("run", workspace_id, run_id, requested_by)


LIMITED = ("COMPLETED_WITH_LIMITATIONS", "EXPIRED")


def _close_with_limitations(s, run: AnalysisRun, outcome: str, reason: str) -> None:
    """A budget ran out (or the wall clock while the run waited for a person): work not started is
    skipped, approvals not yet acted on are invalidated (nothing is published after the stop), and
    the summary says what stands. Only verified findings are results; the rest stay drafts (AT-04)."""
    s.execute(update(RunTask).where(RunTask.run_id == run.id, RunTask.status.in_(["NEW", "READY", "WAITING_USER"]))
              .values(status="SKIPPED", error=f"not run: {reason}", finished_at=utcnow())
              .execution_options(synchronize_session=False))
    invalidate_run_approvals(s, run.id, f"run ended {outcome}: {reason}")
    tasks = list(s.scalars(select(RunTask).where(RunTask.run_id == run.id).execution_options(populate_existing=True)))
    insights = list(s.execute(select(Insight.id, Insight.status).where(Insight.run_id == run.id)))
    used = budget.usage(run, tasks, session=s) if run.limits else {}
    limitations = {"outcome": outcome, "reason": reason, "limits": dict(run.limits or {}), "used": used,
                   "completed_steps": sorted(t.key for t in tasks if t.status == "COMPLETED" and not t.key.startswith("_")),
                   "not_run": sorted(t.key for t in tasks if t.status == "SKIPPED" and (t.error or "").startswith("not run:")),
                   "verified_findings": sorted(i.id for i in insights if i.status == "verified"),
                   "unverified_findings": sum(1 for i in insights if i.status not in ("verified", "superseded"))}
    run.summary = {**(run.summary or {}), "limitations": limitations}
    emit(run.workspace_id, "run.limited", limitations, run_id=run.id, actor="engine", session=s)


def apply_replan(session, run: AnalysisRun, reason: str, *, full: bool = True, reason_code: str | None = None,
                 actor: str | None = None) -> dict:
    """Dynamic replanning (§40): persist, mark impacted, cancel invalid pending work, reuse valid artifacts.

    What is reset comes from the playbook's `replan_boundary` steps:
    full=True  (redirect / deeper analysis): the `redirect` boundary (investigate.v1: hypotheses onward);
               context, metadata, profile and quality results are reused.
    full=False (finding rejected / metric edited): the `finding_rejected` boundary (investigate.v1:
               dataset onward).
    """
    from analystos.autonomy.replan import check_replan_budget, record_superseded

    playbook = run_playbook(run)
    if reason_code is None:  # v1 callers (feedback): the redirect / finding-rejected boundaries
        trigger: str | None = "redirect" if full else "finding_rejected"
        reason_code = "user_redirect" if full else "rejected_hypothesis"
    else:
        trigger = playbook.trigger_for(reason_code)
        full = trigger is not None and trigger != "finding_rejected"  # a re-plan of the analysis supersedes its findings
    check_replan_budget(run)
    reset_keys = playbook.reset_keys(trigger) if trigger else set()
    removed_prefixes = playbook.removed_prefixes(trigger) if trigger else ()
    tasks = list(session.scalars(select(RunTask).where(RunTask.run_id == run.id)))
    record_superseded(session, run, tasks, reason_code=reason_code, detail=reason, actor=actor)
    run.plan_version += 1
    removed, reset, dropped = 0, 0, 0
    for task in tasks:
        if trigger is None and task.status == "NEW" and task.input.get("optional"):
            # budget_downgrade: optional work that has not started is dropped; nothing is redone
            task.status, task.error, task.finished_at = "SKIPPED", f"dropped by replan: {reason_code}", utcnow()
            task.plan_version = run.plan_version
            dropped += 1
        elif removed_prefixes and task.key.startswith(removed_prefixes):
            session.delete(task)
            removed += 1
        elif task.key in reset_keys:
            task.status, task.output, task.error, task.plan_version = "NEW", {}, None, run.plan_version
            task.started_at = task.finished_at = None
            reset += 1
        else:
            task.plan_version = run.plan_version  # still valid (context, metadata, profile, quality): reused
    if full:
        session.execute(update(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status != "superseded")
                        .values(status="superseded"))
        session.execute(update(Insight).where(Insight.run_id == run.id).values(status="superseded"))
        from analystos.evidence.verification import supersede_subjects

        supersede_subjects(session, "insight", session.scalars(select(Insight.id).where(Insight.run_id == run.id)),
                           f"replanned: {reason}")
    invalidated = invalidate_run_approvals(session, run.id, f"replanned: {reason}")
    run.plan_hash = run_hash(run)
    for task in tasks:  # a plan gate still waiting asks again, for the new plan: an invalidated approval never opens it
        spec = playbook.step(task.key)
        if spec is not None and spec.type == "approval_gate" and spec.payload == "plan" and task.status in ("NEW", "WAITING_USER")                 and task in session:
            _request_plan_approval(session, run, task)
    if run.status in ("WAITING_USER", "COMPLETED", "PAUSED"):
        run.status = "RUNNING"
        run.finished_at = None
    emit(run.workspace_id, "run.replanned", {"plan_version": run.plan_version, "plan_hash": run.plan_hash,
                                             "reason": reason, "reason_code": reason_code, "trigger": trigger,
                                             "tasks_reset": reset, "tasks_removed": removed, "tasks_dropped": dropped,
                                             "approvals_invalidated": invalidated}, run_id=run.id, actor=actor, session=session)
    return {"plan_version": run.plan_version, "reason_code": reason_code, "tasks_reset": reset, "tasks_removed": removed,
            "tasks_dropped": dropped, "approvals_invalidated": invalidated}


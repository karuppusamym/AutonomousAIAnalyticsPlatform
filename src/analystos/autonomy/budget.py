"""Run budgets (S06 §3.4): counters a goal run may not exceed, read by the orchestrator loop.

`get_state` (the activity both orchestrators call every iteration) evaluates them, so Temporal and
the local runner enforce the same limits: it stops dispatching when a counter is used up, waits for
running steps, and then ends the run `COMPLETED_WITH_LIMITATIONS` (or `EXPIRED` when the wall clock
ran out while it waited for a person). Validated partial results stand; nothing unverified is
promoted.

Where each counter comes from (no extra statement for runs without limits; the loop's query budget
is pinned by tests/integration/test_engine_queries.py):

  steps     tasks claimed at least once (from the task rows `get_state` already read)
  retries   attempts beyond the first, summed over tasks (same rows)
  replans   plan_version - 1
  duration  now - started_at (the workflow also caps its wait timer at what is left)
  cost_usd  the run's USD counter (runtime/budget_counters.py; the router spends into it), seeded
            from analysis_run.cost_usd
  scan      the run's `scan_bytes` counter: `record_scan` (the gateway's estimate, then its actual)
  rows      SUM(query_execution.row_count) — one statement, only when the goal limits rows

LLM spend is also capped at the call: `runtime/usage.py` tightens the per-run policy cap by
`max_cost_usd`, so a model call that would start past the limit is refused (BudgetExceeded), and the
loop reads that refusal as a budget stop, not a failure.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from sqlalchemy import func, select

from analystos.contracts.goals import LIMIT_KEYS
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger

log = get_logger(__name__)

# limit -> the usage counter it bounds
COUNTER_OF = {"max_steps": "steps", "max_duration_s": "duration_s", "max_scan_bytes": "scan_bytes",
              "max_cost_usd": "cost_usd", "max_replans": "replans", "max_rows": "rows", "max_retries": "retries"}
BUDGET_ERROR = "budget_exceeded"


def _seconds_since(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return 0.0
    return max(0.0, (now - (ts if ts.tzinfo else ts.replace(tzinfo=UTC))).total_seconds())


def usage(run: Any, tasks: Any, *, session: Any = None, now: datetime | None = None) -> dict[str, float]:
    """What the run has used of each counter. `tasks` are its RunTask rows (any iterable)."""
    from analystos.runtime.budget_counters import RUN_TTL_SECONDS, default_budget_counters

    limits = run.limits or {}
    work = [t for t in tasks if not t.key.startswith("_")]
    out: dict[str, float] = {
        "steps": sum(1 for t in work if (t.attempts or 0) > 0),
        "retries": sum(max(0, (t.attempts or 0) - 1) for t in work),
        "replans": max(0, (run.plan_version or 1) - 1),
        "duration_s": round(_seconds_since(run.started_at or run.created_at, now or utcnow()), 1),
        "cost_usd": float(run.cost_usd or 0.0),
        "scan_bytes": 0.0,
        "rows": 0.0,
    }
    counters = default_budget_counters()
    usd = counters.read(counters.run_key(run.id, "usd"), lambda: float(run.cost_usd or 0.0), RUN_TTL_SECONDS)
    if usd is not None:
        out["cost_usd"] = max(out["cost_usd"], usd)
    scan = counters.value(counters.run_key(run.id, "scan_bytes"))
    out["scan_bytes"] = float(scan or 0.0)
    if "max_rows" in limits and session is not None:
        from analystos.db.models import QueryExecution

        out["rows"] = float(session.scalar(select(func.coalesce(func.sum(QueryExecution.row_count), 0))
                                           .where(QueryExecution.run_id == run.id)) or 0)
    return out


def remaining(limits: dict[str, Any], used: dict[str, float]) -> dict[str, float]:
    return {k: max(0.0, float(v) - float(used.get(COUNTER_OF[k], 0.0))) for k, v in limits.items()
            if k in COUNTER_OF and v is not None}


def exhausted(limits: dict[str, Any], used: dict[str, float]) -> list[str]:
    """Limits the run has reached, as `max_steps (12 of 12)`; empty while every counter has room."""
    out = []
    for k in LIMIT_KEYS:
        v = limits.get(k)
        if v is None:
            continue
        u = float(used.get(COUNTER_OF[k], 0.0))
        if u >= float(v):
            out.append(f"{k} ({u:g} of {float(v):g})")
    return out


# Reaching one of these ends the run; the others are allowances (no further step, replan or retry
# once used up), which end the run only when there is still work the allowance would be needed for.
HARD_LIMITS = ("max_duration_s", "max_cost_usd", "max_scan_bytes", "max_rows")


def stops(limits: dict[str, Any], used: dict[str, float]) -> list[str]:
    """Why the run must stop now: a hard counter reached, or an allowance overrun."""
    out = []
    for k in LIMIT_KEYS:
        v = limits.get(k)
        if v is None:
            continue
        u = float(used.get(COUNTER_OF[k], 0.0))
        hard = k in HARD_LIMITS and float(v) > 0  # max_cost_usd 0 = no model spend; deterministic work goes on
        if (u >= float(v)) if hard else (u > float(v)):
            out.append(f"{k} ({u:g} of {float(v):g})")
    return out


def snapshot(limits: dict[str, Any], used: dict[str, float]) -> dict[str, Any]:
    """The payload of a `run.budget` event (the Autonomy Center's remaining-budget gauge)."""
    return {"limits": dict(limits), "used": {COUNTER_OF[k]: used.get(COUNTER_OF[k], 0.0) for k in limits if k in COUNTER_OF},
            "remaining": remaining(limits, used), "exhausted": exhausted(limits, used)}


def is_budget_failure(error: str | None) -> bool:
    """A task refused by a budget (its error carries the BudgetExceeded code): a stop, not a defect."""
    return bool(error) and error.startswith(BUDGET_ERROR)


def record_scan(run_id: str | None, nbytes: int | float) -> None:
    """Add scanned bytes to the run's counter: the gateway calls this with a statement's estimate
    before it runs, and with the difference once the actual is known (S02 §3.5)."""
    if not run_id or not nbytes:
        return
    from analystos.runtime.budget_counters import RUN_TTL_SECONDS, default_budget_counters

    counters = default_budget_counters()
    key = counters.run_key(run_id, "scan_bytes")
    counters.read(key, lambda: 0.0, RUN_TTL_SECONDS)  # seeds a missing counter at 0
    counters.add({key: float(nbytes)}, RUN_TTL_SECONDS)


def check_scan(run_id: str, limits: dict[str, Any], estimate: int | float) -> None:
    """Refuse a statement whose estimate would take the run past `max_scan_bytes`."""
    from analystos.core.errors import BudgetExceeded
    from analystos.runtime.budget_counters import default_budget_counters

    cap = limits.get("max_scan_bytes")
    if cap is None:
        return
    counters = default_budget_counters()
    used = float(counters.value(counters.run_key(run_id, "scan_bytes")) or 0.0)
    if used >= float(cap) or used + float(estimate) > float(cap):
        raise BudgetExceeded(f"run scan budget exhausted: {used + float(estimate):g} of {float(cap):g} bytes",
                             details={"cap": "max_scan_bytes", "used": used, "estimate": estimate, "limit": cap})


# ------------------------------------------------------------------ limits of a run, cached
# A run's limits are fixed when it is created; the model router asks for them before every call.
_LIMITS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_LIMITS_MAX = 2048
_lock = Lock()


def run_limits(session: Any, run_id: str) -> dict[str, Any]:
    with _lock:
        if run_id in _LIMITS:
            _LIMITS.move_to_end(run_id)
            return _LIMITS[run_id]
    from analystos.db.models import AnalysisRun

    value = dict(session.scalar(select(AnalysisRun.limits).where(AnalysisRun.id == run_id)) or {})
    with _lock:
        _LIMITS[run_id] = value
        while len(_LIMITS) > _LIMITS_MAX:
            _LIMITS.popitem(last=False)
    return value


def forget(run_id: str) -> None:
    with _lock:
        _LIMITS.pop(run_id, None)

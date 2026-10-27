"""End work a dead process left "running" (Ask turns, ML experiments).

An Ask turn or an ML experiment runs inside the process that started it; if that process dies (a restart,
an OOM kill) the row stays `running` forever and the UI spins. The sweep marks rows older than a bound
that no live process could still be working on as failed, with a message that says what happened. It runs
at API startup and on every scheduler iteration; the bounds keep it from touching work still in flight in
another process.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from analystos.core.config import get_settings
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AskTurn, MLExperiment
from analystos.events.bus import emit
from analystos.tenancy.background import platform_loop

log = get_logger(__name__)
ASK_TURN_BOUND = timedelta(minutes=30)
INTERRUPTED = "Interrupted: the process running it stopped before it finished. Nothing was answered from a guess; ask again."


def ml_bound() -> timedelta:
    """Longer than any experiment may take: the platform's training ceiling plus the compute wait margin."""
    return timedelta(seconds=max(3600, 2 * int(get_settings().ml_max_seconds) + 900))


@platform_loop
def sweep(now: datetime | None = None, *, ask_after: timedelta = ASK_TURN_BOUND, ml_after: timedelta | None = None) -> dict[str, Any]:
    """Mark stale running Ask turns (refused, kind `failed`) and ML experiments (failed); returns their ids."""
    from analystos.services.ask import refusal

    now = now or utcnow()
    ml_after = ml_after or ml_bound()
    out: dict[str, Any] = {"ask_turns": [], "ml_experiments": []}
    with session_scope() as s:
        for turn in s.scalars(select(AskTurn).where(AskTurn.status == "running", AskTurn.created_at < now - ask_after)
                              .with_for_update(skip_locked=True).limit(500)):
            turn.status, turn.refusal = "refused", refusal("failed", INTERRUPTED, interrupted=True)
            turn.stages = [*(turn.stages or []), {"key": "done", "text": "Interrupted", "at_ms": None}]
            out["ask_turns"].append(turn.id)
        for exp in s.scalars(select(MLExperiment).where(MLExperiment.status == "running", MLExperiment.created_at < now - ml_after)
                             .with_for_update(skip_locked=True).limit(500)):
            exp.status, exp.finished_at = "failed", now
            exp.error = "interrupted: the process running this experiment stopped before it finished; start it again"
            emit(exp.workspace_id, "ml.experiment.failed", {"experiment_id": exp.id, "status": "failed", "verdict": exp.verdict,
                                                            "error": "interrupted"}, run_id=exp.run_id, session=s)
            out["ml_experiments"].append(exp.id)
    if out["ask_turns"] or out["ml_experiments"]:
        log.warning("ended stale running work: %s", out)
    return out


def sweep_quietly() -> dict[str, Any]:
    """For startup and the scheduler loop: a failure is logged, never fatal."""
    try:
        return sweep()
    except Exception:  # noqa: BLE001
        log.exception("stale work sweep failed")
        return {}


__all__ = ["ASK_TURN_BOUND", "INTERRUPTED", "ml_bound", "sweep", "sweep_quietly"]

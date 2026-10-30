"""The decision every run driver takes after `engine.get_state` (ADR-015).

One pure function maps a `get_state` answer to the next action, so the local runner and the LangGraph
driver cannot drift apart. The Temporal workflow keeps its inline copy (changing workflow code risks
replay); `tests/conformance/orchestrator/test_next_action_parity.py` asserts that all three agree on
every state shape, so a new field cannot be handled in one driver and missed in another.

Routing is deterministic on the engine's answer, never chosen by a model (CLAUDE.md rule 3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["stop", "finish", "replan", "execute", "park", "idle"]
PERSON_WAIT_SECONDS = 300  # a run waiting for a person or paused re-checks this often without a signal
TASK_WAIT_SECONDS = 15  # tasks running elsewhere (another attempt, a slot held by another run)


@dataclass(frozen=True)
class Action:
    """What a driver does next.

    * `stop`: the run is already terminal; return `outcome`, call nothing.
    * `finish`: call `engine.finish_run(run_id, outcome, error)`, then return `outcome`.
    * `replan`: call `engine.plan_run` and decide again.
    * `execute`: run `keys` (in parallel where the driver can), then decide again.
    * `park`: the run waits for a person (`WAITING_USER`) or is paused (`PAUSED`). A parking driver
      returns `outcome` and holds nothing; a signal, the sweep or a restart drives it again.
    * `idle`: wait up to `wait_seconds` (or a signal), then decide again.
    """

    kind: Kind
    outcome: str | None = None
    error: str | None = None
    keys: tuple[str, ...] = field(default_factory=tuple)
    wait_seconds: float | None = None


def waits_for_person(state: dict) -> bool:
    """The run waits for a person or is paused: time spent so does not count as busy time."""
    return bool(state.get("waiting_user")) or state.get("control") == "pause"


def wait_seconds(state: dict) -> float:
    """How long a waiting driver sleeps before re-checking, capped by the run's remaining wall-clock
    budget so a goal run expires on time (S06 §3.4)."""
    seconds = PERSON_WAIT_SECONDS if waits_for_person(state) else TASK_WAIT_SECONDS
    left = ((state.get("budget") or {}).get("remaining") or {}).get("max_duration_s")
    if left is not None:
        seconds = max(1, min(seconds, int(left) + 1))
    return seconds


def next_action(state: dict) -> Action:
    """The next action for a `get_state` answer. The order is the Temporal workflow's."""
    if state.get("terminal"):
        return Action("stop", outcome=state.get("status", "COMPLETED"))
    if state.get("control") == "cancel":
        return Action("finish", outcome="CANCELLED")
    if state.get("needs_plan"):
        return Action("replan")
    if state.get("fail"):
        return Action("finish", outcome="FAILED", error=state["fail"])
    if state.get("done"):
        return Action("finish", outcome="COMPLETED")
    if state.get("limited"):  # a budget counter is used up: validated partials stand
        outcome = "EXPIRED" if state.get("expired") else "COMPLETED_WITH_LIMITATIONS"
        return Action("finish", outcome=outcome, error=state["limited"])
    if state.get("ready"):
        return Action("execute", keys=tuple(state["ready"]), wait_seconds=None)
    if waits_for_person(state):
        return Action("park", outcome="PAUSED" if state.get("control") == "pause" else "WAITING_USER",
                      wait_seconds=wait_seconds(state))
    return Action("idle", wait_seconds=wait_seconds(state))

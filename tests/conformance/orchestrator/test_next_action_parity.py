"""ORC-T2 (ADR-015): the three drivers take the same decision on every `get_state` shape.

`runtime/loop.py::next_action` is the shared decision of `local` and `langgraph`. The Temporal workflow
keeps its inline copy (replay safety), so this table drives each driver one step from each shape and
compares what it did: return / finish (outcome, error) / replan / execute (keys) / park / wait (seconds).
A new key in the engine's answer must be added to SHAPES (`test_every_engine_state_key_is_in_the_table`),
so it cannot be handled in one driver and missed in another."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import logging
import textwrap
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.conformance.orchestrator.harness import installed_backends

from analystos.runtime import engine as real_engine
from analystos.runtime.loop import Action, next_action
from analystos.workflows import orchestrator as orch

SNAP = {"limits": {"max_steps": 5}, "used": {"steps": 2}, "remaining": {"steps": 3}}
SHAPES: dict[str, dict] = {
    "terminal-completed": {"terminal": True, "status": "COMPLETED"},
    "terminal-failed": {"terminal": True, "status": "FAILED"},
    "terminal-no-status": {"terminal": True},
    "cancel": {"control": "cancel"},
    "pause": {"control": "pause"},
    "needs-plan": {"needs_plan": True},
    "fail": {"fail": "required task(s) failed: a — policy refused"},
    "deadlock": {"fail": "no runnable task (dependency deadlock)"},
    "done": {"done": True},
    "ready": {"ready": ["a", "b"]},
    "ready-budget": {"ready": ["a"], "budget": SNAP},
    "waiting": {"waiting_user": ["gate"]},
    "waiting-budget": {"waiting_user": ["gate"], "budget": {**SNAP, "remaining": {"max_duration_s": 42}}},
    "waiting-budget-gone": {"waiting_user": ["gate"], "budget": {**SNAP, "remaining": {"max_duration_s": 0}}},
    "running": {"running": ["a"]},
    "running-budget": {"running": ["a"], "budget": {**SNAP, "remaining": {"max_duration_s": 3}}},
    "draining": {"running": ["a"], "budget": SNAP, "draining": ["max_steps (5 of 5)"]},
    "throttled": {"throttled": ["a"], "budget": SNAP},
    "limited": {"limited": "max_steps (5 of 5)", "expired": False, "budget": SNAP},
    "expired": {"limited": "max_duration_s (60 of 60)", "expired": True, "budget": SNAP},
}
SENTINEL = "SENTINEL"  # the state after the first decision: terminal, so each driver stops there


def _observed(kind: str, **kw) -> tuple:
    return (kind, kw.get("outcome"), kw.get("error"), tuple(kw.get("keys") or ()), kw.get("wait"))


def _expected(action: Action, *, temporal: bool) -> tuple:
    """What a driver visibly does for `action`. The Temporal workflow never parks: it waits (with a timer
    whose length it reports) for a nudge; the in-process drivers park instead of waiting."""
    if temporal and action.kind in ("park", "idle"):
        return _observed("idle", wait=action.wait_seconds)
    outcome = action.outcome if action.kind in ("stop", "finish", "park") else None
    return _observed(action.kind, outcome=outcome, error=action.error, keys=action.keys)


# ------------------------------------------------------------------------------------------ in-process drivers
class Scripted:
    """An engine that answers `state` once, then SENTINEL; records what the driver did."""

    def __init__(self, state: dict) -> None:
        self.state, self.calls, self.slept = state, [], []
        self.answered = False

    def plan_run(self, run_id):
        self.calls.append(("plan_run",))

    def get_state(self, run_id):
        if self.answered:
            return {"terminal": True, "status": SENTINEL}
        self.answered = True
        return dict(self.state)

    def execute_task(self, run_id, key):
        self.calls.append(("execute_task", key))
        return {"status": "COMPLETED"}

    def finish_run(self, run_id, outcome, error=None):
        self.calls.append(("finish_run", outcome, error))

    def observed(self, returned: str) -> tuple:
        finish = [c for c in self.calls if c[0] == "finish_run"]
        if finish:
            return _observed("finish", outcome=finish[0][1], error=finish[0][2])
        executed = [c[1] for c in self.calls if c[0] == "execute_task"]
        if executed:
            return _observed("execute", keys=executed)
        if sum(1 for c in self.calls if c[0] == "plan_run") > 1:
            return _observed("replan")
        if returned != SENTINEL:
            return _observed("park" if returned in ("WAITING_USER", "PAUSED") and not self.state.get("terminal")
                             else "stop", outcome=returned)
        return _observed("idle")


def _runtime(backend: str):
    if backend == "langgraph":
        from analystos.workflows.langgraph_backend import LangGraphRuntime

        return LangGraphRuntime(None, poll=0.0, sleep=lambda s: None)
    return orch.LocalRuntime(None, poll=0.0, sleep=lambda s: None)


@pytest.mark.parametrize("backend", installed_backends())
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_in_process_drivers_follow_next_action(shape, backend, monkeypatch):
    from analystos.tenancy import background

    scripted = Scripted(SHAPES[shape])
    for name in ("plan_run", "get_state", "execute_task", "finish_run"):
        monkeypatch.setattr(orch.engine, name, getattr(scripted, name))
    monkeypatch.setattr(background, "owner_tenant", lambda table, row_id: "t-acme")
    rt = _runtime(backend)
    try:
        returned = rt.drive("run-1", park=True)
    finally:
        rt.shutdown()
    assert scripted.observed(returned) == _expected(next_action(SHAPES[shape]), temporal=False), shape


# ------------------------------------------------------------------------------------------ the Temporal workflow
class _Stop(Exception):
    pass


class StubWorkflow:
    """Stands in for `temporalio.workflow` inside AnalysisWorkflow.run: activities are recorded, not run."""

    logger = logging.getLogger("stub-workflow")

    def __init__(self, state: dict) -> None:
        self.state, self.calls, self.waited = state, [], []
        self.answered = False

    def patched(self, patch_id: str) -> bool:
        return True  # new executions take every patched branch

    def info(self):
        return SimpleNamespace(task_queue="analystos-analysis", is_continue_as_new_suggested=lambda: False)

    def continue_as_new(self, *a, **kw):
        raise AssertionError("continue_as_new on the first decision")

    async def execute_activity(self, name, arg=None, *, args=None, **kw):
        args = list(args or ([] if arg is None else [arg]))
        if name == "get_state":
            if self.answered:
                raise _Stop
            self.answered = True
            return dict(self.state)
        self.calls.append((name, *args[1:]))
        return {}

    async def wait_condition(self, fn, timeout: timedelta):
        self.waited.append(timeout.total_seconds())
        raise _Stop

    def observed(self, returned: str | None) -> tuple:
        finish = [c for c in self.calls if c[0] == "finish_run"]
        if finish:
            return _observed("finish", outcome=finish[0][1], error=finish[0][2])
        executed = [c[1] for c in self.calls if c[0] == "execute_task"]
        if executed:
            return _observed("execute", keys=executed)
        if any(c[0] == "plan_run" for c in self.calls):
            return _observed("replan")
        if self.waited:
            return _observed("idle", wait=self.waited[0])
        return _observed("stop", outcome=returned)


@pytest.mark.skipif(importlib.util.find_spec("temporalio") is None, reason="needs the temporal extra")
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_temporal_workflow_agrees_with_next_action(shape, monkeypatch):
    from analystos.workflows import analysis_workflow as aw
    from analystos.workflows.queues import fallback_options

    stub = StubWorkflow(SHAPES[shape])
    monkeypatch.setattr(aw, "workflow", stub)
    wf = aw.AnalysisWorkflow()
    opts = {**fallback_options("analystos"), "resumed": True}  # resumed: skip the initial plan_run

    async def go():
        try:
            return await wf.run("run-1", opts)
        except _Stop:
            return None

    returned = asyncio.run(go())
    assert stub.observed(returned) == _expected(next_action(SHAPES[shape]), temporal=True), shape


# ------------------------------------------------------------------------------------------ table coverage
def _returned_keys(fn) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return keys


def test_every_engine_state_key_is_in_the_table():
    """A key `_decide` can return that no table row exercises would be untested in every driver."""
    engine_keys = _returned_keys(real_engine._decide) | _returned_keys(real_engine._decide_within_budget)
    table_keys = {k for shape in SHAPES.values() for k in shape}
    assert engine_keys - table_keys == set()


def test_next_action_order_matches_the_workflow_for_mixed_shapes():
    """Shapes the engine does not produce today still decide in the workflow's order."""
    assert next_action({"control": "cancel", "ready": ["a"]}).kind == "finish"
    assert next_action({"needs_plan": True, "fail": "x"}).kind == "replan"
    assert next_action({"fail": "x", "done": True}).outcome == "FAILED"
    assert next_action({"done": True, "limited": "y"}).outcome == "COMPLETED"
    assert next_action({"limited": "y", "ready": ["a"]}).kind == "finish"
    assert next_action({"ready": ["a"], "waiting_user": ["g"]}).kind == "execute"

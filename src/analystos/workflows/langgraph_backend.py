"""The LangGraph orchestrator backend (ADR-015): a stateless graph that drives the engine loop.

The graph is a *driver*, like `LocalRuntime.drive`, and holds nothing the engine does not hold:

* It is compiled **without a checkpointer**. All run state (tasks, claims, budgets, approvals) stays in
  Postgres. A wait for a person or a pause ends the invocation (it *parks*), and `signal_run`, the sweep
  or a restart starts a fresh invocation from `engine.get_state`.
* Its shape is fixed here and mirrors the engine loop; playbooks stay the only flow definition. Edges are
  chosen by `runtime/loop.py::next_action` on `get_state`, never by a model. No LangChain chat model,
  tool, agent or retriever is used: agents run through `agents/dispatch.py` inside `engine.execute_task`,
  and SQL goes through `QueryGateway.execute`.
* `interrupt()` / `Command(resume=...)` are not used: a resume value must never stand in for an approval.
  Approvals stay hash-bound and are checked with `verify_for_execution` inside the step.
* Every node runs as the run's tenant (`as_owner_of`), without relying on LangGraph's executor to carry
  context variables (ADR-009, CLAUDE.md rule 5); a task handed to the shared pool carries it too.
* Parallel tasks: `Send` fans out one `execute` per ready key; each submits to the runtime's bounded pool
  (`ANALYSTOS_LOCAL_WORKERS`) and waits, so the global bound is the local one. A superstep waits for all
  its tasks, like the Temporal workflow's `asyncio.gather`. Retries use the engine's claim/attempt rules.
* Long runs: each invocation is bounded by `recursion_limit`; on reaching it the driver re-invokes (the
  equivalent of Temporal's `continue_as_new`).

Hosting reuses `LocalRuntime` (driver registry, the nudge-while-parking guard, the pool, resume, sweeper),
so the API process (or an `analystos driver` process, ORC-T7) drives runs exactly as under `local`.

This is the only module allowed to import `langgraph` / `langchain_*` / `langsmith` (import-linter contract
in pyproject.toml)."""
from __future__ import annotations

import operator
import time
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from analystos.core.config import get_settings
from analystos.core.logging import get_logger
from analystos.runtime import engine
from analystos.runtime.loop import Action, next_action, waits_for_person
from analystos.tenancy.background import as_owner_of, carry_context
from analystos.workflows import orchestrator as _orch
from analystos.workflows.langgraph_guard import tracing_allowed

log = get_logger(__name__)
NODES = ("plan", "state", "execute", "finish", "park", "idle")


class DriveState(TypedDict, total=False):
    """The graph's channels: the run id and this invocation's bookkeeping. Never run state."""

    run_id: str
    key: str  # set only on an `execute` Send
    action: dict[str, Any]
    outcome: str | None
    executed: Annotated[int, operator.add]  # parallel `execute` writes merge here


class _Drive:
    """One invocation's driver-side bookkeeping (busy time, as in LocalRuntime.drive); never persisted."""

    def __init__(self, *, park: bool, max_seconds: float | None, poll: float, clock: Callable[[], float],
                 keep_going: Callable[[], bool] | None = None) -> None:
        self.park, self.max_seconds, self.poll, self.clock, self.keep_going = park, max_seconds, poll, clock, keep_going
        self.busy, self.last = 0.0, clock()
        self.invocations = 0


def _action_dict(action: Action) -> dict[str, Any]:
    return {"kind": action.kind, "outcome": action.outcome, "error": action.error, "keys": list(action.keys),
            "wait_seconds": action.wait_seconds}


class LangGraphRuntime(_orch.LocalRuntime):
    """`LocalRuntime` whose per-run drive loop is a compiled LangGraph graph (no checkpointer)."""

    def __init__(self, workers: int | None = None, *, poll: float = 0.3, recursion_limit: int = 200,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(workers, poll=poll, clock=clock, sleep=sleep)
        self.recursion_limit = recursion_limit
        self.graph = self._build()
        self.reinvocations = 0

    # ---------------------------------------------------------------- the graph
    def _build(self):
        g = StateGraph(DriveState)
        g.add_node("plan", self._plan)
        g.add_node("state", self._state)
        g.add_node("execute", self._execute)
        g.add_node("finish", self._finish)
        g.add_node("park", self._park)
        g.add_node("idle", self._idle)
        g.add_edge(START, "plan")
        g.add_edge("plan", "state")
        g.add_conditional_edges("state", self._route, ["plan", "execute", "finish", "park", "idle", END])
        g.add_edge("execute", "state")
        g.add_edge("idle", "state")
        g.add_edge("finish", END)
        g.add_edge("park", END)
        return g.compile()  # no checkpointer: Postgres is the only state store (ADR-015 §2)

    def mermaid(self) -> str:
        """The fixed graph as a Mermaid diagram (runbook 04)."""
        return self.graph.get_graph().draw_mermaid()

    @staticmethod
    def _drive_of(config: dict) -> _Drive:
        return config["configurable"]["drive"]

    def _plan(self, state: DriveState, config) -> dict:
        with as_owner_of("analysis_run", state["run_id"]):
            engine.plan_run(state["run_id"])
        return {}

    def _state(self, state: DriveState, config) -> dict:
        run_id, d = state["run_id"], self._drive_of(config)
        if d.keep_going is not None and not d.keep_going():  # a leased drive lost its lease (ORC-T7)
            return {"action": _action_dict(Action("stop", outcome=_orch.LEASE_LOST)), "outcome": _orch.LEASE_LOST}
        with as_owner_of("analysis_run", run_id):
            snapshot = engine.get_state(run_id)
        action = next_action(snapshot)
        now = d.clock()
        if not waits_for_person(snapshot):
            d.busy += now - d.last
        d.last = now
        cancel = action.kind == "finish" and action.outcome == "CANCELLED"
        if action.kind != "stop" and not cancel and d.max_seconds is not None and d.busy >= d.max_seconds:
            action = Action("finish", outcome="FAILED", error="langgraph orchestrator timeout")
        if action.kind == "park" and not d.park:
            action = Action("idle", wait_seconds=action.wait_seconds)  # a synchronous drive polls the wait
        out: dict[str, Any] = {"action": _action_dict(action)}
        if action.kind == "stop":
            out["outcome"] = action.outcome
        return out

    @staticmethod
    def _route(state: DriveState):
        """Deterministic routing on the engine's answer (never a model)."""
        action = state["action"]
        kind = action["kind"]
        if kind == "stop":
            return END
        if kind == "replan":
            return "plan"
        if kind == "execute":
            return [Send("execute", {"run_id": state["run_id"], "key": key}) for key in action["keys"]]
        return kind  # finish | park | idle

    def _execute(self, state: DriveState, config) -> dict:
        run_id, key = state["run_id"], state["key"]
        with as_owner_of("analysis_run", run_id):
            if self._tasks is None:
                _orch._execute_with_retries(run_id, key, self.sleep)
            else:  # the shared bounded pool: the global bound is ANALYSTOS_LOCAL_WORKERS, as under local
                self._tasks.submit(carry_context(_orch._execute_with_retries), run_id, key, self.sleep).result()
        return {"executed": 1}

    def _finish(self, state: DriveState, config) -> dict:
        action = state["action"]
        with as_owner_of("analysis_run", state["run_id"]):
            if action["error"] is None:
                engine.finish_run(state["run_id"], action["outcome"])
            else:
                engine.finish_run(state["run_id"], action["outcome"], action["error"])
        return {"outcome": action["outcome"]}

    def _park(self, state: DriveState, config) -> dict:
        return {"outcome": state["action"]["outcome"]}

    def _idle(self, state: DriveState, config) -> dict:
        d = self._drive_of(config)
        cap = state["action"].get("wait_seconds")
        self.sleep(d.poll if cap is None else min(d.poll, cap))
        return {}

    # ---------------------------------------------------------------- driving
    def drive(self, run_id: str, *, park: bool, max_seconds: float | None = None, poll: float | None = None,
              keep_going: Callable[[], bool] | None = None) -> str:
        """Invoke the graph until the run is terminal or parks. Same contract as `LocalRuntime.drive`."""
        d = _Drive(park=park, max_seconds=max_seconds, poll=self.poll if poll is None else poll, clock=self.clock,
                   keep_going=keep_going)
        config = {"recursion_limit": self.recursion_limit, "configurable": {"drive": d},
                  "callbacks": [], "run_name": "analystos-run-driver"}
        while True:
            d.invocations += 1
            try:
                with _tracing_off():
                    final = self.graph.invoke({"run_id": run_id, "executed": 0}, config=config)
            except GraphRecursionError:
                # the invocation reached its superstep bound: start a fresh one from Postgres state
                self.reinvocations += 1
                log.debug("langgraph driver of %s re-invoked after %d supersteps", run_id, self.recursion_limit)
                continue
            return final.get("outcome") or "COMPLETED"


class _tracing_off:
    """Run an invocation with LangSmith tracing explicitly disabled unless the operator opted in."""

    def __enter__(self):
        self._ctx = None
        if tracing_allowed(get_settings()):
            return self
        try:
            from langsmith.run_helpers import tracing_context
        except ImportError:  # langsmith not installed: nothing can trace
            return self
        self._ctx = tracing_context(enabled=False)
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        if self._ctx is not None:
            self._ctx.__exit__(*exc)
        return False

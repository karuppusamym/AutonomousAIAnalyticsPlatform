"""In-memory engine and helpers of the orchestrator conformance suite (ADR-015, ORC-T2)."""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections.abc import Callable

from analystos.tenancy import current_tenant

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "COMPLETED_WITH_LIMITATIONS")
OPS = ("plan_run", "get_state", "execute_task", "finish_run", "nonterminal_runs", "release_orphaned_claims")


class ConformanceEngine:
    """Tasks with dependencies, an optional approval gate, claims with versions, budgets, like runtime/engine."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, dict] = {}
        self.hooks: dict[str, Callable] = {}  # task key -> callable(run_id, key, claim) run while claimed
        self.calls: list[tuple] = []  # (op, run_id, *args, tenant)
        self.on_state: Callable[[str], None] | None = None

    def add_run(self, run_id: str, tasks: dict[str, list[str]], *, gate: str | None = None, tenant: str = "t-acme",
                optional: tuple[str, ...] = (), max_steps: int | None = None, needs_plan: bool = False) -> dict:
        run = {"status": "NEW", "control": "run", "approved": gate is None, "gate": gate, "tenant": tenant,
               "max_steps": max_steps, "needs_plan": needs_plan, "plans": 0, "finished": [],
               "tasks": {k: {"status": "NEW", "deps": d, "claim": 0, "output": None, "error": None,
                             "optional": k in optional} for k, d in tasks.items()}}
        self.runs[run_id] = run
        return run

    def _record(self, op: str, run_id: str | None, *args) -> None:
        with self.lock:
            self.calls.append((op, run_id, *args, current_tenant()))

    # -------------------------------------------------------------- the engine's operations
    def plan_run(self, run_id: str) -> dict:
        self._record("plan_run", run_id)
        with self.lock:
            run = self.runs[run_id]
            run["plans"] += 1
            run["needs_plan"] = False
        return {"plan_version": run["plans"]}

    def get_state(self, run_id: str) -> dict:
        self._record("get_state", run_id)
        if self.on_state:
            self.on_state(run_id)
        with self.lock:
            run = self.runs[run_id]
            if run["status"] in TERMINAL:
                return {"terminal": True, "status": run["status"]}
            if run["control"] == "cancel":
                return {"control": "cancel"}
            if run["control"] == "pause":
                run["status"] = "PAUSED"
                return {"control": "pause"}
            if run["needs_plan"]:
                return {"needs_plan": True}
            tasks = run["tasks"]
            failed = [k for k, t in tasks.items() if t["status"] == "FAILED" and not t["optional"]]
            if failed:
                return {"fail": f"required task(s) failed: {', '.join(failed)}"}
            if all(t["status"] in ("COMPLETED", "FAILED", "SKIPPED") for t in tasks.values()):
                return {"done": True}
            ready, waiting, running = [], [], []
            for key, t in tasks.items():
                if t["status"] == "RUNNING":
                    running.append(key)
                elif t["status"] in ("NEW", "WAITING_USER") and all(
                        tasks[d]["status"] in ("COMPLETED", "SKIPPED") for d in t["deps"]):
                    if key == run["gate"] and not run["approved"]:
                        t["status"] = "WAITING_USER"
                        waiting.append(key)
                    else:
                        ready.append(key)
            steps = sum(1 for t in tasks.values() if t["status"] in ("COMPLETED", "FAILED", "RUNNING"))
            if run["max_steps"] is not None and steps >= run["max_steps"] and (ready or waiting):
                if running:
                    return {"running": running, "draining": ["max_steps"]}
                return {"limited": f"max_steps ({steps} of {run['max_steps']})", "expired": False}
            if ready:
                run["status"] = "RUNNING"
                return {"ready": ready[:4]}
            if waiting and not running:
                run["status"] = "WAITING_USER"
                return {"waiting_user": waiting}
            if running:
                return {"running": running}
            return {"fail": "no runnable task (dependency deadlock)"}

    def execute_task(self, run_id: str, key: str) -> dict:
        self._record("execute_task", run_id, key)
        with self.lock:
            task = self.runs[run_id]["tasks"][key]
            if task["status"] in ("COMPLETED", "FAILED"):
                return {"status": task["status"], "cached": True}
            if task["status"] == "RUNNING":
                return {"status": "in_progress"}
            task["status"], task["claim"] = "RUNNING", task["claim"] + 1
            claim = task["claim"]
        hook = self.hooks.get(key)
        output = hook(run_id, key, claim) if hook else {"by": threading.current_thread().name}
        with self.lock:
            if task["claim"] != claim:
                return {"status": "superseded"}
            if isinstance(output, dict) and output.get("fail"):
                task["status"], task["error"] = "FAILED", output["fail"]
                return {"status": "FAILED"}
            task["status"], task["output"] = "COMPLETED", output
        return {"status": "COMPLETED"}

    def finish_run(self, run_id: str, outcome: str, error: str | None = None) -> None:
        self._record("finish_run", run_id, outcome, error)
        with self.lock:
            run = self.runs[run_id]
            run["finished"].append((outcome, error))
            if run["status"] not in TERMINAL:
                run["status"], run["error"] = outcome, error

    def nonterminal_runs(self) -> list[str]:
        with self.lock:
            return [r for r, v in self.runs.items() if v["status"] not in TERMINAL]

    def release_orphaned_claims(self, run_ids=None) -> list[str]:
        live = self.nonterminal_runs()
        run_ids = live if run_ids is None else [r for r in live if r in set(run_ids)]
        with self.lock:
            for run_id in run_ids:
                for t in self.runs[run_id]["tasks"].values():
                    if t["status"] == "RUNNING":
                        t["status"], t["claim"] = "NEW", t["claim"] + 1
        return run_ids

    # -------------------------------------------------------------- helpers
    def status(self, run_id: str) -> str:
        return self.runs[run_id]["status"]

    def ops(self, op: str, run_id: str | None = None) -> list[tuple]:
        with self.lock:
            return [c for c in self.calls if c[0] == op and (run_id is None or c[1] == run_id)]


def installed_backends() -> list[str]:
    wanted = [b.strip() for b in os.environ.get("ANALYSTOS_CONFORMANCE_BACKENDS", "local,langgraph").split(",") if b.strip()]
    return [b for b in wanted if b != "langgraph" or importlib.util.find_spec("langgraph") is not None]



def until(cond, timeout: float = 10.0) -> None:
    started = time.monotonic()
    while not cond():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


def driver_threads(run_id: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == f"run-{run_id}" and t.is_alive()]

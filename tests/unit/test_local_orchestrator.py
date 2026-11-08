"""P7-16 (ADR-0025): the local orchestrator as a supported mode, against an in-memory engine that keeps
the engine's contract (plan_run / get_state / execute_task / finish_run, optimistic claims). No services.

* a run waiting for approval holds no thread and completes when the approval signal re-drives it,
  however long the wait (simulated clock: two hours);
* an API killed mid-run is resumed by the next process; the dead attempt's late result is superseded;
* ready tasks run in parallel on a bounded pool; without a pool size they run inline, as before;
* a signal that lands while the driver is deciding to park is not lost.
"""
from __future__ import annotations

import threading
import time

import pytest

from analystos.workflows import orchestrator as orch


class FakeEngine:
    """Tasks with dependencies, an optional approval gate, claims with versions, like runtime/engine."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, dict] = {}
        self.hooks: dict[str, object] = {}  # task key -> callable(run_id, key, attempt) run while claimed
        self.state_calls = 0
        self.on_state = None

    def add_run(self, run_id: str, tasks: dict[str, list[str]], *, gate: str | None = None) -> None:
        self.runs[run_id] = {"status": "NEW", "control": "run", "approved": gate is None, "gate": gate,
                             "tasks": {k: {"status": "NEW", "deps": d, "claim": 0, "output": None} for k, d in tasks.items()}}

    # -------------------------------------------------------------- the engine's four operations
    def plan_run(self, run_id: str) -> dict:
        return {"plan_version": 1}

    def get_state(self, run_id: str) -> dict:
        with self.lock:
            self.state_calls += 1
        if self.on_state:
            self.on_state(run_id)
        with self.lock:
            run = self.runs[run_id]
            if run["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                return {"terminal": True, "status": run["status"]}
            if run["control"] == "cancel":
                return {"control": "cancel"}
            if run["control"] == "pause":
                run["status"] = "PAUSED"
                return {"control": "pause"}
            tasks = run["tasks"]
            if all(t["status"] == "COMPLETED" for t in tasks.values()):
                return {"done": True}
            ready, waiting, running = [], [], []
            for key, t in tasks.items():
                if t["status"] == "RUNNING":
                    running.append(key)
                elif t["status"] in ("NEW", "WAITING_USER") and all(tasks[d]["status"] == "COMPLETED" for d in t["deps"]):
                    if key == run["gate"] and not run["approved"]:
                        t["status"] = "WAITING_USER"
                        waiting.append(key)
                    else:
                        ready.append(key)
            if ready:
                run["status"] = "RUNNING"
                return {"ready": ready[:4]}
            if waiting and not running:
                run["status"] = "WAITING_USER"
                return {"waiting_user": waiting}
            return {"running": running}

    def execute_task(self, run_id: str, key: str) -> dict:
        with self.lock:
            task = self.runs[run_id]["tasks"][key]
            if task["status"] == "COMPLETED":
                return {"status": "COMPLETED", "cached": True}
            if task["status"] == "RUNNING":
                return {"status": "in_progress"}
            task["status"], task["claim"] = "RUNNING", task["claim"] + 1
            claim = task["claim"]
        hook = self.hooks.get(key)
        output = hook(run_id, key, claim) if hook else {"by": threading.current_thread().name}
        with self.lock:
            if task["claim"] != claim:
                return {"status": "superseded"}
            task["status"], task["output"] = "COMPLETED", output
        return {"status": "COMPLETED"}

    def finish_run(self, run_id: str, outcome: str, error: str | None = None) -> None:
        with self.lock:
            run = self.runs[run_id]
            if run["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
                run["status"], run["error"] = outcome, error

    def nonterminal_runs(self) -> list[str]:
        with self.lock:
            return [r for r, v in self.runs.items() if v["status"] not in ("COMPLETED", "FAILED", "CANCELLED")]

    def release_orphaned_claims(self, run_ids=None) -> list[str]:
        with self.lock:
            for run in self.runs.values():
                for t in run["tasks"].values():
                    if t["status"] == "RUNNING":
                        t["status"], t["claim"] = "NEW", t["claim"] + 1
        return self.nonterminal_runs()

    # -------------------------------------------------------------- helpers
    def status(self, run_id: str) -> str:
        return self.runs[run_id]["status"]


@pytest.fixture
def fake(monkeypatch):
    engine = FakeEngine()
    for name in ("plan_run", "get_state", "execute_task", "finish_run", "nonterminal_runs", "release_orphaned_claims"):
        monkeypatch.setattr(orch.engine, name, getattr(engine, name))
    return engine


def _until(cond, timeout: float = 5.0) -> None:
    started = time.monotonic()
    while not cond():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


def _driver_threads(run_id: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == f"run-{run_id}"]


def test_a_run_waiting_for_approval_parks_and_completes_after_the_signal(fake):
    fake.add_run("r1", {"plan": [], "gate": ["plan"], "publish": ["gate"]}, gate="gate")
    rt = orch.LocalRuntime(2, poll=0.005)
    rt.submit("r1")
    _until(lambda: fake.status("r1") == "WAITING_USER" and not rt.active())
    _until(lambda: not _driver_threads("r1"))  # no thread is held while the run waits for a person
    time.sleep(0.05)
    calls = fake.state_calls
    time.sleep(0.05)
    assert fake.state_calls == calls  # and nothing polls it
    fake.runs["r1"]["approved"] = True  # the approval lands (hours later: no clock is involved at all)
    rt.submit("r1")  # signal_run
    _until(lambda: fake.status("r1") == "COMPLETED")
    assert fake.runs["r1"]["tasks"]["publish"]["status"] == "COMPLETED"
    rt.shutdown()


def test_two_hours_of_waiting_do_not_count_against_the_synchronous_cap(fake):
    """The old loop failed any run after one hour of wall clock, waiting included."""
    fake.add_run("r2", {"plan": [], "gate": ["plan"], "publish": ["gate"]}, gate="gate")
    now = [0.0]
    waited = []

    def tick(run_id):  # every poll while waiting is ten simulated minutes; approved after two hours
        if fake.runs[run_id]["status"] == "WAITING_USER":
            now[0] += 600
            waited.append(1)
            if now[0] >= 7200:
                fake.runs[run_id]["approved"] = True

    fake.on_state = tick
    rt = orch.LocalRuntime(None, poll=0.0, clock=lambda: now[0], sleep=lambda s: None)
    assert rt.drive("r2", park=False, max_seconds=3600) == "COMPLETED"
    assert now[0] >= 7200 and len(waited) >= 12


def test_busy_time_past_the_cap_still_fails_the_synchronous_loop(fake):
    fake.add_run("r3", {"a": []})
    now = [0.0]
    fake.hooks["a"] = lambda *a: now.__setitem__(0, now[0] + 4000) or {}
    rt = orch.LocalRuntime(None, poll=0.0, clock=lambda: now[0], sleep=lambda s: None)
    fake.runs["r3"]["tasks"]["b"] = {"status": "NEW", "deps": ["a"], "claim": 0, "output": None}
    assert rt.drive("r3", park=False, max_seconds=3600) == "FAILED"
    assert fake.runs["r3"]["error"] == "local orchestrator timeout"


def test_an_api_killed_mid_run_is_resumed_by_the_next_process(fake):
    fake.add_run("r4", {"metadata": [], "analyse": ["metadata"]})
    release = threading.Event()
    started = threading.Event()

    def dies(run_id, key, claim):  # the first process hangs inside the task, then is "killed"
        if claim == 1:
            started.set()
            release.wait(5)
            return {"by": "dead process"}
        return {"by": "resumed process"}

    fake.hooks["metadata"] = dies
    first = orch.LocalRuntime(2, poll=0.005)
    first.submit("r4")
    assert started.wait(5)
    assert fake.runs["r4"]["tasks"]["metadata"]["status"] == "RUNNING"
    # restart: a new process (runtime) starts; the old one no longer drives anything
    second = orch.LocalRuntime(2, poll=0.005)
    first._stop.set()
    assert second.resume() == ["r4"]
    _until(lambda: fake.status("r4") == "COMPLETED")
    release.set()  # the dead attempt's late result must not overwrite the resumed one
    time.sleep(0.05)
    assert fake.runs["r4"]["tasks"]["metadata"]["output"] == {"by": "resumed process"}
    first.shutdown()
    second.shutdown()


def test_ready_tasks_run_in_parallel_on_a_bounded_pool(fake):
    fake.add_run("r5", {f"t{i}": [] for i in range(6)})
    live, peak, lock = [0], [0], threading.Lock()

    def work(run_id, key, claim):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"by": threading.current_thread().name}

    for i in range(6):
        fake.hooks[f"t{i}"] = work
    rt = orch.LocalRuntime(2, poll=0.005)
    assert rt.drive("r5", park=True) == "COMPLETED"
    assert peak[0] == 2  # parallel, and never above ANALYSTOS_LOCAL_WORKERS
    assert all(t["output"]["by"].startswith("local-task") for t in fake.runs["r5"]["tasks"].values())
    rt.shutdown()


def test_without_a_pool_tasks_run_inline_one_at_a_time(fake):
    fake.add_run("r6", {"a": [], "b": []})
    rt = orch.LocalRuntime(None, poll=0.005)
    rt.submit("r6")
    _until(lambda: fake.status("r6") == "COMPLETED")
    assert {t["output"]["by"] for t in fake.runs["r6"]["tasks"].values()} == {"run-r6"}


def test_a_signal_racing_the_park_decision_is_not_lost(fake, monkeypatch):
    fake.add_run("r7", {"plan": [], "gate": ["plan"]}, gate="gate")
    rt = orch.LocalRuntime(1, poll=0.005)
    fired = []

    def get_state(run_id):  # the approval and its signal land between the state read and the park
        state = fake.get_state(run_id)
        if state.get("waiting_user") and not fired:
            fired.append(1)
            fake.runs[run_id]["approved"] = True
            rt.submit(run_id)
        return state

    monkeypatch.setattr(orch.engine, "get_state", get_state)
    rt.submit("r7")
    _until(lambda: fake.status("r7") == "COMPLETED")
    assert fired
    _until(lambda: not rt.active())
    rt.shutdown()


def test_pause_parks_and_resume_redrives(fake):
    fake.add_run("r8", {"a": [], "b": ["a"]})
    fake.runs["r8"]["control"] = "pause"
    rt = orch.LocalRuntime(2, poll=0.005)
    rt.submit("r8")
    _until(lambda: fake.status("r8") == "PAUSED" and not rt.active())
    fake.runs["r8"]["control"] = "run"
    rt.submit("r8")
    _until(lambda: fake.status("r8") == "COMPLETED")
    rt.shutdown()


def test_the_sweep_redrives_parked_runs_decided_elsewhere(fake):
    fake.add_run("r9", {"plan": [], "gate": ["plan"]}, gate="gate")
    rt = orch.LocalRuntime(2, poll=0.005)
    rt.submit("r9")
    _until(lambda: fake.status("r9") == "WAITING_USER" and not rt.active())
    fake.runs["r9"]["approved"] = True  # decided by another process: no signal reaches this one
    assert rt.sweep() == ["r9"]
    _until(lambda: fake.status("r9") == "COMPLETED")
    rt.shutdown()


def test_signal_run_redrives_local_runs(fake, monkeypatch):
    from analystos.core.config import get_settings

    monkeypatch.setattr(get_settings(), "orchestrator", "local")
    rt = orch.LocalRuntime(2, poll=0.005)
    previous = orch.reset_local_runtime(rt)
    try:
        fake.add_run("r10", {"plan": [], "gate": ["plan"]}, gate="gate")
        assert orch.start_run("r10") == "local-r10"
        _until(lambda: fake.status("r10") == "WAITING_USER" and not rt.active())
        fake.runs["r10"]["approved"] = True
        orch.signal_run("r10")
        _until(lambda: fake.status("r10") == "COMPLETED")
    finally:
        orch.reset_local_runtime(previous)
        rt.shutdown()


def test_api_startup_resumes_local_runs_only_when_asked(fake, monkeypatch):
    from analystos.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "orchestrator", "temporal")
    assert orch.start_local_runtime() is None  # standard: Temporal re-dispatches its own work
    monkeypatch.setattr(settings, "orchestrator", "local")
    monkeypatch.setattr(settings, "local_resume", False)
    assert orch.start_local_runtime() is None  # tests and the standard profile drive runs themselves
    fake.add_run("r11", {"a": [], "b": ["a"]})
    fake.runs["r11"]["tasks"]["a"]["status"] = "RUNNING"  # held by the process that died
    monkeypatch.setattr(settings, "local_resume", True)
    monkeypatch.setattr(settings, "local_sweep_seconds", 0.0)
    rt = orch.LocalRuntime(2, poll=0.005)
    previous = orch.reset_local_runtime(rt)
    try:
        assert orch.start_local_runtime() is rt
        _until(lambda: fake.status("r11") == "COMPLETED")
        assert fake.runs["r11"]["tasks"]["a"]["claim"] == 2  # the dead claim was released, then retaken
    finally:
        orch.reset_local_runtime(previous)
        rt.shutdown()


def test_the_in_process_scheduler_loops_until_stopped(monkeypatch):
    from analystos.services import schedules

    # Only the loop is under test: every pass's work is a no-op. The real housekeeping opens a dozen
    # control-plane sessions; with no Postgres on a Windows host each refused connect takes ~2 s, so the
    # thread outlived this test and ran into the next test's SQLite engine as it was disposed (access violation).
    ticks = []
    monkeypatch.setattr(schedules, "claim_due", lambda: ticks.append(1) or [])
    for name in ("housekeeping", "nightly_calibration", "nightly_verification_sweep", "nightly_audit_chain",
                 "nightly_desk_value"):
        monkeypatch.setattr(schedules, name, lambda: None)
    stop = schedules.start_inprocess_scheduler(poll_seconds=0.01)
    try:
        _until(lambda: len(ticks) >= 3)
    finally:
        stop.set()
        for t in [t for t in threading.enumerate() if t.name == "inprocess-scheduler"]:
            t.join(timeout=5)
    assert not [t for t in threading.enumerate() if t.name == "inprocess-scheduler"]

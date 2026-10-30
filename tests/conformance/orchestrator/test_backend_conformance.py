"""ORC-T2 (ADR-015): every in-process orchestrator backend behaves the same over the engine contract.

Parameterised over `local` and `langgraph` (`backend` fixture). Outcomes: done, fail, cancel, limited,
already terminal, replan; waits: approval park + signal, pause/resume, the sweep, a signal racing the park
decision; restart with orphaned claims; the parallel bound; the synchronous drive's busy-time cap.
LangGraph-only properties (tenant bound in every node, no checkpointer, the recursion bound, the tracing
guard) are at the end."""
from __future__ import annotations

import threading
import time

import pytest
from tests.conformance.orchestrator.harness import driver_threads, until

from analystos.workflows import orchestrator as orch


# ------------------------------------------------------------------------------------------ outcomes
def test_a_run_completes_and_is_finished_once(fake, make_runtime):
    fake.add_run("r1", {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    rt = make_runtime(2)
    assert rt.drive("r1", park=True) == "COMPLETED"
    assert fake.runs["r1"]["finished"] == [("COMPLETED", None)]
    assert all(t["status"] == "COMPLETED" for t in fake.runs["r1"]["tasks"].values())
    assert fake.ops("plan_run", "r1")  # the drive plans first (idempotent)


def test_a_required_task_failure_fails_the_run_with_its_cause(fake, make_runtime):
    fake.add_run("r2", {"a": [], "b": ["a"]})
    fake.hooks["a"] = lambda *a: {"fail": "policy refused"}
    rt = make_runtime(2)
    assert rt.drive("r2", park=True) == "FAILED"
    assert fake.runs["r2"]["finished"] == [("FAILED", "required task(s) failed: a")]
    assert fake.runs["r2"]["tasks"]["b"]["status"] == "NEW"  # nothing after the failure ran


def test_an_optional_task_failure_does_not_fail_the_run(fake, make_runtime):
    fake.add_run("r3", {"a": [], "b": []}, optional=("b",))
    fake.hooks["b"] = lambda *a: {"fail": "no data"}
    assert make_runtime(2).drive("r3", park=True) == "COMPLETED"


def test_cancel_finishes_the_run_cancelled(fake, make_runtime):
    fake.add_run("r4", {"a": [], "b": ["a"]})
    fake.runs["r4"]["control"] = "cancel"
    assert make_runtime(2).drive("r4", park=True) == "CANCELLED"
    assert fake.runs["r4"]["finished"] == [("CANCELLED", None)]
    assert not fake.ops("execute_task", "r4")


def test_cancel_lands_while_tasks_run(fake, make_runtime):
    fake.add_run("r5", {"a": [], "b": ["a"]})

    def cancels(run_id, key, claim):
        fake.runs[run_id]["control"] = "cancel"
        return {}

    fake.hooks["a"] = cancels
    assert make_runtime(2).drive("r5", park=True) == "CANCELLED"
    assert fake.runs["r5"]["tasks"]["a"]["status"] == "COMPLETED"  # the started task's result landed first
    assert fake.runs["r5"]["tasks"]["b"]["status"] == "NEW"


def test_a_used_up_budget_ends_with_limitations(fake, make_runtime):
    fake.add_run("r6", {"a": [], "b": ["a"], "c": ["b"]}, max_steps=2)
    assert make_runtime(2).drive("r6", park=True) == "COMPLETED_WITH_LIMITATIONS"
    outcome, error = fake.runs["r6"]["finished"][0]
    assert outcome == "COMPLETED_WITH_LIMITATIONS" and error.startswith("max_steps")
    assert fake.runs["r6"]["tasks"]["c"]["status"] == "NEW"


def test_an_expired_wall_clock_budget_ends_expired(fake, make_runtime):
    fake.add_run("r7", {"a": []})
    real = fake.get_state

    def expired(run_id):
        state = real(run_id)
        return {"limited": "max_duration_s (60 of 60)", "expired": True} if state.get("ready") else state

    fake.get_state = expired
    orch.engine.get_state = expired
    assert make_runtime(2).drive("r7", park=True) == "EXPIRED"
    assert fake.runs["r7"]["finished"] == [("EXPIRED", "max_duration_s (60 of 60)")]


def test_an_already_terminal_run_is_returned_not_finished_again(fake, make_runtime):
    fake.add_run("r8", {"a": []})
    fake.runs["r8"]["status"] = "FAILED"
    assert make_runtime(2).drive("r8", park=True) == "FAILED"
    assert fake.runs["r8"]["finished"] == [] and not fake.ops("execute_task", "r8")


def test_a_replan_request_plans_again_then_continues(fake, make_runtime):
    fake.add_run("r9", {"a": [], "b": ["a"]})

    def asks_for_replan(run_id, key, claim):
        fake.runs[run_id]["needs_plan"] = True  # e.g. feedback reset the plan while it ran
        return {}

    fake.hooks["a"] = asks_for_replan
    assert make_runtime(2).drive("r9", park=True) == "COMPLETED"
    assert fake.runs["r9"]["plans"] == 2


# ------------------------------------------------------------------------------------------ waits
def test_a_run_waiting_for_approval_parks_and_completes_after_the_signal(fake, make_runtime):
    fake.add_run("w1", {"plan": [], "gate": ["plan"], "publish": ["gate"]}, gate="gate")
    rt = make_runtime(2)
    rt.submit("w1")
    until(lambda: fake.status("w1") == "WAITING_USER" and not rt.active())
    until(lambda: not driver_threads("w1"))  # no thread is held while the run waits for a person
    calls = len(fake.ops("get_state", "w1"))
    time.sleep(0.05)
    assert len(fake.ops("get_state", "w1")) == calls  # and nothing polls it
    fake.runs["w1"]["approved"] = True  # the approval lands; no clock involved
    rt.submit("w1")  # signal_run
    until(lambda: fake.status("w1") == "COMPLETED")
    assert fake.runs["w1"]["tasks"]["publish"]["status"] == "COMPLETED"


def test_pause_parks_and_resume_redrives(fake, make_runtime):
    fake.add_run("w2", {"a": [], "b": ["a"]})
    fake.runs["w2"]["control"] = "pause"
    rt = make_runtime(2)
    rt.submit("w2")
    until(lambda: fake.status("w2") == "PAUSED" and not rt.active())
    assert not fake.ops("execute_task", "w2")
    fake.runs["w2"]["control"] = "run"
    rt.submit("w2")
    until(lambda: fake.status("w2") == "COMPLETED")


def test_drive_returns_the_park_outcome(fake, make_runtime):
    fake.add_run("w3", {"gate": []}, gate="gate")
    rt = make_runtime(2)
    assert rt.drive("w3", park=True) == "WAITING_USER"
    fake.runs["w3"]["control"] = "pause"
    assert rt.drive("w3", park=True) == "PAUSED"


def test_a_signal_racing_the_park_decision_is_not_lost(fake, make_runtime, monkeypatch):
    fake.add_run("w4", {"plan": [], "gate": ["plan"]}, gate="gate")
    rt = make_runtime(1)
    fired = []
    real = fake.get_state

    def get_state(run_id):  # the approval and its signal land between the state read and the park
        state = real(run_id)
        if state.get("waiting_user") and not fired:
            fired.append(1)
            fake.runs[run_id]["approved"] = True
            rt.submit(run_id)
        return state

    monkeypatch.setattr(orch.engine, "get_state", get_state)
    rt.submit("w4")
    until(lambda: fake.status("w4") == "COMPLETED")
    assert fired
    until(lambda: not rt.active())


def test_the_sweep_redrives_parked_runs_decided_elsewhere(fake, make_runtime):
    fake.add_run("w5", {"plan": [], "gate": ["plan"]}, gate="gate")
    rt = make_runtime(2)
    rt.submit("w5")
    until(lambda: fake.status("w5") == "WAITING_USER" and not rt.active())
    fake.runs["w5"]["approved"] = True  # decided by another process: no signal reaches this one
    assert rt.sweep() == ["w5"]
    until(lambda: fake.status("w5") == "COMPLETED")


def test_submit_is_idempotent_one_driver_per_run(fake, make_runtime):
    fake.add_run("w6", {"a": []})
    release = threading.Event()
    fake.hooks["a"] = lambda *a: release.wait(5) and {}
    rt = make_runtime(2)
    for _ in range(5):
        rt.submit("w6")
    until(lambda: fake.runs["w6"]["tasks"]["a"]["status"] == "RUNNING")
    assert len(driver_threads("w6")) == 1
    release.set()
    until(lambda: fake.status("w6") == "COMPLETED")
    assert len(fake.ops("execute_task", "w6")) == 1


def test_a_synchronous_drive_polls_a_wait_that_does_not_count_as_busy(fake, make_runtime):
    fake.add_run("w7", {"plan": [], "gate": ["plan"], "publish": ["gate"]}, gate="gate")
    now = [0.0]

    def tick(run_id):  # every poll while waiting is ten simulated minutes; approved after two hours
        if fake.runs[run_id]["status"] == "WAITING_USER":
            now[0] += 600
            if now[0] >= 7200:
                fake.runs[run_id]["approved"] = True

    fake.on_state = tick
    rt = make_runtime(None, poll=0.0, clock=lambda: now[0], sleep=lambda s: None)
    assert rt.drive("w7", park=False, max_seconds=3600) == "COMPLETED"
    assert now[0] >= 7200


def test_busy_time_past_the_cap_fails_the_synchronous_drive(fake, make_runtime):
    fake.add_run("w8", {"a": [], "b": ["a"]})
    now = [0.0]
    fake.hooks["a"] = lambda *a: now.__setitem__(0, now[0] + 4000) or {}
    rt = make_runtime(None, poll=0.0, clock=lambda: now[0], sleep=lambda s: None)
    assert rt.drive("w8", park=False, max_seconds=3600) == "FAILED"
    assert fake.runs["w8"]["finished"][0][1].endswith("orchestrator timeout")


# ------------------------------------------------------------------------------------------ restart, bounds
def test_a_process_killed_mid_run_is_resumed_and_the_dead_attempt_is_superseded(fake, make_runtime):
    fake.add_run("k1", {"metadata": [], "analyse": ["metadata"]})
    release, started = threading.Event(), threading.Event()

    def dies(run_id, key, claim):  # the first process hangs inside the task, then is "killed"
        if claim == 1:
            started.set()
            release.wait(5)
            return {"by": "dead process"}
        return {"by": "resumed process"}

    fake.hooks["metadata"] = dies
    first = make_runtime(2)
    first.submit("k1")
    assert started.wait(5)
    second = make_runtime(2)  # restart: a new process; the old one no longer drives anything
    first._stop.set()
    assert second.resume() == ["k1"]
    until(lambda: fake.status("k1") == "COMPLETED")
    release.set()  # the dead attempt's late result must not overwrite the resumed one
    time.sleep(0.05)
    assert fake.runs["k1"]["tasks"]["metadata"]["output"] == {"by": "resumed process"}


def test_ready_tasks_run_in_parallel_on_the_bounded_pool(fake, make_runtime):
    fake.add_run("p1", {f"t{i}": [] for i in range(6)})
    fake.add_run("p2", {f"t{i}": [] for i in range(6)})
    live, peak, lock = [0], [0], threading.Lock()

    def work(run_id, key, claim):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.03)
        with lock:
            live[0] -= 1
        return {"by": threading.current_thread().name}

    for i in range(6):
        fake.hooks[f"t{i}"] = work
    rt = make_runtime(2)
    rt.submit("p1")
    rt.submit("p2")  # two runs share the one pool: the bound is global
    until(lambda: fake.status("p1") == fake.status("p2") == "COMPLETED")
    assert peak[0] == 2
    assert all(t["output"]["by"].startswith("local-task") for r in ("p1", "p2") for t in fake.runs[r]["tasks"].values())


# ------------------------------------------------------------------------------------------ langgraph only
@pytest.fixture
def langgraph_runtime(backend, make_runtime):
    if backend != "langgraph":
        pytest.skip("LangGraph backend property")
    return make_runtime


def test_every_langgraph_node_runs_as_the_runs_tenant(fake, langgraph_runtime):
    fake.add_run("g1", {"a": [], "b": ["a"], "c": ["a"]}, tenant="t-acme")
    fake.add_run("g2", {"a": []}, tenant="t-globex")
    rt = langgraph_runtime(2)
    rt.submit("g1")
    rt.submit("g2")
    until(lambda: fake.status("g1") == fake.status("g2") == "COMPLETED")
    for op in ("plan_run", "get_state", "execute_task", "finish_run"):
        for call in fake.ops(op, "g1"):
            assert call[-1] == "t-acme", call
        for call in fake.ops(op, "g2"):
            assert call[-1] == "t-globex", call


def test_the_graph_has_no_checkpointer_and_a_fixed_shape(fake, langgraph_runtime):
    from analystos.workflows.langgraph_backend import NODES

    rt = langgraph_runtime(2)
    assert rt.graph.checkpointer is None  # Postgres is the only run-state store (ADR-015 §2)
    assert set(NODES) <= set(rt.graph.get_graph().nodes)
    assert "state" in rt.mermaid()


def test_a_long_run_reinvokes_at_the_recursion_bound_without_losing_state(fake, langgraph_runtime):
    chain = {f"s{i}": ([f"s{i - 1}"] if i else []) for i in range(30)}  # 30 sequential steps ~ 60 supersteps
    fake.add_run("g3", chain)
    rt = langgraph_runtime(2, recursion_limit=20)
    assert rt.drive("g3", park=True) == "COMPLETED"
    assert rt.reinvocations >= 2
    assert all(t["status"] == "COMPLETED" for t in fake.runs["g3"]["tasks"].values())
    assert sorted(c[2] for c in fake.ops("execute_task", "g3")) == sorted(chain)  # none ran twice across invocations


def test_tracing_is_refused_without_the_operators_opt_in(monkeypatch):
    from analystos.core.config import Settings
    from analystos.core.errors import FeatureUnavailable
    from analystos.workflows.backends import validate

    s = Settings(_env_file=None, orchestrator="langgraph")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    with pytest.raises(FeatureUnavailable, match="LANGCHAIN_TRACING_V2"):
        validate(s)
    validate(Settings(_env_file=None, orchestrator="langgraph", langgraph_allow_tracing=True))
    monkeypatch.delenv("LANGCHAIN_TRACING_V2")
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    with pytest.raises(FeatureUnavailable):
        validate(s)
    monkeypatch.delenv("LANGSMITH_TRACING")
    validate(s)  # nothing on: no data leaves, the backend starts


def test_an_invocation_runs_with_tracing_disabled(fake, langgraph_runtime, monkeypatch):
    seen = []

    def spy(run_id, key, claim):
        from langsmith.run_helpers import get_tracing_context

        seen.append(get_tracing_context().get("enabled"))
        return {}

    fake.add_run("g4", {"a": []})
    fake.hooks["a"] = spy
    rt = langgraph_runtime(None)  # inline: the task runs inside the node, in the invocation's context
    assert rt.drive("g4", park=True) == "COMPLETED"
    assert seen == [False]


def test_start_and_signal_go_through_the_langgraph_backend(fake, langgraph_runtime, monkeypatch):
    from analystos.core.config import get_settings

    monkeypatch.setattr(get_settings(), "orchestrator", "langgraph")
    rt = langgraph_runtime(2)
    previous = orch.reset_local_runtime(rt)
    try:
        fake.add_run("g5", {"plan": [], "gate": ["plan"]}, gate="gate")
        assert orch.start_run("g5") == "langgraph-g5"
        until(lambda: fake.status("g5") == "WAITING_USER" and not rt.active())
        fake.runs["g5"]["approved"] = True
        orch.signal_run("g5")
        until(lambda: fake.status("g5") == "COMPLETED")
    finally:
        orch.reset_local_runtime(previous)

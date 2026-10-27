"""P4-S02: optimistic task claims against Postgres. Claimers race on one task from separate threads
(separate connections); every one reads the row before any of them writes, so each compare-and-set
competes on the same claim version. Exactly one wins, whether the task is NEW, holds an expired
claim or a claim released by `release_timed_out_claim` (a Temporal retry)."""
from __future__ import annotations

import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

pytestmark = pytest.mark.integration
CLAIMERS = 6


@pytest.fixture()
def task(control_db):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunTask, User, Workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws, run = new_id("ws"), new_id("run")
        s.add(Workspace(id=ws, name="claims", created_by=admin.id))
        s.flush()
        s.add(AnalysisRun(id=run, workspace_id=ws, objective="claims", status="RUNNING", plan={"steps": []}, plan_version=1,
                          scope={"hash": "h"}, instructions=[], constraints={}, control="run", requested_by=admin.id,
                          summary={}, origin={}))
        s.flush()
        s.add(RunTask(id=new_id("tsk"), run_id=run, key="work", agent_id="a", title="work", status="NEW", depends_on=[],
                      input={}, output={}, plan_version=1, seq=0))
    return SimpleNamespace(run=run, key="work")


def _set(run_id: str, key: str, **values) -> None:
    from analystos.db.base import session_scope
    from analystos.db.models import RunTask

    with session_scope() as s:
        t = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))
        for k, v in values.items():
            setattr(t, k, v)


def _get(run_id: str, key: str):
    from analystos.db.base import session_scope
    from analystos.db.models import RunTask

    with session_scope() as s:
        return s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))


def _race(fn, n: int = CLAIMERS) -> list:
    """Run `fn` in n threads; hold every thread's first UPDATE of run_task until all n got there."""
    from sqlalchemy.engine import Engine

    barrier = threading.Barrier(n, timeout=20)
    held: set[int] = set()

    def hold(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        me = threading.get_ident()
        if statement.startswith("UPDATE run_task") and me in racers and me not in held:
            held.add(me)
            barrier.wait()

    results: list = [None] * n
    racers: set[int] = set()

    def worker(i: int) -> None:
        racers.add(threading.get_ident())
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - reported below
            results[i] = exc

    event.listen(Engine, "before_cursor_execute", hold)
    try:
        from analystos.tenancy.background import carry_context  # racers act for the test's tenant

        threads = [threading.Thread(target=carry_context(worker), args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        event.remove(Engine, "before_cursor_execute", hold)
    assert not [r for r in results if isinstance(r, Exception)], results
    return results


@pytest.mark.parametrize("prior", ["new", "expired_claim", "released_claim"])
def test_concurrent_claimers_exactly_one_wins(task, prior):
    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.runtime import engine
    from analystos.workflows.activities import release_timed_out_claim

    if prior == "expired_claim":
        _set(task.run, task.key, status="RUNNING", attempts=1, claim_version=1,
             started_at=utcnow() - timedelta(seconds=engine.CLAIM_TTL_SECONDS + 5))
    elif prior == "released_claim":
        _set(task.run, task.key, status="RUNNING", attempts=1, claim_version=1, started_at=utcnow())
        assert release_timed_out_claim(task.run, task.key, 2)
    before = _get(task.run, task.key)

    def claim():
        with session_scope() as s:
            return engine._claim(s, task.run, task.key)

    results = _race(claim)
    winners = [r for r in results if "claim" in r]
    assert len(winners) == 1, results
    assert all(r == {"status": "in_progress"} for r in results if "claim" not in r)
    after = _get(task.run, task.key)
    assert (after.status, after.claim_version, after.attempts) == ("RUNNING", before.claim_version + 1, before.attempts + 1)
    assert winners[0]["claim"] == after.claim_version


def test_concurrent_execute_task_runs_the_agent_once(task, monkeypatch):
    from analystos.agents import dispatch as dispatch_mod
    from analystos.runtime import engine

    calls: list[str] = []
    monkeypatch.setattr(dispatch_mod, "dispatch", lambda ctx: calls.append(ctx.key) or {"ok": True})
    monkeypatch.setattr(engine, "RunContext", SimpleNamespace(load=lambda run_id, key, services: SimpleNamespace(key=key)))

    results = _race(lambda: engine.execute_task(task.run, task.key, services=object()))
    assert calls == [task.key]
    assert sorted(r["status"] for r in results) == ["COMPLETED"] + ["in_progress"] * (CLAIMERS - 1)
    done = _get(task.run, task.key)
    assert (done.status, done.attempts, done.claim_version, done.output) == ("COMPLETED", 1, 1, {"ok": True})


def test_superseded_attempt_cannot_overwrite_the_retry(task, monkeypatch):
    """Attempt 1 hangs past its timeout; Temporal's retry releases and retakes the claim and completes.
    When attempt 1 finally returns, its result is dropped."""
    from analystos.agents import dispatch as dispatch_mod
    from analystos.runtime import engine
    from analystos.workflows.activities import release_timed_out_claim

    def first_attempt_hangs(ctx):
        if not getattr(first_attempt_hangs, "nested", False):
            first_attempt_hangs.nested = True
            assert release_timed_out_claim(task.run, task.key, 2)
            assert engine.execute_task(task.run, task.key, services=object())["status"] == "COMPLETED"
            return {"from": "attempt 1"}
        return {"from": "attempt 2"}

    monkeypatch.setattr(dispatch_mod, "dispatch", first_attempt_hangs)
    monkeypatch.setattr(engine, "RunContext", SimpleNamespace(load=lambda run_id, key, services: SimpleNamespace(key=key)))
    assert engine.execute_task(task.run, task.key, services=object()) == {"status": "superseded"}
    done = _get(task.run, task.key)
    assert (done.status, done.output, done.attempts, done.claim_version) == ("COMPLETED", {"from": "attempt 2"}, 2, 2)

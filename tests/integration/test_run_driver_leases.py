"""ORC-T7 (ADR-015 phase 2) against Postgres: dedicated driver processes with run leases.

* two `RunDriver` processes share twelve runs: every run is driven to completion, and no two drivers ever
  drive the same run at the same time (the drive logs every step, so an overlap would show);
* a driver killed while it drives a run loses nothing: once its lease expires the other driver takes the run
  over (a new epoch, `took_over`) and completes it; the dead driver's steps all happened before the takeover;
* a wake during a drive is not lost when the run parks; a parked run is not taken again without one;
* a driver whose renewal finds the lease taken over stops deciding (fencing by epoch).

The drive is a probe (tests/driver_probe.py); claims, renewals, releases, takeovers and the heartbeat rows are
the real `LeaseStore` statements. Migration 0076 (run_lease, run_driver) up/down/up is checked too."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from analystos.core.ids import new_id

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
LEASE = 3.0


@pytest.fixture()
def db(control_db):
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        s.execute(text("DELETE FROM run_lease"))
        s.execute(text("DELETE FROM run_driver"))
        s.execute(text("UPDATE analysis_run SET status = 'CANCELLED' WHERE status NOT IN "
                       "('COMPLETED','FAILED','REJECTED','CANCELLED','COMPLETED_WITH_LIMITATIONS','EXPIRED')"))
    return control_db


def _runs(objectives: list[str]) -> list[str]:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, User, Workspace
    from analystos.tenancy import system_scope

    ids = []
    with system_scope(), session_scope() as s:
        user = s.query(User).order_by(User.email).first()
        ws = Workspace(id=new_id("ws"), name=f"orc-t7-{new_id('x')}", created_by=user.id)
        s.add(ws)
        s.flush()
        for objective in objectives:
            run = AnalysisRun(id=new_id("run"), workspace_id=ws.id, objective=objective, status="NEW", requested_by=user.id)
            s.add(run)
            ids.append(run.id)
    return ids


def _status(run_id: str) -> str:
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        return s.execute(text("SELECT status FROM analysis_run WHERE id = :id"), {"id": run_id}).scalar()


def _spawn(tmp_path: Path, name: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    proc = subprocess.Popen([sys.executable, "-m", "tests.driver_probe", str(tmp_path), name, str(LEASE)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=open(tmp_path / f"{name}.err", "w"))  # noqa: SIM115
    deadline = time.monotonic() + 60
    while not (tmp_path / f"{name}.ready").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError((tmp_path / f"{name}.err").read_text())
        time.sleep(0.05)
    return proc


def _events(tmp_path: Path) -> list[dict]:
    out = []
    for f in tmp_path.glob("*.jsonl"):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return sorted(out, key=lambda e: e["t"])


def _until(cond, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.1)


def _intervals(events: list[dict]) -> dict[str, list[tuple[str, float, float]]]:
    """run -> [(driver, first step time, last step time)] for every drive of it."""
    out: dict[str, dict[tuple, list[float]]] = {}
    for e in events:
        if e["event"] in ("start", "step", "end", "lost"):
            out.setdefault(e["run"], {}).setdefault((e["driver"], e["pid"]), []).append(e["t"])
    return {run: sorted(((d[0], min(ts), max(ts)) for d, ts in drives.items()), key=lambda x: x[1])
            for run, drives in out.items()}


def test_two_driver_processes_share_runs_without_overlap_and_take_over_a_dead_one(db, tmp_path):
    from analystos.workflows.driver import live_drivers, wake

    runs = _runs([f"quick {i}" for i in range(10)] + ["slow A", "slow B"])
    for run_id in runs:
        wake(run_id)
    a, b = _spawn(tmp_path, "a"), _spawn(tmp_path, "b")
    try:
        _until(lambda: len(live_drivers(max_age_seconds=LEASE)) == 2, 20)
        # wait until each slow run is being driven, then kill the driver holding the first one
        slow = runs[-2:]
        _until(lambda: all(any(e["run"] == r and e["event"] == "step" for e in _events(tmp_path)) for r in slow), 30)
        victim_name = next(e["driver"] for e in _events(tmp_path) if e["run"] == slow[0] and e["event"] == "start")
        victim = a if victim_name == "a" else b
        killed_at = time.time()
        victim.kill()
        victim.wait(10)
        _until(lambda: all(_status(r) == "COMPLETED" for r in runs), 90)
    finally:
        for p in (a, b):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()

    events = _events(tmp_path)
    survivor = "b" if victim_name == "a" else "a"
    for run, drives in _intervals(events).items():
        for (d1, _s1, e1), (d2, s2, _e2) in zip(drives, drives[1:], strict=False):
            assert e1 < s2, f"{run}: {d1} and {d2} drove it at the same time"
    # the killed driver's run was taken over by the survivor after its lease expired
    takeover = [e for e in events if e["run"] == slow[0] and e["driver"] == survivor and e["event"] == "start"]
    assert takeover and takeover[0]["t"] >= killed_at
    assert [e for e in events if e["run"] == slow[0] and e["driver"] == survivor and e["event"] == "end"]
    # every run ended exactly once
    ends = [e["run"] for e in events if e["event"] == "end"]
    assert sorted(ends) == sorted(runs)
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        epoch = s.execute(text("SELECT epoch FROM run_lease WHERE run_id = :id"), {"id": slow[0]}).scalar()
        left = s.execute(text("SELECT count(*) FROM run_lease WHERE run_id = ANY(:ids)"), {"ids": runs}).scalar()
    assert epoch is None and left == 0  # completed runs leave no lease behind


def test_a_wake_during_a_drive_is_kept_and_a_parked_run_is_not_retaken_without_one(db):
    from analystos.workflows.driver import LeaseStore, wake

    store = LeaseStore()
    (run_id,) = _runs(["parks"])
    wake(run_id)
    [claim] = store.claim("d1", 10, LEASE, sweep_seconds=0)
    assert claim.run_id == run_id and not claim.took_over
    assert store.claim("d2", 10, LEASE, sweep_seconds=0) == []  # held: nobody else may take it
    wake(run_id)  # an approval lands while d1 drives
    assert store.release("d1", run_id, claim.epoch, parked=True, terminal=False)
    [again] = store.claim("d2", 10, LEASE, sweep_seconds=0)  # the wake keeps it due after the park
    assert again.epoch == claim.epoch + 1
    assert store.release("d2", run_id, again.epoch, parked=True, terminal=False)
    assert store.claim("d1", 10, LEASE, sweep_seconds=0) == []  # parked, no wake, no sweep: stays parked
    assert [c.run_id for c in store.claim("d1", 10, LEASE, sweep_seconds=0.001)] == [run_id]  # the sweep re-drives it


def test_a_driver_whose_lease_was_taken_over_stops_deciding(db):
    from analystos.workflows.driver import LeaseStore, RunDriver, wake

    store = LeaseStore()
    (run_id,) = _runs(["fenced"])
    wake(run_id)
    old = RunDriver(store=store, driver_id="old", lease_seconds=LEASE, drive=lambda *a, **k: "COMPLETED", backend="local")
    [claim] = store.claim("old", 1, LEASE, 0)
    old._held[run_id] = __import__("analystos.workflows.driver", fromlist=["_Held"])._Held(claim.epoch, old.clock() + 60)
    assert old.holds(run_id)
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:  # the lease expires (the old driver stalled)
        s.execute(text("UPDATE run_lease SET expires_at = now() - interval '1 second' WHERE run_id = :id"), {"id": run_id})
    [new] = store.claim("new", 1, LEASE, 0)
    assert new.took_over and new.epoch == claim.epoch + 1
    old.renew()
    assert not old.holds(run_id)  # its renewal found the lease gone: keep_going() is now False
    assert not store.release("old", run_id, claim.epoch, parked=False, terminal=True)  # and it cannot release it


def test_migration_0076_up_down_up(db):
    from alembic import command
    from alembic.config import Config

    from analystos.db.base import get_engine

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with get_engine().begin() as c:
        c.execute(text("DROP TABLE IF EXISTS alembic_version"))
        c.execute(text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('0076_run_leases')"))
    command.downgrade(cfg, "0075_pilot_review_subjects")
    with get_engine().connect() as c:
        assert c.execute(text("SELECT to_regclass('run_lease')")).scalar() is None
    command.upgrade(cfg, "0076_run_leases")
    with get_engine().connect() as c:
        assert c.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'run_lease'")).scalar() is True
        assert c.execute(text("SELECT to_regclass('run_driver')")).scalar() is not None

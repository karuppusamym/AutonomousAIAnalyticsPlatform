"""P7-16 (ADR-0025) against Postgres, with no Redis and no Temporal in the path:

* a run waiting two hours for plan approval holds no thread and completes once the approval lands
  through the HTTP API (the approval signal re-drives it);
* an API process killed while a task runs: the next process resumes the run from its Postgres task
  state and completes it; the dead attempt's late result is superseded, not written;
* hard spend caps hold with the Postgres store (competing reservations never overshoot; UTC day
  rollover; seeding from model_call; refusal with an admin alert; fail closed on a database error);
* migration 0035 (spend_counter) up/down/up.

CI runs this file in the `lite` job (no Redis service, ANALYSTOS_PROFILE=lite) and in the standard job."""
from __future__ import annotations

import shutil
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn
import yaml
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db import vectors

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
PLAYBOOK = {
    "apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.lite_probe", "version": "0.1.0",
    "summary": "Wait for plan approval, collect metadata, write the data dictionary", "determinism": "model",
    "side_effect": "write_internal", "certification": {"status": "tested", "evidence": "tests/integration/test_lite_profile_db.py"},
    "spec": {"framing": False, "steps": [
        {"key": "plan_approval", "use": "agent.supervisor", "behaviour": "plan_approved", "title": "Wait for plan approval",
         "type": "approval_gate", "payload": "plan", "when": "run.autonomy_level <= 2", "gates_roots": True},
        {"key": "metadata", "use": "agent.metadata", "title": "Collect metadata"},
        {"key": "dictionary", "use": "agent.data_dictionary", "title": "Write the data dictionary", "after": ["metadata"]}]}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servicenow_url():
    from analystos.connectors.servicenow_mock import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def runtime(monkeypatch):
    """This process's local orchestrator: a bounded pool, as in the lite profile."""
    from analystos.core.config import get_settings
    from analystos.workflows import orchestrator as orch

    monkeypatch.setattr(get_settings(), "orchestrator", "local")
    rt = orch.LocalRuntime(2, poll=0.05)
    previous = orch.reset_local_runtime(rt)
    yield rt
    orch.reset_local_runtime(previous)
    rt.shutdown()


@pytest.fixture
def world(api, servicenow_url, runtime, tmp_path, monkeypatch):
    from analystos.capabilities import packs as domain_packs
    from analystos.capabilities import registry
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.runtime.context import default_router
    from analystos.services.sources import discover_source, register_source, select_assets

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # every agent takes its deterministic path
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    default_router.cache_clear()
    root = tmp_path / "packs"
    shutil.copytree(registry.PACKS_DIR, root)
    (root / "lite_probe").mkdir()
    (root / "lite_probe" / "playbook.yaml").write_text(yaml.safe_dump(PLAYBOOK), encoding="utf-8")
    monkeypatch.setattr(registry, "PACKS_DIR", root)
    registry.reload()
    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    ws = api.post("/api/workspaces", headers=analyst, json={"name": f"lite {new_id('w')}",
                                                            "objective": "Document the incident table"}).json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    assert api.put(f"/api/workspaces/{ws}/capabilities/playbook.lite_probe", headers=analyst,
                   json={"enabled": True}).status_code == 200
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, owner, ws, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        src_id = src.id
        s.expunge(owner)
    discover_source(owner, src_id)
    select_assets(owner, src_id, ["incident"])
    yield {"ws": ws, "analyst": analyst, "approver": approver}
    monkeypatch.undo()
    registry.reload()
    domain_packs.reset()
    default_router.cache_clear()


def _start(api, world) -> str:
    r = api.post(f"/api/workspaces/{world['ws']}/analysis", headers=world["analyst"],
                 json={"objective": "Document the incident table", "playbook": "playbook.lite_probe", "autonomy_level": 2})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _status(run_id: str) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    with session_scope() as s:
        return s.get(AnalysisRun, run_id).status


def _until(cond, timeout: float = 180.0) -> None:
    started = time.monotonic()
    while not cond():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached in time")
        time.sleep(0.1)


def _pending_approval(run_id: str) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import Approval

    with session_scope() as s:
        return s.scalar(select(Approval.id).where(Approval.run_id == run_id, Approval.status == "pending"))


def test_a_run_waiting_two_hours_for_approval_completes_after_approval(api, world, runtime):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Approval, RunTask

    run_id = _start(api, world)
    _until(lambda: _status(run_id) == "WAITING_USER")
    _until(lambda: run_id not in runtime.active())
    assert not [t for t in threading.enumerate() if t.name == f"run-{run_id}"]  # no thread held while waiting
    approval_id = _pending_approval(run_id)
    with session_scope() as s:  # two hours pass (the old loop failed every run after one hour, waiting included)
        run = s.get(AnalysisRun, run_id)
        run.started_at = run.started_at - timedelta(hours=2)
        apr = s.get(Approval, approval_id)
        apr.created_at = apr.created_at - timedelta(hours=2)
    assert _status(run_id) == "WAITING_USER"
    r = api.post(f"/api/approvals/{approval_id}/approve", headers=world["approver"], json={})
    assert r.status_code == 200, r.text  # the approval signal re-drives the parked run
    _until(lambda: _status(run_id) in ("COMPLETED", "FAILED"))
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        assert run.status == "COMPLETED", run.error
        tasks = {t.key: t.status for t in s.scalars(select(RunTask).where(RunTask.run_id == run_id))}
    assert tasks == {"plan_approval": "COMPLETED", "metadata": "COMPLETED", "dictionary": "COMPLETED"}


def test_killing_the_api_mid_run_and_restarting_completes_the_run(api, world, runtime, monkeypatch):
    from analystos.agents import dispatch as dispatch_mod
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunTask
    from analystos.workflows import orchestrator as orch

    real = dispatch_mod.dispatch
    started, release, calls = threading.Event(), threading.Event(), []

    def dispatch(ctx):
        if ctx.task.key == "metadata" and not calls:
            calls.append(1)
            started.set()  # the first process dies while this task runs
            release.wait(60)
            return {"written_by": "the dead process"}
        return real(ctx)

    monkeypatch.setattr(dispatch_mod, "dispatch", dispatch)
    run_id = _start(api, world)
    _until(lambda: _status(run_id) == "WAITING_USER")
    assert api.post(f"/api/approvals/{_pending_approval(run_id)}/approve", headers=world["approver"], json={}).status_code == 200
    assert started.wait(120)
    with session_scope() as s:
        held = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "metadata"))
        assert held.status == "RUNNING" and held.claim_version == 1

    # Restart: a new process starts, the killed one never drives this run again.
    runtime._stop.set()
    fresh = orch.LocalRuntime(2, poll=0.05)
    orch.reset_local_runtime(fresh)
    try:
        assert fresh.resume([run_id]) == [run_id]
        _until(lambda: _status(run_id) in ("COMPLETED", "FAILED"))
        release.set()  # the dead attempt finally returns: its claim is stale, its result is dropped
        time.sleep(1.0)
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            assert run.status == "COMPLETED", run.error
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "metadata"))
            assert task.status == "COMPLETED" and task.claim_version >= 2
            assert task.output.get("written_by") != "the dead process"
    finally:
        release.set()
        fresh.shutdown()


# ------------------------------------------------------------------------------------ spend caps without Redis
@pytest.fixture
def pg_counters(control_db, monkeypatch):
    from analystos.runtime import budget_counters as bc

    c = bc.BudgetCounters("", f"aostest:{new_id('pg')}:")  # no Redis URL: the Postgres store
    assert c.spend_store == "postgres" and c.pg is not None
    monkeypatch.setattr(bc, "default_budget_counters", lambda: c)
    return c


def _day_cap(c, limit, seed=0.0):
    from analystos.runtime.budget_counters import CapSpec

    return [CapSpec("platform_daily", c.platform_day_key(), limit, 600, lambda: seed)]


def test_competing_reservations_on_postgres_never_overshoot(pg_counters):
    from analystos.runtime.budget_counters import CapExceeded

    c = pg_counters
    granted, refused, lock = [], [], threading.Lock()
    start = threading.Barrier(20)

    def call():
        start.wait()
        for _ in range(10):
            try:
                r = c.reserve(_day_cap(c, 1.0), 0.03)
                with lock:
                    granted.append(r)
            except CapExceeded:
                with lock:
                    refused.append(1)

    threads = [threading.Thread(target=call) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(granted) == 33 and len(refused) == 200 - 33
    assert c.value(c.platform_day_key()) <= 1.0 + 1e-9
    assert all(r.store == "postgres" for r in granted)
    for r in granted:  # every call settles to its actual cost (a third of the estimate)
        c.settle(r, 0.01)
    assert c.value(c.platform_day_key()) == pytest.approx(0.33)
    released = c.reserve(_day_cap(c, 1.0), 0.5)
    c.settle(released, 0.0)
    assert c.value(c.platform_day_key()) == pytest.approx(0.33)


def test_utc_day_rollover_on_postgres(pg_counters):
    from analystos.runtime.budget_counters import CapExceeded

    c = pg_counters
    now = [datetime(2026, 9, 26, 23, 59, 59, tzinfo=UTC)]
    c.clock = lambda: now[0]
    c.reserve(_day_cap(c, 0.1, seed=0.08), 0.02)
    with pytest.raises(CapExceeded):
        c.reserve(_day_cap(c, 0.1), 0.01)
    now[0] += timedelta(seconds=2)
    assert c.reserve(_day_cap(c, 0.1), 0.01).after["platform_daily"] == pytest.approx(0.01)


@pytest.fixture
def spend_tag(control_db):
    """A purpose tag whose model_call rows are removed afterwards: the platform day total is shared by
    every later test of the session (the default daily cap is $2)."""
    from sqlalchemy import delete

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall

    tag = new_id("cap")
    yield tag
    with session_scope() as s:
        s.execute(delete(ModelCall).where(ModelCall.purpose == tag))


def test_the_sink_seeds_refuses_over_the_cap_and_alerts_without_redis(pg_counters, spend_tag, monkeypatch):
    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.core.errors import SpendCapReached
    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall, Notification, RunEvent
    from analystos.llm.router import CallContext
    from analystos.runtime.usage import DbUsageSink, day_cost_sql

    c = pg_counters
    tag = spend_tag
    with session_scope() as s:
        s.add(ModelCall(purpose=tag, profile="-", provider="openrouter", model="m", status="ok", attempt=1, cost_usd=0.7))
    with session_scope() as s:
        seed = day_cost_sql(s, c.clock())
    # A fresh key namespace means a fresh seed; earlier tests' rows are part of it (platform-wide).
    platform = PlatformSettings(llm=LLMSettings(daily_spend_cap_usd=round(seed + 0.3, 6)))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    sink = DbUsageSink(c)
    res = sink.reserve(ctx=CallContext(), purpose=tag, model="openai/gpt-5.4-mini", estimate_usd=0.15)
    assert res.store == "postgres" and res.after["platform_daily"] == pytest.approx(seed + 0.15)
    sink.record(ctx=CallContext(), purpose=tag, profile="p", provider="openrouter", model="openai/gpt-5.4-mini", status="ok",
                attempt=1, latency_ms=1, input_tokens=10, output_tokens=1, cost_usd=0.05, request_hash=None, error=None,
                reservation=res, cost_source="provider")
    assert c.value(c.platform_day_key()) == pytest.approx(seed + 0.05)
    with pytest.raises(SpendCapReached) as exc:
        sink.reserve(ctx=CallContext(), purpose=tag, model="openai/gpt-5.4-mini", estimate_usd=1.0)
    assert exc.value.details["cap"] == "platform_daily"
    with pytest.raises(SpendCapReached):  # still refused; the admin notification is sent once per period
        sink.reserve(ctx=CallContext(), purpose=tag, model="openai/gpt-5.4-mini", estimate_usd=1.0)
    with session_scope() as s:
        reached = [e for e in s.scalars(select(RunEvent).where(RunEvent.workspace_id == "platform",
                                                               RunEvent.type == "budget.cap_reached"))
                   if e.payload.get("purpose") == tag]
        titles = [n.title for n in s.scalars(select(Notification).where(Notification.kind == "budget"))]
    assert len(reached) == 1 and any("refused" in t for t in titles)


def test_a_database_error_refuses_the_call(pg_counters, monkeypatch):
    from analystos.core.errors import SpendCountersUnavailable
    from analystos.llm.router import CallContext
    from analystos.runtime.budget_counters import CountersUnavailable
    from analystos.runtime.usage import DbUsageSink

    c = pg_counters

    class Broken:
        def __enter__(self):
            raise RuntimeError("connection refused")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("analystos.db.base.session_scope", lambda *a, **k: Broken())
    with pytest.raises(CountersUnavailable):
        c.reserve(_day_cap(c, 1.0), 0.01)
    monkeypatch.setattr("analystos.runtime.usage.DbUsageSink.cap_specs", lambda self, ws: _day_cap(c, 1.0))
    with pytest.raises(SpendCountersUnavailable) as exc:
        DbUsageSink(c).reserve(ctx=CallContext(), purpose="p", model="m", estimate_usd=0.01)
    assert exc.value.details["store"] == "postgres" and "fail closed" in exc.value.message


def test_migration_0035_spend_counter_up_down_up(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig35"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)

        def tables() -> set[str]:
            return set(inspect(engine).get_table_names())

        command.upgrade(cfg, "0035")
        assert "spend_counter" in tables()
        command.downgrade(cfg, "0030")
        assert "spend_counter" not in tables()
        command.upgrade(cfg, "0035")
        cols = {c["name"] for c in inspect(engine).get_columns("spend_counter")}
        assert cols == {"key", "value", "expires_at", "updated_at"}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()


def test_health_reports_the_installation(api):
    body = api.get("/api/health").json()
    assert {"profile", "features", "extras"} <= set(body["installation"])
    assert "spend_counters" in body["checks"]["redis"] or body["checks"]["redis"]["ok"] is False

"""Hard spend caps against real Redis + Postgres: the reservation Lua script under competing threads,
settle/release, UTC day rollover, the database seed, cap refusal with an admin alert, the admin model
health endpoint, `schedules disable-demo`, and migration 0027 (escalation columns) up/down/up."""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db import vectors

pytestmark = pytest.mark.integration


@pytest.fixture
def counters(monkeypatch):
    """Real Redis, a key prefix of this test only."""
    from analystos.runtime import budget_counters as bc

    c = bc.BudgetCounters("redis://localhost:6379/0", f"aostest:{new_id('s')}:")
    if not c.available:
        pytest.skip("Redis unavailable")
    monkeypatch.setattr(bc, "default_budget_counters", lambda: c)
    yield c
    for key in c._redis.scan_iter(c.prefix + "*"):
        c._redis.delete(key)


def _day_cap(c, limit, seed=0.0):
    from analystos.runtime.budget_counters import CapSpec

    return [CapSpec("platform_daily", c.platform_day_key(), limit, 600, lambda: seed)]


def test_competing_reservations_on_redis_never_overshoot(counters):
    from analystos.runtime.budget_counters import CapExceeded

    granted, refused, lock = [], [], threading.Lock()
    start = threading.Barrier(40)

    def call():
        start.wait()
        for _ in range(5):
            try:
                r = counters.reserve(_day_cap(counters, 1.0), 0.03)
                with lock:
                    granted.append(r)
            except CapExceeded:
                with lock:
                    refused.append(1)

    threads = [threading.Thread(target=call) for _ in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(granted) == 33 and len(refused) == 200 - 33
    assert counters.value(counters.platform_day_key()) <= 1.0 + 1e-9
    for r in granted:  # every call settles to its actual cost (a third of the estimate)
        counters.settle(r, 0.01)
    assert counters.value(counters.platform_day_key()) == pytest.approx(0.33)
    released = counters.reserve(_day_cap(counters, 1.0), 0.5)
    counters.settle(released, 0.0)
    assert counters.value(counters.platform_day_key()) == pytest.approx(0.33)


def test_utc_day_rollover_on_redis(counters):
    from analystos.runtime.budget_counters import CapExceeded

    now = [datetime(2026, 9, 26, 23, 59, 59, tzinfo=UTC)]
    counters.clock = lambda: now[0]
    counters.reserve(_day_cap(counters, 0.1, seed=0.08), 0.02)
    with pytest.raises(CapExceeded):
        counters.reserve(_day_cap(counters, 0.1), 0.01)
    now[0] += timedelta(seconds=2)
    assert counters.reserve(_day_cap(counters, 0.1), 0.01).after["platform_daily"] == pytest.approx(0.01)


def test_db_sink_seeds_from_todays_rows_refuses_over_the_cap_and_alerts(control_db, counters, monkeypatch):
    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.core.errors import SpendCapReached
    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall, Notification, RunEvent
    from analystos.llm.router import CallContext
    from analystos.runtime.usage import DbUsageSink

    tag = new_id("cap")
    with session_scope() as s:  # today's spend so far, platform-wide (the seed), and yesterday's (not counted)
        s.add(ModelCall(purpose=tag, profile="-", provider="openrouter", model="m", status="ok", attempt=1, cost_usd=0.7))
        s.add(ModelCall(purpose=tag, profile="-", provider="openrouter", model="m", status="ok", attempt=1, cost_usd=5.0,
                        created_at=datetime.now(UTC) - timedelta(days=1, hours=1)))
    from analystos.runtime.usage import day_cost_sql

    with session_scope() as s:
        seed = day_cost_sql(s, counters.clock())
    platform = PlatformSettings(llm=LLMSettings(daily_spend_cap_usd=round(seed + 0.3, 6)))
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    sink = DbUsageSink(counters)
    res = sink.reserve(ctx=CallContext(), purpose=tag, model="openai/gpt-5.4-mini", estimate_usd=0.15)
    assert res.after["platform_daily"] == pytest.approx(seed + 0.15) and seed < 5.0
    sink.record(ctx=CallContext(), purpose=tag, profile="p", provider="openrouter", model="openai/gpt-5.4-mini", status="ok",
                attempt=1, latency_ms=1, input_tokens=10, output_tokens=1, cost_usd=0.05, request_hash=None, error=None,
                reservation=res, cost_source="provider")
    assert counters.value(counters.platform_day_key()) == pytest.approx(seed + 0.05)
    with pytest.raises(SpendCapReached) as exc:
        sink.reserve(ctx=CallContext(), purpose=tag, model="openai/gpt-5.4-mini", estimate_usd=1.0)
    assert exc.value.details["cap"] == "platform_daily" and exc.value.details["resets_at"].endswith("00:00:00+00:00")
    with session_scope() as s:
        types = {e.type for e in s.scalars(select(RunEvent).where(RunEvent.workspace_id == "platform"))}
        titles = [n.title for n in s.scalars(select(Notification).where(Notification.kind == "budget"))]
    assert "budget.cap_reached" in types and any("refused" in t for t in titles)


def test_admin_model_health(control_db, counters, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.fakes import FakeTransport, chat_json

    from analystos.api.app import app
    from analystos.contracts.platform import PlatformSettings
    from analystos.llm.cache import ResponseCache
    from analystos.llm.router import ModelRouter
    from analystos.runtime.usage import DbUsageSink

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    counters.note_cooldown("openrouter", 45, "model provider refused for credits HTTP 402: insufficient credits")
    with TestClient(app) as api:
        admin = {"Authorization": "Bearer " + api.post("/api/auth/login", json={
            "email": "admin@analystos.local", "password": "ChangeMe123!"}).json()["access_token"]}
        analyst = {"Authorization": "Bearer " + api.post("/api/auth/login", json={
            "email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()["access_token"]}
        assert api.get("/api/admin/models/health", headers=analyst).status_code == 403
        body = api.get("/api/admin/models/health", headers=admin).json()
        openrouter = next(p for p in body["providers"] if p["provider"] == "openrouter")
        assert openrouter["key_present"] is False and "set OPENROUTER_API_KEY for the api and worker" in openrouter["message"]
        assert "sk-" not in str(body) and 0 < openrouter["cooldown"]["remaining_seconds"] <= 45
        assert "cap_usd" in body["spend_today"] and body["counters_available"] is True
        probe = api.get("/api/admin/models/health", params={"probe": 1}, headers=admin).json()
        assert next(p for p in probe["providers"] if p["provider"] == "openrouter")["probe"]["probed"] is False

        # With a key in the process, the probe sends one tiny request through the router and ends the cooldown.
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-probe-000000000000000000")
        transport = FakeTransport(chat=lambda p: chat_json({"ok": True}, model=p["model"], cost=0.00001))
        # the platform cap counts the whole day's spend in this database: earlier tests' fake calls can exceed $2
        roomy = PlatformSettings()
        roomy.llm.daily_spend_cap_usd = 1_000.0
        router = ModelRouter(transport=transport, sink=DbUsageSink(counters), max_retries=0,
                             settings_provider=lambda: roomy, cache=ResponseCache(None))
        monkeypatch.setattr("analystos.runtime.context.default_router", lambda: router)
        probe = api.get("/api/admin/models/health", params={"probe": 1}, headers=admin).json()
        result = next(p for p in probe["providers"] if p["provider"] == "openrouter")["probe"]
        assert result["ok"] is True and transport.chat_calls[0]["max_tokens"] == 5, result
        assert counters.cooldown("openrouter") is None
        after = api.get("/api/admin/models/health", headers=admin).json()
        assert next(p for p in after["providers"] if p["provider"] == "openrouter")["last_success_at"]


def test_disable_demo_turns_off_marked_schedules_and_monitors(control_db):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Monitor, Schedule, User
    from analystos.services.schedules import disable_demo
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"demo {new_id('w')}")
        s.flush()
        s.add_all([Schedule(id=new_id("sch"), workspace_id=ws.id, name="[demo] Hourly monitors", kind="monitor", cron="5 * * * *",
                            config={"demo": True}, owner_id=admin.id),
                   Schedule(id=new_id("sch"), workspace_id=ws.id, name="Weekly review", kind="reanalysis", cron="0 7 * * 1",
                            config={}, owner_id=admin.id),
                   Monitor(id=new_id("mon"), workspace_id=ws.id, name="[demo] Volume drift", kind="metric_drift",
                           config={"metric": "record_count"}, created_by=admin.id),
                   Monitor(id=new_id("mon"), workspace_id=ws.id, name="Quality", kind="data_quality", config={"demo": True},
                           created_by=admin.id)])
        wid = ws.id
    with session_scope() as s:
        dry = disable_demo(s, workspace_id=wid, dry_run=True)
    assert len(dry["schedules"]) == 1 and len(dry["monitors"]) == 2
    with session_scope() as s:
        disable_demo(s, workspace_id=wid)
    with session_scope() as s:
        enabled = {x.name for x in s.scalars(select(Schedule).where(Schedule.workspace_id == wid, Schedule.enabled.is_(True)))}
        enabled |= {x.name for x in s.scalars(select(Monitor).where(Monitor.workspace_id == wid, Monitor.enabled.is_(True)))}
    assert enabled == {"Weekly review"}
    with session_scope() as s:
        disable_demo(s, workspace_id=wid, include_all=True)
    with session_scope() as s:
        assert not list(s.scalars(select(Schedule).where(Schedule.workspace_id == wid, Schedule.enabled.is_(True))))


def test_migration_0027_escalation_columns_up_down_up(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig27"
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

        def cols() -> set[str]:
            return {c["name"] for c in inspect(engine).get_columns("model_call")}

        command.upgrade(cfg, "0027")
        assert {"escalated_from", "escalation_reason"} <= cols()
        command.downgrade(cfg, "0024")
        assert not {"escalated_from", "escalation_reason"} & cols()
        command.upgrade(cfg, "0027")
        assert {"escalated_from", "escalation_reason"} <= cols()
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()

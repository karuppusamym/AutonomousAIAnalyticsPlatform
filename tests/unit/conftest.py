"""Unit-test support: an in-memory SQLite control plane, so engine and event code can be tested
without the compose stack. Postgres-only column types are rendered as their SQLite equivalents."""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"  # SQLite only autoincrements INTEGER PRIMARY KEY


@pytest.fixture
def sqlite_db(monkeypatch):
    """Point `session_scope` (and the API `db` dependency) at a fresh in-memory SQLite database."""
    from analystos.db import base, models
    from analystos.events import bus

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
                           json_serializer=base.json_dumps)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")

    tables = [models.Base.metadata.tables[t] for t in (
        "app_user", "workspace", "workspace_member", "analysis_run", "run_task", "run_event", "approval", "hypothesis",
        "insight", "artifact", "artifact_version", "audit_event", "monitor", "alert", "notification", "workspace_capability",
        "agent_definition", "tool_definition", "tool_execution", "agent_message", "workspace_policy", "source", "source_asset",
        "source_column", "model_call", "query_execution", "lineage_edge",
        "verification_record", "verification_dependency", "verification_sweep",
        # P7-03 / P4-06: a run binding reads semantic versions; definitions, pins, outbox, idempotency, work orders
        "semantic_model", "semantic_metric", "definition", "schedule", "schedule_run", "dispatch_outbox",
        "idempotency_record", "work_order",
        "model_config_override")]  # S05-T3: the router reads a workspace's model layers
    models.Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(base, "SessionLocal", lambda: factory())
    monkeypatch.setattr(bus, "_redis", False)  # no Redis nudges from unit tests
    yield factory
    engine.dispose()


@pytest.fixture(autouse=True)
def budget_counters(monkeypatch):
    """Budget counters on an in-memory Redis stand-in: unit tests never touch a real Redis."""
    from tests.fakes import FakeRedis

    from analystos.runtime import budget_counters as bc

    counters = bc.BudgetCounters(None, "unit:", client=FakeRedis())
    monkeypatch.setattr(bc, "default_budget_counters", lambda: counters)
    return counters


@pytest.fixture(autouse=True)
def compiled_contexts():
    """Compiled-context reuse (CTX-005) is process-wide: every unit test starts with an empty cache."""
    from analystos.agents import common
    from analystos.context import cache as context_cache
    from analystos.llm.cache import ResponseCache

    context_cache.use(ResponseCache(None))  # never a real Redis from a unit test
    common._COMPILED.clear()
    yield common._COMPILED
    common._COMPILED.clear()
    context_cache.use(None)

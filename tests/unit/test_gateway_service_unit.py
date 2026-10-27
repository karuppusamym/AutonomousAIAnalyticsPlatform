"""Gateway service and cache behaviour without live infrastructure (fake session / fake Redis)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from analystos.contracts.policy import DataScope
from analystos.core.config import Settings
from analystos.core.errors import Forbidden, InvalidInput, SQLRejected
from analystos.db.models import QueryExecution, Workspace
from analystos.gateway.cache import QueryCache, cache_key
from analystos.gateway.service import QueryGateway, json_safe, result_hash


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.fail = fail
        self.ttl: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        if self.fail:
            raise ConnectionError("down")
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = value.encode()
        self.ttl[key] = ex or 0

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FakeSession:
    def __init__(self, sink: list, sources: dict[str, Any]) -> None:
        self.sink = sink
        self.sources = sources

    def get(self, model, key):  # noqa: ANN001
        if model is Workspace and key == "ws_1":
            return SimpleNamespace(id=key, status="active", deleted_at=None)
        return self.sources.get(key)

    def execute(self, stmt):  # noqa: ANN001
        return SimpleNamespace(one=lambda: (1, None, 10))

    def add(self, obj) -> None:  # noqa: ANN001
        self.sink.append(obj)

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@pytest.fixture()
def scope() -> DataScope:
    return DataScope(
        workspace_id="ws_1", user_id="u_1", role="analyst", source_ids=["s1"], assets=["src_s1.incident"],
        asset_sources={"src_s1.incident": "s1"}, columns={"src_s1.incident": ["number", "priority", "caller_id"]},
        denied_columns=["*.caller_id"], source_dialects={"s1": "postgres"}, max_rows=100, timeout_seconds=10,
    )


@pytest.fixture()
def env():
    audit: list = []
    events: list = []
    source = SimpleNamespace(id="s1", workspace_id="ws_1", kind="servicenow", config={}, secret_ref=None, status="ready",
                             execution_mode="staged", staging_schema="src_s1", last_discovered_at=None)
    cache = QueryCache(Settings(), client=FakeRedis())
    gw = QueryGateway(Settings(), session_factory=lambda: FakeSession(audit, {"s1": source}), cache=cache,
                      on_event=lambda t, p: events.append((t, p)))
    return gw, audit, events, cache


def test_json_safe_conversions() -> None:
    u = uuid.uuid4()
    assert json_safe(Decimal("1.50")) == 1.5
    assert json_safe(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05+00:00"
    assert json_safe(date(2026, 1, 2)) == "2026-01-02"
    assert json_safe(u) == str(u)
    assert json_safe(float("nan")) is None
    assert json_safe(b"\x01\xff") == "01ff"
    assert json_safe({"a": [Decimal("2")]}) == {"a": [2.0]}


def test_result_hash_is_order_sensitive_and_stable() -> None:
    assert result_hash(["a"], [[1], [2]]) == result_hash(["a"], [[1], [2]])
    assert result_hash(["a"], [[1], [2]]) != result_hash(["a"], [[2], [1]])


def test_cache_key_binds_all_parts() -> None:
    base = cache_key("f", "s", "v", 10)
    assert base != cache_key("f2", "s", "v", 10)
    assert base != cache_key("f", "s2", "v", 10)
    assert base != cache_key("f", "s", "v2", 10)
    assert base != cache_key("f", "s", "v", 11)


def test_cache_roundtrip_and_errors_are_misses() -> None:
    fake = FakeRedis()
    cache = QueryCache(Settings(query_cache_ttl_seconds=60), client=fake)
    assert cache.get("k") is None
    assert cache.set("k", {"rows": [[1]], "columns": ["a"]})
    assert cache.get("k") == {"rows": [[1]], "columns": ["a"]}
    assert fake.ttl["k"] == 60
    broken = QueryCache(Settings(), client=FakeRedis(fail=True))
    assert broken.get("k") is None
    assert broken.set("k", {"a": 1}) is False
    fake.store["bad"] = b"not json"
    assert cache.get("bad") is None


def test_rejected_query_is_audited_and_emitted(env, scope: DataScope) -> None:
    gw, audit, events, _ = env
    with pytest.raises(SQLRejected):
        gw.execute(scope, "SELECT caller_id FROM incident", actor="agent:test", run_id="run_1")
    assert len(audit) == 1 and isinstance(audit[0], QueryExecution)
    row = audit[0]
    assert row.status == "rejected" and "caller_id" in row.rejected_reason and row.run_id == "run_1"
    assert events[0][0] == "query.rejected" and events[0][1]["status"] == "rejected"


def test_cache_hit_skips_execution_and_is_audited(env, scope: DataScope) -> None:
    gw, audit, events, cache = env
    from analystos.gateway.validator import validate_sql

    v = validate_sql(scope, "SELECT priority, COUNT(*) AS n FROM incident GROUP BY priority", max_rows=100)
    version_holder: dict[str, str] = {}
    original = gw._load_source

    def spy(scope_, validated):  # noqa: ANN001, ANN202
        src = original(scope_, validated)
        version_holder["v"] = src["version"]
        return src

    gw._load_source = spy  # type: ignore[method-assign]
    gw._run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute"))  # type: ignore[method-assign]
    # Prime: compute the key the gateway will use and store a result under it.
    spy(scope, v)
    key = cache.key(v.fingerprint, scope.scope_hash(), version_holder["v"], 100, policy_version=scope.policy_version)
    cache.set(key, {"columns": ["priority", "n"], "rows": [[1, 5]], "truncated": False, "result_hash": "h"})
    res = gw.execute(scope, "select priority, count(*) as n from incident group by priority", actor="user:u_1")
    assert res.cache_hit and res.rows == [[1, 5]] and res.result_hash == "h"
    assert audit[-1].status == "ok" and audit[-1].cache_hit
    assert events[-1][0] == "query.executed"


def test_effective_row_limit_is_minimum(env, scope: DataScope) -> None:
    gw, audit, _, _ = env
    captured: dict[str, Any] = {}

    def fake_run(source, validated, max_rows, timeout):  # noqa: ANN001, ANN202
        captured.update(max_rows=max_rows, timeout=timeout, sql=validated.executable_sql)
        return ["number"], [[str(i)] for i in range(max_rows + 1)]

    gw._run = fake_run  # type: ignore[method-assign]
    res = gw.execute(scope, "SELECT number FROM incident", actor="user:u_1", max_rows=1000, timeout_seconds=99, use_cache=False)
    assert captured["max_rows"] == 100 and captured["timeout"] == 10
    assert "LIMIT 101" in captured["sql"]
    assert res.truncated and res.row_count == 100
    assert len(audit[-1].result_preview) == 20
    res = gw.execute(scope, "SELECT number FROM incident", actor="user:u_1", max_rows=5, timeout_seconds=2, use_cache=False)
    assert captured["max_rows"] == 5 and captured["timeout"] == 2 and res.row_count == 5


def test_staged_source_schema_mismatch_rejected(env, scope: DataScope) -> None:
    gw, audit, _, _ = env
    s = scope.model_copy(deep=True)
    s.assets = ["public.incident"]
    s.asset_sources = {"public.incident": "s1"}
    s.columns = {"public.incident": ["number"]}
    with pytest.raises(SQLRejected, match="staging schema"):
        gw.execute(s, "SELECT number FROM public.incident", actor="user:u_1")
    assert audit[-1].status == "rejected"


def test_source_from_other_workspace_is_not_found(scope: DataScope) -> None:
    audit: list = []
    source = SimpleNamespace(id="s1", workspace_id="ws_OTHER", kind="csv", config={}, secret_ref=None, status="ready",
                             execution_mode="staged", staging_schema="src_s1", last_discovered_at=None)
    gw = QueryGateway(Settings(), session_factory=lambda: FakeSession(audit, {"s1": source}))
    with pytest.raises(Exception, match="not found"):
        gw.execute(scope, "SELECT number FROM incident", actor="user:u_1")
    assert audit[-1].status == "rejected"


def test_run_sql_for_binding(env, scope: DataScope) -> None:
    gw, *_ = env
    runner = gw.run_sql_for(scope, actor="agent:x")
    assert runner.dialect == "postgres" and runner.source_id == "s1"
    multi = scope.model_copy(deep=True)
    multi.source_ids = ["s1", "s2"]
    multi.assets.append("dbo.t")
    multi.asset_sources["dbo.t"] = "s2"
    multi.columns["dbo.t"] = ["a"]
    multi.source_dialects["s2"] = "tsql"
    with pytest.raises(InvalidInput):
        gw.run_sql_for(multi, actor="agent:x")
    bound = gw.run_sql_for(multi, actor="agent:x", source_id="s2")
    assert bound.dialect == "tsql"
    assert bound._scope.assets == ["dbo.t"]
    with pytest.raises(Forbidden):
        gw.run_sql_for(multi, actor="agent:x", source_id="s3")


def test_reader_identity_guard() -> None:
    with pytest.raises(InvalidInput):
        QueryGateway(Settings(analytics_reader_url="postgresql+psycopg://analystos:analystos@localhost:5432/analystos"))
    with pytest.raises(InvalidInput):
        QueryGateway(Settings(analytics_loader_url="postgresql+psycopg://analystos_loader:loader@localhost:5432/analytics",
                              analytics_reader_url="postgresql+psycopg://analystos_loader:loader@localhost:5432/analytics"))

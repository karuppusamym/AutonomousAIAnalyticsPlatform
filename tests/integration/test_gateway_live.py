"""Gateway end to end against the docker-compose Postgres/Redis: staged ServiceNow data read with the
reader identity, audit rows, cache, timeouts, identity isolation and Postgres pushdown."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import QueryTimeout, SQLRejected  # noqa: E402
from analystos.db.models import QueryExecution  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration


@pytest.fixture()
def gateway(dp_settings, dp_session_factory):
    from analystos.gateway.service import QueryGateway

    events: list = []
    gw = QueryGateway(dp_settings, session_factory=dp_session_factory, on_event=lambda t, p: events.append((t, p)))
    gw.events = events  # type: ignore[attr-defined]
    return gw


def _audit(dp_session_factory, query_id: str) -> QueryExecution:
    with dp_session_factory() as s:
        return s.scalar(select(QueryExecution).where(QueryExecution.id == query_id))


def test_execute_ok_is_audited(gateway, servicenow_scope, staged_servicenow, dp_session_factory) -> None:
    res = gateway.execute(
        servicenow_scope,
        "SELECT assignment_group_name, AVG(reassignment_count) AS mean_reassign, COUNT(*) AS n "
        "FROM incident WHERE assignment_group_name IS NOT NULL GROUP BY assignment_group_name ORDER BY mean_reassign DESC",
        actor="agent:test", run_id="run_dp", task_id="task_dp", use_cache=False,
    )
    assert res.columns == ["assignment_group_name", "mean_reassign", "n"]
    assert res.rows[0][0] == "Network Operations"
    assert isinstance(res.rows[0][1], float)  # Decimal -> float
    assert res.row_count == 12 and not res.truncated and not res.cache_hit
    assert res.referenced_assets == [f"{staged_servicenow['schema']}.incident"]
    row = _audit(dp_session_factory, res.query_id)
    assert row.status == "ok" and row.row_count == 12 and row.result_hash == res.result_hash
    assert row.run_id == "run_dp" and row.task_id == "task_dp" and row.actor == "agent:test"
    assert row.source_id == staged_servicenow["source_id"] and row.executed_sql.endswith("LIMIT 50001")
    assert len(row.result_preview) == 12
    assert gateway.events[-1][0] == "query.executed"


def test_ground_truth_through_gateway(gateway, servicenow_scope) -> None:
    res = gateway.execute(
        servicenow_scope,
        "SELECT c.name, COUNT(*) AS p1 FROM incident i JOIN cmdb_ci c ON c.sys_id = i.cmdb_ci "
        "WHERE i.priority = 1 GROUP BY c.name ORDER BY p1 DESC LIMIT 3",
        actor="user:test", use_cache=False,
    )
    assert res.rows[0][0] == "Payments Gateway"
    ts = gateway.execute(servicenow_scope, "SELECT opened_at FROM incident ORDER BY opened_at LIMIT 1", actor="user:test",
                         use_cache=False)
    assert isinstance(ts.rows[0][0], str) and ts.rows[0][0].startswith("2025-")


def test_truncation_flag(gateway, servicenow_scope) -> None:
    res = gateway.execute(servicenow_scope, "SELECT number FROM incident ORDER BY number", actor="user:test",
                          max_rows=10, use_cache=False)
    assert res.row_count == 10 and res.truncated


def test_rejection_is_audited(gateway, servicenow_scope, dp_session_factory) -> None:
    with pytest.raises(SQLRejected) as info:
        gateway.execute(servicenow_scope, "SELECT caller_id FROM incident", actor="agent:test", run_id="run_rej")
    assert "caller_id" in info.value.message
    with dp_session_factory() as s:
        row = s.scalar(select(QueryExecution).where(QueryExecution.run_id == "run_rej"))
    assert row.status == "rejected" and "restricted" in row.rejected_reason
    assert gateway.events[-1][0] == "query.rejected"


def test_pg_sleep_is_blocked_by_validator(gateway, servicenow_scope) -> None:
    with pytest.raises(SQLRejected, match="pg_sleep"):
        gateway.execute(servicenow_scope, "SELECT number FROM incident WHERE pg_sleep(5) IS NULL", actor="user:test")


def test_statement_timeout_maps_to_query_timeout(gateway, servicenow_scope, dp_session_factory) -> None:
    with pytest.raises(QueryTimeout):
        gateway.execute(
            servicenow_scope,
            "SELECT COUNT(*) FROM incident a JOIN incident b ON a.number <> b.number "
            "JOIN change_request c ON b.number <> c.number",
            actor="user:test", run_id="run_timeout", timeout_seconds=1, use_cache=False,
        )
    with dp_session_factory() as s:
        row = s.scalar(select(QueryExecution).where(QueryExecution.run_id == "run_timeout"))
    assert row.status == "timeout"


def test_cache_hit_on_second_call(dp_settings, dp_session_factory, dp_redis, servicenow_scope) -> None:
    import redis

    from analystos.gateway.cache import QueryCache
    from analystos.gateway.service import QueryGateway

    cache = QueryCache(dp_settings)
    gw = QueryGateway(dp_settings, session_factory=dp_session_factory, cache=cache)
    sql = "SELECT category, COUNT(*) AS n FROM incident GROUP BY category ORDER BY category"
    first = gw.execute(servicenow_scope, sql, actor="user:test")
    second = gw.execute(servicenow_scope, sql.lower(), actor="user:test")
    assert not first.cache_hit and second.cache_hit
    assert first.rows == second.rows and first.result_hash == second.result_hash
    assert _audit(dp_session_factory, second.query_id).cache_hit
    # A different scope (tighter row cap) never reuses the entry.
    narrower = servicenow_scope.model_copy(update={"max_rows": 3})
    third = gw.execute(narrower, sql, actor="user:test")
    assert not third.cache_hit and third.row_count == 3 and third.truncated
    # use_cache=False bypasses.
    assert not gw.execute(servicenow_scope, sql, actor="user:test", use_cache=False).cache_hit
    client = redis.Redis.from_url(dp_settings.redis_url)
    for key in client.scan_iter("analystos:qcache:*"):
        client.delete(key)


def test_retain_rows_false_stores_no_values(dp_settings, dp_session_factory, dp_redis, servicenow_scope) -> None:
    """Crawler PII sampling: executed and audited, but no value lands in the audit preview or the cache."""
    from analystos.gateway.cache import QueryCache
    from analystos.gateway.service import QueryGateway

    gw = QueryGateway(dp_settings, session_factory=dp_session_factory, cache=QueryCache(dp_settings))
    sql = "SELECT DISTINCT category AS v FROM incident WHERE category IS NOT NULL"
    first = gw.execute(servicenow_scope, sql, actor="crawler:test", purpose="crawl.pii_sample", retain_rows=False)
    second = gw.execute(servicenow_scope, sql, actor="crawler:test", purpose="crawl.pii_sample", retain_rows=False)
    assert first.rows and not second.cache_hit  # nothing was cached
    row = _audit(dp_session_factory, first.query_id)
    assert row.status == "ok" and row.row_count == len(first.rows) and row.result_hash == first.result_hash
    assert row.result_preview == []


def test_reader_cannot_write_even_if_validator_bypassed(dp_settings, staged_servicenow) -> None:
    schema = staged_servicenow["schema"]
    reader = create_engine(dp_settings.analytics_reader_url)
    for stmt in (
        f"INSERT INTO \"{schema}\".sys_user_group (sys_id, name) VALUES ('x', 'y')",
        f"UPDATE \"{schema}\".sys_user_group SET name = 'pwned'",
        f"CREATE TABLE \"{schema}\".evil (x int)",
        "CREATE TABLE public.evil (x int)",
        f"DROP TABLE \"{schema}\".sys_user_group",
    ):
        with reader.connect() as conn, pytest.raises(Exception) as info:  # noqa: PT011
            conn.execute(text(stmt))
            conn.commit()
        assert "read-only" in str(info.value) or "permission denied" in str(info.value) or "must be owner" in str(info.value)
    # Even after explicitly asking for a read-write transaction, privileges still deny writes.
    with reader.connect() as conn, pytest.raises(Exception, match="permission denied|must be owner"):  # noqa: PT011
        conn.execute(text("SET TRANSACTION READ WRITE"))
        conn.execute(text(f"INSERT INTO \"{schema}\".sys_user_group (sys_id, name) VALUES ('x', 'y')"))
    reader.dispose()


def test_reader_cannot_connect_to_control_plane(dp_settings) -> None:
    url = make_url(dp_settings.analytics_reader_url).set(database="analystos")
    engine = create_engine(url.render_as_string(hide_password=False), connect_args={"connect_timeout": 3})
    with pytest.raises(Exception, match="permission denied|not permitted|CONNECT"), engine.connect() as conn:  # noqa: PT011
        conn.execute(text("SELECT 1"))
    engine.dispose()


def test_gateway_never_uses_loader_or_control_identity(gateway, servicenow_scope, dp_settings) -> None:
    res = gateway.execute(servicenow_scope, "SELECT COUNT(*) AS n FROM sys_user_group", actor="user:test", use_cache=False)
    assert res.rows == [[12]]
    from analystos.gateway import engines

    used = {make_url(u).username for u in engines._engines}
    assert make_url(dp_settings.analytics_reader_url).username in used


# ------------------------------------------------------------------------------ pushdown postgres


@pytest.fixture()
def pushdown_source(dp_control_url, dp_session_factory, dp_workspace, monkeypatch):
    """A Postgres pushdown source: a small schema with PK/FK inside the throwaway database."""
    from analystos.connectors.postgres import PostgresConnector
    from analystos.core.ids import new_id
    from analystos.db.models import Source

    admin = create_engine(dp_control_url)
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS shop CASCADE"))
        conn.execute(text("CREATE SCHEMA shop"))
        conn.execute(text("CREATE TABLE shop.customer (id int PRIMARY KEY, name text NOT NULL, email text)"))
        conn.execute(text("CREATE TABLE shop.orders (id int PRIMARY KEY, customer_id int REFERENCES shop.customer(id), "
                          "amount numeric(10,2), placed_at timestamptz)"))
        conn.execute(text("INSERT INTO shop.customer VALUES (1, 'Ada', 'ada@x'), (2, 'Bob', 'bob@x')"))
        conn.execute(text("INSERT INTO shop.orders VALUES (1, 1, 10.50, now()), (2, 1, 5.25, now()), (3, 2, 1.00, now())"))
        conn.execute(text("ANALYZE shop.customer; ANALYZE shop.orders"))
    admin.dispose()
    url = make_url(dp_control_url)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_PUSHDOWN_PASSWORD", url.password or "")
    config = {"host": url.host, "port": url.port or 5432, "database": url.database, "username": url.username, "schemas": ["shop"]}
    source_id = new_id("pg")
    with dp_session_factory() as s:
        s.add(Source(id=source_id, workspace_id=dp_workspace["workspace_id"], kind="postgres", name="shop",
                     config=config, secret_ref="env:ANALYSTOS_SOURCE_SECRET_DP_PUSHDOWN_PASSWORD", status="ready", execution_mode="pushdown"))
        s.commit()
    return {"source_id": source_id, "connector": PostgresConnector(config, "env:ANALYSTOS_SOURCE_SECRET_DP_PUSHDOWN_PASSWORD")}


def test_postgres_connector_discovery(pushdown_source) -> None:
    con = pushdown_source["connector"]
    assert con.test().ok
    assets = {a.name: a for a in con.discover()}
    assert set(assets) == {"customer", "orders"}
    orders = {c.name: c for c in assets["orders"].columns}
    assert orders["id"].is_key and not orders["id"].nullable
    assert orders["customer_id"].references == "shop.customer.id"
    assert orders["amount"].data_type == "numeric" and orders["placed_at"].data_type == "timestamp"
    assert assets["orders"].row_count == 3 and assets["orders"].schema_name == "shop"


def test_pushdown_execution_is_read_only(gateway, pushdown_source, dp_workspace, dp_session_factory) -> None:
    from analystos.contracts.policy import DataScope

    sid = pushdown_source["source_id"]
    scope = DataScope(
        workspace_id=dp_workspace["workspace_id"], user_id=dp_workspace["user_id"], role="analyst", source_ids=[sid],
        assets=["shop.customer", "shop.orders"], asset_sources={"shop.customer": sid, "shop.orders": sid},
        columns={"shop.customer": ["id", "name", "email"], "shop.orders": ["id", "customer_id", "amount", "placed_at"]},
        denied_columns=["shop.customer.email"], source_dialects={sid: "postgres"},
    )
    res = gateway.execute(scope, "SELECT c.name, SUM(o.amount) AS total FROM orders o JOIN customer c ON c.id = o.customer_id "
                                 "GROUP BY c.name ORDER BY total DESC", actor="user:test", use_cache=False)
    assert res.rows == [["Ada", 15.75], ["Bob", 1.0]]
    with pytest.raises(SQLRejected, match="email"):
        gateway.execute(scope, "SELECT * FROM customer", actor="user:test")
    runner = gateway.run_sql_for(scope, actor="agent:test")
    assert runner.dialect == "postgres"
    assert runner("SELECT COUNT(*) AS n FROM orders").rows == [[3]]
    # The pushdown transaction is READ ONLY even though this identity could write.
    from analystos.gateway.engines import get_engine

    engine = get_engine(pushdown_source["connector"].sqlalchemy_url())
    with engine.connect() as conn:
        dbapi = conn.connection
        cur = dbapi.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        with pytest.raises(Exception, match="read-only"):  # noqa: PT011
            cur.execute("INSERT INTO shop.customer VALUES (3, 'Eve', 'e')")
        dbapi.rollback()

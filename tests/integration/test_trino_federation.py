"""Trino as a source kind and as the federating engine (TRN-004), end to end against a live Trino.

Needs the compose `federation` profile (Trino 476 with catalogs pg -> the demo Postgres `trino_demo`, tpch and a
writable memory catalog; docs/30-runbooks/10-trino-federation.md) and the data-plane Postgres for the control
plane. ANALYSTOS_TEST_TRINO_URL (default http://127.0.0.1:8086) names the coordinator. Every test skips cleanly
when either is unreachable.

What it proves, all through `QueryGateway.execute` and the validator:
  * one source per catalog, queried in place (pushdown, opt-in), discovery through the read-only connection;
  * read-only by transaction: the same Trino user writes to `memory` in autocommit, the platform's sessions cannot;
  * the row cap, the timeout and cancel on Trino;
  * a federated statement over catalogs pg and tpch runs once on Trino, after each table was checked against its
    own source's scope (a denied column of one source is refused inside the cross-catalog join);
  * the analysis compiler's trino SQL runs there (day of week 0 = Sunday, as on postgres);
  * the scan estimate comes from Trino's plan and a scan budget refuses before execution.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import BudgetExceeded, QueryTimeout, RunCancelled, SQLRejected  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration

TRINO_URL = os.environ.get("ANALYSTOS_TEST_TRINO_URL", "http://127.0.0.1:8086")
USER = "analystos_it"
PG_ASSETS = {"sales.customer": ["id", "name", "nation_key", "email"],
             "sales.orders": ["id", "customer_id", "amount", "placed_at"]}
TPCH_ASSETS = {"tiny.nation": ["nationkey", "name", "regionkey", "comment"],
               "tiny.region": ["regionkey", "name", "comment"],
               "sf1000.lineitem": ["orderkey", "quantity"]}
REVENUE_BY_REGION = ("SELECT r.name AS region, SUM(o.amount) AS revenue FROM sales.orders o "
                     "JOIN sales.customer c ON c.id = o.customer_id JOIN tiny.nation n ON n.nationkey = c.nation_key "
                     "JOIN tiny.region r ON r.regionkey = n.regionkey GROUP BY r.name ORDER BY r.name")


@pytest.fixture(scope="module")
def trino_endpoint() -> dict:
    pytest.importorskip("trino")
    import requests

    parsed = urlparse(TRINO_URL)
    try:
        info = requests.get(f"{TRINO_URL}/v1/info", timeout=3).json()
    except Exception:  # noqa: BLE001
        pytest.skip(f"Trino is not reachable at {TRINO_URL} (docker compose --profile federation up -d trino)")
    if info.get("starting"):
        pytest.skip("Trino is still starting")
    from analystos.engines.trino import run_trino

    url = f"trino://{USER}@{parsed.hostname}:{parsed.port or 8080}/system"
    _, rows = run_trino(url, "SELECT catalog_name FROM system.metadata.catalogs", 100, 30)
    missing = {"pg", "tpch", "memory"} - {r[0] for r in rows}
    if missing:
        pytest.skip(f"Trino lacks the federation demo catalogs {sorted(missing)}")
    return {"host": parsed.hostname, "port": parsed.port or 8080, "version": info["nodeVersion"]["version"]}


def _config(endpoint: dict, catalog: str, schemas: list[str]) -> dict:
    return {"host": endpoint["host"], "port": endpoint["port"], "catalog": catalog, "username": USER,
            "schemas": schemas}


def _register(dp_session_factory, dp_workspace, config: dict) -> str:
    from analystos.db.models import Source

    source_id = new_id("trn")
    with dp_session_factory() as s:
        s.add(Source(id=source_id, workspace_id=dp_workspace["workspace_id"], kind="trino",
                     name=f"trino {config['catalog']}", config=config, secret_ref=None, status="ready",
                     execution_mode="pushdown", last_discovered_at=datetime.now(UTC)))
        s.commit()
    return source_id


@pytest.fixture()
def trino_sources(trino_endpoint, dp_session_factory, dp_workspace) -> dict:
    return {"pg": _register(dp_session_factory, dp_workspace, _config(trino_endpoint, "pg", ["sales"])),
            "tpch": _register(dp_session_factory, dp_workspace, _config(trino_endpoint, "tpch", ["tiny", "sf1000"]))}


def _scope(dp_workspace, parts: dict[str, dict[str, list[str]]], denied: list[str] | None = None):
    from analystos.contracts.policy import DataScope

    assets = {a: cols for p in parts.values() for a, cols in p.items()}
    return DataScope(
        workspace_id=dp_workspace["workspace_id"], user_id=dp_workspace["user_id"], role="analyst",
        source_ids=list(parts), assets=list(assets), asset_sources={a: sid for sid, p in parts.items() for a in p},
        columns=assets, denied_columns=denied or [], source_dialects={sid: "trino" for sid in parts},
        max_rows=10_000, timeout_seconds=60,
    )


@pytest.fixture()
def gateway(dp_settings, dp_session_factory):
    from analystos.gateway.service import QueryGateway

    return QueryGateway(dp_settings, session_factory=dp_session_factory)


def _audit(dp_session_factory, query_id: str):
    from analystos.db.models import QueryExecution

    with dp_session_factory() as s:
        return s.get(QueryExecution, query_id)


def test_trino_source_kind_discovery_and_read_only_by_transaction(trino_endpoint) -> None:
    import trino

    from analystos.connectors import kinds
    from analystos.connectors.generic_sql import GenericSQLConnector

    spec = kinds.get_kind("trino")
    assert spec.pushdown_allowed and spec.readonly_enforcement == "transaction"
    assert kinds.execution_mode_for("trino", None) == "staged" and kinds.execution_mode_for("trino", "pushdown") == "pushdown"
    con = GenericSQLConnector("trino", _config(trino_endpoint, "pg", ["sales"]), None, execution_mode="pushdown")
    probe = con.test()
    assert probe.ok and probe.details["readonly_session"] and probe.details["dialect"] == "trino"
    assets = {a.source_name: a for a in con.discover()}
    assert set(assets) == set(PG_ASSETS)
    types = {c.name: c.data_type for c in assets["sales.orders"].columns}
    assert types["amount"] == "numeric" and types["placed_at"] == "timestamp" and types["id"] == "integer"

    table = f"memory.default.aos_it_{new_id('t').lower()}"
    plain = trino.dbapi.connect(host=trino_endpoint["host"], port=trino_endpoint["port"], user=USER)
    cur = plain.cursor()
    cur.execute(f"CREATE TABLE {table} (x integer)")  # the identity itself may write...
    cur.fetchall()
    try:
        from sqlalchemy import text

        with con._get_engine().connect() as conn, pytest.raises(Exception, match="READ_ONLY_VIOLATION"):  # noqa: PT011
            conn.execute(text(f"INSERT INTO {table} VALUES (1)"))  # ...the connector's sessions cannot
        from analystos.core.errors import Forbidden
        from analystos.engines.trino import run_trino

        with pytest.raises(Forbidden, match="read-only"):  # ...nor the gateway's engine
            run_trino(con.sqlalchemy_url(), f"INSERT INTO {table} VALUES (2)", 1, 30)
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchall() == [[0]]
    finally:
        cur.execute(f"DROP TABLE {table}")
        cur.fetchall()
        con.close()


def test_trino_pushdown_through_the_gateway(trino_sources, gateway, dp_workspace, dp_session_factory) -> None:
    pg = trino_sources["pg"]
    scope = _scope(dp_workspace, {pg: PG_ASSETS}, denied=["sales.customer.email"])
    runner = gateway.run_sql_for(scope, actor="user:test", source_id=pg)
    assert runner.dialect == "trino"
    res = runner("SELECT c.name, SUM(o.amount) AS total FROM orders o JOIN customer c ON c.id = o.customer_id "
                 "GROUP BY c.name ORDER BY c.name", use_cache=False)
    assert res.rows == [["Ada", 150.0], ["Bob", 25.5], ["Chen", 300.0], ["Dana", 80.25], ["Eli", 19.75]]
    assert _audit(dp_session_factory, res.query_id).status == "ok"
    capped = gateway.execute(scope, "SELECT id FROM sales.orders ORDER BY id", actor="user:test", max_rows=2,
                             use_cache=False)
    assert capped.rows == [[1], [2]] and capped.truncated
    for sql in ("SELECT email FROM sales.customer",                    # denied column
                "SELECT * FROM sales.customer",                         # ...through the star
                "SELECT name FROM pg.sales.customer",                   # a catalog hop
                "SELECT * FROM TABLE(pg.system.query(query => 'SELECT 1'))",
                "SELECT name FROM tiny.nation"):                        # outside the scope
        with pytest.raises(SQLRejected):
            gateway.execute(scope, sql, actor="user:test", use_cache=False)


def test_cross_catalog_join_runs_on_trino_through_the_gateway(trino_sources, gateway, dp_workspace,
                                                              dp_session_factory) -> None:
    pg, tpch = trino_sources["pg"], trino_sources["tpch"]
    scope = _scope(dp_workspace, {pg: PG_ASSETS, tpch: TPCH_ASSETS}, denied=["sales.customer.email"])
    runner = gateway.run_sql_for(scope, actor="user:test", federated=True)
    assert runner.dialect == "trino"
    res = runner(REVENUE_BY_REGION, use_cache=False)
    assert res.columns == ["region", "revenue"]
    assert res.rows == [["AMERICA", 80.25], ["ASIA", 300.0], ["EUROPE", 195.25]]
    audit = _audit(dp_session_factory, res.query_id)
    assert audit.status == "ok"
    assert '"pg"."sales"."orders"' in audit.executed_sql and '"tpch"."tiny"."region"' in audit.executed_sql
    assert set(res.referenced_assets) == {"sales.orders", "sales.customer", "tiny.nation", "tiny.region"}
    # Each table is still checked against its own source's scope inside the cross-catalog statement.
    with pytest.raises(SQLRejected, match="restricted"):
        runner("SELECT c.email, n.name FROM sales.customer c JOIN tiny.nation n ON n.nationkey = c.nation_key")
    with pytest.raises(SQLRejected):
        runner("SELECT s.name FROM tiny.supplier s")  # discovered in Trino, not in the scope
    with pytest.raises(SQLRejected, match="Cross-source"):  # without federation, one source per statement
        gateway.execute(scope, REVENUE_BY_REGION, actor="user:test", use_cache=False)


def test_timeout_and_cancel_on_trino(trino_sources, gateway, dp_workspace, dp_session_factory) -> None:
    tpch = trino_sources["tpch"]
    scope = _scope(dp_workspace, {tpch: TPCH_ASSETS})
    slow = "SELECT count(*) AS n, SUM(l.quantity) AS q FROM sf1000.lineitem l"  # ~6e9 generated rows
    started = time.monotonic()
    with pytest.raises(QueryTimeout):
        gateway.execute(scope, slow, actor="user:test", timeout_seconds=1, use_cache=False)
    assert time.monotonic() - started < 10

    qid = new_id("qry")
    out: dict = {}

    def run() -> None:
        try:
            gateway.execute(scope, slow, actor="user:test", query_id=qid, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(1.5)
    assert any(gateway.cancel(qid) or time.sleep(0.1) for _ in range(50))
    thread.join(30)
    assert isinstance(out.get("error"), RunCancelled), out
    assert _audit(dp_session_factory, qid).status == "error"


def test_analysis_compiler_sql_runs_on_trino(trino_sources, gateway, dp_workspace) -> None:
    from analystos.contracts.analysis import AnalysisSpec, Derivation
    from analystos.skills import sqlbuild as sb

    pg = trino_sources["pg"]
    scope = _scope(dp_workspace, {pg: PG_ASSETS})
    dow = sb.aggregate_query("sales.orders", "trino", dimensions=[("dow", Derivation(type="day_of_week", column="placed_at"))],
                             measure=("n", "count", None))
    res = gateway.execute(scope, dow, actor="user:test", use_cache=False)
    # 2026-01-05 Mon, 02-01 Sun, 02-11 Wed, 03-03 Tue, 03-20 Fri, 04-02 Thu; 0 = Sunday as on postgres
    assert sorted(r[0] for r in res.rows) == [0, 1, 2, 3, 4, 5]
    spec = AnalysisSpec(method="trend", asset="sales.orders", time=Derivation(type="date_trunc", column="placed_at",
                                                                              grain="month"))
    trend = sb.compile_spec(spec, "trino", purpose=sb.METHOD_PURPOSES["trend"][0])
    res = gateway.execute(scope, trend.sql, actor="user:test", use_cache=False)
    assert len(res.rows) == 4  # January .. April


def test_scan_estimate_from_the_trino_plan_and_budget(trino_sources, gateway, dp_workspace) -> None:
    pg, tpch = trino_sources["pg"], trino_sources["tpch"]
    scope = _scope(dp_workspace, {pg: PG_ASSETS})
    est = gateway.estimate(scope, "SELECT id, amount FROM sales.orders", actor="user:test")
    assert est.basis == "explain" and est.rows == 6 and est.bytes
    with pytest.raises(BudgetExceeded):
        gateway.execute(scope, "SELECT id, amount FROM sales.orders", actor="user:test", max_scan_bytes=1,
                        use_cache=False)
    fed = _scope(dp_workspace, {pg: PG_ASSETS, tpch: TPCH_ASSETS})
    with pytest.raises(BudgetExceeded):
        gateway.execute(fed, REVENUE_BY_REGION, actor="user:test", federated=True, max_scan_bytes=1, use_cache=False)

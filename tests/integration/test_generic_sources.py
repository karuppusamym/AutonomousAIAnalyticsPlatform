"""Generic SQL sources end to end against real engines.

(a) MySQL 8.4 in a throwaway docker container: discovery (PK/FK/comments/row estimates), the
    read-only session, extraction through the StagingLoader into the analytics DB and a governed
    query through the QueryGateway as the reader identity.
(b) SQLite and DuckDB files: discover, extract, stage, gateway query.
(c) Postgres pushdown through the generic connector: include/exclude, gateway queries, and proof
    that the pushdown session is read-only even for an identity that could write.

Every part skips cleanly when docker / the compose Postgres is unavailable. Passwords are
generated per run and passed through env: secret_refs, never written to files.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.connectors.base import DiscoveredAsset  # noqa: E402
from analystos.connectors.kinds import dialect_for, execution_mode_for  # noqa: E402
from analystos.core.errors import Forbidden, InvalidInput, SQLRejected  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration

MYSQL_IMAGE = os.environ.get("ANALYSTOS_TEST_MYSQL_IMAGE", "mysql:8.4")


# ------------------------------------------------------------------------------ shared helpers


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", MYSQL_IMAGE], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _register_source(dp_session_factory, dp_workspace, *, kind: str, config: dict, secret_ref: str | None,
                     mode: str, staging_schema: str | None = None) -> str:
    from datetime import UTC, datetime

    from analystos.db.models import Source

    source_id = new_id(kind[:3])
    with dp_session_factory() as s:
        s.add(Source(id=source_id, workspace_id=dp_workspace["workspace_id"], kind=kind, name=f"{kind} test",
                     config=config, secret_ref=secret_ref, status="ready", execution_mode=mode,
                     staging_schema=staging_schema, last_discovered_at=datetime.now(UTC)))
        s.commit()
    return source_id


def _scope(dp_workspace, source_id: str, assets: dict[str, list[str]], dialect: str, denied: list[str] | None = None):
    from analystos.contracts.policy import DataScope

    return DataScope(
        workspace_id=dp_workspace["workspace_id"], user_id=dp_workspace["user_id"], role="analyst",
        source_ids=[source_id], assets=list(assets), asset_sources={a: source_id for a in assets},
        columns=assets, denied_columns=denied or [], source_dialects={source_id: dialect},
        max_rows=10_000, timeout_seconds=30,
    )


def _stage_all(connector, loader, source_id: str, assets: list[DiscoveredAsset], workspace_id: str) -> dict[str, dict]:
    """Stage like services.sources.select_assets: the asset is rebuilt from control-plane fields only
    (source_name, name, columns), so extraction must not depend on anything else."""
    loads = {}
    for a in assets:
        stored = DiscoveredAsset(source_name=a.source_name, name=a.name, columns=a.columns, kind="api_table")
        loads[a.name] = loader.load(source_id, stored, connector.extract(stored, max_rows=100_000), workspace_id=workspace_id)
    return loads


@pytest.fixture()
def gateway(dp_settings, dp_session_factory):
    from analystos.gateway.service import QueryGateway

    return QueryGateway(dp_settings, session_factory=dp_session_factory)


# ------------------------------------------------------------------------------ (a) MySQL 8.4


MYSQL_SCHEMA = [
    "CREATE DATABASE shop",
    "CREATE TABLE shop.customer (id INT PRIMARY KEY, name VARCHAR(80) NOT NULL COMMENT 'Customer display name', "
    "email VARCHAR(120), vip TINYINT(1) NOT NULL DEFAULT 0, joined DATE) COMMENT='People who buy things'",
    "CREATE TABLE shop.orders (id INT PRIMARY KEY, customer_id INT NOT NULL, amount DECIMAL(10,2), "
    "placed_at DATETIME, status ENUM('open','paid') DEFAULT 'open', "
    "CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES shop.customer(id)) COMMENT='Orders'",
    "CREATE TABLE shop.audit_log (id INT PRIMARY KEY, msg TEXT)",
    "CREATE VIEW shop.paid_orders AS SELECT id, customer_id, amount FROM shop.orders WHERE status = 'paid'",
    "INSERT INTO shop.customer VALUES (1, 'Ada', 'ada@x', 1, '2024-01-02'), (2, 'Bob', 'bob@x', 0, '2024-03-04'), "
    "(3, 'Cy', NULL, 0, NULL)",
    "INSERT INTO shop.orders VALUES (1, 1, 10.50, '2025-01-01 10:00:00', 'paid'), (2, 1, 5.25, '2025-01-02 11:00:00', 'open'), "
    "(3, 2, 1.00, '2025-01-03 12:00:00', 'paid'), (4, 3, 7.00, NULL, 'paid')",
    "INSERT INTO shop.audit_log VALUES (1, 'x')",
    "ANALYZE TABLE shop.customer, shop.orders, shop.audit_log",
]


@pytest.fixture(scope="session")
def mysql_server():
    if not _docker_ok():
        pytest.skip(f"docker or the {MYSQL_IMAGE} image is not available")
    import pymysql

    port = _free_port()
    root_pw, ro_pw = secrets.token_urlsafe(18), "r0:" + secrets.token_urlsafe(12) + "@/"  # URL-hostile on purpose
    name = f"analystos-test-mysql-{secrets.token_hex(4)}"
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"MYSQL_ROOT_PASSWORD={root_pw}",
         "-p", f"127.0.0.1:{port}:3306", MYSQL_IMAGE],
        capture_output=True, text=True, timeout=60,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start {MYSQL_IMAGE}: {started.stderr.strip()[:200]}")
    try:
        deadline, conn = time.time() + 180, None
        while time.time() < deadline:
            try:
                conn = pymysql.connect(host="127.0.0.1", port=port, user="root", password=root_pw, connect_timeout=3,
                                       autocommit=True)
                break
            except pymysql.err.OperationalError:
                time.sleep(2)
        if conn is None:
            pytest.skip("MySQL container did not become ready")
        with conn.cursor() as cur:
            for stmt in MYSQL_SCHEMA:
                cur.execute(stmt)
            cur.execute("CREATE USER 'ro'@'%%' IDENTIFIED BY %s", (ro_pw,))
            cur.execute("GRANT SELECT, SHOW VIEW ON shop.* TO 'ro'@'%'")
        conn.close()
        yield {"port": port, "root_pw": root_pw, "ro_pw": ro_pw}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture()
def mysql_config(mysql_server, monkeypatch) -> dict:
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_MYSQL_RO_PASSWORD", mysql_server["ro_pw"])
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_MYSQL_ROOT_PASSWORD", mysql_server["root_pw"])
    return {"host": "127.0.0.1", "port": mysql_server["port"], "database": "shop", "username": "ro",
            "exclude": ["*.audit_*"]}


def test_mysql_test_and_discovery(mysql_config) -> None:
    from analystos.connectors.registry import build_connector

    con = build_connector(SimpleNamespace(kind="mysql", config=mysql_config, secret_ref="env:ANALYSTOS_SOURCE_SECRET_DP_MYSQL_RO_PASSWORD"))
    assert con.execution_mode == "staged" and con.dialect == "postgres" and con.source_dialect == "mysql"
    probe = con.test()
    assert probe.ok, probe.message
    assert probe.details["version"].startswith("8.4")
    assets = {a.name: a for a in con.discover()}
    assert set(assets) == {"customer", "orders", "paid_orders"}  # audit_log excluded by pattern
    customer, orders = assets["customer"], assets["orders"]
    assert customer.description == "People who buy things" and customer.source_name == "shop.customer"
    ccols = {c.name: c for c in customer.columns}
    assert ccols["id"].is_key and not ccols["id"].nullable
    assert ccols["name"].description == "Customer display name"
    assert ccols["vip"].data_type == "boolean" and ccols["joined"].data_type == "date"
    ocols = {c.name: c for c in orders.columns}
    assert ocols["customer_id"].references == "shop.customer.id"
    assert ocols["amount"].data_type == "numeric" and ocols["placed_at"].data_type == "timestamp"
    assert ocols["status"].data_type == "text"
    assert orders.row_count is not None and orders.row_count > 0  # information_schema.table_rows estimate
    assert assets["paid_orders"].kind == "view" and assets["paid_orders"].row_count is None


def test_mysql_bad_password_is_a_clear_secret_free_error(mysql_config, monkeypatch) -> None:
    from analystos.connectors.generic_sql import GenericSQLConnector

    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_MYSQL_WRONG", "definitely-wrong-pw")
    res = GenericSQLConnector("mysql", mysql_config, "env:ANALYSTOS_SOURCE_SECRET_DP_MYSQL_WRONG").test()
    assert not res.ok and "authentication failed" in res.message
    assert "definitely-wrong-pw" not in res.message


def test_mysql_session_is_read_only_even_for_root(mysql_config) -> None:
    from analystos.connectors.generic_sql import GenericSQLConnector

    root = GenericSQLConnector("mysql", dict(mysql_config, username="root"), "env:ANALYSTOS_SOURCE_SECRET_DP_MYSQL_ROOT_PASSWORD")
    with root._get_engine().connect() as conn, pytest.raises(Exception, match="READ ONLY"):  # noqa: PT011
        conn.execute(text("INSERT INTO shop.audit_log VALUES (2, 'nope')"))
    root.close()


def test_mysql_extract_stage_and_gateway_query(mysql_config, dp_settings, dp_session_factory, dp_workspace,
                                                gateway) -> None:
    from analystos.connectors.naming import staging_schema_for
    from analystos.connectors.registry import build_connector
    from analystos.staging.loader import StagingLoader

    source_id = _register_source(dp_session_factory, dp_workspace, kind="mysql", config=mysql_config,
                                 secret_ref="env:ANALYSTOS_SOURCE_SECRET_DP_MYSQL_RO_PASSWORD", mode=execution_mode_for("mysql", None))
    schema = staging_schema_for(source_id)
    con = build_connector(SimpleNamespace(kind="mysql", config=mysql_config, secret_ref="env:ANALYSTOS_SOURCE_SECRET_DP_MYSQL_RO_PASSWORD",
                                          execution_mode="staged"))
    assets = con.discover()
    loader = StagingLoader(dp_settings)
    try:
        loads = _stage_all(con, loader, source_id, assets, dp_workspace["workspace_id"])
        assert loads["customer"]["row_count"] == 3 and loads["orders"]["row_count"] == 4
        assert loads["paid_orders"]["row_count"] == 3
        types = {c["name"]: c["type"] for c in loads["orders"]["columns"]}
        assert types["amount"] == "double precision" and types["placed_at"] == "timestamp"
        assert {c["name"]: c["type"] for c in loads["customer"]["columns"]}["vip"] == "boolean"
        scope = _scope(dp_workspace, source_id, {f"{schema}.{a.name}": [c.name for c in a.columns] for a in assets},
                       dialect_for("mysql", "staged"), denied=[f"{schema}.customer.email"])
        res = gateway.execute(scope, "SELECT c.name, SUM(o.amount) AS total, COUNT(*) AS n FROM orders o "
                                     "JOIN customer c ON c.id = o.customer_id GROUP BY c.name ORDER BY total DESC",
                              actor="user:test", use_cache=False)
        assert res.rows == [["Ada", 15.75, 2], ["Cy", 7.0, 1], ["Bob", 1.0, 1]]
        vip = gateway.execute(scope, "SELECT COUNT(*) AS n FROM customer WHERE vip", actor="user:test", use_cache=False)
        assert vip.rows == [[1]]
        with pytest.raises(SQLRejected, match="email"):
            gateway.execute(scope, "SELECT email FROM customer", actor="user:test")
        from analystos.gateway import engines

        assert make_url(dp_settings.analytics_reader_url).username in {make_url(u).username for u in engines._engines}
    finally:
        loader.drop_source(source_id)
        con.close()


# ------------------------------------------------------------------------------ (b) SQLite / DuckDB files


def _make_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE region (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL);
        CREATE TABLE "Sales Orders" (id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES region(id),
                                     "Net Amount" NUMERIC(10,2), sold_on DATE, shipped BOOLEAN);
        CREATE VIEW big_orders AS SELECT * FROM "Sales Orders" WHERE "Net Amount" > 10;
        INSERT INTO region VALUES (1, 'North'), (2, 'South');
        INSERT INTO "Sales Orders" VALUES (1, 1, 12.5, '2025-02-01', 1), (2, 1, 3.0, '2025-02-02', 0),
                                          (3, 2, 40.0, '2025-02-03', 1);
        """
    )
    conn.commit()
    conn.close()


def _make_duckdb(path: Path) -> None:
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE SCHEMA sales;
        CREATE TABLE sales.region (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
        CREATE TABLE sales."Sales Orders" (id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES sales.region(id),
                                           "Net Amount" DECIMAL(10,2), sold_on DATE, shipped BOOLEAN);
        CREATE VIEW sales.big_orders AS SELECT * FROM sales."Sales Orders" WHERE "Net Amount" > 10;
        COMMENT ON TABLE sales.region IS 'Sales regions';
        INSERT INTO sales.region VALUES (1, 'North'), (2, 'South');
        INSERT INTO sales."Sales Orders" VALUES (1, 1, 12.5, '2025-02-01', true), (2, 1, 3.0, '2025-02-02', false),
                                                (3, 2, 40.0, '2025-02-03', true);
        """
    )
    conn.close()


@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_file_database_discover_stage_and_query(kind, tmp_path, dp_settings, dp_session_factory, dp_workspace,
                                                gateway) -> None:
    from analystos.connectors.naming import staging_schema_for
    from analystos.connectors.registry import build_connector
    from analystos.staging.loader import StagingLoader

    fname = "shop.db" if kind == "sqlite" else "shop.duckdb"
    (_make_sqlite if kind == "sqlite" else _make_duckdb)(tmp_path / fname)
    settings = SimpleNamespace(upload_dir=str(tmp_path))
    config = {"path": fname}
    source_id = _register_source(dp_session_factory, dp_workspace, kind=kind, config=config, secret_ref=None,
                                 mode=execution_mode_for(kind, None))  # both file kinds default to staged
    con = build_connector(SimpleNamespace(kind=kind, config=config, secret_ref=None), settings)
    assert con.execution_mode == "staged" and con.dialect == "postgres"
    assert con.test().ok
    assets = {a.name: a for a in con.discover()}
    assert set(assets) == {"region", "sales_orders", "big_orders"}
    orders = assets["sales_orders"]
    cols = {c.name: c for c in orders.columns}
    assert cols["net_amount"].business_name == "Net Amount" and cols["net_amount"].data_type == "numeric"
    assert cols["region_id"].references.endswith(".region.id")
    assert cols["id"].is_key and cols["sold_on"].data_type == "date" and cols["shipped"].data_type == "boolean"
    assert orders.row_count == 3 and assets["region"].row_count == 2 and assets["big_orders"].kind == "view"
    if kind == "duckdb":
        assert assets["region"].description == "Sales regions"
    loader = StagingLoader(dp_settings)
    schema = staging_schema_for(source_id)
    try:
        loads = _stage_all(con, loader, source_id, list(assets.values()), dp_workspace["workspace_id"])
        assert loads["sales_orders"]["row_count"] == 3 and loads["big_orders"]["row_count"] == 2
        scope = _scope(dp_workspace, source_id, {f"{schema}.{a.name}": [c.name for c in a.columns] for a in assets.values()},
                       dialect_for(kind, "staged"))
        res = gateway.execute(scope, "SELECT r.name, SUM(o.net_amount) AS total FROM sales_orders o "
                                     "JOIN region r ON r.id = o.region_id WHERE o.shipped GROUP BY r.name ORDER BY r.name",
                              actor="user:test", use_cache=False)
        assert res.rows == [["North", 12.5], ["South", 40.0]]
    finally:
        loader.drop_source(source_id)
        con.close()


def test_file_database_outside_upload_dir_is_refused(tmp_path) -> None:
    from analystos.connectors.registry import build_connector

    inside = tmp_path / "uploads"
    inside.mkdir()
    _make_sqlite(tmp_path / "secret.db")
    with pytest.raises(InvalidInput, match="upload directory"):
        build_connector(SimpleNamespace(kind="sqlite", config={"path": "../secret.db"}, secret_ref=None),
                        SimpleNamespace(upload_dir=str(inside)))


def test_gateway_refuses_pushdown_for_a_staged_only_kind(tmp_path, dp_session_factory, dp_workspace,
                                                         dp_settings) -> None:
    """A source row wrongly marked pushdown for a kind the catalog stages is refused before any SQL runs."""
    from analystos.gateway.service import QueryGateway

    _make_sqlite(tmp_path / "shop.db")
    source_id = _register_source(dp_session_factory, dp_workspace, kind="sqlite", config={"path": "shop.db"},
                                 secret_ref=None, mode="pushdown")
    settings = dp_settings.model_copy(update={"upload_dir": str(tmp_path)})
    gw = QueryGateway(settings, session_factory=dp_session_factory)
    scope = _scope(dp_workspace, source_id, {"main.region": ["id", "name"]}, "postgres")
    with pytest.raises(InvalidInput, match="cannot be queried in place"):
        gw.execute(scope, "SELECT name FROM region", actor="user:test", use_cache=False)


# ------------------------------------------------------------------------------ (c) Postgres pushdown


@pytest.fixture()
def pg_generic_source(dp_control_url, dp_session_factory, dp_workspace, monkeypatch):
    admin = create_engine(dp_control_url)
    with admin.begin() as conn:
        for stmt in (
            "DROP SCHEMA IF EXISTS gsales CASCADE", "DROP SCHEMA IF EXISTS ghr CASCADE",
            "CREATE SCHEMA gsales", "CREATE SCHEMA ghr",
            "CREATE TABLE gsales.customer (id int PRIMARY KEY, name text NOT NULL)",
            "COMMENT ON TABLE gsales.customer IS 'Buyers'",
            "COMMENT ON COLUMN gsales.customer.name IS 'Display name'",
            "CREATE TABLE gsales.orders (id int PRIMARY KEY, customer_id int REFERENCES gsales.customer(id), "
            "amount numeric(10,2), placed_at timestamptz)",
            "CREATE TABLE gsales.secret_margin (id int PRIMARY KEY, margin numeric)",
            "CREATE VIEW gsales.order_totals AS SELECT customer_id, SUM(amount) AS total FROM gsales.orders GROUP BY 1",
            "CREATE TABLE ghr.salary (id int PRIMARY KEY, amount numeric)",
            "INSERT INTO gsales.customer VALUES (1, 'Ada'), (2, 'Bob')",
            "INSERT INTO gsales.orders VALUES (1, 1, 10.50, now()), (2, 1, 5.25, now()), (3, 2, 1.00, now())",
            "ANALYZE gsales.customer", "ANALYZE gsales.orders",
        ):
            conn.execute(text(stmt))
    url = make_url(dp_control_url)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_GENERIC_PG_PASSWORD", url.password or "")
    config = {"host": url.host, "port": url.port or 5432, "database": url.database, "username": url.username,
              "include": ["gsales", "ghr"], "exclude": ["ghr", "*.secret_*"]}
    source_id = _register_source(dp_session_factory, dp_workspace, kind="postgres", config=config,
                                 secret_ref="env:ANALYSTOS_SOURCE_SECRET_DP_GENERIC_PG_PASSWORD", mode=execution_mode_for("postgres", None))
    yield {"source_id": source_id, "config": config}
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS gsales CASCADE"))
        conn.execute(text("DROP SCHEMA IF EXISTS ghr CASCADE"))
    admin.dispose()


def test_postgres_generic_discovery_with_filters(pg_generic_source) -> None:
    from analystos.connectors.generic_sql import GenericSQLConnector

    con = GenericSQLConnector("postgres", pg_generic_source["config"], "env:ANALYSTOS_SOURCE_SECRET_DP_GENERIC_PG_PASSWORD")
    assert con.execution_mode == "pushdown" and con.dialect == "postgres" and con.schemas is None
    assert con.test().ok
    assets = {a.source_name: a for a in con.discover()}
    assert set(assets) == {"gsales.customer", "gsales.orders", "gsales.order_totals"}
    customer = assets["gsales.customer"]
    assert customer.name == "customer" and customer.schema_name == "gsales" and customer.description == "Buyers"
    assert {c.name: c.description for c in customer.columns}["name"] == "Display name"
    orders = {c.name: c for c in assets["gsales.orders"].columns}
    assert orders["id"].is_key and not orders["id"].nullable
    assert orders["customer_id"].references == "gsales.customer.id"
    assert orders["amount"].data_type == "numeric" and orders["placed_at"].data_type == "timestamp"
    assert assets["gsales.orders"].row_count == 3 and assets["gsales.order_totals"].kind == "view"
    with pytest.raises(InvalidInput, match="pushdown"):
        next(con.extract(assets["gsales.orders"], max_rows=10))
    capped = GenericSQLConnector("postgres", dict(pg_generic_source["config"], max_tables=1), "env:ANALYSTOS_SOURCE_SECRET_DP_GENERIC_PG_PASSWORD")
    assert len(capped.discover()) == 1 and capped.truncated


def test_postgres_pushdown_gateway_and_read_only_session(pg_generic_source, dp_settings, dp_session_factory,
                                                         dp_workspace) -> None:
    from analystos.connectors.generic_sql import GenericSQLConnector
    from analystos.gateway.service import QueryGateway

    sid = pg_generic_source["source_id"]
    built: list = []

    def factory(source, settings=None, **kw):  # the generic connector itself, not the Postgres subclass
        con = GenericSQLConnector(source.kind, source.config, source.secret_ref, execution_mode=source.execution_mode)
        built.append(con)
        return con

    for gw in (QueryGateway(dp_settings, session_factory=dp_session_factory),  # registry -> PostgresConnector
               QueryGateway(dp_settings, session_factory=dp_session_factory, connector_factory=factory)):
        scope = _scope(dp_workspace, sid, {"gsales.customer": ["id", "name"],
                                           "gsales.orders": ["id", "customer_id", "amount", "placed_at"]},
                       dialect_for("postgres", "pushdown"))
        res = gw.execute(scope, "SELECT c.name, SUM(o.amount) AS total FROM orders o JOIN customer c "
                                "ON c.id = o.customer_id GROUP BY c.name ORDER BY total DESC",
                         actor="user:test", use_cache=False)
        assert res.rows == [["Ada", 15.75], ["Bob", 1.0]]
        with pytest.raises(SQLRejected):
            gw.execute(scope, "SELECT margin FROM secret_margin", actor="user:test")
    assert built and isinstance(built[0], GenericSQLConnector)
    # The identity here can write (it owns the schema); the pushdown execution path still cannot.
    gw = QueryGateway(dp_settings, session_factory=dp_session_factory, connector_factory=factory)
    with pytest.raises(Forbidden, match="read-only"):
        gw._run_postgres(built[0].sqlalchemy_url(), "INSERT INTO gsales.customer VALUES (3, 'Eve')", 10, 5)
    # ...nor can the connector's own sessions (discovery/extraction): SET SESSION ... READ ONLY.
    with built[0]._get_engine().connect() as conn, pytest.raises(Exception, match="read-only"):  # noqa: PT011
        conn.execute(text("INSERT INTO gsales.customer VALUES (4, 'Mallory')"))
    admin = create_engine(dp_settings.database_url)
    with admin.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM gsales.customer")).scalar() == 2
    admin.dispose()
    for con in built:
        con.close()

"""S02-T5 on a live SQL Server (compose profile `mssql`, container `analystos-mssql`): the end user's identity
reaches the source's own row security through `QueryGateway.execute` only. A SQL Server security policy filters
`s02shop.events` on SESSION_CONTEXT(N'app.user'), which the gateway sets read-only per statement from the
catalog's `capabilities.session_identity`.

Needs the compose Postgres (control plane, `dataplane_fixtures`) and SQL Server with
ANALYSTOS_MSSQL_SA_PASSWORD (and ANALYSTOS_MSSQL_HOST / ANALYSTOS_MSSQL_PORT, default 127.0.0.1:14330) in the
environment. Skips cleanly when either is absent."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import SQLRejected  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration
HOST = os.environ.get("ANALYSTOS_MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("ANALYSTOS_MSSQL_PORT", "14330"))
DATABASE = "aostest_s02_mssql"
READER = "aostest_s02_mssql_ro"
READER_PASSWORD = "rls-test-only-1"  # a throwaway login created and dropped by this module, not a real credential
ROWS = 2000


def _sa_connection():
    password = os.environ.get("ANALYSTOS_MSSQL_SA_PASSWORD")
    if not password:
        pytest.skip("ANALYSTOS_MSSQL_SA_PASSWORD is not set (compose profile `mssql`)")
    pymssql = pytest.importorskip("pymssql")
    try:
        socket.create_connection((HOST, PORT), timeout=2).close()
    except OSError:
        pytest.skip(f"SQL Server is not reachable on {HOST}:{PORT}")
    return pymssql.connect(server=HOST, port=PORT, user="sa", password=password, autocommit=True, login_timeout=10)


def _drop(cur) -> None:
    cur.execute(f"IF DB_ID(N'{DATABASE}') IS NOT NULL BEGIN "
                f"ALTER DATABASE {DATABASE} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {DATABASE}; END")
    cur.execute(f"IF SUSER_ID(N'{READER}') IS NOT NULL DROP LOGIN {READER}")


@pytest.fixture(scope="module")
def shop(dp_session_factory, dp_workspace):
    """`s02shop.events` (2000 rows, half per owner) under a security policy keyed on SESSION_CONTEXT(N'app.user'),
    read by a SELECT-only login, registered as a SQL Server pushdown source with catalogued columns."""
    from analystos.db.models import Source, SourceAsset, SourceColumn

    conn = _sa_connection()
    cur = conn.cursor()
    _drop(cur)
    cur.execute(f"CREATE DATABASE {DATABASE}")
    cur.execute(f"CREATE LOGIN {READER} WITH PASSWORD = '{READER_PASSWORD}', CHECK_POLICY = OFF, "
                f"DEFAULT_DATABASE = {DATABASE}")
    cur.execute(f"USE {DATABASE}")
    for stmt in (
        f"CREATE USER {READER} FOR LOGIN {READER}",
        "EXEC (N'CREATE SCHEMA s02shop')",
        "CREATE TABLE s02shop.events (id int PRIMARY KEY, owner nvarchar(50) NOT NULL, amount decimal(10, 2), "
        "note nvarchar(100))",
        f"INSERT INTO s02shop.events SELECT value, CASE WHEN value % 2 = 0 THEN N'u_alice' ELSE N'u_bob' END, "
        f"value * 1.25, REPLICATE(N'n', 40) FROM GENERATE_SERIES(1, {ROWS})",
        "EXEC (N'CREATE FUNCTION s02shop.own_rows(@owner nvarchar(50)) RETURNS TABLE WITH SCHEMABINDING AS "
        "RETURN SELECT 1 AS visible WHERE @owner = CAST(SESSION_CONTEXT(N''app.user'') AS nvarchar(50))')",
        "CREATE SECURITY POLICY s02shop.own_rows_policy ADD FILTER PREDICATE s02shop.own_rows(owner) "
        "ON s02shop.events WITH (STATE = ON)",
        f"GRANT SELECT ON SCHEMA::s02shop TO {READER}",
    ):
        cur.execute(stmt)
    sid = new_id("mssql")
    ws = dp_workspace["workspace_id"]
    config = {"host": HOST, "port": PORT, "database": DATABASE, "username": READER, "schemas": ["s02shop"]}
    with dp_session_factory() as s:
        s.add(Source(id=sid, workspace_id=ws, kind="sqlserver", name="s02 shop (SQL Server)", config=config,
                     secret_ref="env:ANALYSTOS_SOURCE_SECRET_S02_MSSQL", status="ready", execution_mode="pushdown"))
        s.flush()
        aid = new_id("ast")
        s.add(SourceAsset(id=aid, source_id=sid, workspace_id=ws, schema_name="s02shop", name="events",
                          source_name="events", selected=True, row_count=ROWS))
        s.flush()
        for i, (col, typ) in enumerate((("id", "integer"), ("owner", "text"), ("amount", "numeric"), ("note", "text"))):
            s.add(SourceColumn(asset_id=aid, name=col, ordinal=i, data_type=typ))
        s.commit()
    yield SimpleNamespace(source_id=sid, workspace_id=ws)
    from analystos.gateway.engines import dispose_all

    dispose_all()  # pooled connections of the reader login close before it is dropped
    cur.execute("USE master")
    _drop(cur)
    conn.close()


@pytest.fixture()
def gw(shop, dp_settings, dp_session_factory, tmp_path, monkeypatch):
    from analystos.gateway.service import QueryGateway

    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_S02_MSSQL", READER_PASSWORD)
    settings = dp_settings.model_copy(update={"result_store_dir": tmp_path / "results"})
    return QueryGateway(settings, session_factory=dp_session_factory)


def _scope(shop, user: str):
    from analystos.contracts.policy import DataScope

    sid = shop.source_id
    return DataScope(workspace_id=shop.workspace_id, user_id=user, role="analyst", source_ids=[sid],
                     assets=["s02shop.events"], asset_sources={"s02shop.events": sid},
                     columns={"s02shop.events": ["id", "owner", "amount", "note"]}, source_dialects={sid: "tsql"},
                     max_rows=50_000, timeout_seconds=20)


def test_session_identity_reaches_the_sql_server_security_policy(gw, shop) -> None:
    """The security policy (owner = SESSION_CONTEXT(N'app.user')) filters by the end user, users alternating on
    the same gateway: SESSION_CONTEXT is read-only for the session, so a connection that carried one user's
    identity must never serve the next (S02-T5)."""
    for user in ("u_alice", "u_bob", "u_alice", "u_bob"):
        res = gw.execute(_scope(shop, user), "SELECT owner, COUNT(*) AS n FROM events GROUP BY owner",
                         actor=f"user:{user}", use_cache=False)
        assert res.rows == [[user, ROWS // 2]], user
    nobody = gw.execute(_scope(shop, "u_carol"), "SELECT COUNT(*) AS n FROM events", actor="user:u_carol",
                        use_cache=False)
    assert nobody.rows == [[0]]


def test_a_denied_column_is_refused_before_sql_server_sees_it(gw, shop) -> None:
    scope = _scope(shop, "u_alice").model_copy(update={"denied_columns": ["s02shop.events.note"]})
    ok = gw.execute(scope, "SELECT COUNT(*) AS n FROM events", actor="user:u_alice", use_cache=False)
    assert ok.rows == [[ROWS // 2]]
    for sql in ("SELECT note FROM events", "SELECT * FROM events", "SELECT n FROM (SELECT note AS n FROM events) q"):
        with pytest.raises(SQLRejected):
            gw.execute(scope, sql, actor="user:u_alice", use_cache=False)


def test_a_query_cannot_set_its_own_identity(gw, shop) -> None:
    """The identity comes from the scope only: a statement that tries to set SESSION_CONTEXT is refused."""
    for sql in ("EXEC sp_set_session_context @key = N'app.user', @value = N'u_bob'",
                "SELECT COUNT(*) FROM events; EXEC sp_set_session_context N'app.user', N'u_bob'"):
        with pytest.raises(SQLRejected):
            gw.execute(_scope(shop, "u_alice"), sql, actor="user:u_alice", use_cache=False)

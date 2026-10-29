"""The session-SQL engine (SQL Server pushdown) without a server: pymssql errors are classified by their SQL Server
/ DB-Library numbers, connections that carried session identity or failed are discarded instead of pooled, and a
pymssql connection can be cancelled. The live counterparts: `analystos certify-connector --kind sqlserver` and
tests/integration/test_gateway_mssql.py."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from analystos.core.errors import Forbidden, InvalidInput, QueryTimeout, RunCancelled, UpstreamUnavailable
from analystos.engines import sql as engine_sql

# pymssql raises OperationalError for most server errors; the module name is how the engine recognises them.
PymssqlError = type("OperationalError", (Exception,), {"__module__": "pymssql.exceptions"})


class _Inner:
    def __init__(self) -> None:
        self.cancels = 0

    def cancel(self) -> None:
        self.cancels += 1


class _Cursor:
    def __init__(self, dbapi: _DBAPI) -> None:
        self.dbapi = dbapi
        self.description = [("n", 3)]

    def execute(self, sql: str, params: object = None) -> None:
        self.dbapi.executed.append(sql)
        if self.dbapi.on_execute is not None:
            self.dbapi.on_execute(sql)

    def fetchmany(self, n: int) -> list:
        return [(1,)]

    def close(self) -> None:
        pass


class _DBAPI:
    def __init__(self, on_execute=None) -> None:  # noqa: ANN001
        self.on_execute = on_execute
        self.executed: list[str] = []
        self.rollbacks = 0
        self._conn = _Inner()

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1


class _Conn:
    def __init__(self, dbapi: _DBAPI) -> None:
        self.connection = dbapi
        self.invalidated = False

    def invalidate(self) -> None:
        self.invalidated = True


@pytest.fixture()
def fake(monkeypatch):
    holder: dict = {}

    def install(on_execute=None):  # noqa: ANN001, ANN202
        dbapi = _DBAPI(on_execute)
        conn = _Conn(dbapi)

        class _Engine:
            @contextmanager
            def connect(self):  # noqa: ANN202
                yield conn

        monkeypatch.setattr(engine_sql, "get_engine", lambda url: _Engine())
        holder.update(dbapi=dbapi, conn=conn)
        return dbapi, conn

    return install


def _raise(number: int, message: str):  # noqa: ANN202
    def on_execute(sql: str) -> None:
        if sql == "SELECT n":
            raise PymssqlError(number, message.encode())
    return on_execute


def test_permission_denied_is_a_refusal_not_an_outage(fake) -> None:
    fake(_raise(262, "CREATE TABLE permission denied in database 'db'.DB-Lib error message 20018, severity 14:\n"
                     "General SQL Server error: Check messages from the SQL Server\n"))
    with pytest.raises(Forbidden, match="CREATE TABLE permission denied in database 'db'.$"):
        engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10)


def test_a_driver_timeout_is_a_timeout_and_the_dead_connection_is_discarded(fake) -> None:
    dbapi, conn = fake(_raise(20047, "DB-Lib error message 20003, severity 6:\nTDS server connection timed out\n"
                                     "DB-Lib error message 20047, severity 9:\nDBPROCESS is dead or not enabled\n"))
    with pytest.raises(QueryTimeout):
        engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10)
    assert conn.invalidated and dbapi.rollbacks == 0  # a rollback on a dead TDS connection would mask the timeout


@pytest.mark.parametrize(("number", "error"), [(18456, UpstreamUnavailable), (20009, UpstreamUnavailable),
                                               (208, InvalidInput), (15664, InvalidInput)])
def test_other_server_errors(fake, number, error) -> None:
    fake(_raise(number, f"error {number}.DB-Lib error message 20018, severity 16:\n"))
    with pytest.raises(error, match=f"error {number}"):
        engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10)


def test_a_connection_that_carried_session_identity_is_never_pooled(fake) -> None:
    """SQL Server's read-only SESSION_CONTEXT keys outlive the statement: the next user must get a new session."""
    dbapi, conn = fake()
    identity = (("EXEC sp_set_session_context @key = N'app.user', @value = %(user)s, @read_only = 1", {"user": "u_a"}),)
    assert engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10, identity_sql=identity) == (["n"], [[1]])
    assert conn.invalidated

    dbapi, conn = fake()
    engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10)
    assert not conn.invalidated and dbapi.rollbacks == 1  # no identity: back to the pool after a rollback


def test_a_pymssql_connection_can_be_cancelled(fake) -> None:
    """pymssql keeps cancel on its inner connection; a cancel that ends the batch quietly is still a cancel."""
    seen: dict = {}

    def on_execute(sql: str) -> None:
        if sql == "SELECT n":
            seen["accepted"] = engine_sql.cancel_running("qry_cancel_probe")

    dbapi, conn = fake(on_execute)
    with pytest.raises(RunCancelled):
        engine_sql.run_session_sql("mssql+pymssql://x", "SELECT n", 10, query_id="qry_cancel_probe")
    assert seen["accepted"] and conn.invalidated
    assert engine_sql._canceller(dbapi) == dbapi._conn.cancel

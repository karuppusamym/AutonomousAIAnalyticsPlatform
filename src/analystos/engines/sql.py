"""SQL engines over SQLAlchemy/DBAPI: the Postgres engine (analytics DB and Postgres pushdown) and
the session engine that wraps any ``GenericSQLConnector`` kind (P4-E01).

The code paths are the increment-3 gateway paths, moved here unchanged: Postgres opens every query
in a READ ONLY transaction with a statement timeout (and, for staged sources, SET LOCAL ROLE to the
workspace reader role); the session engine runs the kind's catalog read-only/timeout statements on
the connection first and passes the driver query timeout in the URL.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
from typing import Any

from analystos.connectors.naming import is_safe_identifier
from analystos.core.errors import (
    AnalystOSError,
    Forbidden,
    InvalidInput,
    QueryTimeout,
    RunCancelled,
    UpstreamUnavailable,
)
from analystos.engines.base import Limits, ReaderIdentity, ReadOnlyEngine, Rows
from analystos.gateway.engines import get_engine
from analystos.gateway.types import ValidatedSQL
from analystos.gateway.values import first_line, json_safe


class PostgresEngine(ReadOnlyEngine):
    id = "engine.postgres"
    dialect = "postgres"
    features = frozenset({"pushdown"})

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        return run_postgres(identity.url or "", stmt.executable_sql, limits.max_rows, limits.timeout_seconds,
                            role=identity.role)


class SessionSQLEngine(ReadOnlyEngine):
    """Any SQLAlchemy kind whose catalog entry allows pushdown (tsql today, see connectors/kinds.py)."""

    def __init__(self, kind: str, dialect: str, *, features: frozenset[str] = frozenset({"pushdown"})) -> None:
        self.id = f"engine.{kind}"
        self.dialect = dialect
        self.features = features

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        if "pushdown" not in self.features:
            raise InvalidInput(f"{self.id} cannot query a source in place: the kind has no read-only session and its "
                               "engine is not certified; stage the source instead")
        return run_session_sql(identity.url or "", stmt.executable_sql, limits.max_rows, identity.session_sql)


# Statements in flight, by query execution id: `cancel_running` interrupts one from another thread (psycopg
# `Connection.cancel()` sends the server a cancel request; a driver without one cannot be cancelled).
_running: dict[str, Any] = {}
_cancelled: set[str] = set()
_running_lock = threading.Lock()
IdentitySQL = tuple[tuple[str, dict[str, Any]], ...]


def cancel_running(query_id: str) -> bool:
    """Ask the source to cancel ``query_id``. A cancel request only interrupts a statement that is executing, so
    one that lands during the session preamble would be lost: the id is flagged (checked right before the
    statement starts) and the request is re-sent until the statement has ended (about 10 s at most)."""
    with _running_lock:
        conn = _running.get(query_id)
        cancel = _canceller(conn)
        if conn is None or not callable(cancel):
            return False
        _cancelled.add(query_id)

    def still_running() -> bool:
        with _running_lock:
            return _running.get(query_id) is conn

    def keep_cancelling() -> None:
        for _ in range(50):
            if not still_running():
                return
            with contextlib.suppress(Exception):  # the statement may have finished in between
                cancel()
            time.sleep(0.2)

    threading.Thread(target=keep_cancelling, daemon=True, name=f"cancel-{query_id[:20]}").start()
    return True


def _canceller(conn: Any) -> Any:
    """The driver's cancel call: psycopg >= 3.2 ``cancel_safe`` / ``cancel``; pymssql keeps it on the inner
    ``_mssql`` connection (FreeTDS ``dbcancel``, which may be called while another thread waits on the batch)."""
    if conn is None:
        return None
    return (getattr(conn, "cancel_safe", None) or getattr(conn, "cancel", None)
            or getattr(getattr(conn, "_conn", None), "cancel", None))


def _raise_if_cancelled(query_id: str | None) -> None:
    if query_id:
        with _running_lock:
            if query_id in _cancelled:
                raise RunCancelled("The query was cancelled.")


def _track(query_id: str | None, conn: Any) -> None:
    if query_id:
        with _running_lock:
            _running[query_id] = conn


def _untrack(query_id: str | None) -> bool:
    """Forget a statement; True when it was cancelled on request."""
    if not query_id:
        return False
    with _running_lock:
        _running.pop(query_id, None)
        if query_id in _cancelled:
            _cancelled.discard(query_id)
            return True
    return False


def _type_code(d: Any) -> Any:
    return d[1] if isinstance(d, tuple) else getattr(d, "type_code", None)


def _fetch(cur: Any, max_rows: int, sink: Any | None) -> list[Any]:
    """Without a sink: max_rows + 1 raw rows (the extra one marks truncation). With a sink: JSON-safe batches
    stream into it until it has max_rows (it records the truncation itself) and nothing is returned."""
    if sink is None:
        return cur.fetchmany(max_rows + 1)
    from analystos.gateway.handles import BATCH_ROWS, PG_TYPE_HINTS

    desc = cur.description or []
    sink.begin([d[0] for d in desc], [PG_TYPE_HINTS.get(_type_code(d)) for d in desc])
    while sink.wants_more:
        batch = cur.fetchmany(BATCH_ROWS)
        if not batch:
            break
        sink.add([[json_safe(v) for v in r] for r in batch])
    return []


def _driver(dbapi: Any) -> Any:
    return getattr(dbapi, "driver_connection", None) or dbapi


def run_postgres(url: str, sql: str, max_rows: int, timeout: int, role: str | None = None, *, sink: Any | None = None,
                 identity_sql: IdentitySQL = (), query_id: str | None = None) -> Rows:
    """``role``: staged sources run as the owning workspace's reader role for this transaction only.
    ``identity_sql``: the source kind's session-identity statements (S02-T5), parameters bound by the driver,
    run inside the READ ONLY transaction so the source's own row security sees the end user.
    ``sink``: stream rows into a ResultSink (S02-T3) instead of returning them."""
    import psycopg

    if role is not None and not is_safe_identifier(role):
        raise Forbidden("workspace reader role name is not a safe identifier")

    engine = get_engine(url)
    raw_rows: list[Any] = []
    columns: list[str] = []
    cancelled = False
    try:
        with engine.connect() as conn:
            dbapi = conn.connection
            cur = dbapi.cursor()
            _track(query_id, _driver(dbapi))
            try:
                dbapi.rollback()  # start from a clean transaction (pool pre-ping may have opened one)
                cur.execute("SET TRANSACTION READ ONLY")
                if role is not None:
                    try:
                        cur.execute(f'SET LOCAL ROLE "{role}"')
                    except psycopg.Error:
                        raise Forbidden(f"The workspace reader role {role} is not provisioned for this reader; "
                                        "re-stage the source or run `analystos migrate`") from None
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
                cur.execute("SET LOCAL idle_in_transaction_session_timeout = 60000")
                for stmt, params in identity_sql:
                    cur.execute(stmt, params)
                _raise_if_cancelled(query_id)
                cur.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                raw_rows = _fetch(cur, max_rows, sink)
            finally:
                cancelled = _untrack(query_id)
                try:
                    dbapi.rollback()
                finally:
                    cur.close()
    except psycopg.errors.QueryCanceled:
        if cancelled:
            raise RunCancelled("The query was cancelled.") from None
        raise QueryTimeout(f"The query exceeded the {timeout}s timeout. Aggregate more or filter the data.") from None
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as exc:
        raise Forbidden(f"The source refused the query: {first_line(exc)}") from None
    except psycopg.OperationalError as exc:
        raise UpstreamUnavailable(f"The source database is unavailable: {first_line(exc)}") from None
    except psycopg.Error as exc:
        raise InvalidInput(f"The query failed in the source: {first_line(exc)}") from None
    except AnalystOSError:
        raise
    except Exception as exc:  # sqlalchemy-wrapped connection errors
        orig = getattr(exc, "orig", None)
        if isinstance(orig, psycopg.OperationalError) or exc.__class__.__name__ == "OperationalError":
            raise UpstreamUnavailable(f"The source database is unavailable: {first_line(orig or exc)}") from None
        raise
    rows = [[json_safe(v) for v in r] for r in raw_rows]
    return columns, rows


# SQL Server message numbers (sys.messages) and DB-Library client codes pymssql reports as ``args[0]``. pymssql
# raises OperationalError for most server errors, so the class name alone would call a refusal an outage.
MSSQL_DENIED = {229, 230, 262, 297, 300, 916, 15247}
MSSQL_TIMEOUT = {20003}
MSSQL_UNAVAILABLE = {20002, 20004, 20006, 20009, 20047, 4060, 18456}


_DBLIB = re.compile(r"DB-Lib error message (\d+), severity \d+:")


def _driver_error(exc: BaseException) -> tuple[set[int], str]:
    """(SQL Server / DB-Library error numbers, first line of the server's message) for a driver error,
    secret-free. pymssql puts the last code in ``args[0]`` and every DB-Library code in the message (a timeout is
    20003 followed by 20047 "DBPROCESS is dead")."""
    orig = getattr(exc, "orig", None) or exc
    args = getattr(orig, "args", ())
    if type(orig).__module__.startswith("pymssql") and len(args) >= 2 and isinstance(args[0], int):
        msg = args[1].decode("utf-8", "replace") if isinstance(args[1], (bytes, bytearray)) else str(args[1])
        numbers = {args[0], *(int(n) for n in _DBLIB.findall(msg))}
        lines = [ln.strip() for ln in _DBLIB.sub("\n", msg).splitlines()
                 if ln.strip() and not ln.strip().startswith("General SQL Server error")]
        return numbers, (lines[0] if lines else msg.strip())[:500]
    return set(), first_line(orig)


def run_session_sql(url: str, sql: str, max_rows: int, session_sql: list[str] | tuple[str, ...] = (), *,
                    sink: Any | None = None, identity_sql: IdentitySQL = (), query_id: str | None = None) -> Rows:
    """Non-postgres pushdown: the catalog's read-only/timeout session statements (if the kind
    has any) run on the same connection first; the driver URL carries the query timeout.

    A connection is closed instead of pooled when it carried session identity (SQL Server's read-only
    SESSION_CONTEXT keys last for the session and cannot be reset, so the next user would inherit or fail on
    them) or when the statement failed or was cancelled (the TDS connection may be dead or mid-batch)."""
    engine = get_engine(url)
    cancelled = False
    try:
        with engine.connect() as conn:
            dbapi = conn.connection
            cur = dbapi.cursor()
            _track(query_id, _driver(dbapi))
            discard = bool(identity_sql)
            try:
                for stmt in session_sql:
                    cur.execute(stmt)
                for stmt, params in identity_sql:
                    cur.execute(stmt, params)
                _raise_if_cancelled(query_id)
                cur.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                raw_rows = _fetch(cur, max_rows, sink)
            except BaseException:
                discard = True
                raise
            finally:
                cancelled = _untrack(query_id)
                if discard or cancelled:
                    with contextlib.suppress(Exception):
                        cur.close()
                    conn.invalidate()
                else:
                    try:
                        dbapi.rollback()
                    finally:
                        cur.close()
    except AnalystOSError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver specific
        if cancelled:
            raise RunCancelled("The query was cancelled.") from None
        numbers, text_ = _driver_error(exc)
        if numbers & MSSQL_TIMEOUT or "timeout" in text_.lower() or "timed out" in text_.lower():
            raise QueryTimeout("The query exceeded the timeout. Aggregate more or filter the data.") from None
        if numbers & MSSQL_DENIED:
            raise Forbidden(f"The source refused the query: {text_}") from None
        if numbers & MSSQL_UNAVAILABLE or (not numbers and exc.__class__.__name__ in ("OperationalError",
                                                                                      "InterfaceError")):
            raise UpstreamUnavailable(f"The source database is unavailable: {text_}") from None
        raise InvalidInput(f"The query failed in the source: {text_}") from None
    if cancelled:  # the cancel landed and the driver ended the batch without an error
        raise RunCancelled("The query was cancelled.")
    return columns, [[json_safe(v) for v in r] for r in raw_rows]

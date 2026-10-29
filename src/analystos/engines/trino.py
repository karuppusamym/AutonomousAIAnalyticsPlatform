"""Trino engine (TRN-004): a Trino catalog queried in place, and federation across catalogs of one cluster.

Read-only is enforced by the transaction, not by trusting the identity: every connection this module opens is a
``ReadOnlyConnection``, whose transactions all start with ``START TRANSACTION READ ONLY`` and which never runs a
statement outside one (its isolation level is not autocommit, so ``cursor()`` opens the transaction first). Trino
then refuses any write with READ_ONLY_VIOLATION, whatever the user could otherwise do. The Trino client tracks a
transaction only through the ``X-Trino-Started-Transaction-Id`` header of the request that began it, which is why
a ``START TRANSACTION`` *statement* sent through a cursor would not hold, and why this is a driver subclass.

The statement timeout is the ``query_max_execution_time`` session property (sent as a header with every request);
``cancel_running`` interrupts a statement through its cursor (``DELETE`` of the query's next URI). The row cap is
the validator's ``LIMIT cap + 1`` plus ``fetchmany(cap + 1)``; the cursor is cancelled afterwards so Trino stops
any remaining work.

Federation: one AnalystOS source per Trino catalog. A federated statement over sources that share one cluster and
one identity is validated in the trino dialect against every source's scope (``validate_federated_sql``), then
``qualify_catalogs`` prefixes each table with its source's catalog and the statement runs here, so the join
happens inside Trino and no leg is pulled through AnalystOS.
"""
from __future__ import annotations

import contextlib
from functools import lru_cache
from typing import Any

from analystos.core.errors import (
    AnalystOSError,
    Forbidden,
    InvalidInput,
    QueryTimeout,
    RunCancelled,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.engines.base import Limits, ReaderIdentity, ReadOnlyEngine, Rows
from analystos.engines.sql import _fetch, _raise_if_cancelled, _track, _untrack
from analystos.gateway.types import ValidatedSQL
from analystos.gateway.values import first_line, json_safe

READ_ONLY_START = "START TRANSACTION READ ONLY"
TIMEOUT_PROPERTY = "query_max_execution_time"
_REFUSED = {"READ_ONLY_VIOLATION", "PERMISSION_DENIED", "NOT_SUPPORTED"}


@lru_cache(maxsize=1)
def connection_class() -> type:
    """``trino.dbapi.Connection`` whose every transaction is READ ONLY (built lazily: trino is an optional extra)."""
    import trino.dbapi
    from trino import constants
    from trino.transaction import NO_TRANSACTION, IsolationLevel, Transaction

    class ReadOnlyTransaction(Transaction):
        def begin(self) -> None:
            """``Transaction.begin`` with READ ONLY; fails closed when Trino reports no transaction."""
            def started(response: Any) -> None:
                found = response.headers.get(constants.HEADER_STARTED_TRANSACTION)
                if found and found != NO_TRANSACTION:
                    self._id = found

            response = self._request.post(READ_ONLY_START)
            started(response)
            status = self._request.process(response)
            while status.next_uri:
                response = self._request.get(status.next_uri)
                started(response)
                status = self._request.process(response)
            if self._id == NO_TRANSACTION:
                raise Forbidden("Trino did not open a read-only transaction; the query was not run")
            self._request.transaction_id = self._id

    class ReadOnlyConnection(trino.dbapi.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["isolation_level"] = IsolationLevel.READ_COMMITTED  # not autocommit: cursor() opens a transaction
            super().__init__(*args, **kwargs)

        def start_transaction(self) -> Any:
            self._transaction = ReadOnlyTransaction(self._create_request())
            self._transaction.begin()
            return self._transaction

    return ReadOnlyConnection


def connect_args(url: str) -> dict[str, Any]:
    """DBAPI keyword arguments for a ``trino://`` SQLAlchemy URL (the driver's own translation)."""
    from sqlalchemy.engine import make_url
    from trino.sqlalchemy.dialect import TrinoDialect

    args, kwargs = TrinoDialect().create_connect_args(make_url(url))
    if args:
        raise InvalidInput("unexpected positional Trino connect arguments")
    return dict(kwargs)


def connect_read_only(url: str = "", *, session_properties: dict[str, str] | None = None, **kwargs: Any) -> Any:
    """A ``ReadOnlyConnection`` from a URL (or from DBAPI keyword arguments, as SQLAlchemy's ``do_connect`` passes)."""
    params = {**(connect_args(url) if url else {}), **kwargs}
    props = {**(params.pop("session_properties", None) or {}), **(session_properties or {})}
    return connection_class()(session_properties=props or None, **params)


def timeout_properties(timeout_seconds: int) -> dict[str, str]:
    return {TIMEOUT_PROPERTY: f"{max(1, int(timeout_seconds))}s"}


def _translate(exc: BaseException, timeout: int, cancelled: bool) -> AnalystOSError:
    import trino.exceptions as te

    if isinstance(exc, te.TrinoQueryError):
        name, message = exc.error_name or "", exc.message or first_line(exc)
        if name == "EXCEEDED_TIME_LIMIT":
            return QueryTimeout(f"The query exceeded the {timeout}s timeout. Aggregate more or filter the data.")
        if name == "USER_CANCELED" or cancelled:
            return RunCancelled("The query was cancelled.")
        if name in _REFUSED:
            return Forbidden(f"The source refused the query (read-only): {message}")
        if exc.error_type == "INSUFFICIENT_RESOURCES":
            return InvalidInput(f"The source ran out of resources for the query: {message}")
        if isinstance(exc, te.TrinoUserError):
            return InvalidInput(f"The query failed in the source: {message}")
        return UpstreamUnavailable(f"The source failed the query: {message}")
    if cancelled:
        return RunCancelled("The query was cancelled.")
    return UpstreamUnavailable(f"The source database is unavailable: {first_line(exc)}")


def run_trino(url: str, sql: str, max_rows: int, timeout: int, *, sink: Any | None = None,
              query_id: str | None = None) -> Rows:
    """Run one validated statement in a READ ONLY transaction with the timeout as a session property.
    ``sink`` / ``query_id`` as in ``engines.sql.run_postgres`` (streaming, cancel)."""
    conn = connect_read_only(url, session_properties=timeout_properties(timeout))
    cancelled = False
    try:
        cur = conn.cursor()  # opens the READ ONLY transaction
        _track(query_id, cur)
        try:
            _raise_if_cancelled(query_id)
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            raw_rows = _fetch(cur, max_rows, sink)
        finally:
            cancelled = _untrack(query_id)
            with contextlib.suppress(Exception):
                cur.cancel()  # a capped read leaves the rest of the query running otherwise
            with contextlib.suppress(Exception):
                conn.rollback()
    except AnalystOSError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver specific, translated
        raise _translate(exc, timeout, cancelled) from None
    finally:
        conn.close()
    return columns, [[json_safe(v) for v in r] for r in raw_rows]


def qualify_catalogs(sql: str, catalogs: dict[str, str]) -> str:
    """Prefix every table of a validated trino statement with its source's catalog. ``catalogs`` maps
    ``schema.table`` (lower case) to the catalog. A schema-qualified table not in the map is refused (fail
    closed); unqualified names are CTE references (the validator qualifies every real table)."""
    import sqlglot
    from sqlglot import exp

    root = sqlglot.parse_one(sql, read="trino")
    for table in root.find_all(exp.Table):
        if not table.args.get("db"):
            continue
        if table.args.get("catalog") is not None:
            raise SQLRejected(f"Table {table.sql(dialect='trino')} is already catalog-qualified")
        key = f"{table.db}.{table.name}".lower()
        catalog = catalogs.get(key)
        if not catalog:
            raise SQLRejected(f"Table {table.sql(dialect='trino')} has no Trino catalog in the federated scope")
        table.set("catalog", exp.to_identifier(catalog, quoted=True))
    return root.sql(dialect="trino", comments=False)


def cluster_of(config: dict[str, Any], secret_ref: str | None) -> tuple[Any, ...]:
    """What must match for two trino sources to share one federated statement: endpoint and identity."""
    return (str(config.get("host") or "").lower(), int(config.get("port") or 8080), config.get("username"),
            (config.get("http_scheme") or "http").lower(), secret_ref or "")


class TrinoEngine(ReadOnlyEngine):
    id = "engine.trino"
    dialect = "trino"
    features = frozenset({"pushdown", "federate"})

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        if not identity.url:
            raise InvalidInput("Trino pushdown needs the source's connection URL")
        return run_trino(identity.url, stmt.executable_sql, limits.max_rows, limits.timeout_seconds)

    def federate(self, tables: Any, stmt: ValidatedSQL, *, limits: Limits) -> Rows:  # noqa: ARG002
        raise InvalidInput("Trino federates in place (one catalog per source); it does not join pulled legs")

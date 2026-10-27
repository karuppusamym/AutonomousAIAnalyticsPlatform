"""DuckDB engine: DuckDB files in place, and local federation for cross-source runs (P4-E01, P4-E03).

Every connection is opened sandboxed: ``enable_external_access=false`` (no file, URL, ATTACH or
extension access beyond the database it was opened on), no extension auto-install/auto-load, and
``lock_configuration`` so a statement cannot turn any of that back on. A database file is attached
READ_ONLY. Timeouts interrupt the connection from a timer.

Federation never touches a source itself. The gateway extracts each source's *leg* (only the
referenced columns of the referenced tables) through ``QueryGateway.execute`` with that source's
own scope and identity, and hands the rows here; the federated statement, validated in the duckdb
dialect against every source's scope, then runs over those in-memory tables and nothing else.
"""
from __future__ import annotations

import threading
from typing import Any

from analystos.core.errors import Forbidden, InvalidInput, QueryTimeout, UpstreamUnavailable
from analystos.engines.base import Limits, ReaderIdentity, ReadOnlyEngine, Rows
from analystos.gateway.types import ValidatedSQL
from analystos.gateway.values import first_line, json_safe

SANDBOX = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
}
RESERVED_SCHEMAS = frozenset({"information_schema", "pg_catalog", "temp", "system"})
SOURCE_ALIAS = "__analystos_src"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class DuckDBEngine(ReadOnlyEngine):
    id = "engine.duckdb"
    dialect = "duckdb"
    features = frozenset({"pushdown", "federate"})

    def __init__(self, *, memory_limit: str = "1GB", threads: int = 2) -> None:
        self.memory_limit = memory_limit
        self.threads = threads

    def _config(self) -> dict[str, Any]:
        return {**SANDBOX, "memory_limit": self.memory_limit, "threads": self.threads}

    # -- a DuckDB file in place ---------------------------------------------------------------
    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        import duckdb

        if not identity.path:
            raise InvalidInput("DuckDB pushdown needs the database file path of the source")
        # Attached under a fixed alias and made the default catalog, so "schema.table" never collides with
        # a catalog named after the file (crm.duckdb holding schema crm). External access is switched off
        # right after the ATTACH, then the configuration is locked.
        config = {k: v for k, v in self._config().items() if k != "enable_external_access"}
        con = duckdb.connect(":memory:", config=config)
        try:
            try:
                con.execute(f"ATTACH '{identity.path.replace(chr(39), chr(39) * 2)}' AS {_q(SOURCE_ALIAS)} (READ_ONLY)")
                con.execute(f"USE {_q(SOURCE_ALIAS)}")
            except duckdb.Error as exc:
                raise UpstreamUnavailable(f"The DuckDB file could not be opened: {first_line(exc)}") from None
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            return _run(con, stmt.executable_sql, limits)
        finally:
            con.close()

    # -- local federation ---------------------------------------------------------------------
    def federate(self, tables: dict[str, Any], stmt: ValidatedSQL, *, limits: Limits) -> Rows:
        """Run ``stmt`` over ``tables`` in a sandboxed in-memory DB. A table is ("schema.table" ->) either
        (columns, rows) or a pyarrow dataset over a ResultHandle's Parquet file (S02-T7): DuckDB scans the
        dataset lazily through Arrow, so a leg is never loaded whole and no file access is enabled."""
        import duckdb
        import pyarrow as pa

        con = duckdb.connect(":memory:", config=self._config())
        try:
            for i, (asset, data_in) in enumerate(sorted(tables.items())):
                schema, table = asset.split(".", 1)
                if schema.lower() in RESERVED_SCHEMAS:
                    raise InvalidInput(f"Asset {asset} uses a reserved schema name and cannot be federated")
                if not isinstance(data_in, tuple):  # a dataset over a handle: a view, scanned when the join runs
                    leg = f"__leg_{i}"
                    con.register(leg, data_in)
                    con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
                    con.execute(f"CREATE VIEW {_q(schema)}.{_q(table)} AS SELECT * FROM {_q(leg)}")
                    continue
                columns, rows = data_in
                arrays = []
                for j, _ in enumerate(columns):
                    values = [r[j] for r in rows]
                    try:
                        arrays.append(pa.array(values))
                    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
                        arrays.append(pa.array([None if v is None else str(v) for v in values], type=pa.string()))
                if columns:
                    data = pa.Table.from_arrays(arrays, names=list(columns))
                else:
                    data = pa.table({"_row": pa.array([1] * len(rows), type=pa.int8())})
                leg = f"__leg_{i}"
                con.register(leg, data)
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
                con.execute(f"CREATE TABLE {_q(schema)}.{_q(table)} AS SELECT * FROM {_q(leg)}")
                con.unregister(leg)
            con.execute("SET lock_configuration = true")
            return _run(con, stmt.executable_sql, limits)
        finally:
            con.close()


class DraftFederationEngine(ReadOnlyEngine):
    """A federating engine that exists behind the protocol but has no live certification (Trino, Spark)."""

    def __init__(self, kind: str, dialect: str) -> None:
        self.id = f"engine.{kind}"
        self.dialect = dialect
        self.features = frozenset({"federate"})

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:  # noqa: ARG002
        raise InvalidInput(f"{self.id} is a draft engine: it runs only after a live certification (P4-E02)")

    def federate(self, tables: Any, stmt: ValidatedSQL, *, limits: Limits) -> Rows:  # noqa: ARG002
        raise InvalidInput(f"{self.id} is a draft engine: it runs only after a live certification (P4-E02)")


def _run(con: Any, sql: str, limits: Limits) -> Rows:
    import duckdb

    timer = threading.Timer(max(1, int(limits.timeout_seconds)), con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        raw_rows = cur.fetchmany(limits.max_rows + 1)
    except duckdb.InterruptException:
        raise QueryTimeout(f"The query exceeded the {limits.timeout_seconds}s timeout. Aggregate more or filter the "
                           "data.") from None
    except duckdb.PermissionException as exc:
        raise Forbidden(f"The engine refused the query: {first_line(exc)}") from None
    except duckdb.Error as exc:
        raise InvalidInput(f"The query failed in the engine: {first_line(exc)}") from None
    finally:
        timer.cancel()
    return columns, [[json_safe(v) for v in r] for r in raw_rows]

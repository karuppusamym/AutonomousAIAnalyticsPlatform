"""Generic SQL connector: any SQLAlchemy-reachable database described in config/source_kinds.yaml.

One implementation for every SQL kind (MySQL, Oracle, Snowflake, BigQuery, Databricks, Trino,
ClickHouse, DuckDB/SQLite files, ...). The catalog (``connectors/kinds.py``) decides the URL, the
dialect, whether the source is pushdown or staged, the read-only session statements and the
row-estimate strategy; this module does the work:

* ``test``      connect with a bounded connect timeout and run a trivial query; failures become a
                short, secret-free reason (authentication, host, refused, timeout, database, driver);
* ``discover``  SQLAlchemy Inspector (DuckDB: its duckdb_*() functions) over ``config.schemas`` or
                every non-system schema, include/exclude fnmatch filters on ``schema`` and
                ``schema.table`` (case-insensitive, exclude wins), tables + views, normalized column
                types, primary keys, foreign keys, comments and cheap row estimates; bounded by
                ``config.max_tables`` (default 2000);
* ``extract``   (staged kinds) streams the asset as Arrow batches of 10k rows over a read-only
                session, bounded by ``max_rows`` and chosen by the declared sampling strategy
                (``connectors/sampling.py``), recording truncation and population in ``last_snapshot``;
* ``sqlalchemy_url`` / ``query_url`` / ``session_statements`` (pushdown kinds) for the gateway.

Staged assets get sanitized names (what the staging loader creates); pushdown assets keep their
origin names. Credentials come from ``secret_ref`` just in time and never enter logs or reprs.
"""
from __future__ import annotations

import base64
import contextlib
import json
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus

import pyarrow as pa
import sqlalchemy as sa
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import ObjectKind
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool

from analystos.connectors import kinds as catalog
from analystos.connectors import sampling
from analystos.connectors.base import ConnectionTest, DiscoveredAsset
from analystos.connectors.naming import sanitize_identifier, unique_identifiers
from analystos.connectors.secrets import resolve_secret
from analystos.connectors.servicenow import arrow_type
from analystos.connectors.sql_metadata import build_assets, normalize_sql_type
from analystos.core.errors import InvalidInput, NotFound, UpstreamUnavailable
from analystos.core.logging import get_logger

CHUNK_ROWS = 10_000
DEFAULT_MAX_TABLES = 2000
DEFAULT_ROW_COUNT_CAP = 1_000_000
MAX_STAGED_TABLE_NAME = 63 - len("__load")  # staging.loader.LOAD_SUFFIX
MAX_COUNTED_TABLES = 500

_log = get_logger(__name__)

# Cheap statistics per strategy. Each returns rows (s, t, n); :schemas is an expanding parameter.
ROW_ESTIMATES: dict[str, str] = {
    "pg_class": (
        "SELECT ns.nspname AS s, c.relname AS t, CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS n "
        "FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p', 'm') AND ns.nspname IN :schemas"
    ),
    "redshift_svv_table_info": 'SELECT "schema" AS s, "table" AS t, tbl_rows AS n FROM svv_table_info WHERE "schema" IN :schemas',
    "mysql_information_schema": (
        "SELECT table_schema AS s, table_name AS t, table_rows AS n FROM information_schema.tables "
        "WHERE table_schema IN :schemas AND table_type = 'BASE TABLE'"
    ),
    "sqlserver_partition_stats": (
        "SELECT sc.name AS s, o.name AS t, SUM(p.row_count) AS n FROM sys.dm_db_partition_stats p "
        "JOIN sys.objects o ON o.object_id = p.object_id JOIN sys.schemas sc ON sc.schema_id = o.schema_id "
        "WHERE p.index_id IN (0, 1) AND sc.name IN :schemas GROUP BY sc.name, o.name"
    ),
    "oracle_all_tables": "SELECT owner AS s, table_name AS t, num_rows AS n FROM all_tables WHERE owner IN :schemas",
    "snowflake_information_schema": (
        "SELECT table_schema AS s, table_name AS t, row_count AS n FROM information_schema.tables WHERE table_schema IN :schemas"
    ),
    "clickhouse_system_tables": "SELECT database AS s, name AS t, total_rows AS n FROM system.tables WHERE database IN :schemas",
    "duckdb_estimated_size": (
        "SELECT schema_name AS s, table_name AS t, estimated_size AS n FROM duckdb_tables() "
        "WHERE database_name = current_database() AND schema_name IN :schemas"
    ),
}

PROBE_SQL = {"oracle": "SELECT 1 FROM DUAL"}

_DUCKDB_SCHEMAS = "SELECT DISTINCT schema_name FROM duckdb_schemas() WHERE database_name = current_database()"
_DUCKDB_TABLES = ("SELECT schema_name, table_name, comment FROM duckdb_tables() "
                  "WHERE database_name = current_database() AND NOT temporary")
_DUCKDB_VIEWS = ("SELECT schema_name, view_name, comment FROM duckdb_views() "
                 "WHERE database_name = current_database() AND NOT internal AND NOT temporary")
_DUCKDB_COLUMNS = ("SELECT schema_name, table_name, column_name, data_type, is_nullable, column_index, comment "
                   "FROM duckdb_columns() WHERE database_name = current_database()")
_DUCKDB_CONSTRAINTS = ("SELECT schema_name, table_name, constraint_type, constraint_column_names, referenced_table, "
                       "referenced_column_names FROM duckdb_constraints() WHERE database_name = current_database() "
                       "AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')")


# ------------------------------------------------------------------------------------------ helpers


def _as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _match_any(value: str, patterns: Iterable[str]) -> bool:
    lowered = value.lower()
    return any(fnmatchcase(lowered, p.lower()) for p in patterns)


def matches_filters(schema: str, table: str | None, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> bool:
    """Include/exclude rule shared by discovery and extraction.

    Patterns are fnmatch globs matched case-insensitively against ``schema`` and ``schema.table``.
    Exclude wins. With include patterns, an object must match one of them (a schema pattern such
    as ``sales`` includes all its tables; ``*.orders`` names a table in any schema). ``table=None``
    asks whether a schema can contain anything included (used to prune whole schemas)."""
    include, exclude = list(include), list(exclude)
    candidates = [schema] if table is None else [schema, f"{schema}.{table}"]
    if any(_match_any(c, exclude) for c in candidates):
        return False
    if not include:
        return True
    if table is None:  # can a table in this schema still match an include pattern?
        return any(_match_any(schema, [p]) or (("." in p) and _match_any(schema, [p.split(".", 1)[0]])) for p in include)
    return any(_match_any(c, include) for c in candidates)


def is_system_schema(schema: str, patterns: Iterable[str]) -> bool:
    return _match_any(schema, patterns)


def describe_error(exc: BaseException, *, secrets: Iterable[str | None] = ()) -> str:
    """A short, actionable reason for a connection failure, with any secret scrubbed."""
    orig = getattr(exc, "orig", None) or exc
    raw = (str(orig).strip().splitlines() or [orig.__class__.__name__])[0][:300]
    low = raw.lower()
    if any(k in low for k in ("password authentication failed", "access denied", "login failed", "authentication failed",
                              "invalid username/password", "ora-01017", "incorrect username or password", "unauthorized")):
        reason = "authentication failed (check config.username and the secret_ref)"
    elif any(k in low for k in ("could not translate host", "name or service not known", "nodename nor servname",
                                "getaddrinfo", "unknown host", "no such host", "temporary failure in name resolution")):
        reason = "host not found (check config.host)"
    elif any(k in low for k in ("connection refused", "can't connect", "could not connect", "actively refused",
                                "network is unreachable", "no route to host")):
        reason = "connection refused (check host, port and network access)"
    elif "timeout" in low or "timed out" in low:
        reason = "connection timed out"
    elif any(k in low for k in ("unknown database", "does not exist", "cannot open database", "ora-12514")):
        reason = "database not found (check config.database)"
    elif any(k in low for k in ("unable to open database file", "no such file", "cannot open file", "io error")):
        reason = "file could not be opened"
    else:
        reason = f"{orig.__class__.__name__}: {raw}"
    for secret in secrets:
        if secret and len(secret) >= 3:
            # the raw value, and the forms a driver may echo inside a URL or a token
            for form in {secret, quote(secret, safe=""), quote_plus(secret), base64.b64encode(secret.encode()).decode()}:
                reason = reason.replace(form, "***")
    return reason


def _type_name(type_: Any, dialect: Any) -> str:
    if type_ is None:
        return "text"
    try:
        return str(type_.compile(dialect=dialect))
    except Exception:  # noqa: BLE001 - some dialect types cannot compile standalone
        return type(type_).__name__


def _coerce(value: Any, normalized: str) -> Any:
    if value is None:
        return None
    if normalized in ("integer", "bigint"):
        if isinstance(value, bool):
            return int(value)
        return int(value) if not isinstance(value, str) else int(float(value))
    if normalized in ("numeric", "double"):
        return float(value)
    if normalized == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes", "y")
        if isinstance(value, (bytes, bytearray)):
            return any(value)
        return bool(value)
    if normalized == "date":
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if normalized == "timestamp":
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        elif isinstance(value, date) and not isinstance(value, datetime):
            value = datetime(value.year, value.month, value.day)
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value
    if normalized == "json":
        return value if isinstance(value, str) else json.dumps(value, default=str)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value if isinstance(value, str) else str(value)


def _arrow_column(values: list[Any], normalized: str) -> pa.Array:
    try:
        return pa.array([_coerce(v, normalized) for v in values], type=arrow_type(normalized))
    except (ValueError, TypeError, OverflowError, pa.ArrowException):
        _log.warning("column values did not fit %s; staging them as text", normalized)
        return pa.array([None if v is None else (v if isinstance(v, str) else str(v)) for v in values], type=pa.string())


def _rows_to_batch(rows: list[Any], names: list[str], normalized: list[str]) -> pa.RecordBatch:
    columns = list(zip(*rows, strict=True)) if rows else [() for _ in names]
    arrays = [_arrow_column(list(col), norm) for col, norm in zip(columns, normalized, strict=True)]
    return pa.RecordBatch.from_arrays(arrays, names=names)


def _key(schema: str | None, table: str, default_schema: str) -> tuple[str, str]:
    return (schema or default_schema, table)


# ------------------------------------------------------------------------------------------ connector


class GenericSQLConnector:
    """Connector for any SQL kind in the source-kind catalog (see module docstring)."""

    def __init__(
        self,
        kind: str,
        config: dict[str, Any],
        secret_ref: str | None = None,
        *,
        password: str | None = None,
        execution_mode: str | None = None,
        allowed_dir: Path | str | None = None,
    ) -> None:
        self.spec = catalog.get_kind(kind)
        if not self.spec.is_sql:
            raise InvalidInput(f"{self.spec.label} is not a SQL source kind")
        self.kind = self.spec.kind
        self.config = dict(config or {})
        self.execution_mode: Literal["pushdown", "staged"] = catalog.execution_mode_for(
            self.kind, execution_mode or self.config.get("execution_mode"))
        self.source_dialect = self.spec.sqlglot_dialect
        self.dialect = catalog.dialect_for(self.kind, self.execution_mode)
        missing = catalog.missing_config(self.kind, self.config)
        if missing:
            raise InvalidInput(f"{self.spec.label} source needs config.{', config.'.join(missing)}")
        if "path" in self.spec.required:
            self.config["path"] = str(self._resolve_path(self.config["path"], allowed_dir))
        self.include = _as_list(self.config.get("include"))
        self.exclude = _as_list(self.config.get("exclude"))
        self.max_tables = int(self.config.get("max_tables") or DEFAULT_MAX_TABLES)
        if self.max_tables < 1:
            raise InvalidInput("config.max_tables must be at least 1")
        self.connect_timeout = int(self.config.get("connect_timeout") or 10)
        explicit = _as_list(self.config.get("schemas"))
        if explicit:
            self.schemas: list[str] | None = [s for s in explicit if not is_system_schema(s, self.spec.system_schemas)]
            if not self.schemas:
                raise InvalidInput(f"{self.spec.label} source needs at least one non-system schema in config.schemas")
        elif self.spec.default_schemas is not None:
            self.schemas = [s.format(**{k: self.config.get(k, "") for k in ("database", "username")})
                            for s in self.spec.default_schemas]
            self.schemas = [s for s in self.schemas if s]
        else:
            self.schemas = None  # every non-system schema
        self._secret_ref = secret_ref
        self._password = password
        self._engine: Engine | None = None
        self.truncated = False
        self.skipped: list[dict[str, str]] = []
        self.last_snapshot: dict[str, Any] | None = None  # set by extract(): what the snapshot is (P4-C12)

    # -- identity / URLs -------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"GenericSQLConnector(kind={self.kind!r}, target={self._target()!r}, mode={self.execution_mode!r})"

    def _target(self) -> str:
        c = self.config
        if c.get("path"):
            return Path(str(c["path"])).name
        host = c.get("host") or c.get("account") or c.get("project") or ""
        port = c.get("port") or self.spec.default_port
        db = c.get("database") or c.get("catalog") or ""
        return f"{host}{':' + str(port) if port and c.get('host') else ''}/{db}"

    @staticmethod
    def _resolve_path(raw: Any, allowed_dir: Path | str | None) -> Path:
        from analystos.connectors.csv_file import default_upload_dir

        base = Path(allowed_dir or default_upload_dir()).resolve()
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base):
            raise InvalidInput(f"Database file path must be inside the upload directory ({base})")
        return resolved

    def _secret(self) -> str | None:
        return self._password if self._password is not None else resolve_secret(self._secret_ref)

    def sqlalchemy_url(self) -> str:
        """URL of the configured (least-privilege) identity. Contains the secret: never log it."""
        return catalog.build_url(self.kind, self.config, self._secret())

    def query_url(self, timeout_seconds: int) -> str:
        """URL with the driver-level query timeout for kinds that take one as a URL parameter."""
        url = self.sqlalchemy_url()
        param = self.spec.timeout_url_param
        if not param:
            return url
        return f"{url}{'&' if '?' in url else '?'}{param}={int(timeout_seconds)}"

    def session_statements(self, timeout_seconds: int | None = None) -> list[str]:
        """Statements the gateway runs before a pushdown query: statement timeout + read-only."""
        return catalog.session_statements(self.kind, timeout_seconds)

    # -- engine ----------------------------------------------------------------------------

    def _connect_args(self) -> dict[str, Any]:
        args = dict(self.spec.connect_args)
        if self.spec.connect_timeout_arg:
            args[self.spec.connect_timeout_arg] = self.connect_timeout
        return args

    def _get_engine(self) -> Engine:
        if self._engine is None:
            catalog.require_driver(self.kind)
            engine = sa.create_engine(self.sqlalchemy_url(), connect_args=self._connect_args(), poolclass=NullPool)
            if self.spec.readonly_transaction:
                from analystos.engines.trino import connect_read_only

                @event.listens_for(engine, "do_connect")
                def _read_only_transaction(_dialect: Any, _record: Any, cargs: Any, cparams: Any) -> Any:
                    return connect_read_only(**cparams)  # every transaction it opens is READ ONLY

            statements = catalog.session_statements(self.kind, self.config.get("statement_timeout_seconds"))
            if statements:
                @event.listens_for(engine, "connect")
                def _read_only_session(dbapi_conn: Any, _record: Any) -> None:
                    cur = dbapi_conn.cursor()
                    try:
                        for stmt in statements:
                            cur.execute(stmt)
                    finally:
                        cur.close()
                    dbapi_conn.commit()  # a session SET inside a rolled-back transaction would be undone

            self._engine = engine
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- protocol: test --------------------------------------------------------------------

    def test(self) -> ConnectionTest:
        started = time.perf_counter()
        target = self._target()
        try:
            engine = self._get_engine()
            with engine.connect() as conn:
                conn.execute(text(PROBE_SQL.get(self.kind, "SELECT 1"))).scalar()
                version = conn.dialect.server_version_info
        except InvalidInput as exc:
            return ConnectionTest(ok=False, message=exc.message, latency_ms=int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return ConnectionTest(
                ok=False,
                message=f"Could not connect to {self.spec.label} {target}: {describe_error(exc, secrets=[self._safe_secret()])}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        details: dict[str, Any] = {"kind": self.kind, "execution_mode": self.execution_mode, "dialect": self.dialect,
                                   "readonly_session": bool(self.spec.session_sql.readonly) or self.spec.readonly_transaction}
        if version:
            details["version"] = ".".join(str(v) for v in version)
        return ConnectionTest(ok=True, message=f"Connected to {self.spec.label} {target}",
                              latency_ms=int((time.perf_counter() - started) * 1000), details=details)

    def _safe_secret(self) -> str | None:
        try:
            return self._secret()
        except Exception:  # noqa: BLE001
            return None

    # -- protocol: discover ----------------------------------------------------------------

    def discover(self) -> list[DiscoveredAsset]:
        engine = self._get_engine()
        self.truncated, self.skipped = False, []
        try:
            with engine.connect() as conn:
                objects = self._list_objects(conn)
                tables, columns, pks, fks = self._metadata(conn, objects)
        except (InvalidInput, NotFound):
            raise
        except Exception as exc:  # noqa: BLE001
            raise UpstreamUnavailable(
                f"{self.spec.label} discovery failed: {describe_error(exc, secrets=[self._safe_secret()])}"
            ) from None
        counts = self._row_estimates(engine, objects)
        assets = build_assets(tables, columns, pks, fks, counts)
        return self._staged_names(assets) if self.execution_mode == "staged" else assets

    def _default_schema(self, conn: Any) -> str:
        try:
            return conn.dialect.default_schema_name or "main"
        except Exception:  # noqa: BLE001
            return "main"

    def _candidate_schemas(self, conn: Any) -> list[str]:
        if self.schemas is not None:
            names = list(self.schemas)
        elif self.spec.catalog == "duckdb":
            names = [r[0] for r in conn.execute(text(_DUCKDB_SCHEMAS))]
        else:
            names = list(sa.inspect(conn).get_schema_names())
        return [s for s in names if not is_system_schema(s, self.spec.system_schemas)
                and matches_filters(s, None, self.include, self.exclude)]

    def _list_objects(self, conn: Any) -> list[tuple[str, str, str, str | None]]:
        """(schema, name, 'BASE TABLE'|'VIEW', comment?) after filters, bounded by max_tables."""
        found: list[tuple[str, str, str, str | None]] = []
        schemas = self._candidate_schemas(conn)
        if self.spec.catalog == "duckdb":
            wanted = set(schemas)
            rows = [(r[0], r[1], "BASE TABLE", r[2]) for r in conn.execute(text(_DUCKDB_TABLES))]
            rows += [(r[0], r[1], "VIEW", r[2]) for r in conn.execute(text(_DUCKDB_VIEWS))]
            candidates = sorted((r for r in rows if r[0] in wanted), key=lambda r: (r[0], r[1]))
        else:
            insp = sa.inspect(conn)
            candidates = []
            for schema in schemas:
                names = [(schema, t, "BASE TABLE", None) for t in insp.get_table_names(schema=schema)]
                views = set(insp.get_view_names(schema=schema))
                with contextlib.suppress(NotImplementedError, AttributeError):
                    views |= set(insp.get_materialized_view_names(schema=schema))
                names += [(schema, v, "VIEW", None) for v in views]
                candidates += sorted(names, key=lambda r: r[1])
        for obj in candidates:
            if not matches_filters(obj[0], obj[1], self.include, self.exclude):
                continue
            if len(found) >= self.max_tables:
                self.truncated = True
                _log.warning("%s discovery stopped at max_tables=%s", self.kind, self.max_tables)
                break
            found.append(obj)
        return found

    def _metadata(self, conn: Any, objects: list[tuple[str, str, str, str | None]]):  # noqa: ANN202
        tables = [{"schema": s, "table": t, "table_type": ty, "description": c} for s, t, ty, c in objects]
        if self.spec.catalog == "duckdb":
            return (tables, *self._duckdb_metadata(conn, objects))
        insp = sa.inspect(conn)
        default = self._default_schema(conn)
        columns: list[dict[str, Any]] = []
        pks: list[dict[str, Any]] = []
        fks: list[dict[str, Any]] = []
        comments: dict[tuple[str, str], str | None] = {}
        by_schema: dict[str, list[tuple[str, str]]] = {}
        for s, t, ty, _ in objects:
            by_schema.setdefault(s, []).append((t, ty))
        for schema, entries in by_schema.items():
            names = [t for t, _ in entries]
            table_names = [t for t, ty in entries if ty == "BASE TABLE"]
            cols = self._multi(insp.get_multi_columns, insp.get_columns, schema, names, ObjectKind.ANY, default)
            for (sch, tbl), items in cols.items():
                for i, col in enumerate(items or [], start=1):
                    columns.append({"schema": sch, "table": tbl, "column": col["name"],
                                    "data_type": _type_name(col.get("type"), conn.dialect),
                                    "is_nullable": bool(col.get("nullable", True)), "ordinal": i,
                                    "description": col.get("comment")})
            if table_names:
                for (sch, tbl), pk in self._multi(insp.get_multi_pk_constraint, insp.get_pk_constraint, schema,
                                                  table_names, ObjectKind.TABLE, default).items():
                    for col in (pk or {}).get("constrained_columns") or []:
                        pks.append({"schema": sch, "table": tbl, "column": col})
                for (sch, tbl), items in self._multi(insp.get_multi_foreign_keys, insp.get_foreign_keys, schema,
                                                     table_names, ObjectKind.TABLE, default).items():
                    for fk in items or []:
                        for col, ref in zip(fk.get("constrained_columns") or [], fk.get("referred_columns") or [],
                                            strict=False):
                            fks.append({"schema": sch, "table": tbl, "column": col,
                                        "ref_schema": fk.get("referred_schema") or sch,
                                        "ref_table": fk.get("referred_table"), "ref_column": ref})
            try:
                for (sch, tbl), comment in self._multi(insp.get_multi_table_comment, insp.get_table_comment, schema,
                                                       names, ObjectKind.ANY, default).items():
                    comments[(sch, tbl)] = (comment or {}).get("text")
            except NotImplementedError:
                pass
        for t in tables:
            t["description"] = t["description"] or comments.get((t["schema"], t["table"]))
        return tables, columns, pks, fks

    def _multi(self, multi: Any, single: Any, schema: str, names: list[str], kind: Any, default: str) -> dict:
        """Batch reflection (one round trip per schema) with a per-object fallback, keyed (schema, name)."""
        try:
            raw = multi(schema=schema, filter_names=names, kind=kind)
            return {_key(k[0], k[1], schema): v for k, v in raw.items() if k[1] in set(names)}
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - some dialects only implement the single-object API
            _log.info("%s batch reflection failed (%s); falling back per object", self.kind, exc.__class__.__name__)
        out: dict[tuple[str, str], Any] = {}
        for name in names:
            try:
                out[(schema, name)] = single(name, schema=schema)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001 - one unreadable object must not hide the rest
                self.skipped.append({"asset": f"{schema}.{name}", "reason": exc.__class__.__name__})
        return out

    def _duckdb_metadata(self, conn: Any, objects: list[tuple[str, str, str, str | None]]):  # noqa: ANN202
        wanted = {(s, t) for s, t, _, _ in objects}
        columns = [{"schema": r[0], "table": r[1], "column": r[2], "data_type": r[3], "is_nullable": bool(r[4]),
                    "ordinal": int(r[5]), "description": r[6]}
                   for r in conn.execute(text(_DUCKDB_COLUMNS)) if (r[0], r[1]) in wanted]
        pks: list[dict[str, Any]] = []
        fks: list[dict[str, Any]] = []
        for schema, table, ctype, cols, ref_table, ref_cols in conn.execute(text(_DUCKDB_CONSTRAINTS)):
            if (schema, table) not in wanted:
                continue
            if ctype == "PRIMARY KEY":
                pks += [{"schema": schema, "table": table, "column": c} for c in cols or []]
            else:  # DuckDB foreign keys cannot cross schemas
                fks += [{"schema": schema, "table": table, "column": c, "ref_schema": schema, "ref_table": ref_table,
                         "ref_column": rc} for c, rc in zip(cols or [], ref_cols or [], strict=False)]
        return columns, pks, fks

    def _row_estimates(self, engine: Engine, objects: list[tuple[str, str, str, str | None]]) -> list[dict[str, Any]]:
        """Best effort, in its own connection: a failure (missing grant, old version) means unknown."""
        strategy = self.spec.row_estimate
        tables = [(s, t) for s, t, ty, _ in objects if ty == "BASE TABLE"]
        if strategy == "none" or not tables:
            return []
        schemas = sorted({s for s, _ in tables})
        known = {(s.lower(), t.lower()): (s, t) for s, t in tables}
        out: list[dict[str, Any]] = []
        try:
            with engine.connect() as conn:
                if strategy == "count_capped":
                    cap = int(self.config.get("row_count_cap") or DEFAULT_ROW_COUNT_CAP)
                    for s, t in tables[:MAX_COUNTED_TABLES]:
                        inner = sa.select(sa.literal(1)).select_from(sa.table(t, schema=s)).limit(cap).subquery()
                        n = conn.execute(sa.select(sa.func.count()).select_from(inner)).scalar()
                        out.append({"schema": s, "table": t, "row_count": n})
                    return out
                sql = ROW_ESTIMATES.get(strategy)
                if sql is None:
                    return []
                variants = sorted({v for s in schemas for v in (s, s.upper(), s.lower())})
                stmt = text(sql).bindparams(sa.bindparam("schemas", expanding=True))
                for s, t, n in conn.execute(stmt, {"schemas": variants}):
                    hit = known.get((str(s).lower(), str(t).lower()))
                    if hit is not None:
                        out.append({"schema": hit[0], "table": hit[1], "row_count": n})
        except Exception as exc:  # noqa: BLE001
            _log.info("%s row estimates unavailable: %s", self.kind, exc.__class__.__name__)
        return out

    def _staged_names(self, assets: list[DiscoveredAsset]) -> list[DiscoveredAsset]:
        """Staged assets are named as the staging loader will create them: ``[a-z0-9_]``, table name
        alone unless it exists in several schemas (then ``schema_table``); columns likewise, with the
        origin name kept as ``business_name``."""
        seen: dict[str, int] = {}
        for a in assets:
            key = sanitize_identifier(a.name, max_length=MAX_STAGED_TABLE_NAME, fallback="t")
            seen[key] = seen.get(key, 0) + 1
        raw = []
        for a in assets:
            base = sanitize_identifier(a.name, max_length=MAX_STAGED_TABLE_NAME, fallback="t")
            raw.append(f"{a.schema_name}_{a.name}" if seen[base] > 1 else a.name)
        names = unique_identifiers(raw, max_length=MAX_STAGED_TABLE_NAME)
        out = []
        for a, name in zip(assets, names, strict=True):
            clean = unique_identifiers([c.name for c in a.columns])
            cols = [c.model_copy(update={"name": n, "business_name": c.name if c.name != n else c.business_name})
                    for c, n in zip(a.columns, clean, strict=True)]
            out.append(a.model_copy(update={"name": name, "columns": cols}))
        return out

    # -- protocol: extract -----------------------------------------------------------------

    def _locate(self, asset: DiscoveredAsset) -> tuple[str, str]:
        schema, sep, table = asset.source_name.partition(".")
        if not sep or not schema or not table:
            raise InvalidInput(f"Asset {asset.source_name!r} is not a 'schema.table' name; rediscover the source")
        if self.schemas is not None and schema.lower() not in {s.lower() for s in self.schemas}:
            raise InvalidInput(f"Schema {schema!r} is not configured for this source")
        if is_system_schema(schema, self.spec.system_schemas) or not matches_filters(schema, table, self.include, self.exclude):
            raise InvalidInput(f"Asset {asset.source_name} is excluded by the source's filters")
        return schema, table

    def _table_columns(self, conn: Any, schema: str, table: str) -> list[tuple[str, Any, str]]:
        """(origin name, SQLAlchemy type or None, normalized type) in ordinal order."""
        if self.spec.catalog == "duckdb":
            rows = conn.execute(text(_DUCKDB_COLUMNS + " AND schema_name = :s AND table_name = :t ORDER BY column_index"),
                                {"s": schema, "t": table}).all()
            return [(r[2], None, normalize_sql_type(r[3])) for r in rows]
        cols = sa.inspect(conn).get_columns(table, schema=schema)
        return [(c["name"], c.get("type"), normalize_sql_type(_type_name(c.get("type"), conn.dialect))) for c in cols]

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        """Stream up to ``max_rows`` rows of the asset in 10k-row Arrow batches (read-only session,
        server-side cursor where the driver has one). Column names match discovery's staged names.

        Rows are chosen by the declared sampling strategy (``connectors/sampling.py``); one extra row
        is read to know, not guess, whether the cap truncated the population. ``last_snapshot``
        records what the snapshot is once the stream is exhausted."""
        if self.execution_mode != "staged":
            raise InvalidInput(f"{self.spec.label} sources in pushdown mode are queried in place; they are not staged")
        schema, table = self._locate(asset)
        spec = sampling.sampling_for(self.config, asset.source_name, asset.name)
        cap = max(0, int(max_rows))
        self.last_snapshot = None
        engine = self._get_engine()
        with engine.connect() as conn:
            try:
                origin = self._table_columns(conn, schema, table)
            except sa.exc.NoSuchTableError:
                raise NotFound(f"{asset.source_name} no longer exists in the source") from None
            if not origin:
                raise NotFound(f"{asset.source_name} no longer exists in the source")
            clean = unique_identifiers([o[0] for o in origin])
            by_clean = dict(zip(clean, origin, strict=True))
            wanted = [c.name for c in asset.columns] or clean
            missing = [c for c in wanted if c not in by_clean]
            if missing:
                raise InvalidInput(f"{asset.source_name} no longer has columns {missing}; rediscover the source")
            picked = [by_clean[c] for c in wanted]
            time_col = self._time_column(spec, asset, by_clean) if spec is not None and spec.method == "time_window" else None
            ts_index = next((i for i, p in enumerate(picked) if p is time_col), None)
            table_cols = picked + ([time_col] if time_col is not None and ts_index is None else [])
            cols = [sa.column(name, type_) if type_ is not None else sa.column(name) for name, type_, _ in table_cols]
            plain = sa.table(table, *[sa.column(name) for name, _, _ in table_cols], schema=schema)
            params: dict[str, Any] = {}
            where: list[Any] = []
            order: list[Any] = []
            if spec is not None and spec.method == "tablesample":
                seed = spec.seed if spec.seed is not None else sampling.default_seed(asset.source_name)
                clause = sampling.tablesample_sql(self.spec.sqlglot_dialect, spec.percent or 0, spec.sampler, seed=seed)
                tbl = _SampledTable(table, *cols, schema=schema, suffix=clause.from_suffix)
                if clause.where:
                    where.append(text(clause.where))
                params.update(seed=seed, sampler=clause.sampler, native=clause.native, repeatable=clause.repeatable)
            else:
                tbl = sa.table(table, *cols, schema=schema)
            cutoff = None
            if time_col is not None:
                ts = tbl.c[time_col[0]]
                end = datetime.now(UTC).replace(tzinfo=None) if spec.anchor == "now" else \
                    conn.execute(sa.select(sa.func.max(plain.c[time_col[0]]))).scalar()
                end = _coerce(end, "timestamp") if end is not None else None
                cutoff = end - sampling.window_delta(spec.window or "") if end is not None else None
                where.append(ts >= sa.bindparam("cutoff", cutoff, type_=ts.type) if cutoff is not None else sa.false())
                order.append(ts.desc())
                params.update(column=time_col[0], window_start=_iso(cutoff), window_end=_iso(end))
            total, basis, population = self._population(conn, engine, schema, table, plain, spec, time_col, cutoff)
            stmt = sa.select(*[tbl.c[name] for name, _, _ in picked])
            for w in where:
                stmt = stmt.where(w)
            if order:
                stmt = stmt.order_by(*order)
            stmt = stmt.limit(cap + 1)
            normalized = [n for _, _, n in picked]
            staged, truncated, last_ts = 0, False, None
            result = conn.execution_options(stream_results=True, max_row_buffer=CHUNK_ROWS).execute(stmt)
            try:
                while True:
                    rows = result.fetchmany(CHUNK_ROWS)
                    if not rows:
                        break
                    if staged + len(rows) > cap:
                        rows, truncated = rows[:cap - staged], True
                    if rows:
                        staged += len(rows)
                        if ts_index is not None:
                            last_ts = rows[-1][ts_index]
                        yield _rows_to_batch([tuple(r) for r in rows], wanted, normalized)
                    if truncated:
                        break
            finally:
                result.close()
            if truncated and last_ts is not None:
                params["effective_window_start"] = _iso(_coerce(last_ts, "timestamp"))
            self.last_snapshot = sampling.snapshot_record(
                spec=spec, rows_staged=staged, cap=cap, truncated=truncated, source_total_rows=total,
                total_basis=basis, population_rows=population, **params)

    def _time_column(self, spec: Any, asset: DiscoveredAsset, by_clean: dict[str, tuple[str, Any, str]]):  # noqa: ANN202
        """The declared time_window column, by staged or origin name; it must be a date/timestamp."""
        wanted = str(spec.column)
        hit = by_clean.get(wanted) or next((o for o in by_clean.values() if o[0].lower() == wanted.lower()), None)
        if hit is None:
            raise InvalidInput(f"sampling column {wanted!r} is not a column of {asset.source_name}")
        if hit[2] not in ("timestamp", "date"):
            raise InvalidInput(f"sampling column {wanted!r} of {asset.source_name} is {hit[2]}, not a date or timestamp")
        return hit

    def _population(self, conn: Any, engine: Engine, schema: str, table: str, plain: Any, spec: Any,
                    time_col: Any, cutoff: datetime | None) -> tuple[int | None, str, int | None]:
        """(origin table rows, how they were measured, rows in the declared window). Exact COUNT(*)
        unless the source asks for the catalog estimate; a failure is recorded as unavailable."""
        total, basis = None, "unavailable"
        if spec is not None and spec.count == "estimate" and self.spec.row_estimate != "none":
            hit = self._row_estimates(engine, [(schema, table, "BASE TABLE", None)])
            if hit and hit[0].get("row_count") is not None:
                total, basis = int(hit[0]["row_count"]), f"estimate:{self.spec.row_estimate}"
        if total is None:
            try:
                total, basis = int(conn.execute(sa.select(sa.func.count()).select_from(plain)).scalar() or 0), "count"
            except Exception as exc:  # noqa: BLE001 - the snapshot still loads; its population is unknown
                _log.info("%s row count for %s.%s unavailable: %s", self.kind, schema, table, exc.__class__.__name__)
                conn.rollback()
        population = total
        if time_col is not None:
            try:
                population = int(conn.execute(
                    sa.select(sa.func.count()).select_from(plain)
                    .where(plain.c[time_col[0]] >= sa.bindparam("cutoff", cutoff, type_=time_col[1]))
                ).scalar() or 0) if cutoff is not None else 0
            except Exception as exc:  # noqa: BLE001
                _log.info("%s window count for %s.%s unavailable: %s", self.kind, schema, table, exc.__class__.__name__)
                conn.rollback()
                population = None
        return total, basis, population


class _SampledTable(sa.sql.expression.TableClause):
    """A table reference followed by a dialect sampling clause in FROM (only validated numbers)."""

    inherit_cache = False

    def __init__(self, name: str, *columns: Any, schema: str | None = None, suffix: str | None = None) -> None:
        super().__init__(name, *columns, schema=schema)
        self.sample_suffix = suffix


@compiles(_SampledTable)
def _compile_sampled(element: _SampledTable, compiler: Any, **kw: Any) -> str:
    rendered = compiler.visit_table(element, **kw)
    return f"{rendered} {element.sample_suffix}" if element.sample_suffix and kw.get("asfrom") else rendered


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else (None if value is None else str(value))

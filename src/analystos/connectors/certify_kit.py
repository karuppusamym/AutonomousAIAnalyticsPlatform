"""Connector certification kit (S02-T6): certify a source kind against a live engine.

    analystos certify-connector --kind postgres --dsn-ref env:ANALYSTOS_CERT_PG_DSN

The DSN reference is operator configuration (resolved as a platform secret, never logged or written). The kit
runs, through the same engine code the gateway uses:

    validator      the negative security suite for the kind's dialect (every statement must be refused)
    read_only      a write on the gateway's session must fail (READ ONLY transaction / read-only session)
    timeout        a statement past the timeout is stopped and reported as a timeout
    cancel         a statement in flight is interrupted by `cancel(query_id)`
    row_cap        the engine returns at most cap + 1 rows (the marker the gateway truncates on)
    metadata       the connector's crawl finds tables with typed columns
    type_mapping   typed literals come back JSON-safe and keep their Arrow types in a result handle
    estimate       the pre-execution cost estimate reads a plan (where the dialect has an explainer)
    identity       session identity reaches the session (kinds that declare `capabilities.session_identity`)

and writes `docs/evidence/connectors/{kind}-{YYYY-MM-DD}.md` with the outcome of every check. A kind is
`certified` only with a passing file younger than 180 days (`connectors/certification.py`). Engine-specific
checks are implemented for PostgreSQL; other kinds report the checks they cannot run as failed, so a kind is
never certified by the checks it skipped.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from analystos.core.errors import AnalystOSError, SQLRejected

EVIDENCE_DIR_NAME = Path("docs") / "evidence" / "connectors"
FIXTURE_ASSET = "cert.items"

# Statements the validator must refuse in every dialect: writes, DDL, several statements, file/network/
# session functions, catalog reads, assets outside the scope and the denied column reached in every way.
COMMON_NEGATIVE = [
    ("DROP TABLE cert.items", "DDL"),
    ("DELETE FROM cert.items", "DML"),
    ("UPDATE cert.items SET name = 'x'", "DML"),
    ("INSERT INTO cert.items (id) VALUES (1)", "DML"),
    ("CREATE TABLE cert.copy AS SELECT id FROM cert.items", "DDL"),
    ("TRUNCATE cert.items", "DDL"),
    ("GRANT SELECT ON cert.items TO public", "DCL"),
    ("SELECT id FROM cert.items; DROP TABLE cert.items", "several statements"),
    ("SELECT secret FROM cert.items", "denied column"),
    ("SELECT * FROM cert.items", "denied column through *"),
    ("SELECT s FROM (SELECT secret AS s FROM cert.items) q", "denied column through an alias"),
    ("WITH x AS (SELECT secret FROM cert.items) SELECT * FROM x", "denied column through a CTE"),
    ("SELECT id FROM cert.other", "asset outside the scope"),
    ("SELECT id FROM cert.items WHERE id IN (SELECT id FROM cert.other)", "asset outside the scope in a subquery"),
    ("SELECT pg_read_file('/etc/passwd')", "file read"),
    ("SELECT id FROM cert.items i JOIN cert.items j ON 1 = 1", "unkeyed join"),
    ("SELECT my_udf(id) FROM cert.items", "unknown function (possible UDF)"),
]
DIALECT_NEGATIVE: dict[str, list[tuple[str, str]]] = {
    "postgres": [("SELECT pg_sleep(10)", "sleep"), ("SELECT current_setting('is_superuser')", "session state"),
                 ("SELECT * FROM pg_catalog.pg_authid", "system catalog"), ("COPY cert.items TO PROGRAM 'id'", "COPY"),
                 ("SELECT dblink('host=evil', 'select 1')", "network"), ("SELECT id INTO cert.copy FROM cert.items", "SELECT INTO")],
    "tsql": [("EXEC xp_cmdshell 'dir'", "shell"), ("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'select 1')", "remote"),
             ("WAITFOR DELAY '00:00:10'", "sleep"), ("SELECT id FROM cert.items WITH (NOLOCK)", "table hint")],
    "snowflake": [("SELECT * FROM @stage/file.csv", "stage read"), ("SELECT SYSTEM$WAIT(10)", "sleep"),
                  ("SELECT id FROM cert.items AT(OFFSET => -60)", "time travel")],
    "bigquery": [("SELECT * FROM cert.items FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", "time travel"),
                 ("SELECT * FROM EXTERNAL_QUERY('conn', 'select 1')", "external query")],
    "duckdb": [("SELECT * FROM read_csv('/etc/passwd')", "file read"), ("ATTACH '/tmp/x.db' AS x", "attach")],
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    seconds: float = 0.0


@dataclass
class Report:
    kind: str
    dialect: str
    engine: str
    checks: list[Check] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def _scope(dialect: str) -> Any:
    from analystos.contracts.policy import DataScope

    return DataScope(workspace_id="ws_cert", user_id="u_cert", role="analyst", source_ids=["src_cert"],
                     assets=[FIXTURE_ASSET], asset_sources={FIXTURE_ASSET: "src_cert"},
                     columns={FIXTURE_ASSET: ["id", "name", "secret"]}, denied_columns=[f"{FIXTURE_ASSET}.secret"],
                     source_dialects={"src_cert": dialect})


def validator_suite(dialect: str) -> Check:
    """Every statement of the suite must be refused by the validator in this dialect; a safe one must pass."""
    from analystos.gateway.validator import validate_sql

    scope = _scope(dialect)
    leaked = []
    suite = COMMON_NEGATIVE + DIALECT_NEGATIVE.get(dialect, [])
    for sql, why in suite:
        try:
            validate_sql(scope, sql, max_rows=10)
            leaked.append(f"{why}: {sql}")
        except SQLRejected:
            continue
        except Exception as exc:  # noqa: BLE001 - a crash is not a refusal
            leaked.append(f"{why}: {sql} ({exc.__class__.__name__})")
    try:
        validate_sql(scope, "SELECT id, name FROM cert.items WHERE id > 1", max_rows=10)
    except SQLRejected as exc:
        leaked.append(f"a safe statement was refused: {exc.message}")
    if leaked:
        return Check("validator", False, "not refused: " + "; ".join(leaked[:5]))
    return Check("validator", True, f"{len(suite)} hostile statements refused in {dialect}; a safe one accepted")


def _timed(name: str, fn: Callable[[], str]) -> Check:
    started = time.monotonic()
    try:
        return Check(name, True, fn(), round(time.monotonic() - started, 2))
    except AssertionError as exc:
        return Check(name, False, str(exc) or "assertion failed", round(time.monotonic() - started, 2))
    except AnalystOSError as exc:
        return Check(name, False, f"{exc.__class__.__name__}: {exc.message}", round(time.monotonic() - started, 2))
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}",
                     round(time.monotonic() - started, 2))


# ---------------------------------------------------------------------------------------------- postgres
def _pg_checks(url: str, connector: Any, report: Report, settings: Any) -> None:
    from analystos.core.errors import Forbidden, InvalidInput, QueryTimeout, RunCancelled
    from analystos.engines.sql import cancel_running, run_postgres
    from analystos.gateway.cost import postgres_estimate
    from analystos.gateway.handles import ResultSink, uri_for

    def read_only() -> str:
        for stmt in ("CREATE TEMP TABLE aos_cert_probe (x int)", "CREATE TABLE aos_cert_probe (x int)"):
            try:
                run_postgres(url, stmt, 1, 10)
            except (Forbidden, InvalidInput) as exc:
                assert "read-only" in exc.message.lower() or "read only" in exc.message.lower() \
                    or "permission" in exc.message.lower(), f"refused for another reason: {exc.message}"
                continue
            raise AssertionError(f"the write succeeded: {stmt}")
        return "writes refused inside the gateway's READ ONLY transaction"

    def timeout() -> str:
        started = time.monotonic()
        try:
            run_postgres(url, "SELECT pg_sleep(10)", 1, 1)
        except QueryTimeout:
            took = time.monotonic() - started
            assert took < 5, f"stopped only after {took:.1f}s"
            return f"stopped after {took:.1f}s with a 1s timeout"
        raise AssertionError("a 10s statement finished under a 1s timeout")

    def cancel() -> str:
        qid = f"qry_cert_{int(time.time() * 1000)}"
        out: dict[str, Any] = {}

        def run() -> None:
            try:
                run_postgres(url, "SELECT pg_sleep(20)", 1, 60, query_id=qid)
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        thread.start()
        for _ in range(100):
            time.sleep(0.05)
            if cancel_running(qid):
                break
        thread.join(15)
        assert isinstance(out.get("error"), RunCancelled), f"not cancelled: {out.get('error')!r}"
        return f"interrupted after {time.monotonic() - started:.1f}s"

    def row_cap() -> str:
        _, rows = run_postgres(url, "SELECT g FROM generate_series(1, 1000) g", 10, 10)
        assert len(rows) == 11, f"{len(rows)} rows for a cap of 10 (expected 11: cap + truncation marker)"
        return "1000-row statement returned cap + 1 = 11 rows"

    def metadata() -> str:
        assets = connector.discover()
        assert assets, "the crawl found no table: point the DSN at a database with at least one table"
        typed = [a for a in assets if a.columns and all(c.data_type for c in a.columns)]
        assert typed, "no table came back with typed columns"
        return f"{len(assets)} tables, {sum(len(a.columns) for a in assets)} columns (e.g. {typed[0].schema_name}.{typed[0].name})"

    def type_mapping() -> str:
        sql = ("SELECT 1::int AS i, 2::bigint AS b, 1.5::numeric AS n, 2.5::float8 AS d, true AS t, 'x'::text AS s, "
               "DATE '2026-01-02' AS dt, TIMESTAMP '2026-01-02 03:04:05' AS ts, '{\"a\": 1}'::json AS j, NULL::int AS z")
        _, rows = run_postgres(url, sql, 1, 10)
        want = [1, 2, 1.5, 2.5, True, "x", "2026-01-02", "2026-01-02T03:04:05", {"a": 1}, None]
        assert rows and rows[0] == want, f"got {rows[:1]}, want {want}"
        sink = ResultSink(settings, uri=uri_for("cert", f"qry_cert_types_{int(time.time())}"), mode="handle", max_rows=5)
        run_postgres(url, sql, 5, 10, sink=sink)
        facts = sink.finish()
        kinds = [c["type"] for c in facts["schema"]]
        from analystos.gateway.handles import path_for

        path_for(settings, facts["uri"]).unlink(missing_ok=True)
        assert kinds[:5] == ["int", "int", "float", "float", "bool"], f"handle types {kinds}"
        return "int, bigint, numeric, float8, bool, text, date, timestamp, json, NULL map to JSON-safe values and " \
               "typed Parquet columns"

    def estimate() -> str:
        _, rows = run_postgres(url, "EXPLAIN (FORMAT JSON, VERBOSE) SELECT g FROM generate_series(1, 1000) g", 1, 10)
        est = postgres_estimate(rows[0][0])
        assert est.basis == "explain" and est.rows, f"no plan estimate: {est}"
        return f"EXPLAIN read: about {est.rows} rows, {est.bytes} bytes"

    def identity() -> str:
        from analystos.connectors import kinds

        statements = kinds.get_kind(report.kind).capabilities.session_identity
        assert statements, "the kind declares no session identity"
        params = {"user": "u_cert_probe", "user_email": "cert@example.invalid", "workspace": "ws_cert"}
        _, rows = run_postgres(url, "SELECT current_setting('app.user', true), current_setting('app.workspace', true)",
                               1, 10, identity_sql=tuple((s, params) for s in statements))
        assert rows == [["u_cert_probe", "ws_cert"]], f"the session saw {rows}"
        return "app.user / app.workspace set for the transaction (a source RLS policy can read them)"

    for name, fn in (("read_only", read_only), ("timeout", timeout), ("cancel", cancel), ("row_cap", row_cap),
                     ("metadata", metadata), ("type_mapping", type_mapping), ("estimate", estimate),
                     ("identity", identity)):
        report.checks.append(_timed(name, fn))


def _engine_version(url: str) -> str:
    from sqlalchemy import text

    from analystos.gateway.engines import get_engine

    with get_engine(url).connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        host = conn.engine.url.host
    return f"PostgreSQL {version} ({host})"


def _connector_for(kind: str, url: str, settings: Any, schemas: list[str] | None = None) -> Any:
    from sqlalchemy.engine import make_url

    from analystos.connectors.registry import build_connector

    u = make_url(url)
    config = {"host": u.host, "port": u.port or 5432, "database": u.database, "username": u.username}
    if schemas:
        config["schemas"] = list(schemas)
    return build_connector(SimpleNamespace(kind=kind, config=config, secret_ref=None, execution_mode="pushdown"),
                           settings, password=u.password or "")


def certify(kind: str, dsn: str, *, settings: Any, schemas: list[str] | None = None) -> Report:
    """Run every check for ``kind`` against ``dsn`` (a SQLAlchemy URL with the read-only identity)."""
    from analystos.connectors import kinds

    spec = kinds.get_kind(kind)
    url = dsn.replace("postgresql://", "postgresql+psycopg://", 1) if dsn.startswith("postgresql://") else dsn
    report = Report(kind=spec.kind, dialect=spec.sqlglot_dialect, engine=spec.label)
    report.checks.append(validator_suite(spec.sqlglot_dialect))
    if spec.sqlglot_dialect != "postgres" or spec.kind not in ("postgres",):
        for name in ("read_only", "timeout", "cancel", "row_cap", "metadata", "type_mapping", "estimate", "identity"):
            report.checks.append(Check(name, False, f"no live check for {spec.kind} in this kit yet (needs access)"))
        return report
    try:
        report.engine = _engine_version(url)
    except Exception as exc:  # noqa: BLE001
        report.checks.append(Check("connect", False, f"could not connect: {exc.__class__.__name__}"))
        return report
    _pg_checks(url, _connector_for(spec.kind, url, settings, schemas), report, settings)
    return report


def write_evidence(report: Report, root: Path, *, commit: str = "") -> Path:
    day = report.started.date().isoformat()
    folder = root / EVIDENCE_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{report.kind}-{day}.md"
    lines = ["---", f"kind: {report.kind}", f"date: {day}", f"engine: {report.engine}",
             "test: analystos certify-connector", f"result: {'pass' if report.passed else 'fail'}",
             f"dialect: {report.dialect}", f"commit: {commit or 'unknown'}", "---",
             f"# Connector certification: {report.kind}", "",
             f"Live run {report.started:%Y-%m-%d %H:%M} UTC against {report.engine}, written by "
             "`analystos certify-connector` (S02-T6). The DSN reference is not recorded.", "",
             "| Check | Result | Seconds | Detail |", "|---|---|---|---|"]
    lines += [f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {c.seconds:.2f} | {c.detail.replace('|', '/')} |"
              for c in report.checks]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

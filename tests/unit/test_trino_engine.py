"""Trino engine and federation (TRN-004) without a server: the read-only transaction, error mapping, catalog
qualification of federated statements, the federation dialect, the trino analysis compiler and the plan estimate.
The live behaviour is certified by tests/integration/test_trino_federation.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.policy import DataScope  # noqa: E402
from analystos.core.errors import (  # noqa: E402
    Forbidden,
    InvalidInput,
    QueryTimeout,
    RunCancelled,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.engines import trino as te  # noqa: E402
from analystos.gateway.cost import trino_estimate  # noqa: E402
from analystos.gateway.validator import federation_dialect, validate_federated_sql, validate_sql  # noqa: E402

trino_client = pytest.importorskip("trino")


# ------------------------------------------------------------------ the READ ONLY transaction
class _Response:
    def __init__(self, txn: str | None) -> None:
        self.headers = {"X-Trino-Started-Transaction-Id": txn} if txn else {}
        self.ok = True


class _Request:
    """A TrinoRequest double: records what was posted and which transaction id the request then carries."""

    def __init__(self, txn: str | None) -> None:
        self.txn, self.posted, self.transaction_id = txn, [], None

    def post(self, sql: str) -> _Response:
        self.posted.append(sql)
        return _Response(self.txn)

    def get(self, uri: str) -> _Response:  # noqa: ARG002
        return _Response(None)

    def process(self, response: _Response) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(next_uri=None)


def _connection(monkeypatch, txn: str | None):
    request = _Request(txn)
    conn = te.connect_read_only(host="127.0.0.1", port=8080, user="u", catalog="pg")
    monkeypatch.setattr(conn, "_create_request", lambda: request)
    return conn, request


def test_every_cursor_runs_inside_a_read_only_transaction(monkeypatch) -> None:
    conn, request = _connection(monkeypatch, "txn-1")
    assert isinstance(conn, trino_client.dbapi.Connection)
    assert conn.isolation_level != trino_client.transaction.IsolationLevel.AUTOCOMMIT  # cursor() opens one first
    conn.cursor()
    assert request.posted == ["START TRANSACTION READ ONLY"] and request.transaction_id == "txn-1"
    assert conn.transaction is not None and conn.transaction.id == "txn-1"


def test_no_transaction_id_fails_closed(monkeypatch) -> None:
    conn, _ = _connection(monkeypatch, None)
    with pytest.raises(Forbidden, match="read-only transaction"):
        conn.cursor()


def test_connect_args_and_timeout_property() -> None:
    args = te.connect_args("trino://u@h:8086/pg?http_scheme=http")
    assert args["host"] == "h" and args["port"] == 8086 and args["catalog"] == "pg" and args["user"] == "u"
    assert te.timeout_properties(0) == {"query_max_execution_time": "1s"}
    assert te.timeout_properties(30) == {"query_max_execution_time": "30s"}
    conn = te.connect_read_only("trino://u@h:8086/pg", session_properties=te.timeout_properties(5))
    assert conn.session_properties == {"query_max_execution_time": "5s"}


def _query_error(name: str, type_: str = "USER_ERROR", cls: type | None = None):
    cls = cls or trino_client.exceptions.TrinoUserError
    return cls({"errorName": name, "errorType": type_, "message": f"{name} happened"}, query_id="q1")


@pytest.mark.parametrize(("name", "type_", "want"), [
    ("EXCEEDED_TIME_LIMIT", "INSUFFICIENT_RESOURCES", QueryTimeout),
    ("USER_CANCELED", "USER_ERROR", RunCancelled),
    ("READ_ONLY_VIOLATION", "USER_ERROR", Forbidden),
    ("PERMISSION_DENIED", "USER_ERROR", Forbidden),
    ("EXCEEDED_LOCAL_MEMORY_LIMIT", "INSUFFICIENT_RESOURCES", InvalidInput),
    ("SYNTAX_ERROR", "USER_ERROR", InvalidInput),
])
def test_trino_errors_map_to_platform_errors(name: str, type_: str, want: type) -> None:
    got = te._translate(_query_error(name, type_), 7, cancelled=False)
    assert isinstance(got, want)
    if want is QueryTimeout:
        assert "7s" in got.message


def test_unreachable_cluster_and_cancelled_flag() -> None:
    assert isinstance(te._translate(ConnectionError("refused"), 5, cancelled=False), UpstreamUnavailable)
    assert isinstance(te._translate(ConnectionError("reset"), 5, cancelled=True), RunCancelled)
    internal = _query_error("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR", trino_client.exceptions.TrinoQueryError)
    assert isinstance(te._translate(internal, 5, cancelled=False), UpstreamUnavailable)


# ------------------------------------------------------------------ federation in place
def test_qualify_catalogs_prefixes_every_table_and_fails_closed() -> None:
    sql = ('WITH "x" AS (SELECT "c"."id" FROM "sales"."customer" AS "c") SELECT "x"."id", "n"."name" FROM "x" '
           'JOIN "tiny"."nation" AS "n" ON "n"."nationkey" = "x"."id" LIMIT 11')
    out = te.qualify_catalogs(sql, {"sales.customer": "pg", "tiny.nation": "tpch"})
    tables = {t.sql(dialect="trino") for t in sqlglot.parse_one(out, read="trino").find_all(exp.Table)}
    assert '"pg"."sales"."customer" AS "c"' in tables and '"tpch"."tiny"."nation" AS "n"' in tables
    assert '"x"' in tables  # the CTE reference stays unqualified
    with pytest.raises(SQLRejected, match="no Trino catalog"):
        te.qualify_catalogs(sql, {"sales.customer": "pg"})
    with pytest.raises(SQLRejected, match="already"):
        te.qualify_catalogs('SELECT "id" FROM "pg"."sales"."customer"', {"sales.customer": "pg"})


def test_cluster_identity_must_match() -> None:
    a = te.cluster_of({"host": "Trino", "port": 8080, "username": "u", "catalog": "pg"}, None)
    b = te.cluster_of({"host": "trino", "username": "u", "catalog": "tpch"}, None)
    assert a == b  # the catalog differs, the cluster and identity do not
    assert a != te.cluster_of({"host": "trino", "username": "other", "catalog": "tpch"}, None)
    assert a != te.cluster_of({"host": "trino", "username": "u", "catalog": "tpch"}, "env:ANALYSTOS_SOURCE_SECRET_T")


def _scope(dialects: dict[str, str], denied: list[str] | None = None) -> DataScope:
    assets = {"sales.customer": ["id", "name", "nation_key", "email"], "tiny.nation": ["nationkey", "name"]}
    sources = {"sales.customer": "src_pg", "tiny.nation": "src_tpch"}
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=list(dialects), assets=list(assets),
                     asset_sources=sources, columns=assets, denied_columns=denied or [], source_dialects=dialects)


def test_federation_dialect_is_trino_only_when_every_source_is_trino() -> None:
    assert federation_dialect(_scope({"src_pg": "trino", "src_tpch": "trino"})) == "trino"
    assert federation_dialect(_scope({"src_pg": "postgres", "src_tpch": "trino"})) == "duckdb"
    assert federation_dialect(_scope({"src_pg": "postgres", "src_tpch": "postgres"})) == "duckdb"


def test_federated_trino_statement_is_checked_per_source() -> None:
    scope = _scope({"src_pg": "trino", "src_tpch": "trino"}, denied=["sales.customer.email"])
    ok = validate_federated_sql(scope, "SELECT c.name, n.name AS nation FROM sales.customer c JOIN tiny.nation n "
                                       "ON n.nationkey = c.nation_key", max_rows=10)
    assert ok.dialect == "trino" and ok.asset_sources == {"sales.customer": "src_pg", "tiny.nation": "src_tpch"}
    assert "LIMIT 11" in ok.executable_sql
    for sql in ("SELECT c.email FROM sales.customer c JOIN tiny.nation n ON n.nationkey = c.nation_key",
                "SELECT c.name FROM pg.sales.customer c",
                "SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
                "SELECT c.name FROM sales.customer c JOIN tiny.region r ON r.regionkey = c.nation_key"):
        with pytest.raises(SQLRejected):
            validate_federated_sql(scope, sql, max_rows=10)
    with pytest.raises(SQLRejected, match="Cross-source"):
        validate_sql(scope, "SELECT c.name FROM sales.customer c JOIN tiny.nation n ON n.nationkey = c.nation_key",
                     max_rows=10)


# ------------------------------------------------------------------ the analysis compiler on trino
def test_compiler_trino_specifics() -> None:
    import datetime as dt

    from analystos.contracts.analysis import Derivation
    from analystos.skills import sqlbuild as sb

    dow = sb.to_sql(sb.derive(Derivation(type="day_of_week", column="ts"), "trino"), "trino")
    assert dow == '(EXTRACT(DOW FROM CAST("ts" AS TIMESTAMP)) % 7)'  # ISO 7 = Sunday -> 0
    hours = sb.to_sql(sb.derive(Derivation(type="duration_hours", column="a", end_column="b"), "trino"), "trino")
    assert "TO_UNIXTIME" in hours.upper() and "EPOCH" not in hours.upper()
    with pytest.raises(InvalidInput, match="exact percentile"):
        sb.median(sb.col("x"), "trino")
    assert sb.to_sql(sb.lit(dt.date(2026, 1, 2), "trino"), "trino") == "CAST('2026-01-02' AS DATE)"
    assert sb.to_sql(sb.lit(dt.date(2026, 1, 2), "postgres"), "postgres") == "'2026-01-02'"
    assert "trino" in sb.PARTITION_DIALECTS


def test_every_compiled_spec_passes_the_trino_validator() -> None:
    """The platform's own trino SQL is never refused by the strict trino profile."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tests.unit.test_skills_sqlbuild import SPECS

    from analystos.skills import sqlbuild as sb

    checked = 0
    for name, spec in SPECS:
        for purpose in sb.METHOD_PURPOSES[spec.method]:
            sql = sb.compile_spec(spec, "trino", purpose=purpose, sample_rows=500).sql
            cols = sorted({c.name for c in sqlglot.parse_one(sql, read="trino").find_all(exp.Column)})
            scope = DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["s"], assets=[spec.asset],
                              asset_sources={spec.asset: "s"}, columns={spec.asset: cols},
                              source_dialects={"s": "trino"})
            try:
                validate_sql(scope, sql, max_rows=1000)
            except SQLRejected as exc:  # pragma: no cover - the assertion message names the case
                raise AssertionError(f"{name}/{purpose}: {exc.message}\n{sql}") from None
            checked += 1
    assert checked > 20


# ------------------------------------------------------------------ the plan estimate
PLAN = {"0": {"name": "Output", "estimates": [{"outputRowCount": 5.0, "outputSizeInBytes": 105.4}], "children": [
    {"name": "InnerJoin", "estimates": [{"outputRowCount": 5.0, "outputSizeInBytes": 105.4}], "children": [
        {"name": "ScanFilter", "estimates": [{"outputRowCount": 25.0, "outputSizeInBytes": 527.0}], "children": []},
        {"name": "RemoteSource", "estimates": [], "children": []}]}]},
    "1": {"name": "TableScan", "estimates": [{"outputRowCount": 5.0, "outputSizeInBytes": 90.0}], "children": []}}


def test_trino_estimate_sums_scans_and_fails_closed_without_statistics() -> None:
    est = trino_estimate(json.dumps(PLAN))
    assert est.basis == "explain" and est.rows == 5 and est.bytes == 617
    unknown = json.loads(json.dumps(PLAN))
    unknown["1"]["estimates"][0]["outputSizeInBytes"] = "NaN"
    assert trino_estimate(unknown).basis == "explain_unreadable"
    assert trino_estimate(None).basis == "explain_unreadable"
    assert trino_estimate({"0": {"name": "Output", "estimates": [], "children": []}}).basis == "explain_unreadable"

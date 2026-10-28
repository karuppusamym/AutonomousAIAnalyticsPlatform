"""Generic SQL connector without services: filters, type normalization, naming, errors, registry."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pyarrow as pa
import pytest

from analystos.connectors import kinds
from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
from analystos.connectors.generic_sql import (
    GenericSQLConnector,
    _rows_to_batch,
    describe_error,
    is_system_schema,
    matches_filters,
)
from analystos.connectors.postgres import PostgresConnector
from analystos.connectors.registry import build_connector
from analystos.connectors.sql_metadata import normalize_sql_type
from analystos.connectors.sqlserver import SQLServerConnector
from analystos.core.errors import InvalidInput

SECRET = "Sup3r$ecret!pw"

# ------------------------------------------------------------------------------ include / exclude


@pytest.mark.parametrize(
    ("schema", "table", "include", "exclude", "expected"),
    [
        ("sales", "orders", [], [], True),
        ("sales", "orders", ["sales"], [], True),  # schema pattern includes its tables
        ("hr", "salary", ["sales"], [], False),
        ("sales", "orders", ["*.orders"], [], True),
        ("sales", "customers", ["*.orders"], [], False),
        ("Sales", "Orders", ["sales.ord*"], [], True),  # case-insensitive
        ("sales", "orders", ["sales"], ["sales.orders"], False),  # exclude wins
        ("sales", "tmp_x", [], ["*.tmp_*"], False),
        ("staging", "orders", [], ["staging"], False),  # schema exclude drops every table
        ("sales", "orders", ["SALES.*"], [], True),
    ],
)
def test_matches_filters(schema, table, include, exclude, expected) -> None:
    assert matches_filters(schema, table, include, exclude) is expected


def test_schema_pruning() -> None:
    assert matches_filters("sales", None, ["sales.orders"], [])
    assert not matches_filters("hr", None, ["sales.orders"], [])
    assert matches_filters("hr", None, ["*.orders"], [])  # table pattern in any schema keeps schemas
    assert not matches_filters("hr", None, [], ["hr"])
    assert is_system_schema("pg_temp_3", kinds.get_kind("postgres").system_schemas)
    assert is_system_schema("information_schema", kinds.get_kind("mysql").system_schemas)
    assert is_system_schema("sys", kinds.get_kind("oracle").system_schemas)  # case-insensitive
    assert not is_system_schema("public", kinds.get_kind("postgres").system_schemas)


# ------------------------------------------------------------------------------ type normalization


@pytest.mark.parametrize(
    ("type_name", "normalized"),
    [
        # postgres / redshift
        ("INTEGER", "integer"), ("BIGINT", "bigint"), ("NUMERIC(10, 2)", "numeric"), ("DOUBLE PRECISION", "double"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"), ("TIMESTAMP(6) WITHOUT TIME ZONE", "timestamp"), ("JSONB", "json"),
        ("VARCHAR(20)", "text"), ("INTEGER[]", "json"), ("UUID", "text"), ("BOOLEAN", "boolean"),
        # mysql / mariadb
        ("TINYINT(1)", "boolean"), ("TINYINT", "integer"), ("INTEGER UNSIGNED", "integer"), ("BIGINT UNSIGNED", "bigint"),
        ("MEDIUMINT", "integer"), ("DATETIME", "timestamp"), ("DATETIME(6)", "timestamp"), ("LONGTEXT", "text"),
        ("ENUM('open','paid')", "text"), ("DECIMAL(10,2)", "numeric"), ("YEAR", "integer"), ("DOUBLE", "double"),
        # sql server
        ("NVARCHAR(max)", "text"), ("DATETIME2", "timestamp"), ("BIT", "boolean"), ("UNIQUEIDENTIFIER", "text"),
        # oracle
        ("NUMBER(10,0)", "bigint"), ("NUMBER(5, 0)", "integer"), ("NUMBER(12,2)", "numeric"), ("NUMBER", "numeric"),
        ("VARCHAR2(100 CHAR)", "text"), ("CLOB", "text"), ("DATE", "date"), ("TIMESTAMP WITH LOCAL TIME ZONE", "timestamp"),
        ("BINARY_DOUBLE", "double"),
        # snowflake / bigquery / databricks / trino
        ("TIMESTAMP_NTZ", "timestamp"), ("TIMESTAMP_TZ(9)", "timestamp"), ("VARIANT", "json"), ("OBJECT", "json"),
        ("STRING", "text"), ("INT64", "bigint"), ("FLOAT64", "double"), ("BIGNUMERIC", "numeric"),
        ("ARRAY<STRING>", "json"), ("STRUCT<a INT64>", "json"), ("MAP<STRING,INT>", "json"), ("ROW(a integer)", "json"),
        ("TIMESTAMP(3) WITH TIME ZONE", "timestamp"),
        # clickhouse
        ("Nullable(Int32)", "integer"), ("LowCardinality(Nullable(String))", "text"), ("DateTime64(3, 'UTC')", "timestamp"),
        ("UInt64", "numeric"), ("Float32", "double"), ("Array(Int32)", "json"), ("Date32", "date"),
        # duckdb / sqlite
        ("HUGEINT", "numeric"), ("TIMESTAMP_NS", "timestamp"), ("INT", "integer"), ("BLOB", "text"),
        ("SOMETHING_ELSE", "text"), (None, "text"),
    ],
)
def test_type_normalization(type_name, normalized) -> None:
    assert normalize_sql_type(type_name) == normalized


# ------------------------------------------------------------------------------ connector config


def _con(kind: str = "mysql", **config) -> GenericSQLConnector:
    base = {"host": "db.internal", "database": "shop", "username": "reader"}
    base.update(config)
    return GenericSQLConnector(kind, base, password=SECRET)


def test_connector_modes_and_schemas() -> None:
    con = _con()
    assert con.execution_mode == "staged" and con.dialect == "postgres" and con.source_dialect == "mysql"
    assert con.schemas == ["shop"]  # MySQL default: the configured database
    assert _con(schemas=["shop", "mysql", "information_schema"]).schemas == ["shop"]
    with pytest.raises(InvalidInput, match="non-system schema"):
        _con(schemas=["mysql"])
    assert _con("postgres").schemas is None  # generic postgres: every non-system schema
    assert _con("postgres", execution_mode="staged").dialect == "postgres"
    ora = GenericSQLConnector("oracle", {"host": "h", "database": "svc", "username": "scott"}, password="x")
    assert ora.schemas == ["scott"]
    assert _con(include="sales, *.orders").include == ["sales", "*.orders"]
    with pytest.raises(InvalidInput, match="max_tables"):
        _con(max_tables=-1)
    with pytest.raises(InvalidInput, match="config.host"):
        GenericSQLConnector("mysql", {"username": "u"})
    with pytest.raises(InvalidInput, match="not a SQL source kind"):
        GenericSQLConnector("servicenow", {"username": "u"})


def test_secret_never_in_repr_or_logs(caplog) -> None:
    con = GenericSQLConnector("mysql", {"host": "127.0.0.1", "port": 1, "database": "shop", "username": "reader",
                                        "connect_timeout": 2}, password=SECRET)
    assert SECRET not in repr(con) and "reader" not in repr(con)
    assert "%24ecret" in con.sqlalchemy_url()  # the URL does carry it (escaped) -- only for the engine
    with caplog.at_level(logging.DEBUG):
        result = con.test()
    assert not result.ok and "connection refused" in result.message
    assert SECRET not in result.message and SECRET not in caplog.text and "%24ecret" not in caplog.text
    con.close()


def test_describe_error_mapping_and_scrubbing() -> None:
    cases = {
        'password authentication failed for user "x"': "authentication failed",
        "(1045, \"Access denied for user 'ro'@'172.17.0.1'\")": "authentication failed",
        "ORA-01017: invalid username/password; logon denied": "authentication failed",
        'could not translate host name "nope" to address': "host not found",
        "(2003, \"Can't connect to MySQL server on 'h'\")": "connection refused",
        "connection timeout expired": "timed out",
        "(1049, \"Unknown database 'x'\")": "database not found",
        "unable to open database file": "file could not be opened",
    }
    for text_, reason in cases.items():
        assert reason in describe_error(RuntimeError(text_))
    assert SECRET not in describe_error(RuntimeError(f"weird failure {SECRET}"), secrets=[SECRET])


def test_missing_driver_names_the_pip_extra(monkeypatch) -> None:
    real = kinds.importlib.util.find_spec
    monkeypatch.setattr(kinds.importlib.util, "find_spec", lambda name: None if name == "pymysql" else real(name))
    src = SimpleNamespace(kind="mysql", config={"host": "h", "database": "d", "username": "u"}, secret_ref=None)
    with pytest.raises(InvalidInput, match=r"analystos\[mysql\]"):
        build_connector(src)
    res = GenericSQLConnector("mysql", src.config, password="x").test()
    assert not res.ok and "analystos[mysql]" in res.message


def test_registry_builds_every_catalog_kind(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(kinds, "driver_available", lambda kind: True)
    sample = {"host": "h", "database": "d", "username": "u", "account": "a", "project": "p", "http_path": "/x",
              "catalog": "c", "path": "db.file"}
    settings = SimpleNamespace(upload_dir=str(tmp_path), servicenow_mock_url="http://mock")
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    for spec in kinds.list_kinds():
        config = {"path": "a.csv"} if spec.kind == "csv" else ({"username": "u"} if spec.kind == "servicenow" else sample)
        if spec.kind == "rest":  # a declarative description (S02-T8)
            config = {"base_url": "https://api.example.com", "endpoints": [
                {"name": "t", "path": "/t", "columns": [{"name": "id", "path": "id"}]}]}
        con = build_connector(SimpleNamespace(kind=spec.kind, config=config, secret_ref=None), settings)
        assert con.kind == spec.kind
        if spec.is_sql:
            assert isinstance(con, GenericSQLConnector)
            assert con.execution_mode == kinds.execution_mode_for(spec.kind, None)
            assert con.dialect == kinds.dialect_for(spec.kind)
    assert isinstance(build_connector(SimpleNamespace(kind="postgres", config=sample, secret_ref=None)), PostgresConnector)
    assert isinstance(build_connector(SimpleNamespace(kind="sqlserver", config=sample, secret_ref=None)), SQLServerConnector)
    staged_pg = build_connector(SimpleNamespace(kind="postgres", config=sample, secret_ref=None, execution_mode="staged"))
    assert staged_pg.execution_mode == "staged"
    with pytest.raises(InvalidInput, match="expected one of .*snowflake"):
        build_connector(SimpleNamespace(kind="db2", config={}, secret_ref=None))


def test_sqlserver_generic_features() -> None:
    con = SQLServerConnector({"host": "db", "database": "Sales", "username": "r", "login_timeout": 7,
                              "exclude": ["dbo.tmp*"]}, password="p")
    assert con.sqlalchemy_url().endswith("?login_timeout=7")
    assert con.query_url(30).endswith("?login_timeout=7&timeout=30")
    assert con.session_statements(30) == []  # no session read-only switch: least-privilege login (documented)
    assert con.exclude == ["dbo.tmp*"]


def test_file_path_confined_to_upload_dir(tmp_path) -> None:
    (tmp_path / "ok.db").write_bytes(b"")
    con = GenericSQLConnector("sqlite", {"path": "ok.db"}, allowed_dir=tmp_path)
    assert con.config["path"] == str((tmp_path / "ok.db").resolve())
    with pytest.raises(InvalidInput, match="upload directory"):
        GenericSQLConnector("sqlite", {"path": "/etc/passwd"}, allowed_dir=tmp_path)
    with pytest.raises(InvalidInput, match="upload directory"):
        GenericSQLConnector("duckdb", {"path": "../x.duckdb"}, allowed_dir=tmp_path)


# ------------------------------------------------------------------------------ staged naming / batches


def test_staged_names_are_loader_safe() -> None:
    con = _con()
    assets = [
        DiscoveredAsset(source_name="a.Orders", name="Orders", schema_name="a",
                        columns=[DiscoveredColumn(name="Order ID", data_type="integer"),
                                 DiscoveredColumn(name="order_id", data_type="integer")]),
        DiscoveredAsset(source_name="b.orders", name="orders", schema_name="b", columns=[]),
        DiscoveredAsset(source_name="a.Customer-List", name="Customer-List", schema_name="a", columns=[]),
    ]
    out = con._staged_names(assets)
    assert [a.name for a in out] == ["a_orders", "b_orders", "customer_list"]
    assert [c.name for c in out[0].columns] == ["order_id", "order_id_2"]
    assert out[0].columns[0].business_name == "Order ID" and out[0].columns[1].business_name == "order_id"
    assert out[0].source_name == "a.Orders"  # origin kept for extraction


def test_extract_refuses_filtered_or_foreign_assets() -> None:
    con = _con(exclude=["shop.secret*"])
    with pytest.raises(InvalidInput, match="excluded"):
        next(con.extract(DiscoveredAsset(source_name="shop.secret_pay", name="secret_pay"), max_rows=10))
    with pytest.raises(InvalidInput, match="not configured"):
        next(con.extract(DiscoveredAsset(source_name="other.t", name="t"), max_rows=10))
    with pytest.raises(InvalidInput, match="schema.table"):
        next(con.extract(DiscoveredAsset(source_name="t", name="t"), max_rows=10))
    with pytest.raises(InvalidInput, match="pushdown"):
        next(_con("postgres").extract(DiscoveredAsset(source_name="public.t", name="t"), max_rows=10))


def test_rows_to_arrow_batch_coercion() -> None:
    aware = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    batch = _rows_to_batch(
        [(1, Decimal("10.50"), 1, "2025-01-02", aware, {"a": 1}, b"\x01"),
         (None, None, 0, date(2025, 1, 3), "2025-01-01T13:00:00", None, None)],
        ["id", "amount", "vip", "day", "at", "doc", "raw"],
        ["integer", "numeric", "boolean", "date", "timestamp", "json", "text"],
    )
    assert batch.schema.field("amount").type == pa.float64() and batch.schema.field("vip").type == pa.bool_()
    rows = batch.to_pylist()
    assert rows[0] == {"id": 1, "amount": 10.5, "vip": True, "day": date(2025, 1, 2), "at": datetime(2025, 1, 1, 12, 0),
                       "doc": '{"a": 1}', "raw": "01"}
    assert rows[1]["vip"] is False and rows[1]["at"] == datetime(2025, 1, 1, 13, 0)
    odd = _rows_to_batch([("not a number",)], ["n"], ["integer"])  # falls back to text, never drops data
    assert odd.schema.field("n").type == pa.string() and odd.to_pylist() == [{"n": "not a number"}]

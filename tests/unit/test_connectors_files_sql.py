"""File connector, SQL Server / Postgres metadata mapping, secrets, naming and registry."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from analystos.connectors.csv_file import CSVFileConnector, excel_engine_available
from analystos.connectors.naming import sanitize_identifier, staging_schema_for, unique_identifiers
from analystos.connectors.postgres import PostgresConnector
from analystos.connectors.registry import build_connector
from analystos.connectors.secrets import resolve_secret
from analystos.connectors.servicenow import ServiceNowConnector
from analystos.connectors.sql_metadata import build_assets, normalize_sql_type
from analystos.connectors.sqlserver import SQLServerConnector, catalog_queries, pymssql_available
from analystos.core.errors import InvalidInput, NotFound, UpstreamUnavailable
from analystos.staging.loader import pg_type_for

# ------------------------------------------------------------------------------ secrets


def test_resolve_secret_env(monkeypatch) -> None:
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_TEST", "hunter2")
    assert resolve_secret("env:ANALYSTOS_SOURCE_SECRET_DP_TEST") == "hunter2"
    assert resolve_secret(None) is None
    assert resolve_secret("") is None


def test_resolve_secret_file(tmp_path: Path, monkeypatch) -> None:
    from analystos.core.config import get_settings

    monkeypatch.setattr(get_settings(), "secret_file_dirs", str(tmp_path))
    f = tmp_path / "pw"
    f.write_text("s3cret\n", encoding="utf-8")
    assert resolve_secret(f"file:{f}") == "s3cret"


@pytest.mark.parametrize("ref", ["plain-password", "smb:x", "env:", "env:1BAD", "file:relative/path",
                                 "env:ANALYSTOS_SOURCE_SECRET_DP_TEST_MISSING"])
def test_resolve_secret_rejects(ref: str, monkeypatch) -> None:
    monkeypatch.delenv("ANALYSTOS_SOURCE_SECRET_DP_TEST_MISSING", raising=False)
    with pytest.raises(InvalidInput) as info:
        resolve_secret(ref)
    assert "plain-password" not in info.value.message or ref != "plain-password"


def test_resolve_secret_error_never_contains_value(tmp_path: Path, monkeypatch) -> None:
    from analystos.core.config import get_settings

    monkeypatch.setattr(get_settings(), "secret_file_dirs", str(tmp_path))
    with pytest.raises(InvalidInput) as info:
        resolve_secret(f"file:{tmp_path / 'missing'}")
    assert "missing" in info.value.message


# ------------------------------------------------------------------------------ naming


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Incident Report 2024.csv", "incident_report_2024_csv"),
        ("  --Weird__Name--  ", "weird_name"),
        ("123abc", "col_123abc"),
        ('x"; DROP TABLE y; --', "x_drop_table_y"),
        ("Ünïcødé", "n_c_d"),
        ("", "col"),
    ],
)
def test_sanitize_identifier(raw: str, expected: str) -> None:
    assert sanitize_identifier(raw) == expected


def test_unique_identifiers_and_length() -> None:
    assert unique_identifiers(["A", "a", "a "]) == ["a", "a_2", "a_3"]
    assert len(sanitize_identifier("x" * 200)) == 63
    assert staging_schema_for("abc123") == "src_abc123"


# ------------------------------------------------------------------------------ files


@pytest.fixture()
def upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    (d / "Sales Data 2026.csv").write_text(
        "Order ID,Order Date,Amount,Region,Is Returned\n"
        "1,2026-01-05,10.5,EMEA,true\n2,2026-01-06,20,APAC,false\n3,2026-02-01,,EMEA,false\n"
    , encoding="utf-8")
    pa_table = pa.table({"id": [1, 2], "label": ["a", "b"]})
    import pyarrow.parquet as pq

    pq.write_table(pa_table, d / "labels.parquet")
    (d / "notes.xlsx").write_bytes(b"not really excel")
    (d / "readme.md").write_text("ignored", encoding="utf-8")
    return d


def test_csv_discover_infers_types_and_sanitizes(upload_dir: Path) -> None:
    con = CSVFileConnector({"path": "."}, allowed_dir=upload_dir)
    assert con.execution_mode == "staged" and con.dialect == "postgres"
    assets = {a.name: a for a in con.discover()}
    assert "sales_data_2026" in assets and "labels" in assets
    if not excel_engine_available():
        assert "notes" not in assets  # skipped gracefully
    sales = assets["sales_data_2026"]
    assert sales.row_count == 3 and sales.kind == "file" and sales.source_name == "Sales Data 2026.csv"
    types = {c.name: c.data_type for c in sales.columns}
    assert types == {"order_id": "bigint", "order_date": "date", "amount": "double", "region": "text", "is_returned": "boolean"}
    assert next(c for c in sales.columns if c.name == "order_id").business_name == "Order ID"


def test_csv_extract(upload_dir: Path) -> None:
    con = CSVFileConnector({"path": "Sales Data 2026.csv"}, allowed_dir=upload_dir)
    (asset,) = con.discover()
    table = pa.Table.from_batches(list(con.extract(asset, max_rows=2)))
    assert table.num_rows == 2
    assert table.column_names == ["order_id", "order_date", "amount", "region", "is_returned"]
    assert table.column("amount").to_pylist() == [10.5, 20.0]


def test_parquet_extract(upload_dir: Path) -> None:
    con = CSVFileConnector({"path": "labels.parquet"}, allowed_dir=upload_dir)
    (asset,) = con.discover()
    assert pa.Table.from_batches(list(con.extract(asset, max_rows=10))).num_rows == 2


@pytest.mark.parametrize("path", ["../outside.csv", "/etc/passwd", "sub/../../x.csv"])
def test_file_path_must_stay_in_upload_dir(upload_dir: Path, path: str) -> None:
    with pytest.raises(InvalidInput):
        CSVFileConnector({"path": path}, allowed_dir=upload_dir)


def test_symlink_escape_rejected(upload_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    os.symlink(secret, upload_dir / "link.csv")
    with pytest.raises(InvalidInput):
        CSVFileConnector({"path": "link.csv"}, allowed_dir=upload_dir)
    names = [a.name for a in CSVFileConnector({"path": "."}, allowed_dir=upload_dir).discover()]
    assert "link" not in names


def test_missing_file(upload_dir: Path) -> None:
    with pytest.raises(NotFound):
        CSVFileConnector({"path": "nope.csv"}, allowed_dir=upload_dir).discover()


# ------------------------------------------------------------------------------ sql metadata


def test_sql_type_normalization() -> None:
    assert normalize_sql_type("character varying") == "text"
    assert normalize_sql_type("timestamp with time zone") == "timestamp"
    assert normalize_sql_type("numeric(10,2)") == "numeric"
    assert normalize_sql_type("nvarchar") == "text"
    assert normalize_sql_type("datetime2") == "timestamp"
    assert normalize_sql_type("bit") == "boolean"
    assert normalize_sql_type("uniqueidentifier") == "text"
    assert normalize_sql_type("tinyint") == "integer"
    assert normalize_sql_type("bigint") == "bigint"
    assert normalize_sql_type(None) == "text"


def test_build_assets_from_catalog_rows() -> None:
    assets = build_assets(
        tables=[{"schema": "dbo", "table": "Orders", "table_type": "BASE TABLE"},
                {"schema": "dbo", "table": "Customers", "table_type": "BASE TABLE"},
                {"schema": "dbo", "table": "vOrders", "table_type": "VIEW"}],
        columns=[
            {"schema": "dbo", "table": "Orders", "column": "CustomerId", "data_type": "int", "is_nullable": "YES", "ordinal": 2},
            {"schema": "dbo", "table": "Orders", "column": "OrderId", "data_type": "int", "is_nullable": "NO", "ordinal": 1},
            {"schema": "dbo", "table": "Orders", "column": "PlacedAt", "data_type": "datetime2", "is_nullable": "YES", "ordinal": 3},
            {"schema": "dbo", "table": "Customers", "column": "CustomerId", "data_type": "int", "is_nullable": "NO", "ordinal": 1},
        ],
        primary_keys=[{"schema": "dbo", "table": "Orders", "column": "OrderId"},
                      {"schema": "dbo", "table": "Customers", "column": "CustomerId"}],
        foreign_keys=[{"schema": "dbo", "table": "Orders", "column": "CustomerId", "ref_schema": "dbo",
                       "ref_table": "Customers", "ref_column": "CustomerId"}],
        row_counts=[{"schema": "dbo", "table": "Orders", "row_count": 42}, {"schema": "dbo", "table": "Customers", "row_count": -1}],
    )
    by = {a.name: a for a in assets}
    orders = by["Orders"]
    assert orders.schema_name == "dbo" and orders.row_count == 42 and orders.kind == "table"
    assert [c.name for c in orders.columns] == ["OrderId", "CustomerId", "PlacedAt"]
    assert orders.columns[0].is_key and not orders.columns[0].nullable
    assert orders.columns[1].references == "dbo.Customers.CustomerId"
    assert orders.columns[2].data_type == "timestamp"
    assert by["Customers"].row_count is None
    assert by["vOrders"].kind == "view"


def test_sqlserver_catalog_sql_escapes_schema_names() -> None:
    q = catalog_queries(["dbo", "sales'; DROP TABLE x;--"])
    assert "N'dbo'" in q["tables"]
    assert "N'sales''; DROP TABLE x;--'" in q["columns"]
    assert "INFORMATION_SCHEMA.TABLES" in q["tables"]
    assert "REFERENTIAL_CONSTRAINTS" in q["foreign_keys"]
    assert "PRIMARY KEY" in q["primary_keys"]


def test_sqlserver_connector_config_and_url() -> None:
    con = SQLServerConnector({"host": "db", "database": "Sales", "username": "reader", "schemas": ["dbo", "sys"]}, password="p@ss")
    assert con.execution_mode == "pushdown" and con.dialect == "tsql"
    assert con.schemas == ["dbo"]
    url = con.sqlalchemy_url()
    assert url.startswith("mssql+pymssql://reader:p%40ss@db:1433/Sales")
    assert "timeout=30" in con.query_url(30)
    if not pymssql_available():
        assert not con.test().ok
        with pytest.raises(UpstreamUnavailable, match="pymssql"):
            con.discover()


def test_postgres_connector_url_and_validation(monkeypatch) -> None:
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_DP_PG_PW", "pw/with:chars")
    con = PostgresConnector({"host": "h", "database": "d", "username": "r", "schemas": ["public", "pg_catalog"]}, "env:ANALYSTOS_SOURCE_SECRET_DP_PG_PW")
    assert con.schemas == ["public"]
    assert con.sqlalchemy_url().startswith("postgresql+psycopg://r:pw%2Fwith%3Achars@h:5432/d")
    with pytest.raises(InvalidInput):
        PostgresConnector({"host": "h"})


# ------------------------------------------------------------------------------ registry & loader types


def test_registry_builds_by_kind(tmp_path: Path) -> None:
    settings = SimpleNamespace(servicenow_mock_url="http://mock:8090", upload_dir=str(tmp_path))
    pg = build_connector(SimpleNamespace(kind="postgres", config={"host": "h", "database": "d", "username": "u"}, secret_ref=None), settings)
    assert isinstance(pg, PostgresConnector)
    ms = build_connector(SimpleNamespace(kind="sqlserver", config={"host": "h", "database": "d", "username": "u"}, secret_ref=None), settings)
    assert isinstance(ms, SQLServerConnector)
    sn = build_connector(SimpleNamespace(kind="servicenow", config={"username": "admin"}, secret_ref=None), settings)
    assert isinstance(sn, ServiceNowConnector) and sn.instance_url == "http://mock:8090"
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    f = build_connector(SimpleNamespace(kind="csv", config={"path": "a.csv"}, secret_ref=None), settings)
    assert isinstance(f, CSVFileConnector)
    with pytest.raises(InvalidInput):
        build_connector(SimpleNamespace(kind="oracle", config={}, secret_ref=None), settings)


@pytest.mark.parametrize(
    ("dtype", "pg"),
    [
        (pa.int64(), "bigint"), (pa.int32(), "integer"), (pa.int16(), "smallint"), (pa.float64(), "double precision"),
        (pa.bool_(), "boolean"), (pa.string(), "text"), (pa.large_string(), "text"), (pa.date32(), "date"),
        (pa.timestamp("us"), "timestamp"), (pa.timestamp("us", tz="UTC"), "timestamptz"), (pa.decimal128(12, 2), "numeric(12,2)"),
        (pa.list_(pa.int64()), "jsonb"), (pa.struct([("a", pa.int64())]), "jsonb"), (pa.null(), "text"), (pa.binary(), "bytea"),
        (pa.uint64(), "numeric(20,0)"),
    ],
)
def test_arrow_to_postgres_types(dtype: pa.DataType, pg: str) -> None:
    assert pg_type_for(dtype) == pg


def test_corrupt_file_is_skipped_not_fatal(upload_dir: Path) -> None:
    from analystos.connectors.csv_file import CSVFileConnector

    con = CSVFileConnector({"path": str(upload_dir)}, allowed_dir=upload_dir.parent)
    names = {a.source_name for a in con.discover()}
    assert "notes.xlsx" not in names and names
    assert any(s["file"] == "notes.xlsx" for s in con.skipped)

"""Source-kind catalog: completeness, URL rendering/escaping, dialect and execution-mode rules."""
from __future__ import annotations

import base64
import tomllib
from pathlib import Path
from urllib.parse import unquote

import pytest
import sqlglot
from sqlalchemy.engine import make_url

from analystos.connectors import kinds
from analystos.core.errors import InvalidInput

REPO = Path(__file__).resolve().parents[2]
EXPECTED = {"postgres", "redshift", "mysql", "mariadb", "sqlserver", "oracle", "snowflake", "bigquery", "databricks",
            "trino", "clickhouse", "duckdb", "sqlite", "servicenow", "csv", "rest"}
NASTY_PASSWORD = "p@ss:w/rd?#&=% x"
SAMPLE = {
    "host": "db.example.com", "database": "sales", "username": "ana lyst@corp", "account": "xy12345.eu-west-1",
    "warehouse": "WH 1", "role": "ANALYST", "project": "proj-1", "http_path": "/sql/1.0/warehouses/abc",
    "catalog": "main", "schema": "public", "path": "/data/uploads/shop db.sqlite",
}
SQL_KINDS = sorted(k.kind for k in kinds.list_kinds() if k.is_sql)


def test_catalog_has_every_expected_kind() -> None:
    assert set(kinds.kind_ids()) == EXPECTED
    assert kinds.get_kind("servicenow").category == "api" and kinds.get_kind("csv").category == "file"
    assert kinds.get_kind("file").kind == "csv" and kinds.get_kind("PostgreSQL").kind == "postgres"
    with pytest.raises(InvalidInput, match="expected one of .*mysql"):
        kinds.get_kind("db2")


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_every_kind_is_well_formed(kind: str) -> None:
    spec = kinds.get_kind(kind)
    sqlglot.Dialect.get_or_raise(spec.sqlglot_dialect)  # a real sqlglot dialect
    assert spec.label and spec.docs and spec.category in ("database", "warehouse", "lakehouse", "engine", "file", "api")
    if spec.secret is not None:
        assert spec.secret.field not in spec.required  # the secret is never a config key
    if spec.is_sql:
        assert spec.driver.modules
        assert spec.row_estimate == "none" or spec.row_estimate == "count_capped" or spec.row_estimate in __import__(
            "analystos.connectors.generic_sql", fromlist=["ROW_ESTIMATES"]).ROW_ESTIMATES
        for field in spec.required:
            assert "{" + field + "}" in spec.url_template or field == "path"


def test_pip_extras_exist_in_pyproject() -> None:
    extras = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    for spec in kinds.list_kinds():
        if spec.driver.extra:
            assert spec.driver.extra in extras, spec.kind
            assert f"analystos[{spec.driver.extra}]" in spec.pip_install_hint
    wanted = {"mysql", "oracle", "snowflake", "bigquery", "databricks", "trino", "clickhouse", "duckdb", "redshift"}
    assert wanted <= set(extras)
    everything = " ".join(extras["all-sources"])
    for extra in wanted | {"sqlserver"}:
        for pkg in extras[extra]:
            assert pkg.split(">")[0] in everything


@pytest.mark.parametrize("kind", SQL_KINDS)
def test_every_sql_kind_renders_an_escaped_url(kind: str) -> None:
    spec = kinds.get_kind(kind)
    password = NASTY_PASSWORD if spec.secret else None
    url = kinds.build_url(kind, dict(SAMPLE), password)
    assert url.startswith(spec.url_template.split(":", 1)[0] + ":")
    assert "{" not in url and "}" not in url
    assert NASTY_PASSWORD not in url and "ana lyst@corp" not in url  # escaped, never raw
    parsed = make_url(url)
    if spec.secret and "{password}@" in spec.url_template:
        assert parsed.password == NASTY_PASSWORD
        assert NASTY_PASSWORD not in repr(parsed) and NASTY_PASSWORD not in kinds.redact_url(url)
    if "{username}" in spec.url_template:
        assert parsed.username == "ana lyst@corp"
    if spec.secret and spec.secret.encoding == "base64":
        encoded = parsed.query["credentials_base64"]
        assert base64.urlsafe_b64decode(encoded).decode() == NASTY_PASSWORD
        redacted = kinds.redact_url(url)
        assert encoded not in redacted and ("***" in redacted or "%2A%2A%2A" in redacted)
    if spec.default_port and "{port}" in spec.url_template:
        assert parsed.port == spec.default_port


def test_url_details() -> None:
    assert kinds.build_url("postgres", {"host": "h", "database": "d", "username": "r"}, "pw/with:chars") == \
        "postgresql+psycopg://r:pw%2Fwith%3Achars@h:5432/d?connect_timeout=10"
    assert kinds.build_url("mysql", {"host": "h", "port": 3307, "database": "d", "username": "u"}, "x") == \
        "mysql+pymysql://u:x@h:3307/d?charset=utf8mb4"
    oracle = make_url(kinds.build_url("oracle", {"host": "h", "database": "ORCLPDB1", "username": "u"}, "x"))
    assert oracle.query["service_name"] == "ORCLPDB1" and oracle.port == 1521
    snow = kinds.build_url("snowflake", {"account": "acct", "database": "DB", "username": "u"}, "x")
    assert snow == "snowflake://u:x@acct/DB"  # empty optional schema/warehouse/role dropped
    trino = kinds.build_url("trino", {"host": "t", "catalog": "hive", "username": "u"}, None)
    assert trino == "trino://u@t:8080/hive"  # optional secret absent -> no ':password'
    bq = kinds.build_url("bigquery", {"project": "p", "database": "ds"}, None)
    assert bq == "bigquery://p/ds"  # application-default credentials
    sqlite = kinds.build_url("sqlite", {"path": "/data/a b.db"}, None)
    assert sqlite == "sqlite:///file:/data/a%20b.db?mode=ro&uri=true"
    assert unquote(make_url(sqlite).database) == "file:/data/a b.db"
    assert kinds.build_url("duckdb", {"path": "/data/a.duckdb"}, None) == "duckdb:////data/a.duckdb"


def test_url_validation() -> None:
    with pytest.raises(InvalidInput, match="config.host, config.database"):
        kinds.build_url("mysql", {"username": "u"}, "x")
    with pytest.raises(InvalidInput, match="secret_ref"):
        kinds.build_url("mysql", {"host": "h", "database": "d", "username": "u"}, None)
    with pytest.raises(InvalidInput, match="not a host name"):
        kinds.build_url("mysql", {"host": "evil@h/x", "database": "d", "username": "u"}, "x")
    with pytest.raises(InvalidInput, match="must not contain"):
        kinds.build_url("duckdb", {"path": "/a.duckdb?access_mode=read_write"}, None)
    with pytest.raises(InvalidInput, match="no SQL endpoint"):
        kinds.build_url("csv", {"path": "a.csv"}, None)


def test_pushdown_only_for_compiler_dialects() -> None:
    """Pushdown = validator suite + analysis compiler + read-only session (P4-E01); sqlserver is the
    legacy least-privilege-identity exception, DuckDB files are opt-in."""
    from analystos.gateway.dialects import SUPPORTED_DIALECTS
    from analystos.skills.sqlbuild import DIALECTS

    assert SUPPORTED_DIALECTS & set(DIALECTS) == kinds.PUSHDOWN_DIALECTS
    pushdown = {k.kind for k in kinds.list_kinds() if k.pushdown_allowed}
    assert pushdown == {"postgres", "sqlserver", "duckdb"}
    for spec in kinds.list_kinds():
        if spec.pushdown_allowed:
            assert spec.sqlglot_dialect in kinds.PUSHDOWN_DIALECTS
            assert spec.readonly_enforcement != "identity" or spec.kind in kinds.LEGACY_IDENTITY_PUSHDOWN
    assert kinds.get_kind("duckdb").readonly_enforcement == "connect"
    assert kinds.get_kind("postgres").readonly_enforcement == "session"
    assert kinds.get_kind("snowflake").readonly_enforcement == "identity"
    assert kinds.execution_mode_for("postgres", None) == "pushdown"
    assert kinds.execution_mode_for("postgres", "staged") == "staged"
    assert kinds.execution_mode_for("sqlserver", "pushdown") == "pushdown"
    assert kinds.execution_mode_for("duckdb", None) == "staged"  # opt-in
    assert kinds.execution_mode_for("duckdb", "pushdown") == "pushdown"
    for kind in ("mysql", "mariadb", "oracle", "snowflake", "bigquery", "databricks", "trino", "clickhouse", "redshift",
                 "sqlite", "servicenow", "csv", "file"):
        assert kinds.execution_mode_for(kind, "pushdown") == "staged"
        assert kinds.execution_mode_for(kind, None) == "staged"
    with pytest.raises(InvalidInput, match="execution_mode"):
        kinds.execution_mode_for("postgres", "federated")


def test_gateway_dialect_rules() -> None:
    from analystos.gateway.validator import SUPPORTED_DIALECTS
    from analystos.skills.sqlbuild import DIALECTS

    assert kinds.dialect_for("postgres") == "postgres"
    assert kinds.dialect_for("sqlserver") == "tsql" and kinds.dialect_for("sqlserver", "staged") == "postgres"
    assert kinds.dialect_for("mysql") == "postgres" and kinds.dialect_for("mysql", "pushdown") == "postgres"
    assert kinds.dialect_for("servicenow") == "postgres" and kinds.dialect_for("file") == "postgres"
    for kind in kinds.kind_ids():
        for mode in (None, "pushdown", "staged"):
            d = kinds.dialect_for(kind, mode)
            assert d in SUPPORTED_DIALECTS and d in DIALECTS  # validator and analysis compiler both speak it


def test_session_statements() -> None:
    assert kinds.session_statements("postgres") == ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]
    assert kinds.session_statements("postgres", 30) == [
        "SET statement_timeout = 30000", "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]
    assert kinds.session_statements("mysql", 5) == ["SET SESSION MAX_EXECUTION_TIME = 5000",
                                                    "SET SESSION TRANSACTION READ ONLY"]
    assert kinds.session_statements("mariadb", 5)[0] == "SET SESSION max_statement_time = 5"
    assert kinds.session_statements("sqlite") == ["PRAGMA query_only = ON"]
    for kind in ("oracle", "snowflake", "bigquery", "sqlserver"):  # least-privilege identity instead (documented)
        assert kinds.session_statements(kind) == []
        assert "SELECT" in kinds.get_kind(kind).docs or "least-privilege" in kinds.get_kind(kind).docs


def test_driver_detection(monkeypatch) -> None:
    assert kinds.driver_available("sqlite") and kinds.driver_available("postgres")
    real = kinds.importlib.util.find_spec
    monkeypatch.setattr(kinds.importlib.util, "find_spec", lambda name: None if name == "pymysql" else real(name))
    assert not kinds.driver_available("mysql")
    with pytest.raises(InvalidInput, match=r"pip install 'analystos\[mysql\]'"):
        kinds.require_driver("mysql")


def test_public_listing_has_no_templates() -> None:
    entry = kinds.get_kind("snowflake").public()
    assert entry["secret_field"] == "password" and entry["pushdown_allowed"] is False
    assert entry["default_execution_mode"] == "staged" and entry["pip_extra"] == "snowflake"
    assert "url_template" not in entry

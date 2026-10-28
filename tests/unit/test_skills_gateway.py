"""Everything the skills generate must pass the real governed gateway (analystos.gateway.validator)
and still produce the same answers when the gateway's re-generated, row-capped SQL is executed."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests.unit.test_skills_analysis import null_controls, planted  # noqa: E402
from tests.unit.test_skills_sqlbuild import SPECS  # noqa: E402

from analystos.gateway.validator import validate_sql  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from analystos.skills.analysis import run_analysis, verify_analysis  # noqa: E402
from analystos.skills.profiling import profile_asset  # noqa: E402
from analystos.skills.quality import check_quality  # noqa: E402
from analystos.skills.relationships import discover_relationships  # noqa: E402
from skills_fixtures import (  # noqa: E402
    INCIDENT_COLUMNS,
    NOW,
    PG_DSN,
    GatewayRunSQL,
    PgRunSQL,
    TsqlViaDuckRunSQL,
    duck_dataset,
    gateway_scope,
    pg_available,
    pg_load,
)

RELS = [{"from_asset": "itsm.incident", "from_column": "assignment_group", "to_asset": "itsm.sys_user_group",
         "to_column": "sys_id"}]
REL_ASSETS = [
    {"asset": "itsm.incident", "columns": INCIDENT_COLUMNS},
    {"asset": "itsm.sys_user_group", "columns": [{"name": "sys_id", "data_type": "VARCHAR", "is_key": True},
                                                 {"name": "name", "data_type": "VARCHAR"}]},
    {"asset": "crm.customer", "columns": [{"name": "id", "data_type": "BIGINT", "is_key": True},
                                          {"name": "name", "data_type": "VARCHAR"}]},
    {"asset": "crm.orders", "columns": [{"name": "id", "data_type": "BIGINT", "is_key": True},
                                        {"name": "customer_id", "data_type": "BIGINT"}, {"name": "amount", "data_type": "DOUBLE"},
                                        {"name": "unrelated_code", "data_type": "BIGINT"}]},
]


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
@pytest.mark.parametrize("name,spec", SPECS, ids=[n for n, _ in SPECS])
def test_every_compiled_spec_passes_gateway(dialect, name, spec):
    scope = gateway_scope(dialect)
    for purpose in sb.METHOD_PURPOSES[spec.method]:
        cq = sb.compile_spec(spec, dialect, purpose=purpose, sample_rows=500)
        v = validate_sql(scope, cq.sql, max_rows=cq.max_rows or 1000)
        assert v.referenced_assets == ["itsm.incident"]


@pytest.fixture(scope="module")
def duck():
    return duck_dataset()


@pytest.mark.parametrize("dialect", ["duckdb", "tsql"])
def test_skills_end_to_end_through_gateway(duck, dialect):
    inner = duck if dialect == "duckdb" else TsqlViaDuckRunSQL(duck)
    gw = GatewayRunSQL(inner, gateway_scope(dialect))
    for name, spec in planted().items():
        prim = run_analysis(spec, gw)
        assert prim.stat.supported, (dialect, name)
        assert verify_analysis(spec, gw, prim.stat).agrees, (dialect, name)
    for name, spec in null_controls().items():
        if spec.method != "driver_model":
            assert run_analysis(spec, gw).stat.supported is False, (dialect, name)
    p = profile_asset(gw, "itsm.incident", INCIDENT_COLUMNS)
    codes = {(i.code, i.column) for i in check_quality(gw, "itsm.incident", p, RELS, now=NOW)}
    assert {("duplicate_key", "number"), ("temporal_order", "resolved_at"), ("future_timestamp", "closed_at"),
            ("orphan_reference", "assignment_group"), ("case_variant_category", "category")} <= codes
    rels = discover_relationships(gw, REL_ASSETS)
    assert {(r.from_column, r.to_asset) for r in rels} == {("assignment_group", "itsm.sys_user_group"),
                                                           ("customer_id", "crm.customer")}


@pytest.mark.integration
def test_postgres_through_gateway():
    if not pg_available():
        pytest.skip("local Postgres not reachable")
    import psycopg

    conn = psycopg.connect(PG_DSN)
    schema = f"aos_gw_{uuid.uuid4().hex[:8]}"
    try:
        pg_load(conn, schema)
        gw = GatewayRunSQL(PgRunSQL(conn), gateway_scope("postgres", {"itsm": schema, "crm": schema}))
        for name, spec in planted(f"{schema}.incident").items():
            prim = run_analysis(spec, gw)
            assert prim.stat.supported and verify_analysis(spec, gw, prim.stat).agrees, name
        cols = [dict(c, data_type={"VARCHAR": "text", "TIMESTAMP": "timestamp", "BIGINT": "bigint", "BOOLEAN": "boolean",
                                   "DOUBLE": "double precision"}[c["data_type"]]) for c in INCIDENT_COLUMNS]
        p = profile_asset(gw, f"{schema}.incident", cols)
        rels = [{**RELS[0], "from_asset": f"{schema}.incident", "to_asset": f"{schema}.sys_user_group"}]
        codes = {i.code for i in check_quality(gw, f"{schema}.incident", p, rels, now=NOW)}
        assert {"duplicate_key", "temporal_order", "future_timestamp", "orphan_reference", "case_variant_category"} <= codes
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()

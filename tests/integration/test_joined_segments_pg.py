"""P8-16 on Postgres: the joined-segment and later-than specs compile for postgres, pass the real gateway
validator and find the retail journey's planted causes on a real engine, with unchanged row counts.

Runs in its own throw-away database (never the platform's `analystos` database); skips when Postgres is down."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
import retail_fixtures as rf  # noqa: E402
from analystos.agents import investigator as inv  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from analystos.skills.analysis import run_analysis, verify_analysis  # noqa: E402
from skills_fixtures import GatewayRunSQL, PgRunSQL  # noqa: E402

pytestmark = pytest.mark.integration

MAINTENANCE = os.environ.get("ANALYSTOS_TEST_PG_MAINTENANCE", "postgresql://analystos:analystos@localhost:5432/postgres")


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(MAINTENANCE, connect_timeout=2, autocommit=True)
    except Exception:  # noqa: BLE001
        pytest.skip("local Postgres not reachable")
    name = f"aos_test_p816_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    conn = psycopg.connect(MAINTENANCE.rsplit("/", 1)[0] + f"/{name}")
    try:
        data = rf.generate()
        rf.load_postgres(conn, data)
        yield conn, rf.truth(data)
    finally:
        conn.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def test_postgres_finds_the_causes_one_join_away(pg):
    from tests.unit.test_joined_segments import CUSTOMER_JOIN, LOOKUPS, PRODUCT_JOIN, RETURNED, TYPES, scope, spec

    conn, truth = pg
    sc = scope("postgres")
    gw = GatewayRunSQL(PgRunSQL(conn), sc)
    late = spec(joins=[CUSTOMER_JOIN, PRODUCT_JOIN])
    assert inv.validate_spec(late, sc, TYPES, LOOKUPS) == []
    rows = gw(sb.compile_spec(late, "postgres").sql).records()
    assert sum(r["n_rows"] for r in rows) == truth["orders"]  # the joins keep every order exactly once
    out = run_analysis(late, gw)
    assert out.stat.supported and out.stat.highlights["top_segment"] == "West"
    assert out.stat.highlights["top_rate"] == pytest.approx(truth["late_west"], abs=1e-3)
    assert verify_analysis(late, gw, out.stat).agrees

    returns = AnalysisSpec.model_validate({**late.model_dump(), "outcome": RETURNED,
                                           "segment": {"type": "column", "column": "category", "via": "product_id",
                                                       "label": "product category"}})
    out = run_analysis(returns, gw)
    assert out.stat.supported and out.stat.highlights["top_segment"] == "Electronics"
    assert run_analysis(returns.model_copy(update={"segment": late.segment}), gw).stat.supported is False


def test_validated_lookups_come_from_the_catalog(control_db):
    """Only a validated (or user-declared) many-to-one relationship between in-scope tables of one source, on
    visible keys, becomes a lookup; the rule playbook then proposes the related table's attribute."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from analystos.contracts.policy import DataScope
    from analystos.core.ids import new_id, utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import Relationship, Source, SourceAsset, SourceColumn, User, Workspace

    tables = {"orders": [("order_id", "id"), ("customer_id", "id"), ("product_id", "id"), ("note_id", "id"), ("ref_id", "id"),
                         ("order_date", "datetime"), ("promised_date", "datetime"), ("delivered_date", "datetime"),
                         ("sales_channel", "categorical")],
              "customers": [("customer_id", "id"), ("region", "categorical")],
              "products": [("product_id", "id"), ("category", "categorical")],
              "notes": [("note_id", "id"), ("kind", "categorical")]}
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws = new_id("ws")
        s.add(Workspace(id=ws, name="p8-16 lookups", created_by=admin.id))
        s.flush()
        sid = new_id("src")
        schema = f"src_p816_{sid[-6:]}"
        s.add(Source(id=sid, workspace_id=ws, kind="csv", name="retail", config={}, status="ready"))
        s.flush()
        ids = {}
        for name, cols in tables.items():
            ids[name] = new_id("ast")
            meta = {"profiled_at": utcnow().isoformat(), "fingerprint": f"fp-{name}",
                    "sampled": False, "truncated": False, "snapshot_load": None}
            s.add(SourceAsset(id=ids[name], source_id=sid, workspace_id=ws, schema_name=schema, name=name, source_name=name,
                              kind="table", selected=True, row_count=6000 if name == "orders" else 100,
                              fingerprint=f"fp-{name}", stats={"profile_meta": meta}))
            s.flush()
            for i, (c, t) in enumerate(cols):
                s.add(SourceColumn(asset_id=ids[name], name=c, ordinal=i, data_type="text", semantic_type=t,
                                   profile={"distinct": 4 if t == "categorical" else 100, "null_count": 0}))

        def rel(frm, col, to, to_col, **kw):
            s.add(Relationship(id=new_id("rel"), workspace_id=ws, from_asset_id=ids[frm], from_column=col, to_asset_id=ids[to],
                               to_column=to_col, confidence=0.9, **{"cardinality": "many_to_one", "validated": True, **kw}))
        rel("orders", "customer_id", "customers", "customer_id")
        rel("orders", "product_id", "products", "product_id", validated=False, origin="user")  # user-declared
        rel("orders", "note_id", "notes", "note_id", validated=False)  # discovered, never validated
        rel("customers", "region", "notes", "kind", cardinality="many_to_many")
        rel("orders", "ref_id", "notes", "note_id")  # polymorphic: one column, two validated targets
        rel("orders", "ref_id", "customers", "customer_id")
    fq = {n: f"{schema}.{n}" for n in tables}
    scope = DataScope(workspace_id=ws, user_id="u", role="analyst", source_ids=[sid], assets=list(fq.values()),
                      asset_sources={a: sid for a in fq.values()}, columns={fq[n]: [c for c, _ in cols] for n, cols in tables.items()},
                      denied_columns=[f"{fq['products']}.product_id"], source_dialects={sid: "postgres"})
    ctx = SimpleNamespace(workspace=SimpleNamespace(id=ws), scope=scope, policy=None)
    found = inv.validated_lookups(ctx)
    assert [(x.from_column, x.to_asset) for x in found] == [("customer_id", fq["customers"])]  # product key denied
    scope.denied_columns = []
    ctx = SimpleNamespace(workspace=SimpleNamespace(id=ws), scope=scope, policy=None)
    assert {x.from_column for x in inv.validated_lookups(ctx)} == {"customer_id", "product_id"}
    types = inv.semantic_types(ctx)
    props = inv.heuristic_proposals(ctx, types, packs=[])
    joined = {(p["spec"]["segment"].get("via"), p["spec"]["segment"]["column"]) for p in props if p["spec"].get("segment")}
    assert {("customer_id", "region"), ("product_id", "category")} <= joined and ("note_id", "kind") not in joined
    assert any((p["spec"].get("outcome") or {}).get("type") == "later_than" for p in props)

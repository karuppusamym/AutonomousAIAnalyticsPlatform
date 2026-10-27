"""Cross-source run acceptance (P4-E03) against two real sources: a Postgres pushdown source (the
compose Postgres) and a DuckDB database file queried in place.

Both sources are discovered by their real connectors, the run scope comes from
``governance.policy.resolve_scope`` over both, and every statement goes through
``QueryGateway.execute``: the join key is proposed by rules and by a (simulated) model, decided by
containment and cardinality, and the planted effect (enterprise customers' tickets take three times
longer, the tier living only in the DuckDB source) is found by the federated statements. A column
restricted in source B is rejected even inside a federated query. Skips cleanly without Postgres.
"""
from __future__ import annotations

import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import SQLRejected  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration

SEED = 11


def _plant(dp_control_url: str, folder: Path) -> dict[int, str]:
    rng = random.Random(SEED)
    tiers = {i: rng.choice(["enterprise", "smb", "mid"]) for i in range(1, 151)}
    con = duckdb.connect(str(folder / "crm.duckdb"))
    con.execute("CREATE SCHEMA crm")
    con.execute("CREATE TABLE crm.crm.customers (customer_id INTEGER PRIMARY KEY, tier VARCHAR, email VARCHAR, "
                "region VARCHAR)")
    con.executemany("INSERT INTO crm.crm.customers VALUES (?, ?, ?, ?)",
                    [(i, t, f"c{i}@example.com", rng.choice(["north", "south"])) for i, t in tiers.items()])
    con.close()
    rows = []
    for i in range(1, 1201):
        cid = rng.randint(1, 150)
        hours = rng.uniform(2, 10) * (3.0 if tiers[cid] == "enterprise" else 1.0)
        rows.append(f"({i}, {cid}, {hours:.2f}, '{rng.choice(['email', 'phone'])}')")
    admin = create_engine(dp_control_url)
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS xops CASCADE"))
        conn.execute(text("CREATE SCHEMA xops"))
        conn.execute(text("CREATE TABLE xops.tickets (id int PRIMARY KEY, customer_id int, resolution_hours numeric(8,2), "
                          "channel text)"))
        conn.execute(text("INSERT INTO xops.tickets VALUES " + ", ".join(rows)))
        conn.execute(text("ANALYZE xops.tickets"))
    admin.dispose()
    return tiers


@pytest.fixture()
def two_sources(dp_control_url, dp_settings, dp_session_factory, dp_workspace, tmp_path, monkeypatch):
    from analystos.connectors.registry import build_connector
    from analystos.db.models import Source, SourceAsset, SourceColumn

    _plant(dp_control_url, tmp_path)
    url = make_url(dp_control_url)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_XSRC_PG_PASSWORD", url.password or "")
    settings = dp_settings.model_copy(update={"upload_dir": tmp_path})
    ws = dp_workspace["workspace_id"]
    specs = {
        "postgres": ({"host": url.host, "port": url.port or 5432, "database": url.database, "username": url.username,
                      "schemas": ["xops"]}, "env:ANALYSTOS_SOURCE_SECRET_XSRC_PG_PASSWORD"),
        "duckdb": ({"path": "crm.duckdb"}, None),
    }
    ids: dict[str, str] = {}
    with dp_session_factory() as s:
        for kind, (config, secret_ref) in specs.items():
            sid = new_id(kind[:3])
            ids[kind] = sid
            s.add(Source(id=sid, workspace_id=ws, kind=kind, name=f"xsrc {kind}", config=config, secret_ref=secret_ref,
                         status="ready", execution_mode="pushdown", last_discovered_at=datetime.now(UTC)))
            s.flush()
            con = build_connector(SimpleNamespace(kind=kind, config=config, secret_ref=secret_ref,
                                                  execution_mode="pushdown"), settings)
            assert con.execution_mode == "pushdown"
            found = con.discover()
            assert found, f"{kind}: nothing discovered"
            for asset in found:
                aid = new_id("ast")
                s.add(SourceAsset(id=aid, source_id=sid, workspace_id=ws, schema_name=asset.schema_name,
                                  name=asset.name, source_name=asset.source_name, selected=True,
                                  row_count=asset.row_count))
                s.flush()
                for i, c in enumerate(asset.columns):
                    s.add(SourceColumn(asset_id=aid, name=c.name, ordinal=i, data_type=c.data_type, is_key=c.is_key,
                                       tags=["restricted"] if c.name == "email" else []))
            con.close()
        s.commit()
    yield SimpleNamespace(ids=ids, settings=settings)
    admin = create_engine(dp_control_url)
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS xops CASCADE"))
    admin.dispose()


def _scope_and_assets(dp_session_factory, dp_workspace, ids):
    from analystos.db.models import SourceAsset, SourceColumn, User
    from analystos.governance.policy import resolve_scope

    with dp_session_factory() as s:
        user = s.get(User, dp_workspace["user_id"])
        scope = resolve_scope(s, user, dp_workspace["workspace_id"], source_ids=list(ids.values()))
        assets = []
        for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id.in_(list(ids.values())))):
            cols = s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
            assets.append({"asset": f"{a.schema_name}.{a.name}",
                           "columns": [{"name": c.name, "data_type": c.data_type, "is_key": c.is_key} for c in cols]})
    return scope, assets


def test_planted_cross_source_effect_postgres_plus_duckdb(two_sources, dp_session_factory, dp_workspace) -> None:
    from analystos.db.models import QueryExecution
    from analystos.gateway.service import QueryGateway
    from analystos.skills.federation import JoinProposal, investigate_cross_source

    ids = two_sources.ids
    scope, assets = _scope_and_assets(dp_session_factory, dp_workspace, ids)
    assert scope.source_dialects == {ids["postgres"]: "postgres", ids["duckdb"]: "duckdb"}
    assert scope.asset_sources == {"xops.tickets": ids["postgres"], "crm.customers": ids["duckdb"]}
    assert "crm.customers.email" in scope.denied_columns

    gw = QueryGateway(two_sources.settings, session_factory=dp_session_factory)
    run_id = new_id("run")
    runner = gw.run_sql_for(scope, actor="agent:integration", run_id=run_id, federated=True)
    model_proposal = JoinProposal(from_asset="xops.tickets", from_column="id", to_asset="crm.customers",
                                  to_column="customer_id", origin="model")  # plausible-looking, wrong
    report = investigate_cross_source(runner, assets, scope.asset_sources, extra_proposals=[model_proposal])

    accepted = [d for d in report.decisions if d.accepted]
    assert [(d.proposal.from_column, d.proposal.to_column, d.proposal.origin) for d in accepted] == [
        ("customer_id", "customer_id", "rule")]
    rejected_model = next(d for d in report.decisions if d.proposal.origin == "model")
    assert not rejected_model.accepted and "containment" in rejected_model.reason
    top = report.effects[0]
    assert top.stat.supported and top.outcome == "xops.tickets.resolution_hours" and top.segment == "crm.customers.tier"
    assert top.stat.highlights["top_segment"] == "enterprise" and top.stat.p_value < 1e-6
    assert not next(e for e in report.effects if e.segment == "crm.customers.region").stat.supported

    with dp_session_factory() as s:
        rows = list(s.scalars(select(QueryExecution).where(QueryExecution.run_id == run_id)))
    legs = [r for r in rows if r.purpose.startswith("federation.leg:")]
    assert {r.source_id for r in legs} == set(ids.values()) and all(r.status == "ok" for r in legs)
    assert all(r.result_preview == [] for r in legs)
    federated = [r for r in rows if r.purpose.startswith("federated:")]
    assert federated and all(r.status == "ok" and r.source_id is None for r in federated)


def test_per_source_scope_is_enforced_inside_federated_queries(two_sources, dp_session_factory, dp_workspace) -> None:
    from analystos.db.models import QueryExecution
    from analystos.gateway.service import QueryGateway

    scope, _ = _scope_and_assets(dp_session_factory, dp_workspace, two_sources.ids)
    gw = QueryGateway(two_sources.settings, session_factory=dp_session_factory)
    run_id = new_id("run")
    runner = gw.run_sql_for(scope, actor="agent:integration", run_id=run_id, federated=True)
    for sql in (
        "SELECT c.email, AVG(t.resolution_hours) AS h FROM xops.tickets t JOIN crm.customers c "
        "ON c.customer_id = t.customer_id GROUP BY c.email",
        "SELECT * FROM xops.tickets t JOIN crm.customers c ON c.customer_id = t.customer_id",
        "SELECT t.id FROM xops.tickets t WHERE t.channel IN (SELECT email FROM crm.customers)",
    ):
        with pytest.raises(SQLRejected, match="restricted by policy"):
            runner(sql)
    with pytest.raises(SQLRejected):  # an asset outside the scope, in either source
        runner("SELECT COUNT(*) AS n FROM xops.tickets t JOIN crm.secret s ON s.id = t.id")
    with pytest.raises(SQLRejected, match="Cross-source queries are not supported"):  # not federated: refused
        gw.execute(scope, "SELECT COUNT(*) AS n FROM xops.tickets t JOIN crm.customers c "
                          "ON c.customer_id = t.customer_id", actor="agent:integration")
    ok = runner("SELECT c.tier, COUNT(*) AS n FROM xops.tickets t JOIN crm.customers c "
                "ON c.customer_id = t.customer_id GROUP BY c.tier ORDER BY c.tier")
    assert sum(r[1] for r in ok.rows) == 1200
    with dp_session_factory() as s:
        rows = list(s.scalars(select(QueryExecution).where(QueryExecution.run_id == run_id)))
    assert sum(1 for r in rows if r.status == "rejected") == 4
    assert not [r for r in rows if r.purpose.startswith("federation.leg:") and r.status != "ok"]

"""Cross-source runs on the DuckDB federation engine (P4-E03) and the Engine layer (P4-E01), with no
services: two real DuckDB database files as two pushdown sources, a fake control-plane session.

The Postgres + DuckDB version of the acceptance scenario is
tests/integration/test_cross_source_runs.py.
"""
from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from analystos.contracts.policy import DataScope
from analystos.core.config import Settings
from analystos.core.errors import Forbidden, InvalidInput, SQLRejected
from analystos.engines.base import Engine, Limits
from analystos.engines.duckdb import DuckDBEngine
from analystos.engines.registry import catalog_entries, engine_for_kind, engine_manifests, federation_engine
from analystos.gateway.service import QueryGateway
from analystos.gateway.validator import validate_federated_sql, validate_sql
from analystos.skills.federation import JoinProposal, investigate_cross_source, validate_join_keys

TICKET_COLS = [("id", "integer", True), ("customer_id", "integer", False), ("resolution_hours", "double", False),
               ("channel", "text", False)]
CUSTOMER_COLS = [("customer_id", "integer", True), ("tier", "text", False), ("email", "text", False),
                 ("region", "text", False)]


class FakeSession:
    def __init__(self, sink: list, sources: dict[str, Any]) -> None:
        self.sink, self.sources = sink, sources

    def get(self, model, key):  # noqa: ANN001
        for obj in self.sink:  # rows written earlier (S02: a federation leg's result_handle)
            if isinstance(obj, model) and getattr(obj, "id", None) == key:
                return obj
        return self.sources.get(key)

    def scalar(self, stmt):  # noqa: ANN001 - no workspace policy row, no run limits
        return None

    def execute(self, stmt):  # noqa: ANN001
        return SimpleNamespace(one=lambda: (1, None, 10))

    def add(self, obj) -> None:  # noqa: ANN001
        self.sink.append(obj)

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def make_files(folder: Path, *, seed: int = 7) -> None:
    rng = random.Random(seed)
    tiers = {i: rng.choice(["enterprise", "smb", "mid"]) for i in range(1, 121)}
    con = duckdb.connect(str(folder / "crm.duckdb"))
    con.execute("CREATE SCHEMA crm; CREATE TABLE crm.crm.customers (customer_id INTEGER PRIMARY KEY, tier VARCHAR, "
                "email VARCHAR, region VARCHAR)")
    con.executemany("INSERT INTO crm.crm.customers VALUES (?, ?, ?, ?)",
                    [(i, t, f"c{i}@example.com", rng.choice(["north", "south"])) for i, t in tiers.items()])
    con.close()
    con = duckdb.connect(str(folder / "ops.duckdb"))
    con.execute("CREATE SCHEMA ops; CREATE TABLE ops.ops.tickets (id INTEGER PRIMARY KEY, customer_id INTEGER, "
                "resolution_hours DOUBLE, channel VARCHAR)")
    rows = []
    for i in range(1, 901):
        cid = rng.randint(1, 120)
        base = rng.uniform(2, 10)
        hours = base * (3.0 if tiers[cid] == "enterprise" else 1.0)  # the planted cross-source effect
        rows.append((i, cid, round(hours, 2), rng.choice(["email", "phone"])))
    con.executemany("INSERT INTO ops.ops.tickets VALUES (?, ?, ?, ?)", rows)
    con.close()


@pytest.fixture()
def fed(tmp_path: Path):
    make_files(tmp_path)
    audit: list = []
    sources = {
        sid: SimpleNamespace(id=sid, workspace_id="ws", kind="duckdb", config={"path": fname}, secret_ref=None,
                             status="ready", execution_mode="pushdown", staging_schema=None, last_discovered_at=None)
        for sid, fname in (("s_ops", "ops.duckdb"), ("s_crm", "crm.duckdb"))
    }
    settings = Settings(upload_dir=tmp_path, result_store_dir=tmp_path / "results")
    gw = QueryGateway(settings, session_factory=lambda: FakeSession(audit, sources))
    columns = {"ops.tickets": [c for c, _, _ in TICKET_COLS], "crm.customers": [c for c, _, _ in CUSTOMER_COLS]}
    scope = DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["s_ops", "s_crm"],
                      assets=list(columns), asset_sources={"ops.tickets": "s_ops", "crm.customers": "s_crm"},
                      columns=columns, denied_columns=["crm.customers.email"],
                      source_dialects={"s_ops": "duckdb", "s_crm": "duckdb"}, max_rows=10_000, timeout_seconds=20)
    assets = [
        {"asset": "ops.tickets", "columns": [{"name": n, "data_type": t, "is_key": k} for n, t, k in TICKET_COLS]},
        {"asset": "crm.customers", "columns": [{"name": n, "data_type": t, "is_key": k} for n, t, k in CUSTOMER_COLS]},
    ]
    return SimpleNamespace(gw=gw, audit=audit, scope=scope, assets=assets, asset_sources=scope.asset_sources)


def test_planted_cross_source_effect_is_found(fed) -> None:
    runner = fed.gw.run_sql_for(fed.scope, actor="agent:test", federated=True)
    assert runner.dialect == "duckdb"
    report = investigate_cross_source(runner, fed.assets, fed.asset_sources)
    accepted = [d for d in report.decisions if d.accepted]
    assert [(d.proposal.from_asset, d.proposal.from_column, d.proposal.to_asset, d.proposal.to_column)
            for d in accepted] == [("ops.tickets", "customer_id", "crm.customers", "customer_id")]
    assert accepted[0].measured.cardinality == "many_to_one" and accepted[0].measured.evidence["containment"] == 1.0
    top = report.effects[0]
    assert top.stat.supported and top.outcome == "ops.tickets.resolution_hours" and top.segment == "crm.customers.tier"
    assert top.stat.highlights["top_segment"] == "enterprise" and top.stat.p_value < 1e-6
    region = next(e for e in report.effects if e.segment == "crm.customers.region")
    assert not region.stat.supported  # no effect was planted there
    # Every leg ran through execute() under its own source; the federated statement has its own audit row.
    audit = [r for r in fed.audit if hasattr(r, "purpose")]  # query_execution rows (legs also write result_handle rows)
    legs = [r for r in audit if r.purpose.startswith("federation.leg:")]
    assert {r.source_id for r in legs} == {"s_ops", "s_crm"} and all(r.status == "ok" for r in legs)
    assert all(r.result_uri for r in legs)  # S02-T7: every leg came back as a Parquet handle
    assert all(r.result_preview == [] for r in legs)  # leg rows are never retained in the audit
    fed_rows = [r for r in audit if r.purpose.startswith("federated:")]
    assert fed_rows and all(r.source_id is None for r in fed_rows)
    assert all('"email"' not in (r.executed_sql or "") for r in legs)  # only referenced columns are extracted


def test_denied_column_of_source_b_is_rejected_inside_a_federated_query(fed) -> None:
    runner = fed.gw.run_sql_for(fed.scope, actor="agent:test", federated=True)
    for sql in (
        "SELECT c.email, AVG(t.resolution_hours) AS h FROM ops.tickets t JOIN crm.customers c "
        "ON c.customer_id = t.customer_id GROUP BY c.email",
        "SELECT t.id FROM ops.tickets t JOIN crm.customers c ON c.email = t.channel",
        "SELECT * FROM ops.tickets t JOIN crm.customers c ON c.customer_id = t.customer_id",
        "WITH x AS (SELECT email FROM crm.customers) SELECT COUNT(*) AS n FROM x",
    ):
        with pytest.raises(SQLRejected, match="restricted by policy"):
            runner(sql)
    rejected = [r for r in fed.audit if r.status == "rejected"]
    assert len(rejected) == 4 and not [r for r in fed.audit if getattr(r, "purpose", "").startswith("federation.leg:")]


def test_join_proposals_are_decided_by_containment_and_cardinality(fed) -> None:
    runner = fed.gw.run_sql_for(fed.scope, actor="agent:test", federated=True)
    proposals = [  # e.g. from a model: only the first one is sound
        JoinProposal(from_asset="ops.tickets", from_column="customer_id", to_asset="crm.customers",
                     to_column="customer_id", origin="model"),
        JoinProposal(from_asset="ops.tickets", from_column="id", to_asset="crm.customers", to_column="customer_id",
                     origin="model"),  # ids 1..900 vs 1..120: containment 0.133
        JoinProposal(from_asset="crm.customers", from_column="region", to_asset="ops.tickets", to_column="channel",
                     origin="model"),  # no overlap
        JoinProposal(from_asset="ops.tickets", from_column="channel", to_asset="crm.customers", to_column="email",
                     origin="model"),  # denied column
        JoinProposal(from_asset="ops.tickets", from_column="nope", to_asset="crm.customers", to_column="customer_id",
                     origin="model"),
    ]
    decisions = validate_join_keys(runner, proposals, fed.assets, fed.asset_sources)
    assert [d.accepted for d in decisions] == [True, False, False, False, False]
    assert "containment" in decisions[1].reason
    assert "restricted by policy" in decisions[3].reason
    assert "metadata" in decisions[4].reason
    many = validate_join_keys(runner, [JoinProposal(from_asset="crm.customers", from_column="customer_id",
                                                    to_asset="ops.tickets", to_column="customer_id")],
                              fed.assets, fed.asset_sources)
    # customers.customer_id is unique and tickets.customer_id is not: measured as one_to_many (P7-09, the
    # reversed direction), and refused because the join would duplicate the from-rows.
    assert not many[0].accepted and "one_to_many" in many[0].reason
    assert many[0].measured.cardinality == "one_to_many"


def test_cross_source_needs_the_federated_runner(fed) -> None:
    sql = "SELECT COUNT(*) AS n FROM ops.tickets t JOIN crm.customers c ON c.customer_id = t.customer_id"
    with pytest.raises(SQLRejected, match="Cross-source queries are not supported"):
        fed.gw.execute(fed.scope, sql, actor="user:x")
    assert fed.gw.execute(fed.scope, sql, actor="user:x", federated=True).rows == [[900]]
    with pytest.raises(InvalidInput):
        fed.gw.run_sql_for(fed.scope, actor="x", federated=True, source_id="s_ops")


def test_federated_validation_rules() -> None:
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["a", "b"],
                      assets=["x.t", "y.u", "x.t"], asset_sources={"x.t": "b", "y.u": "b"},
                      columns={"x.t": ["k"], "y.u": ["k", "s"]}, denied_columns=["*.s"],
                      source_dialects={"a": "postgres", "b": "postgres"})
    with pytest.raises(SQLRejected, match="more than one source"):
        validate_federated_sql(scope, "SELECT k FROM x.t", max_rows=10)
    with pytest.raises(SQLRejected, match="restricted"):
        validate_federated_sql(scope, "SELECT s FROM y.u", max_rows=10)
    with pytest.raises(SQLRejected):
        validate_federated_sql(scope, "SELECT * FROM read_csv('/etc/passwd')", max_rows=10)
    v = validate_federated_sql(scope, "SELECT COUNT(*) AS n FROM y.u", max_rows=10)
    assert v.source_id == "federated" and v.asset_sources == {"y.u": "b"} and v.dialect == "duckdb"


def test_federation_engine_is_sandboxed(tmp_path: Path) -> None:
    """Defence in depth: even SQL that never passed the validator cannot read files or unlock settings."""
    (tmp_path / "secret.csv").write_text("x\n42\n", encoding="utf-8")
    engine = DuckDBEngine()
    raw = SimpleNamespace(executable_sql=f"SELECT * FROM read_csv('{tmp_path / 'secret.csv'}')")
    with pytest.raises(Forbidden):
        engine.federate({"a.t": (["k"], [[1]])}, raw, limits=Limits(max_rows=10, timeout_seconds=5))
    raw.executable_sql = "SET enable_external_access = true"
    with pytest.raises(InvalidInput):
        engine.federate({"a.t": (["k"], [[1]])}, raw, limits=Limits(max_rows=10, timeout_seconds=5))
    raw.executable_sql = 'SELECT k, v FROM "a"."t" ORDER BY k'
    assert engine.federate({"a.t": (["k", "v"], [[2, "b"], [1, None]])}, raw,
                           limits=Limits(max_rows=10, timeout_seconds=5)) == (["k", "v"], [[1, None], [2, "b"]])


def test_duckdb_pushdown_is_read_only_and_opt_in(fed) -> None:
    single = fed.gw.run_sql_for(fed.scope, actor="user:x", source_id="s_crm")
    assert single.dialect == "duckdb"
    assert single("SELECT tier, COUNT(*) AS n FROM crm.customers GROUP BY tier ORDER BY tier").row_count == 3
    scope = fed.scope.model_copy(update={"source_ids": ["s_crm"], "assets": ["crm.customers"],
                                         "asset_sources": {"crm.customers": "s_crm"}})
    validated = validate_sql(scope, "SELECT tier FROM crm.customers", max_rows=5)
    with pytest.raises((InvalidInput, Forbidden)):
        fed.gw._run({"kind": "duckdb", "id": "s_crm", "config": {"path": "crm.duckdb"}, "secret_ref": None,
                     "execution_mode": "pushdown", "staging_schema": None, "workspace_id": "ws"},
                    validated.model_copy(update={"executable_sql": "INSERT INTO crm.customers VALUES (999, 'x', 'y', 'z')"}),
                    5, 5)


def test_engine_layer_and_catalog() -> None:
    for kind in ("postgres", "sqlserver", "duckdb", "snowflake"):
        assert isinstance(engine_for_kind(kind), Engine)
    assert engine_for_kind("postgres").id == "engine.postgres"
    snowflake = engine_for_kind("snowflake")
    assert "pushdown" not in snowflake.features  # validated dialect, but no read-only session: stays staged
    with pytest.raises(InvalidInput, match="stage the source"):
        snowflake.execute_read(SimpleNamespace(executable_sql="SELECT 1"), identity=SimpleNamespace(url="x", session_sql=()),
                               limits=Limits(max_rows=1, timeout_seconds=1))
    with pytest.raises(Forbidden):
        engine_for_kind("postgres").plan_write(object())
    with pytest.raises(InvalidInput, match="draft"):
        federation_engine("trino").federate({}, SimpleNamespace(executable_sql="SELECT 1"),
                                            limits=Limits(max_rows=1, timeout_seconds=1))
    by = {e["kind"]: e for e in catalog_entries()}
    for kind in ("snowflake", "bigquery", "databricks", "trino"):
        assert by[kind]["status"] == "draft" and by[kind]["dialect_validated"] and "pushdown" not in by[kind]["features"]
    assert by["duckdb"]["features"] == ["federate", "pushdown"]
    assert by["mysql"]["dialect_validated"] and by["mysql"]["features"] == []  # compiler does not emit mysql yet
    ids = {m["id"] for m in engine_manifests()}
    assert {"engine.postgres", "engine.duckdb", "engine.snowflake"} <= ids

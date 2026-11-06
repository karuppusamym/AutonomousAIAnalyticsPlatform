"""P1-7 declared quality rules: the contract, SQL rendering and verdicts (DuckDB), owner SQL refusal, declaring and
running through a gateway double, the trust signal, profile recommendations through the review queue, the
quality_rules monitor and the OKF round trip (no services: SQLite control plane, DuckDB data)."""
from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest
from sqlalchemy import select

from analystos.contracts.policy import DataScope
from analystos.contracts.quality import QualityRule
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import (
    KnowledgeSuggestion,
    Monitor,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    Workspace,
    WorkspaceMember,
)
from analystos.gateway.types import QueryResult
from analystos.quality import monitoring, okf_rules, recommend, service
from analystos.quality.models import QualityRuleResult, QualityRuleRow
from analystos.skills.quality_rules import check_expression, compile_rule, judge, run_rule
from analystos.tenancy import DEFAULT_TENANT_ID, tenant_scope

WS = "ws_dq"
COLS = ["id", "status", "amount", "email", "code"]
SCOPE = DataScope(workspace_id=WS, user_id="usr_ow", role="owner", source_ids=["src1"], assets=["shop.orders"],
                  asset_sources={"shop.orders": "src1"}, columns={"shop.orders": COLS}, source_dialects={"src1": "duckdb"})
EXTRA_TABLES = ("knowledge_suggestion", "knowledge_pack", "knowledge_document", "quality_rule", "quality_rule_result")


def _duck() -> duckdb.DuckDBPyConnection:
    db = duckdb.connect()
    db.execute("CREATE SCHEMA shop")
    db.execute("CREATE TABLE shop.orders(id INTEGER, status VARCHAR, amount DOUBLE, email VARCHAR, code VARCHAR)")
    rows = [(i, ["open", "closed", "void"][i % 3], float(i), f"u{i}@x.org", f"A{i:03d}") for i in range(1, 101)]
    rows[5] = (6, None, 6.0, None, "bad")  # one null status, one bad code
    rows[9] = (9, "open", 9000.0, "u9@x.org", "A009")  # an outlier and a duplicate id
    db.executemany("INSERT INTO shop.orders VALUES (?, ?, ?, ?, ?)", rows)
    return db


class DuckGateway:
    def __init__(self) -> None:
        self.db, self.calls = _duck(), []

    def run_sql_for(self, scope, *, actor, run_id=None, task_id=None, source_id=None):
        def run(sql, purpose="x", max_rows=None):
            self.calls.append({"sql": sql, "purpose": purpose, "actor": actor, "max_rows": max_rows})
            cur = self.db.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            return QueryResult(query_id=new_id("qry"), columns=cols, rows=rows, row_count=len(rows), fingerprint="f",
                               result_hash=stable_hash(rows), sql=sql)
        return run


def _rule(**kw) -> QualityRule:
    return QualityRule(**{"asset": "shop.orders", **kw})


# ------------------------------------------------------------------------------------ contract
def test_rule_contract_shapes():
    assert _rule(type="not_null", column="id").dimension == "completeness"
    assert _rule(type="unique", columns=["id", "status"]).key() == "unique(shop.orders.id,status)"
    for bad, msg in [({"type": "not_null"}, "needs column"), ({"type": "accepted_values", "column": "s"}, "needs values"),
                     ({"type": "range", "column": "a"}, "min or max"), ({"type": "range", "column": "a", "min": 2, "max": 1}, "greater"),
                     ({"type": "regex", "column": "a", "pattern": "("}, "regular expression"),
                     ({"type": "statistic_range", "column": "a", "max": 1}, "needs statistic"),
                     ({"type": "sql_row", "column": "a", "sql": "a > 1"}, "inside sql"),
                     ({"type": "sql_aggregate", "sql": "sum(a)", "min": 1, "threshold": 0.5}, "whole"),
                     ({"type": "not_null", "column": "a", "sql": "a > 1"}, "only for sql"),
                     ({"type": "not_null", "column": "a", "asset": "orders"}, "schema.table")]:
        with pytest.raises(ValueError, match=msg):
            QualityRule(**{"asset": "shop.orders", **bad})
    # presentation is not identity: renaming a rule keeps its hash
    assert _rule(type="not_null", column="id", name="x").content_hash() == _rule(type="not_null", column="id").content_hash()
    assert _rule(type="not_null", column="id").content_hash() != _rule(type="not_null", column="id", threshold=0.9).content_hash()


# ------------------------------------------------------------------------------------ SQL + verdicts
@pytest.mark.parametrize("kw, status, rate", [
    ({"type": "not_null", "column": "status"}, "failed", 0.99),
    ({"type": "not_null", "column": "status", "threshold": 0.99}, "passed", 0.99),
    ({"type": "unique", "column": "id"}, "failed", 0.99),
    ({"type": "accepted_values", "column": "status", "values": ["open", "closed", "void"]}, "passed", 1.0),
    ({"type": "accepted_values", "column": "status", "values": ["open", "closed", "void"], "allow_null": False}, "failed", 0.99),
    ({"type": "range", "column": "amount", "min": 0, "max": 100}, "failed", 0.99),
    ({"type": "regex", "column": "code", "pattern": "^A[0-9]{3}$", "threshold": 0.95}, "passed", 0.99),
    ({"type": "sql_row", "sql": "amount < 1000 OR status = 'void'"}, "failed", 0.99),
    ({"type": "statistic_range", "column": "amount", "statistic": "max", "max": 1000}, "failed", 0.0),
    ({"type": "statistic_range", "column": "status", "statistic": "null_rate", "max": 0.05}, "passed", 1.0),
    ({"type": "statistic_range", "column": "amount", "statistic": "median", "min": 40, "max": 60}, "passed", 1.0),
    ({"type": "statistic_range", "column": "status", "statistic": "distinct_count", "min": 3, "max": 3}, "passed", 1.0),
    ({"type": "sql_aggregate", "sql": "COUNT(DISTINCT id) * 1.0 / COUNT(*)", "min": 1.0}, "failed", 0.0),
])
def test_rules_compile_run_and_judge_on_duckdb(kw, status, rate):
    db = _duck()
    rule = _rule(**kw)
    compiled = compile_rule(rule, "duckdb", columns=COLS)
    cur = db.execute(compiled.sql)
    row = dict(zip([d[0].lower() for d in cur.description], cur.fetchone(), strict=True))
    out = judge(rule, compiled, row)
    assert (out.status, out.pass_rate) == (status, rate)


def test_rule_sql_is_one_aggregate_statement_in_every_dialect():
    rule = _rule(type="range", column="amount", min=0, max=5)
    for dialect in ("postgres", "tsql", "trino", "duckdb"):
        sql = compile_rule(rule, dialect).sql
        assert sql.upper().startswith("SELECT COUNT(*)") and "LIMIT" not in sql.upper()
    with pytest.raises(InvalidInput, match="not supported on tsql"):
        compile_rule(_rule(type="regex", column="code", pattern="^A"), "tsql")
    with pytest.raises(InvalidInput, match="no column"):
        compile_rule(_rule(type="not_null", column="nope"), "duckdb", columns=COLS)


@pytest.mark.parametrize("sql, why", [
    ("(SELECT 1) = 1", "subquery"), ("pg_sleep(1) > 0", "anonymous"), ("o.amount > 1", "qualify"),
    ("amount > 1; DROP TABLE x", "one expression"), ("nope > 1", "unknown column"), ("SUM(amount) > 1", "no aggregates"),
])
def test_owner_sql_is_an_expression_over_the_asset_only(sql, why):
    with pytest.raises(InvalidInput, match=why):
        check_expression(sql, "postgres", COLS, aggregate=False)
    with pytest.raises(InvalidInput, match="needs an aggregate"):
        check_expression("amount", "postgres", COLS, aggregate=True)
    assert check_expression("COUNT(*) > 0", "postgres", COLS, aggregate=True) is not None


def test_a_refused_statement_is_an_error_never_a_pass():
    from analystos.core.errors import PolicyDenied

    def refused(sql, purpose="x", max_rows=None):
        raise PolicyDenied("column denied")

    refused.dialect = "duckdb"
    out = run_rule(refused, _rule(type="not_null", column="email"))
    assert out.status == "error" and out.pass_rate is None and "denied" in out.error


# ------------------------------------------------------------------------------------ persisted flow
@pytest.fixture
def world(sqlite_db, monkeypatch):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in EXTRA_TABLES])
    with tenant_scope(DEFAULT_TENANT_ID):
        with session_scope() as s:
            for uid in ("usr_ow", "usr_ed", "usr_vw", "usr_an"):
                s.add(User(id=uid, email=f"{uid}@dq", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
            s.add(Workspace(id=WS, name="dq", created_by="usr_ow", settings={}, policy_version=1, tenant_id=DEFAULT_TENANT_ID))
            for uid, role in (("usr_ow", "owner"), ("usr_ed", "editor"), ("usr_vw", "viewer"), ("usr_an", "analyst")):
                s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
            s.add(Source(id="src1", workspace_id=WS, kind="duckdb", name="shop", config={}, status="ready",
                         execution_mode="pushdown", owners={}))
            s.add(SourceAsset(id="ast1", source_id="src1", workspace_id=WS, schema_name="shop", name="orders",
                              source_name="orders", selected=True, stats={"row_count": 100}))
            prof = {
                "id": {"non_null": 100, "null_count": 0, "null_rate": 0.0, "distinct": 99, "semantic_type": "id",
                       "type_family": "numeric", "percentiles": {"p01": 1, "p99": 99}},
                "status": {"non_null": 99, "null_count": 1, "null_rate": 0.01, "distinct": 3, "semantic_type": "categorical",
                           "type_family": "text", "values_complete": True, "values": ["closed", "open", "void"]},
                "amount": {"non_null": 100, "null_count": 0, "null_rate": 0.0, "distinct": 100, "semantic_type": "numeric",
                           "type_family": "numeric", "percentiles": {"p01": 1.0, "p50": 50.0, "p99": 99.0}},
                "email": {"non_null": 99, "null_count": 1, "null_rate": 0.01, "distinct": 99, "semantic_type": "text",
                          "type_family": "text", "values_complete": True, "values": ["u1@x.org", "u2@x.org"]},
                "code": {"non_null": 100, "null_count": 0, "null_rate": 0.0, "distinct": 100, "semantic_type": "text",
                         "type_family": "text"},
            }
            for i, c in enumerate(COLS):
                s.add(SourceColumn(asset_id="ast1", name=c, ordinal=i, data_type="DOUBLE" if c in ("id", "amount") else "VARCHAR",
                                   tags=["pii"] if c == "email" else [], profile=prof[c], semantics={},
                                   is_key=c == "id"))
        gw = DuckGateway()
        monkeypatch.setattr("analystos.governance.policy.resolve_scope", lambda *a, **k: SCOPE)
        monkeypatch.setattr("analystos.runtime.context.default_gateway", lambda: gw)
        users = {}
        with session_scope() as s:
            for uid in ("usr_ow", "usr_ed", "usr_vw", "usr_an"):
                u = s.get(User, uid)
                s.expunge(u)
                users[uid] = u
        yield SimpleNamespace(users=users, gw=gw)


def _declare(user, **kw):
    with session_scope() as s:
        row, created = service.declare(s, s.merge(user), WS, _rule(**kw))
        return row.id, created


def test_declare_validates_against_the_catalog_and_is_idempotent(world):
    rid, created = _declare(world.users["usr_ed"], type="not_null", column="status", threshold=0.99)
    assert created and _declare(world.users["usr_ed"], type="not_null", column="status", threshold=0.99) == (rid, False)
    with pytest.raises(InvalidInput, match="no column"):
        _declare(world.users["usr_ed"], type="not_null", column="missing")
    with pytest.raises(InvalidInput, match="unknown column"):
        _declare(world.users["usr_ed"], type="sql_row", sql="ghost > 1")
    with pytest.raises(Forbidden):
        _declare(world.users["usr_an"], type="not_null", column="id")  # analysts cannot declare
    with pytest.raises(NotFound), session_scope() as s:
        service.declare(s, s.merge(world.users["usr_ed"]), WS, QualityRule(type="not_null", asset="shop.nope", column="x"))


def test_named_source_owners_decide_their_assets_rules(world):
    with session_scope() as s:
        s.get(Source, "src1").owners = {"technical": {"name": "Owen", "user_id": "usr_ow"}}
    with pytest.raises(Forbidden, match="named owners"):
        _declare(world.users["usr_ed"], type="not_null", column="id")
    assert _declare(world.users["usr_ow"], type="not_null", column="id")[1]


def test_run_rules_through_the_gateway_records_counts_and_trust(world):
    a, _ = _declare(world.users["usr_ed"], type="not_null", column="status", threshold=0.99)
    b, _ = _declare(world.users["usr_ed"], type="unique", column="id", severity="critical")
    with session_scope() as s:
        trust = service.asset_trust(s, WS)
        assert trust["shop.orders"]["level"] == "unverified" and trust["shop.orders"]["unrun"] == 2
        out = service.run_rules(s, s.merge(world.users["usr_an"]), WS, trigger="user:usr_an")
    assert {o["rule_id"]: o["status"] for o in out} == {a: "passed", b: "failed"}
    assert all(c["purpose"].startswith("quality.rule.") and c["max_rows"] == 1 for c in world.gw.calls)
    with session_scope() as s:
        results = list(s.scalars(select(QualityRuleResult)))
        assert len(results) == 2 and all(r.query_id and r.evaluated for r in results)
        trust = service.asset_trust(s, WS)["shop.orders"]
        assert trust["level"] == "failing" and trust["passing"] == 1 and trust["failing"] == 1
        assert trust["by_dimension"]["uniqueness"] == {"rules": 1, "passing": 0}
        service.set_status(s, s.merge(world.users["usr_ed"]), b, "disabled")
        assert service.asset_trust(s, WS)["shop.orders"]["level"] == "trusted"


def test_rule_on_an_asset_outside_the_runner_scope_is_an_error(world, monkeypatch):
    rid, _ = _declare(world.users["usr_ed"], type="not_null", column="id")
    monkeypatch.setattr("analystos.governance.policy.resolve_scope",
                        lambda *a, **k: SCOPE.model_copy(update={"assets": [], "asset_sources": {}}))
    with session_scope() as s:
        out = service.run_rules(s, s.merge(world.users["usr_an"]), WS, trigger="t")
    assert out[0]["status"] == "error" and "not in the runner's data scope" in out[0]["error"] and not world.gw.calls


def test_profile_recommendations_land_as_drafts_and_an_owner_approves(world):
    from analystos.knowledge.suggestions import review

    with session_scope() as s:
        res = recommend.recommend(s, s.merge(world.users["usr_ed"]), WS, "ast1")
    kinds = {(q["column"], q["type"]) for q in res["queued"]}
    assert ("status", "not_null") in kinds and ("status", "accepted_values") in kinds
    assert ("amount", "range") in kinds and ("id", "not_null") in kinds
    assert ("email", "not_null") in kinds and ("email", "accepted_values") not in kinds  # PII: never a literal
    assert ("id", "unique") not in kinds  # 99 distinct of 100: not a key in today's data
    with session_scope() as s:
        assert service.list_rules(s, WS) == []  # nothing runs before review
        drafts = {(d.fields["rule"]["value"]["column"], d.fields["rule"]["value"]["type"]): d.id
                  for d in s.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.kind == "quality_rule"))}
        rng = s.get(KnowledgeSuggestion, drafts[("amount", "range")])
        assert rng.fields["rule"]["value"]["min"] == 1.0 and rng.fields["rule"]["value"]["threshold"] == 0.98
        assert s.get(KnowledgeSuggestion, drafts[("status", "not_null")]).fields["rule"]["value"]["threshold"] == 0.98
    edited = {**rng.fields["rule"]["value"], "max": 150.0}
    with session_scope() as s:
        out = review(s, WS, s.merge(world.users["usr_ow"]), [
            {"id": drafts[("status", "accepted_values")], "action": "approve"},
            {"id": drafts[("amount", "range")], "action": "edit", "fields": {"rule": edited}},
            {"id": drafts[("id", "not_null")], "action": "edit", "fields": {"rule": {"type": "bogus"}}},
        ])
    assert len(out["approved"]) == 2 and out["errors"] == [{"id": drafts[("id", "not_null")], "error": "invalid_rule"}]
    with session_scope() as s:
        rules = {r.type: r for r in service.list_rules(s, WS)}
        assert set(rules) == {"accepted_values", "range"} and rules["range"].definition["max"] == 150.0
        assert rules["range"].origin == "recommended" and rules["range"].approved_by == "usr_ow"
        again = recommend.recommend(s, s.merge(world.users["usr_ed"]), WS, "ast1")
        assert ("amount", "range") not in {(q["column"], q["type"]) for q in again["queued"]}  # declared already


def test_quality_rules_monitor_alerts_on_failing_rules(world):
    from analystos.services.monitors import create_monitor

    _declare(world.users["usr_ed"], type="not_null", column="status", threshold=0.99)
    bad, _ = _declare(world.users["usr_ed"], type="range", column="amount", min=0, max=100, severity="critical")
    _declare(world.users["usr_ed"], type="not_null", column="status", severity="info")  # info never alerts
    with session_scope() as s:
        m = create_monitor(s, s.merge(world.users["usr_ed"]), WS, name="orders dq", kind="quality_rules",
                           config={"assets": ["shop.orders"]})
        with pytest.raises(InvalidInput, match="not found"):
            create_monitor(s, s.merge(world.users["usr_ed"]), WS, name="x", kind="quality_rules", config={"rules": ["qrule_no"]})
        mon = s.get(Monitor, m.id)
        result = monitoring.evaluate(s, s.merge(world.users["usr_an"]), mon)
    assert result["alert"] and result["severity"] == "critical" and result["new"] == [bad]
    assert result["summary"] == {"rules": 3, "passed": 1, "failed": 2, "errors": 0}
    assert "pass rate 99.00% < 100.00%" in result["message"]


def test_rules_round_trip_through_okf(world):
    from analystos.knowledge.suggestions import review

    _declare(world.users["usr_ed"], type="accepted_values", column="status", values=["open", "closed", "void"],
             description="Order lifecycle states.")
    _declare(world.users["usr_ed"], type="sql_aggregate", sql="SUM(amount) / COUNT(*)", min=0, max=500)
    with session_scope() as s:
        first = okf_rules.export_pack(s, s.merge(world.users["usr_vw"]), WS)
    assert first["documents"] == 2 and "index.md" in first["files"]
    parsed, errors = okf_rules.parse_files(first["files"])
    assert errors == [] and {r.type for _, r in parsed} == {"accepted_values", "sql_aggregate"}
    with session_scope() as s:  # a clean copy: disable the rules, then import the pack back
        for r in service.list_rules(s, WS):
            s.delete(r)
    with session_scope() as s:
        imported = okf_rules.import_pack(s, s.merge(world.users["usr_ed"]), WS, first["files"])
        assert len(imported["suggestions"]) == 2 and service.list_rules(s, WS) == []  # drafts only
    with session_scope() as s:
        review(s, WS, s.merge(world.users["usr_ow"]), [{"id": i, "action": "approve"} for i in imported["suggestions"]])
    with session_scope() as s:
        assert {r.origin for r in service.list_rules(s, WS)} == {"import:okf"}
        second = okf_rules.export_pack(s, s.merge(world.users["usr_vw"]), WS)
    assert second["files"] == first["files"] and second["digest"] == first["digest"]
    with pytest.raises(InvalidInput):
        okf_rules.parse_files({"x.md": b"---\ntype: [\n---\n"})
    _, errs = okf_rules.parse_files({"notes/a.md": b"---\ntype: Note\ntitle: a\n---\n\nhello\n"})
    assert errs == [{"path": "notes/a.md", "error": "not_a_quality_rule"}]


def test_rule_tables_round_trip_rows(world):
    rid, _ = _declare(world.users["usr_ed"], type="not_null", column="id")
    with session_scope() as s:
        row = s.get(QualityRuleRow, rid)
        assert row.workspace_id == WS and row.asset_id == "ast1" and QualityRule.model_validate(row.definition).column == "id"

"""Review P0-2 / P1-4 without services: `context.lookup` (YAML/JSON contract, scope, budget, value-free profile and
usage), the human-only `guidelines` field, the bounded usage factor in the ledger rerank and its benchmark."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from analystos.context import lookup as L
from analystos.context import retrieval, usage
from analystos.core.errors import ContextOverBudget, InvalidInput, PolicyDenied

WS = "ws_lookup"
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
EXTRA_TABLES = ("context_entry", "relationship", "knowledge_suggestion", "request", "semantic_relationship_candidate",
                "knowledge_pack", "knowledge_document", "conversation_summary", "conversation_correction")


@pytest.fixture
def db(sqlite_db):
    from analystos.db import models
    from analystos.db.base import session_scope
    from analystos.db.models import (
        Artifact,
        QueryExecution,
        Relationship,
        Source,
        SourceAsset,
        SourceColumn,
        User,
        Workspace,
        WorkspaceMember,
    )

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in EXTRA_TABLES])
    with session_scope() as s:
        s.add_all([User(id=u, email=f"{u}@x", name=u, password_hash="x", is_admin=False, active=True, attributes={})
                   for u in ("u_editor", "u_viewer", "u_other")])
        s.add(User(id="u_svc", email="svc@mcp", name="svc", password_hash="!", is_admin=False, active=True,
                   attributes={"service_account": "mcp_client", "client_id": "mcpc_1"}))
        s.flush()
        s.add(Workspace(id=WS, name=WS, description="", objective="", autonomy_level=3, created_by="u_editor"))
        s.flush()
        s.add_all([WorkspaceMember(workspace_id=WS, user_id="u_editor", role="editor"),
                   WorkspaceMember(workspace_id=WS, user_id="u_viewer", role="viewer"),
                   WorkspaceMember(workspace_id=WS, user_id="u_svc", role="editor")])
        s.add(Source(id="src_1", workspace_id=WS, kind="postgres", name="itsm", status="ready", execution_mode="pushdown"))
        s.flush()
        profiled = {"profile_meta": {"rows_profiled": 100, "profiled_at": "2026-11-01T00:00:00+00:00"}}
        s.add_all([
            SourceAsset(id="ast_inc", source_id="src_1", workspace_id=WS, schema_name="itsm", name="incident",
                        source_name="incident", selected=True, row_count=100, stats=profiled, business_name="Incidents",
                        description="One row per incident.", description_origin="user",
                        semantics={"role": "fact", "grain": "incident",
                                   "guidelines": {"text": "Use closed_at for resolution time.", "by": "u_editor",
                                                  "at": "2026-11-02T00:00:00+00:00"}}),
            SourceAsset(id="ast_team", source_id="src_1", workspace_id=WS, schema_name="itsm", name="team",
                        source_name="team", selected=True, row_count=8, stats=profiled, semantics={"role": "dimension"}),
            SourceAsset(id="ast_pay", source_id="src_1", workspace_id=WS, schema_name="hr", name="payroll",
                        source_name="payroll", selected=False, row_count=50, stats=profiled,
                        description="Salaries per employee.", semantics={}),
        ])
        s.flush()
        col = SourceColumn
        s.add_all([
            col(asset_id="ast_inc", name="id", ordinal=0, data_type="INTEGER", semantic_type="id", nullable=False, is_key=True,
                profile={"null_rate": 0, "distinct": 100}),
            col(asset_id="ast_inc", name="priority", ordinal=1, data_type="TEXT", semantic_type="categorical",
                description="Urgency, P1 to P4", description_origin="user",
                profile={"null_rate": 0.01, "distinct": 4, "top_values": [{"value": "P3", "count": 50}, {"value": "P1", "count": 9}]}),
            col(asset_id="ast_inc", name="caller_email", ordinal=2, data_type="TEXT", semantic_type="text", tags=["pii"],
                profile={"null_rate": 0.2, "distinct": 80, "top_values": [{"value": "a@b.c", "count": 3}]}),
            col(asset_id="ast_inc", name="team_id", ordinal=3, data_type="INTEGER", semantic_type="id",
                profile={"null_rate": 0, "distinct": 8, "min": 1, "max": 8}),
            col(asset_id="ast_team", name="id", ordinal=0, data_type="INTEGER", semantic_type="id", nullable=False, is_key=True,
                profile={"null_rate": 0, "distinct": 8}),
            col(asset_id="ast_team", name="name", ordinal=1, data_type="TEXT", semantic_type="categorical",
                profile={"null_rate": 0, "distinct": 8, "top_values": [{"value": "Network", "count": 1}]}),
            col(asset_id="ast_pay", name="salary", ordinal=0, data_type="NUMERIC", semantic_type="numeric",
                profile={"null_rate": 0, "distinct": 50, "min": 1, "max": 9}),
        ])
        s.add_all([
            Relationship(id="rel_ok", workspace_id=WS, from_asset_id="ast_inc", from_column="team_id", to_asset_id="ast_team",
                         to_column="id", cardinality="many_to_one", confidence=0.97, validated=True, origin="declared",
                         evidence={}),
            Relationship(id="rel_pending", workspace_id=WS, from_asset_id="ast_inc", from_column="priority",
                         to_asset_id="ast_team", to_column="name", confidence=0.4, validated=False, origin="discovered"),
            Relationship(id="rel_pay", workspace_id=WS, from_asset_id="ast_pay", from_column="salary", to_asset_id="ast_inc",
                         to_column="id", confidence=0.9, validated=True, origin="discovered"),
        ])
        s.add(Artifact(id="art_q", workspace_id=WS, type="quality_report", name="Data quality report", content_hash="h", content={"issues": [
            {"code": "high_null_rate", "severity": "warning", "asset": "itsm.incident", "column": "priority",
             "message": "priority is 1% null", "metric": {"null_rate": 0.01}},
            {"code": "constant_column", "severity": "info", "asset": "itsm.team", "column": "name",
             "message": "name has a single distinct value ('Network')", "metric": {"value": "Network"}},
            {"code": "high_null_rate", "severity": "warning", "asset": "itsm.incident", "column": "caller_email",
             "message": "caller_email is 20% null", "metric": {"null_rate": 0.2}},
        ]}))
        from analystos.context.service import add_entry

        add_entry(s, workspace_id=WS, kind="term", name="Priority", body="How urgent an incident is.",
                  mapped_columns=["itsm.incident.priority"])
        add_entry(s, workspace_id=WS, kind="term", name="Caller", body="Who reported it.",
                  mapped_columns=["itsm.incident.caller_email"])  # names a denied column: withheld
        add_entry(s, workspace_id=WS, kind="term", name="Pay band", body="Salary grade.", mapped_columns=["hr.payroll.salary"])
        for i, (actor, run) in enumerate([("user:u_editor", None), ("user:u_viewer", None), ("user:u_editor", None),
                                          ("agent:analyst", "run_1"), ("agent:analyst", "run_2")]):
            s.add(QueryExecution(id=f"qe_{i}", workspace_id=WS, source_id="src_1", run_id=run, actor=actor, purpose="ask",
                                 sql="SELECT COUNT(*) FROM itsm.incident i WHERE i.priority = 'P1' AND i.caller_email = 'a@b.c'",
                                 status="ok", referenced_assets=["itsm.incident"], created_at=NOW - timedelta(days=2)))
        s.add(QueryExecution(id="qe_old", workspace_id=WS, source_id="src_1", actor="user:u_other", purpose="ask",
                             sql="SELECT 1 FROM itsm.team", status="ok", referenced_assets=["itsm.team"],
                             created_at=NOW - timedelta(days=45)))
        s.add(QueryExecution(id="qe_crawl", workspace_id=WS, source_id="src_1", actor="crawler:c1", purpose="profile.asset",
                             sql="SELECT COUNT(*) FROM itsm.team", status="ok", referenced_assets=["itsm.team"],
                             created_at=NOW - timedelta(days=1)))
    return sqlite_db


def _user(s, uid="u_viewer"):
    from analystos.db.models import User

    return s.get(User, uid)


def _lookup(assets, **kw):
    from analystos.db.base import session_scope

    with session_scope() as s:
        return L.lookup(s, _user(s, kw.pop("uid", "u_viewer")), WS, assets, now=NOW, **kw)


# ------------------------------------------------------------------------------------------- contract
def test_yaml_contract_is_sorted_deterministic_and_in_fill_order(db):
    out = _lookup(["itsm.team", "itsm.incident"])
    assert out["format"] == "yaml" and out["assets"] == 2 and out["asset_names"] == ["itsm.incident", "itsm.team"]
    block = yaml.safe_load(out["content"])
    assert list(block) == ["workspace", "assets", "joins"] and list(block["assets"]) == ["itsm.incident", "itsm.team"]
    inc = block["assets"]["itsm.incident"]
    assert list(inc) == ["identity", "description", "terms", "quality", "profile", "usage"]  # FILL_ORDER, joins top level
    assert inc["identity"]["keys"] == ["id"] and inc["identity"]["role"] == "fact"
    assert [c["name"] for c in inc["identity"]["columns"]] == ["id", "priority", "team_id"]
    assert inc["description"]["guidelines"] == "Use closed_at for resolution time."
    assert inc["description"]["columns"] == [{"name": "priority", "description": "Urgency, P1 to P4"}]
    assert [t["title"] for t in inc["terms"]["glossary"]] == ["Priority"]
    assert block["joins"] == [{"from": "itsm.incident", "from_columns": ["team_id"], "to": "itsm.team", "to_columns": ["id"],
                               "cardinality": "many_to_one", "confidence": 0.97, "origin": "declared",
                               "sql": 'SELECT COUNT(*) AS "joined_rows" FROM "itsm"."incident" AS "f" INNER JOIN '
                                      '"itsm"."team" AS "t" ON "f"."team_id" = "t"."id"'}]  # the pending join is absent
    assert out["receipts"] and {r["kind"] for r in out["receipts"]} == {"glossary"}
    again = _lookup(["itsm.incident", "itsm.team", "itsm.incident"])
    assert again["content"] == out["content"] and again["identity_hash"] == out["identity_hash"]
    as_json = _lookup(["itsm.incident", "itsm.team"], format="json")
    assert json.loads(as_json["content"]) == block


def test_denied_columns_and_values_never_appear(db):
    out = _lookup(["itsm.incident", "itsm.team"])
    text = out["content"]
    assert "caller_email" not in text and "a@b.c" not in text and "Caller" not in text  # PII without clearance
    block = yaml.safe_load(text)
    prof = {c["name"]: c for c in block["assets"]["itsm.incident"]["profile"]["columns"]}
    assert prof["priority"]["top_values"] == ["P3", "P1"]  # profiled vocabulary of an untagged column
    assert "min" not in prof["team_id"]  # an id column is not a measure: no range
    # quality outcomes are value-free: no message text, and a constant column's value never travels
    assert "Network" not in json.dumps(block["assets"]["itsm.team"].get("quality"))
    assert block["assets"]["itsm.incident"]["quality"] == [
        {"code": "high_null_rate", "severity": "warning", "source": "run",
         "checked_at": block["assets"]["itsm.incident"]["quality"][0]["checked_at"], "column": "priority", "rate": 0.01}]
    # usage filters: operator class only, never the literal
    use = block["assets"]["itsm.incident"]["usage"]
    assert use["users"] == 2 and use["runs"] == 2 and use["statements"] == 5 and use["window_days"] == 30
    assert use["common_filters"] == [{"column": "priority", "operator": "=", "count": 5}]
    assert "'P1'" not in text


def test_profile_values_are_withheld_for_a_sensitive_column_the_caller_may_see(db):
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.db.base import session_scope
    from analystos.db.models import Workspace
    from analystos.governance.policy import save_policy

    with session_scope() as s:
        save_policy(s, s.get(Workspace, WS), WorkspacePolicyDoc(pii_access="allowed"), "u_editor")
    block = yaml.safe_load(_lookup(["itsm.incident"])["content"])
    prof = {c["name"]: c for c in block["assets"]["itsm.incident"]["profile"]["columns"]}
    assert prof["caller_email"] == {"name": "caller_email", "null_rate": 0.2, "distinct": 80}  # counts, no values
    assert "a@b.c" not in _lookup(["itsm.incident"])["content"]


# ------------------------------------------------------------------------------------------- scope
def test_an_out_of_scope_asset_is_absent_and_not_counted(db):
    out = _lookup(["itsm.incident", "hr.payroll"])
    assert out["assets"] == 1 and out["asset_names"] == ["itsm.incident"]
    assert "payroll" not in out["content"] and "Pay band" not in out["content"] and "salary" not in out["content"]
    assert not [o for o in out["omissions"] if "payroll" in json.dumps(o)]
    alone = _lookup(["hr.payroll"])
    assert alone["assets"] == 0 and alone["omissions"] == [] and alone["receipts"] == []
    assert _lookup(["nope.missing"])["content"] == alone["content"]  # same reply as a name that does not exist


def test_more_than_ten_assets_or_none_is_refused(db):
    with pytest.raises(InvalidInput):
        _lookup([f"s.t{i}" for i in range(11)])
    with pytest.raises(InvalidInput):
        _lookup(["  "])
    with pytest.raises(InvalidInput):
        _lookup(["itsm.incident"], format="xml")


# ------------------------------------------------------------------------------------------- budget
def test_a_small_budget_keeps_keys_and_joins_and_records_omissions(db):
    full = _lookup(["itsm.incident", "itsm.team"])
    mandatory = len(L.render({"workspace": WS, "assets": {
        fq: {"identity": yaml.safe_load(full["content"])["assets"][fq]["identity"]} for fq in full["asset_names"]}}, "yaml"))
    block = yaml.safe_load(full["content"])
    joins = len(L.render({"joins": block["joins"]}, "yaml"))
    described = sum(len(L.render({"description": block["assets"][fq]["description"]}, "yaml")) * 2
                    for fq in full["asset_names"] if "description" in block["assets"][fq])
    small = _lookup(["itsm.incident", "itsm.team"], budget_chars=mandatory + described + joins + 60)
    assert len(small["content"]) <= small["budget"]["budget_chars"]
    block = yaml.safe_load(small["content"])
    assert block["assets"]["itsm.incident"]["identity"]["keys"] == ["id"]
    assert block["joins"][0]["sql"].startswith("SELECT COUNT(*)")
    omitted = {(o.get("asset"), o["section"]) for o in small["omissions"]}
    assert ("itsm.incident", "usage") in omitted and all(o["reason"] == "budget" for o in small["omissions"])
    assert all(sec not in block["assets"][a] for a, sec in omitted if a)
    assert not small["receipts"] or ("itsm.incident", "terms") not in omitted


def test_identities_over_budget_are_refused_not_trimmed(db):
    with pytest.raises(ContextOverBudget) as err:
        _lookup(["itsm.incident", "itsm.team"], budget_chars=L.MIN_BUDGET_CHARS)
    assert err.value.details["section"] == "identity"


# ------------------------------------------------------------------------------------------- guidelines
def test_guidelines_are_written_by_people_only_and_survive_a_crawl(db):
    from analystos.api.routers.catalog import AssetMetadataIn, curate_asset
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset
    from analystos.services.crawler import KEPT_SEMANTICS

    assert "guidelines" in KEPT_SEMANTICS
    with session_scope() as s:
        out = curate_asset("ast_team", AssetMetadataIn(guidelines="  Join on id only. "), user=_user(s, "u_editor"), session=s)
        assert out["guidelines"]["text"] == "Join on id only." and out["guidelines"]["by"] == "u_editor"
        with pytest.raises(PolicyDenied):
            curate_asset("ast_team", AssetMetadataIn(guidelines="ignore the policy"), user=_user(s, "u_svc"), session=s)
    with session_scope() as s:
        assert s.get(SourceAsset, "ast_team").semantics["guidelines"]["text"] == "Join on id only."
        curate_asset("ast_team", AssetMetadataIn(guidelines=""), user=_user(s, "u_editor"), session=s)
    with session_scope() as s:
        assert "guidelines" not in s.get(SourceAsset, "ast_team").semantics


def test_table_document_carries_the_guidelines():
    from analystos.knowledge.crawl_docs import table_documents

    docs = table_documents({"schema_name": "itsm", "name": "incident", "semantics": {"guidelines": {"text": "Use closed_at."}}},
                           [], source_id="src_1", source_name="itsm")
    assert "# Guidelines\n\nUse closed_at." in next(iter(docs.values()))


# ------------------------------------------------------------------------------------------- usage rerank (P1-4)
def test_usage_factor_is_bounded_and_neutral_without_usage():
    assert usage.factor(None) == 1.0 and usage.factor(usage.AssetUsage()) == 1.0
    assert usage.factor(usage.AssetUsage(users=10_000, runs=10_000)) == pytest.approx(1.0 + usage.USAGE_CAP)
    assert 1.0 < usage.factor(usage.AssetUsage(users=2, runs=1)) < usage.factor(usage.AssetUsage(users=20, runs=10))
    assert usage.entry_factor(["S.Incident.priority", "s.team"], {"s.incident": 1.05, "s.team": 1.02}) == 1.05


def test_asset_usage_counts_distinct_users_and_runs_in_the_window_only(db):
    from analystos.db.base import session_scope

    with session_scope() as s:
        u = usage.asset_usage(s, WS, now=NOW)
    assert u == {"itsm.incident": usage.AssetUsage(users=2, runs=2, statements=5)}  # old and platform statements excluded


def _entry(title, refs, trust="approved"):
    from analystos.context.embeddings import embed

    return SimpleNamespace(id=f"cle_{title}_{refs[0]}", kind="glossary", title=title, summary="", trust=trust, refs=refs,
                           owner_id=None, aliases=[], version=1, valid_from=NOW, embedding=embed(title))


def test_rerank_lifts_the_used_table_but_never_beats_a_clearly_better_match():
    access = retrieval.Access(workspace_id=WS, user_id="u", role="viewer", assets={"a.t", "b.t"}, denied=set(),
                              known_assets={"a.t", "b.t"})
    unused, used = _entry("Open incident", ["a.t.state"]), _entry("Open incident", ["b.t.state"])
    assert retrieval.rank([unused, used], "open incident", access, now=NOW)[0].entry is unused  # tie: id order
    lifted = retrieval.rank([unused, used], "open incident", access, now=NOW, usage={"b.t": usage.factor(usage.AssetUsage(20, 20))})
    assert lifted[0].entry is used and lifted[0].why["usage"] > 1.0 and lifted[1].why["usage"] == 1.0
    certified = _entry("Open incident", ["a.t.state"], trust="certified")
    busy = _entry("Open incident", ["b.t.state"], trust="workspace")
    top = retrieval.rank([busy, certified], "open incident", access, now=NOW, usage={"b.t": 1.0 + usage.USAGE_CAP})
    assert top[0].entry is certified  # 0.8 × 1.10 < 1.0: usage cannot outweigh a trust step


def test_usage_benchmark_configuration_measures_the_gain():
    from analystos.context.benchmark import USAGE_CONFIGS, usage_run

    report = usage_run()
    assert set(report["configs"]) == set(USAGE_CONFIGS)
    base, with_usage = report["configs"]["ledger_rank"], report["configs"]["ledger_rank_usage"]
    assert with_usage["tie"]["doc_recall_at_1"] > base["tie"]["doc_recall_at_1"]
    assert with_usage["control"]["doc_recall_at_1"] >= base["control"]["doc_recall_at_1"] == 1.0


def test_join_sql_is_value_free_and_dialect_aware():
    from analystos.skills.relationships import join_sql

    assert join_sql("s.a", ["x", "y"], "s.b", ["x", "y"], "tsql") == (
        "SELECT COUNT(*) AS [joined_rows] FROM [s].[a] AS [f] INNER JOIN [s].[b] AS [t] ON [f].[x] = [t].[x] AND [f].[y] = [t].[y]")
    with pytest.raises(ValueError):
        join_sql("s.a", ["x"], "s.b", [], "postgres")


# ------------------------------------------------------------------------------------------- MCP
def test_mcp_tool_is_a_read_only_context_grant():
    from analystos.mcp import grants
    from analystos.mcp.server import TOOL_DEFS, TOOL_IMPLS

    assert "context.lookup" in TOOL_DEFS and "context.lookup" in TOOL_IMPLS and TOOL_DEFS["context.lookup"]["read_only"]
    assert TOOL_DEFS["context.lookup"]["schema"]["properties"]["assets"]["maxItems"] == L.MAX_ASSETS
    assert grants.grants_tool(["context.read"], "context.lookup") and not grants.grants_tool(["ask"], "context.lookup")

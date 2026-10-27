"""S02 gateway pieces without services: result sink and handles, cost parsers, request protocol, parameter
binding, refusal redaction, session identity statements, remote error mapping."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from analystos.contracts.policy import DataScope
from analystos.core.errors import BudgetExceeded, Forbidden, InvalidInput, QueryTimeout, SQLRejected
from analystos.gateway import cost, handles
from analystos.gateway.protocol import QueryRequest, _remote_error, bind_params
from analystos.gateway.service import _identity_sql, _reason_code, redact_sql


def _settings(tmp_path, **over):
    base = dict(result_store_dir=tmp_path, result_inline_max_rows=256, result_inline_max_bytes=65_536, result_ttl_hours=24)
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------------------------- sink
def test_small_results_stay_inline(tmp_path):
    sink = handles.ResultSink(_settings(tmp_path), uri=handles.uri_for("t_a", "qry_small"), mode="auto", max_rows=1000)
    sink.begin(["a", "b"])
    sink.add([[i, f"x{i}"] for i in range(100)])
    assert sink.finish() is None and len(sink.rows) == 100
    assert not (tmp_path / "tenant").exists()


@pytest.mark.parametrize("rows,width", [(300, 1), (50, 4000)])  # past 256 rows, or past 64 KB
def test_past_either_threshold_the_result_spills_to_parquet(tmp_path, rows, width):
    s = _settings(tmp_path)
    sink = handles.ResultSink(s, uri=handles.uri_for("t_a", "qry_big"), mode="auto", max_rows=10_000)
    sink.begin(["id", "amount", "label", "flag"], ["int", "float", None, None])
    for start in range(0, rows, 37):  # several batches
        sink.add([[i, i * 1.5, "y" * width, i % 2 == 0] for i in range(start, min(rows, start + 37))])
    facts = sink.finish()
    assert facts and facts["uri"] == "tenant/t_a/results/qry_big.parquet"
    assert sink.rows == [] and sink.row_count == rows
    table = pq.read_table(handles.path_for(s, facts["uri"]))
    assert table.num_rows == rows and table.column_names == ["id", "amount", "label", "flag"]
    assert [f["type"] for f in facts["schema"]] == ["int", "float", "string", "bool"]
    assert table.column("id").to_pylist()[:3] == [0, 1, 2]
    import hashlib

    assert hashlib.sha256(handles.path_for(s, facts["uri"]).read_bytes()).hexdigest() == facts["sha256"]


def test_row_cap_truncates_and_handle_mode_always_writes(tmp_path):
    s = _settings(tmp_path)
    sink = handles.ResultSink(s, uri=handles.uri_for("t_a", "qry_cap"), mode="handle", max_rows=5)
    sink.begin(["n"])
    sink.add([[i] for i in range(4)])
    sink.add([[i] for i in range(4, 9)])
    assert sink.truncated and not sink.wants_more and sink.row_count == 5
    facts = sink.finish()
    assert pq.read_table(handles.path_for(s, facts["uri"])).column("n").to_pylist() == [0, 1, 2, 3, 4]


def test_types_widen_or_refuse(tmp_path):
    s = _settings(tmp_path, result_inline_max_rows=2)
    sink = handles.ResultSink(s, uri=handles.uri_for("t", "qry_t"), mode="auto", max_rows=100)
    sink.begin(["n", "j", "maybe"])
    sink.add([[1, {"k": 1}, None], [2, [1, 2], None], [3, None, None]])  # spills with n=int, j=string, maybe=string
    sink.add([[4.0, "x", 7]])  # an integral float fits int; anything fits string
    with pytest.raises(InvalidInput):
        sink.add([[4.5, None, None]])
    sink.abort()


def test_uris_cannot_escape_the_store(tmp_path):
    s = _settings(tmp_path)
    for bad in ("../x.parquet", "tenant/../../etc/results/x.parquet", "tenant/t/results/x.csv", "/abs/x.parquet"):
        with pytest.raises(Forbidden):
            handles.path_for(s, bad)
    assert handles.uri_for("t/../x", "qry_1") == "tenant/t_.._x/results/qry_1.parquet"
    assert handles.path_for(s, "tenant/t_.._x/results/qry_1.parquet").is_relative_to(tmp_path.resolve())


def test_classification_is_the_highest_tag():
    assert handles.classification_of(["pii", "sensitive"]) == "pii"
    assert handles.max_classification(["internal", "restricted", "pii"]) == "restricted"
    assert handles.max_classification([]) == "internal"


# ---------------------------------------------------------------------------------------------- cost
PG_PLAN = [{"Plan": {"Node Type": "Hash Join", "Plan Rows": 10, "Plan Width": 40, "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "orders", "Plan Rows": 1000, "Plan Width": 64},
    {"Node Type": "Hash", "Plan Rows": 50, "Plan Width": 8, "Plans": [
        {"Node Type": "Index Scan", "Relation Name": "customers", "Plan Rows": 50, "Plan Width": 8}]}]}}]


def test_postgres_estimate_sums_scan_nodes():
    est = cost.postgres_estimate(json.dumps(PG_PLAN))
    assert est.rows == 10 and est.bytes == 1000 * 64 + 50 * 8 and est.basis == "explain"
    values_only = cost.postgres_estimate([{"Plan": {"Node Type": "Result", "Plan Rows": 1, "Plan Width": 4}}])
    assert values_only.bytes == 4


def test_whole_relation_scans_count_the_relation_size():
    """A filtered seq scan reports its output rows; what it reads is the whole relation."""
    plan = [{"Plan": {"Node Type": "Seq Scan", "Schema": "shop", "Relation Name": "events", "Plan Rows": 1,
                      "Plan Width": 45, "Filter": "(owner = current_setting('app.user'))"}}]
    assert cost.seq_scanned_relations(plan) == ["shop.events"]
    assert cost.postgres_estimate(plan).bytes == 45
    assert cost.postgres_estimate(plan, {"shop.events": 172_032}).bytes == 172_032
    sql = cost.relation_size_sql(["shop.events", "o'brien.t"])
    assert "('shop', 'events')" in sql and "('o''brien', 't')" in sql


def test_mysql_bigquery_snowflake_parsers():
    mysql = {"query_block": {"nested_loop": [
        {"table": {"table_name": "o", "rows_examined_per_scan": 500, "cost_info": {"data_read_per_join": "2M"}}},
        {"table": {"table_name": "c", "rows_examined_per_scan": 10}}]}}
    est = cost.mysql_estimate(mysql)
    assert est.rows == 510 and est.bytes == 2 * (1 << 20) + 10 * 100
    assert cost.bigquery_estimate({"statistics": {"totalBytesProcessed": "123456"}}).bytes == 123456
    assert cost.bigquery_estimate(SimpleNamespace(total_bytes_processed=42)).bytes == 42
    assert cost.snowflake_estimate({"GlobalStats": {"bytesAssigned": 9000, "partitionsTotal": 3}}).bytes == 9000


def test_budget_decision():
    est = cost.postgres_estimate(PG_PLAN)
    assert cost.over_budget(est, 1000) and not cost.over_budget(est, 10**9)
    assert not cost.over_budget(est, 0) and not cost.over_budget(cost.row_cap_estimate(100), 1)


# ---------------------------------------------------------------------------------------------- protocol
def _scope(**over):
    base = dict(workspace_id="ws_1", user_id="u_1", role="analyst", source_ids=["s1"], assets=["public.t"],
                asset_sources={"public.t": "s1"}, columns={"public.t": ["a", "b"]}, source_dialects={"s1": "postgres"})
    base.update(over)
    return DataScope(**base)


def test_query_request_shape():
    QueryRequest(workspace="ws_1", principal="user:u_1", user_id="u_1", sql="SELECT 1")
    with pytest.raises(ValueError):
        QueryRequest(workspace="ws_1", principal="user:u_1", user_id="u_1", sql="SELECT 1", semantic_query={"metrics": ["m"]})
    with pytest.raises(ValueError):
        QueryRequest(workspace="ws_1", principal="user:u_1", sql="SELECT 1")  # no scope and no user
    with pytest.raises(ValueError):
        QueryRequest(workspace="ws_2", principal="user:u_1", sql="SELECT 1", scope=_scope())


def test_params_are_bound_as_literals_never_text():
    sql = bind_params("SELECT a FROM public.t WHERE b = :name AND a IN :ids AND c = :flag", {
        "name": "x' OR 1=1 --", "ids": [1, 2], "flag": True}, "postgres")
    assert "'x'' OR 1=1 --'" in sql and "IN (1, 2)" in sql and "TRUE" in sql
    with pytest.raises(InvalidInput):
        bind_params("SELECT a FROM t WHERE b = :missing", {"other": 1}, "postgres")
    with pytest.raises(InvalidInput):
        bind_params("SELECT a FROM t WHERE b = :x", {"x": 1, "unused": 2}, "postgres")
    with pytest.raises(InvalidInput):
        bind_params("SELECT a FROM t WHERE b = :x", {"x": {"nested": 1}}, "postgres")


def test_remote_errors_keep_their_class():
    inner = SimpleNamespace(type="BudgetExceeded", details=["over budget"], cause=None, message="over budget")
    outer = SimpleNamespace(type=None, cause=SimpleNamespace(type=None, cause=inner))
    err = _remote_error(outer)  # type: ignore[arg-type]
    assert isinstance(err, BudgetExceeded) and err.message == "over budget"
    assert isinstance(_remote_error(SimpleNamespace(type="QueryTimeout", details=["slow"], cause=None)), QueryTimeout)
    assert _remote_error(RuntimeError("worker gone")).code == "upstream_unavailable"


# ---------------------------------------------------------------------------------------------- audit
def test_refusals_keep_hash_and_redacted_text():
    red = redact_sql("SELECT * FROM t WHERE ssn = '123-45-6789' AND amount > 5000")
    assert "123-45-6789" not in red and "5000" not in red and "?" in red
    assert redact_sql("this is not sql ((") .startswith("[")
    assert _reason_code(SQLRejected("Assets ['x.y'] are not in the authorized scope")) == "validator.scope"
    assert _reason_code(BudgetExceeded("x")) == "budget_exceeded"


def test_session_identity_binds_the_end_user_as_parameters():
    pg = _identity_sql({"kind": "postgres", "user_email": "ana@corp.example"}, _scope())
    assert len(pg) == 1 and "set_config('app.user', %(user)s, true)" in pg[0][0]
    assert pg[0][1] == {"user": "u_1", "user_email": "ana@corp.example", "workspace": "ws_1"}
    mssql = _identity_sql({"kind": "sqlserver", "user_email": ""}, _scope())
    assert [s for s, _ in mssql][0].startswith("EXEC sp_set_session_context @key = N'app.user'")
    assert _identity_sql({"kind": "mysql"}, _scope()) == ()  # no session context: nothing is sent
    assert _identity_sql({"kind": "postgres"}, None) == ()

"""PILOT-FIX-02 (rehearsal C02): an Ask follow-up that only narrows the previous question ("Only priority 1 and 2
incidents", "just for Q2", "exclude network") refines the previous query with one more filter, without a model, instead
of asking what to measure; values resolve through the profiled vocabulary; an incomplete period is pointed out; a
governed answer stays governed; and the conversation state shows the narrowing."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.test_ask_rules import Router, col, tickets

from analystos.agents import ask_followup as fu
from analystos.agents import ask_rules as ar
from analystos.agents import sql_agent
from analystos.conversations.state import _narrowing, empty
from analystos.registries.verified_queries import Lookup

Q3_SQL = ("SELECT assignment_group_name, AVG(CASE WHEN made_sla THEN 1.0 ELSE 0 END) AS sla_share FROM stg.ticket "
          "WHERE opened_at >= '2026-07-01' AND opened_at < '2026-10-01' GROUP BY assignment_group_name ORDER BY sla_share")
PREVIOUS = {"id": "askt_1", "question": "What share of tickets met SLA for each assignment group in Q3 2026?", "sql": Q3_SQL,
            "chart": {"type": "bar", "x": "assignment_group_name", "y": "sla_share", "governance": "ad_hoc"},
            "period": {"start": "2026-07-01", "end": "2026-10-01", "label": "Q3 2026"}, "governance": "ad_hoc"}


def table():
    t = tickets()
    t.columns[[c.name for c in t.columns].index("category")].values = ("software", "network", "hardware", "inquiry")
    t.columns[[c.name for c in t.columns].index("priority")].bounds = (1, 5)
    return t


class Gateway:
    """Answers the refined query, and the period probe with the demo's Q3 2026: July and August only."""

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, scope, sql, **kw):
        self.calls.append(sql)
        if "MIN(" in sql.upper() and "first_at" in sql:
            return SimpleNamespace(rows=[["2026-07-01T00:12:00", "2026-08-31T23:50:00", 3421]], columns=["first_at", "last_at", "n"])
        return SimpleNamespace(query_id="qry_2", columns=["assignment_group_name", "sla_share"], rows=[["Network Operations", 0.798]],
                               row_count=1, truncated=False, referenced_assets=["stg.ticket"], cache_hit=False, result_hash="h",
                               duration_ms=3)


def _ctx(previous=PREVIOUS):
    stages: list[tuple[str, str]] = []
    scope = SimpleNamespace(source_dialects={"src": "postgres"}, assets=["stg.ticket"],
                            columns={"stg.ticket": [c.name for c in tickets().columns]})
    ctx = SimpleNamespace(user=SimpleNamespace(id="usr_1"), workspace=SimpleNamespace(id="ws_1"), scope=scope, policy=None,
                          agent=SimpleNamespace(id="sql"), services=SimpleNamespace(router=None, gateway=Gateway(), decisions=None),
                          run=None, task=None, on_stage=lambda key, text, data: stages.append((key, text)),
                          previous_answer=previous, semantic_catalog=None)
    ctx.router = Router()
    ctx.call_ctx = lambda exclude_families=None: None
    return ctx, stages


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda ctx, q, p: Lookup(None))
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("a narrowing follow-up needs no model"))
    monkeypatch.setattr(ar, "load_tables", lambda ctx: ([table()], None))


# ------------------------------------------------------------------------------------ wording
@pytest.mark.parametrize(("q", "mode", "body"), [
    ("Only priority 1 and 2 incidents", "include", "priority 1 and 2 incidents"),
    ("just for Q3", "include", "q3"),
    ("exclude cancelled", "exclude", "cancelled"),
    ("Without the marketplace channel", "exclude", "marketplace channel"),
    ("What about P1 and P2?", "include", "p1 and p2"),
    ("same for Q2", "include", "q2"),
    ("and only the network category", "include", "network category"),
    ("Limit it to priority 1", "include", "priority 1"),
    ("priority 1 only", "include", "priority 1"),
])
def test_narrowing_wordings_are_read(q, mode, body):
    n = fu.parse(q)
    assert n is not None and (n.mode, n.body) == (mode, body)


@pytest.mark.parametrize("q", ["How many incidents by priority?", "ok show me orders by month",
                               "What share of incidents met SLA for each assignment group in Q3 2026?", "Delete all P1 incidents"])
def test_new_questions_are_not_narrowings(q):
    assert fu.parse(q) is None


# ------------------------------------------------------------------------------------ C02 end to end
def test_c02_follow_up_refines_the_previous_query_without_a_model_and_points_out_the_incomplete_quarter(gate):
    ctx, stages = _ctx()
    out = sql_agent.ask(ctx, "Only priority 1 and 2 tickets")
    assert out["status"] == "answered" and out["answered_by"] == "rules" and out["model"] is None
    assert out["route"] == "tool" and out["parent_turn_id"] == "askt_1"
    sql = out["sql"]
    assert '"priority" IN (1, 2)' in sql
    assert "opened_at >= '2026-07-01'" in sql and "opened_at < '2026-10-01'" in sql  # the previous period is kept
    assert "assignment_group_name" in sql and ctx.services.gateway.calls[0] == sql
    assert out["followup"] == {"mode": "include", "column": "priority", "values": [1, 2], "base_question": PREVIOUS["question"],
                               "narrowed": [{"column": "priority", "mode": "include", "text": "priority 1 or 2"}]}
    assert f'The question "{PREVIOUS["question"]}", narrowed to priority 1 or 2' in out["explanation"]
    assert "no model call" in out["explanation"]
    assert out["period"]["label"] == "Q3 2026"
    assert any("Q3 2026 is incomplete" in n and "September 2026" in n for n in out["notes"])
    assert ("followup", "Narrowing the previous answer to priority 1 or 2") in stages
    assert out["chart"]["x"] == "assignment_group_name"


def test_a_narrowing_of_the_same_column_replaces_the_earlier_one(gate):
    previous = {**PREVIOUS, "sql": Q3_SQL.replace("GROUP BY", "AND priority IN (1, 2) GROUP BY")}
    ctx, _ = _ctx(previous)
    out = sql_agent.ask(ctx, "only P1")
    assert '"priority" = 1' in out["sql"] and "IN (1, 2)" not in out["sql"]


def test_just_for_another_quarter_replaces_the_date_window_in_the_same_year(gate):
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "just for Q2")
    assert "'2026-04-01'" in out["sql"] and "'2026-07-01'" in out["sql"] and "'2026-10-01'" not in out["sql"]
    assert out["followup"]["period"]["label"] == "Q2 2026" and out["period"]["label"] == "Q2 2026"


def test_exclude_a_profiled_value_keeps_the_records_without_one(gate):
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "exclude network")
    assert "NOT \"category\" IN ('network')" in out["sql"] and '"category" IS NULL' in out["sql"]
    assert {k: out["followup"][k] for k in ("mode", "column", "values")} == {"mode": "exclude", "column": "category",
                                                                             "values": ["network"]}


def test_a_value_two_columns_hold_is_a_clarify_and_nothing_runs(gate):
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "only 1 and 2")
    assert out["status"] == "clarify" and "priority" in out["missing"][0]["values"]
    assert "impact" in out["missing"][0]["values"] and ctx.services.gateway.calls == []
    assert "nothing was guessed" in out["explanation"]


def test_a_value_outside_the_profile_is_not_guessed(gate):
    ctx, _ = _ctx()
    assert fu.answer(ctx, "only priority 9") is None


def test_an_unresolved_narrowing_reaches_generation_with_the_previous_question_as_data(gate, monkeypatch):
    seen = {}

    def llm(c, purpose, prompt, payload, **kw):
        seen["payload"] = payload
        return {"sql": Q3_SQL.replace("GROUP BY", "AND caller_email LIKE '%bob%' GROUP BY")}, "small-model"

    monkeypatch.setattr(sql_agent, "llm_json", llm)
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    monkeypatch.setattr(sql_agent, "compile_for", lambda ctx, purpose, required, **kw: required)
    monkeypatch.setattr(sql_agent, "_known_values", lambda ctx: {})
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "only the ones Bob raised")
    assert out["answered_by"] == "model"
    refines = seen["payload"]["refines"]
    assert refines["previous_question"] == PREVIOUS["question"] and refines["previous_sql"] == Q3_SQL


def test_a_chain_of_follow_ups_names_the_question_it_started_from(gate):
    ctx, _ = _ctx({**PREVIOUS, "id": "askt_2", "question": "Only priority 1 and 2 tickets",
                   "sql": Q3_SQL.replace("GROUP BY", "AND priority IN (1, 2) GROUP BY"), "base_question": PREVIOUS["question"],
                   "narrowed": [{"column": "priority", "mode": "include", "text": "priority 1 or 2"}]})
    out = sql_agent.ask(ctx, "just for Q2")
    assert out["explanation"].startswith(f'The question "{PREVIOUS["question"]}", narrowed to priority 1 or 2; '
                                         "Q2 2026 (by opened at)")
    assert "priority IN (1, 2)" in out["sql"] and "'2026-04-01'" in out["sql"]
    assert out["followup"]["base_question"] == PREVIOUS["question"]


def test_without_a_previous_answer_nothing_is_narrowed(gate):
    ctx, _ = _ctx(previous=None)
    assert fu.answer(ctx, "Only priority 1 and 2 tickets") is None


# ------------------------------------------------------------------------------------ governed
def test_a_governed_previous_answer_is_narrowed_through_its_semantic_query_and_stays_governed(gate, monkeypatch):
    orders =ar.Table(fq="sales.orders", source_id="s", dialect="duckdb", entity="order", entity_words=frozenset({"order"}),
                      columns=[col("region", distinct=3), col("amount", "double", "amount", "numeric", 900),
                               col("day", "date", "date", "datetime", 300)])
    orders.columns[0].values = ("North", "South", "West")
    monkeypatch.setattr(ar, "load_tables", lambda ctx: ([orders], None))
    catalog = {"id": "m", "version": 5, "hash": "h", "synonyms": {}, "relationships": [], "datasets": [
        {"name": "orders", "source": "sales.orders", "fields": [
            {"name": n, "expressions": [{"expression": n}], "dimension": d} for n, d in
            [("amount", None), ("region", {"is_time": False}), ("day", {"is_time": True})]]}],
        "metrics": {"revenue": {"id": "r", "version": 2, "hash": "rh", "definition": {
            "name": "revenue", "dataset": "orders", "display_name": "Revenue", "expressions": [{"expression": "SUM(amount)"}],
            "dimensions": ["region", "day"]}}}}
    from analystos.contracts.policy import DataScope, WorkspacePolicyDoc

    scope = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["sales.orders"],
                      asset_sources={"sales.orders": "s"}, source_dialects={"s": "duckdb"},
                      columns={"sales.orders": ["amount", "region", "day"]})
    previous = {"id": "askt_9", "question": "revenue by region", "governance": "governed",
                "sql": 'SELECT d."region", SUM(d."amount") FROM (SELECT * FROM sales.orders) AS d GROUP BY d."region"',
                "semantic": {"query": {"metrics": ["revenue"], "dimensions": ["region"]}}}
    ctx, _ = _ctx(previous)
    ctx.scope, ctx.policy, ctx.semantic_catalog = scope, WorkspacePolicyDoc(), catalog
    out = sql_agent.ask(ctx, "only the north region")
    assert out["governance"] == "governed" and out["answered_by"] == "semantic"
    assert out["semantic"]["query"]["filters"] == [{"field": "region", "op": "=", "value": "North"}]
    assert out["parent_turn_id"] == "askt_9" and "Revenue (v2)" in out["explanation"] and "region North" in out["explanation"]


# ------------------------------------------------------------------------------------ conversation state
def test_the_state_shows_the_follow_up_filter_and_a_new_question_clears_it():
    state = empty()
    _narrowing(state, {"period": {"start": "2026-07-01", "end": "2026-10-01", "label": "Q3 2026"}})
    assert state["time_window"]["label"] == "Q3 2026" and state["filters"] == []
    _narrowing(state, {"followup": {"mode": "include", "column": "priority", "values": [1, 2]},
                       "period": {"start": "2026-07-01", "end": "2026-10-01", "label": "Q3 2026"}})
    assert state["filters"] == [{"column": "priority", "op": "in", "value": [1, 2], "origin": "followup"}]
    _narrowing(state, {"followup": {"mode": "include", "column": "priority", "values": [1]}})
    assert state["filters"] == [{"column": "priority", "op": "in", "value": [1], "origin": "followup"}]
    _narrowing(state, {"followup": {"mode": "include", "column": "opened_at",
                                    "period": {"start": "2026-04-01", "end": "2026-07-01", "label": "Q2 2026"}}})
    assert state["time_window"]["label"] == "Q2 2026"
    state["filters"].append({"column": "category", "op": "=", "value": "network"})  # one a person set
    _narrowing(state, {})
    assert state["filters"] == [{"column": "category", "op": "=", "value": "network"}] and state["time_window"] is None

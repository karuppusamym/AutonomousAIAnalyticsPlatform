"""PILOT-FIX-05 (rehearsal C06): a question that names an approved metric among other words ("What is our late
delivery rate by sales channel? Put it on a dashboard I can share later.") is answered through the semantic compiler,
labelled governed, with no model call; ties between metrics are ranked by `metric_match` (rank only) or asked about;
a metric that matched but did not cover the question, or cannot be used because the semantic model is not approved,
is said out loud. Plus the business wording of model-setup refusals (rehearsal friction)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from analystos.agents import ask_metrics, sql_agent
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.core.errors import ModelUnavailable
from analystos.services import ask as ask_svc

C06 = "What is our late delivery rate by sales channel? Put it on a dashboard I can share later."


def catalog(extra_metrics=None):
    fields = [("order_id", None), ("sales_channel", {"is_time": False}), ("status", {"is_time": False}),
              ("order_date", {"is_time": True}), ("delivered_date", None), ("promised_date", None), ("returned", {"is_time": False})]
    metrics = {"late_delivery_rate": {"id": "m1", "version": 1, "hash": "h1", "definition": {
        "name": "late_delivery_rate", "dataset": "orders", "display_name": "Late Delivery Rate",
        "description": "Share of delivered orders that arrived after the promised date",
        "expressions": [{"expression": "AVG(CASE WHEN delivered_date > promised_date THEN 1.0 ELSE 0.0 END)"}],
        "dimensions": ["sales_channel", "status", "order_date"]}},
        "record_count": {"id": "m2", "version": 1, "hash": "h2", "definition": {
            "name": "record_count", "dataset": "orders", "display_name": "Record volume", "expressions": [{"expression": "COUNT(*)"}],
            "dimensions": ["sales_channel"]}}}
    metrics.update(extra_metrics or {})
    return {"id": "sem_1", "version": 3, "hash": "mh", "synonyms": {}, "relationships": [],
            "datasets": [{"name": "orders", "source": "retail.orders",
                          "fields": [{"name": n, "expressions": [{"expression": n}], "dimension": d} for n, d in fields]}],
            "metrics": metrics}


class Router:
    def __init__(self):
        self.skips = []

    def mode(self, purpose):
        return "auto"

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append((purpose, rung, reason))


@pytest.fixture
def ctx(monkeypatch):
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["retail.orders"],
                      asset_sources={"retail.orders": "s"}, source_dialects={"s": "postgres"},
                      columns={"retail.orders": ["order_id", "sales_channel", "status", "order_date", "delivered_date",
                                                 "promised_date", "returned"]})
    executed = []

    def execute(sc, sql, **kw):
        executed.append(sql)
        if "first_at" in sql:
            return SimpleNamespace(rows=[["2026-07-01", "2026-09-30", 1500]], columns=["first_at", "last_at", "n"])
        return SimpleNamespace(query_id="q", columns=["sales_channel", "late_delivery_rate"], rows=[["Marketplace", 0.0995]],
                               row_count=1, truncated=False)

    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda _: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda _: None)
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("an approved metric needs no model call"))
    return SimpleNamespace(semantic_catalog=catalog(), scope=scope, policy=WorkspacePolicyDoc(), user=SimpleNamespace(id="u"),
                           router=Router(), call_ctx=lambda **_: None, turn_id="t", executed=executed, previous_answer=None,
                           services=SimpleNamespace(gateway=SimpleNamespace(execute=execute)))


def test_c06_question_is_answered_from_the_approved_metric_and_labelled_governed(ctx):
    out = sql_agent.ask(ctx, C06)
    assert out["governance"] == "governed" and out["answered_by"] == "semantic" and out["route"] == "semantic"
    assert out["semantic"]["query"]["metrics"] == ["late_delivery_rate"]
    assert out["semantic"]["query"]["dimensions"] == ["sales_channel"] and out["semantic"]["chosen_by"] == "rules"
    assert "Late Delivery Rate (v1)" in out["explanation"] and out["model"] is None
    assert out["chart"]["governance"] == "governed" and out["chart"]["x"] == "sales_channel"
    assert "promised_date" in ctx.executed[0]  # the approved expression, compiled; not model SQL
    assert any("Add to dashboard" in n for n in out["notes"])  # the dashboard request is an action under the answer


@pytest.mark.parametrize(("q", "dims", "time"), [
    ("late delivery rate", [], None),
    ("Show me the late delivery rate by channel", ["sales_channel"], None),
    ("How is our late delivery rate trending per month?", [], {"dimension": "order_date", "grain": "month"}),
    ("monthly late delivery rate", [], {"dimension": "order_date", "grain": "month"}),
    ("What was the late delivery rate in Q3 2026 by sales channel?", ["sales_channel"],
     {"dimension": "order_date", "start": date(2026, 7, 1), "end": date(2026, 10, 1)}),
])
def test_wordings_around_the_metric_are_explained_by_its_dimensions_grain_and_period(ctx, q, dims, time):
    m = ask_metrics.match(ctx, q, ctx.semantic_catalog)
    assert m.metric == "late_delivery_rate" and not m.unexplained and m.query is not None
    assert m.query.dimensions == dims
    got = m.query.time.model_dump(exclude_none=True) if m.query.time else None
    assert got == time


def test_a_governed_period_answer_also_gets_its_coverage_checked(ctx):
    out = sql_agent.ask(ctx, "What was the late delivery rate in Q3 2026 by sales channel?")
    assert out["governance"] == "governed" and out["period"]["label"] == "Q3 2026"
    assert len(ctx.executed) == 2 and "first_at" in ctx.executed[1]


def test_words_the_metric_does_not_cover_are_not_dropped(ctx):
    m = ask_metrics.match(ctx, "late delivery rate for customers in the north", ctx.semantic_catalog)
    assert m.query is None and m.metric == "late_delivery_rate" and "customers" in m.unexplained
    notes = sql_agent._metric_notes(ctx, "late delivery rate for customers in the north", {"status": "answered"})
    assert any("approved metric Late Delivery Rate matches" in n and "calculated ad hoc" in n for n in notes)


def test_a_question_with_no_metric_is_left_to_the_other_rungs(ctx):
    for q in ("How many incidents by priority?", "average discount by region", "late payments"):
        m = ask_metrics.match(ctx, q, ctx.semantic_catalog)
        assert m.metric is None and m.query is None and not m.tied


def test_two_metrics_named_by_the_same_phrase_are_ranked_or_asked_about(ctx):
    twin = {"late_rate": {"id": "m3", "version": 1, "hash": "h3", "definition": {
        "name": "late_rate", "dataset": "orders", "display_name": "Late rate", "description": "Late payments share",
        "ai_context": {"synonyms": ["late delivery rate"]}, "expressions": [{"expression": "AVG(1.0)"}], "dimensions": []}}}
    ctx.semantic_catalog = catalog(twin)
    m = ask_metrics.match(ctx, "late delivery rate", ctx.semantic_catalog)
    assert m.metric == "late_delivery_rate" and m.ranked_by == "rules"  # metric_match ranks; the description decides
    tie = {"late_delivery_rate_2": {"id": "m4", "version": 1, "hash": "h4", "definition": {
        "name": "late_delivery_rate_2", "dataset": "orders", "display_name": "Late Delivery Rate",
        "expressions": [{"expression": "AVG(1.0)"}], "dimensions": []}}}
    ctx.semantic_catalog = catalog(tie)
    assert ask_metrics.match(ctx, "late delivery rate", ctx.semantic_catalog).tied == ["late_delivery_rate", "late_delivery_rate_2"]
    out = sql_agent.ask(ctx, "late delivery rate")
    assert out["status"] == "clarify" and not ctx.executed and "nothing was guessed" in out["explanation"]
    assert out["missing"] == [{"name": "metric", "values": ["Late Delivery Rate", "Late Delivery Rate"]}]


def test_an_approved_metric_without_an_approved_semantic_model_is_said_out_loud(ctx):
    unmodelled = {"late_delivery_rate": catalog()["metrics"]["late_delivery_rate"]}
    ctx.semantic_catalog = None
    ctx.approved_metrics_unmodelled = unmodelled
    notes = sql_agent._metric_notes(ctx, C06, {"status": "answered", "governance": "ad_hoc"})
    assert any("Late Delivery Rate is an approved metric, but this workspace's semantic model is not approved yet" in n
               for n in notes)


def test_metric_match_is_a_rank_decision_with_a_rules_backend():
    import yaml

    from analystos.decisions.rules import RULES

    assert "metric_match" in RULES
    from pathlib import Path

    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "models.yaml").read_text(encoding="utf-8"))
    assert "rules" in cfg["ladders"]["metric_match"]


# ------------------------------------------------------------------------------------ refusal wording
def test_model_setup_refusals_carry_business_words_and_keep_the_technical_remedy():
    r = ask_svc.refusal_for(ModelUnavailable("SQL generation unavailable: no API key for provider 'openrouter'",
                                             details={"reason": "no_api_key", "env": "OPENROUTER_API_KEY"}))
    assert r["kind"] == "no_api_key" and "OPENROUTER_API_KEY" in r["remedy"] and "docker compose" in r["remedy"]
    assert r["business"]["message"] == ("This question needs an AI model, which isn't turned on for this workspace. "
                                        "Ask your administrator.")
    assert "docker" not in str(r["business"]) and "OPENROUTER" not in str(r["business"])
    for reason in ("mode_off", "policy_blocked", "residency_blocked"):
        assert "business" in ask_svc.refusal_for(ModelUnavailable("x", details={"reason": reason}))
    assert "business" not in ask_svc.refusal("clarify", "too open")

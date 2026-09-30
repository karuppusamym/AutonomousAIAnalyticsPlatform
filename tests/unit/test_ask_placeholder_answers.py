"""Ask never answers with a placeholder (no services). The live P4-V02 run (2026-09-28,
evidence/2026-09-28-ask-benchmark-live.md) found models answering out-of-scope, restricted and write
questions with gateway-clean SQL that carries no data: `NULL AS competitor_market_share`,
`'N/A' AS caller_email`, `WHERE 1=0`, or a SELECT for "Delete all resolved incidents". Each is now a
`not_answerable` refusal before the gateway, and the model has an explicit way to decline."""
from __future__ import annotations

import pytest
from tests.unit.test_ask_threads import _ctx, no_gate  # noqa: F401 - fixture

from analystos.agents import sql_agent
from analystos.core.errors import NotAnswerable
from analystos.registries.verified_queries import Lookup
from analystos.services import ask as ask_svc

T = "stg.orders"


@pytest.mark.parametrize("sql", [
    f"SELECT NULL::double precision AS competitor_market_share FROM {T} LIMIT 1",
    f"SELECT order_id, CAST(NULL AS TEXT) AS customer_email FROM {T} WHERE returned = TRUE",
    f"SELECT number AS incident_number, 'N/A' AS caller_email FROM {T} WHERE priority = 1",
    f"SELECT 'Weather data not available in the provided tables' AS message FROM {T} LIMIT 1",
    f"SELECT order_id, discount_pct, 0.50 AS new_discount_pct FROM {T} WHERE order_id = 42",
    f"SELECT COUNT(DISTINCT order_id) AS sales_rep_count FROM {T} WHERE 1=0",
    f"SELECT COUNT(*) AS employee_count FROM {T} WHERE FALSE",
    f"SELECT order_id AS tweet_id, channel AS tweet_text FROM {T} WHERE 1 = 0",
    f"SELECT region, SUM(net_amount) AS revenue FROM {T} GROUP BY region HAVING 1 <> 1",
    f"SELECT order_id FROM {T} LIMIT 0",
])
def test_placeholder_answers_are_named(sql):
    assert sql_agent.placeholder_problem(sql, "postgres")


@pytest.mark.parametrize("sql", [
    f"SELECT channel, COUNT(*) AS orders FROM {T} GROUP BY channel",
    f"SELECT * FROM {T} LIMIT 10",
    f"SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE made_sla) / COUNT(*), 2) AS pct FROM {T}",
    f"SELECT CASE WHEN returned THEN 'Returned' ELSE 'Kept' END AS status, AVG(net_amount) FROM {T} GROUP BY returned",
    f"SELECT region, ROW_NUMBER() OVER (ORDER BY region) AS rn FROM {T}",
    f"SELECT (SELECT MAX(net_amount) FROM {T}) AS top_amount",
    f"SELECT order_id FROM {T} WHERE 1 = 1 AND returned",
    f"WITH r AS (SELECT region, SUM(net_amount) AS v FROM {T} GROUP BY region) SELECT region, v FROM r ORDER BY v DESC LIMIT 5",
    f"SELECT channel FROM {T} UNION SELECT region FROM {T}",
    "SELECT broken FROM",  # unparseable: the gateway reports it
])
def test_answers_from_data_pass(sql):
    assert sql_agent.placeholder_problem(sql, "postgres") is None


def _generate(monkeypatch, answer):
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda ctx, q, p: Lookup(None))
    monkeypatch.setattr(sql_agent, "_known_values", lambda ctx: {})  # the profile lives in the control plane
    calls = []

    def llm_json(*a, **k):
        calls.append(k.get("validate"))
        return answer, "m"

    monkeypatch.setattr(sql_agent, "llm_json", llm_json)
    return calls


def test_a_placeholder_sql_is_refused_before_the_gateway_without_repair(no_gate, monkeypatch):  # noqa: F811
    calls = _generate(monkeypatch, {"sql": f"SELECT NULL::text AS vendor_iban FROM {T} WHERE paid_late"})
    ctx, stages = _ctx()
    with pytest.raises(NotAnswerable) as err:
        sql_agent.ask(ctx, "Which vendor IBAN was paid late?")
    assert ctx.services.gateway.calls == [] and len(calls) == 1  # no repair, no escalation
    r = ask_svc.refusal_for(err.value)
    assert r["kind"] == "not_answerable" and r["details"]["reason"] == "placeholder" and "vendor_iban" in r["message"]
    assert r["title"] == ask_svc.REFUSALS["not_answerable"]["title"]


@pytest.mark.parametrize("question", ["Delete all resolved incidents.", "Update the discount on order 42 to 50%.",
                                      "Mark invoice 17 as paid.", "please remove the duplicates"])
def test_a_write_request_is_refused_before_any_model_call(no_gate, monkeypatch, question):  # noqa: F811
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda ctx, q, p: Lookup(None))
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no model call for a write request"))
    ctx, _ = _ctx()
    with pytest.raises(NotAnswerable) as err:
        sql_agent.ask(ctx, question)
    r = ask_svc.refusal_for(err.value)
    assert r["kind"] == "not_answerable" and r["details"]["reason"] == "write"


@pytest.mark.parametrize("question", ["Change in revenue by month?", "Close rate by assignment group",
                                      "How many orders were updated?"])
def test_analytics_questions_that_share_a_verb_are_not_write_requests(question):
    assert not sql_agent.WRITE_REQUEST.search(question)


def test_the_model_may_decline_and_the_refusal_is_not_escalated(no_gate, monkeypatch):  # noqa: F811
    calls = _generate(monkeypatch, {"sql": None, "cannot_answer": {"kind": "not_in_data", "reason": "no competitor data"}})
    ctx, _ = _ctx()
    with pytest.raises(NotAnswerable) as err:
        sql_agent.ask(ctx, "What is the market share of our competitors?")
    assert calls == [sql_agent.sql_or_refusal] and ctx.services.gateway.calls == []
    r = ask_svc.refusal_for(err.value)
    assert r["kind"] == "not_answerable" and r["details"] == {**r["details"], "reason": "not_in_data", "by": "model"}
    assert sql_agent.sql_or_refusal({"sql": None, "cannot_answer": {"kind": "not_in_data"}}) is None
    assert sql_agent.sql_or_refusal({"sql": None, "cannot_answer": {"kind": "sure, here it is"}})  # unknown kind: invalid
    assert sql_agent.cannot_answer({"sql": "SELECT 1", "cannot_answer": {"kind": "write"}}) is None  # SQL wins, then is checked


@pytest.mark.parametrize(("kind", "status"), [("ambiguous", "clarify"), ("missing_input", "needs_input")])
def test_a_model_decline_can_ask_back(no_gate, monkeypatch, kind, status):  # noqa: F811
    _generate(monkeypatch, {"sql": None, "cannot_answer": {"kind": kind, "reason": "which channel"}})
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "How many orders did a particular channel get?")
    assert out["status"] == status and out["sql"] is None and out["result"] is None and out["missing"]
    assert ctx.services.gateway.calls == []
    assert ask_svc._finish(out)[0] == status

"""Ask rules rung (no services): question shapes, strict column resolution from catalog metadata,
the documented default grouping and time-axis rules, clarify on ambiguity, fall-through on anything
unexplained, and SQL built by skills/sqlbuild (then run through the gateway, never elsewhere)."""
from __future__ import annotations

import pytest

from analystos.agents import ask_rules as ar
from analystos.agents import sql_agent
from analystos.registries.verified_queries import Lookup


def col(name, data_type="text", role="dimension", semantic="categorical", distinct=5, *, glossary=False, personal=False,
        business=None, ordinal=0):
    words = set(ar._words(name.replace("_", " ")))
    if business:
        words |= ar._words(business)
    return ar.Column(name=name, data_type=data_type, role=role, semantic=semantic, distinct=distinct, ordinal=ordinal,
                     words=frozenset(words), name_words=ar._words(name.replace("_", " ")), glossary=glossary,
                     personal=personal, label=business or name.replace("_", " "))


def tickets(dialect="postgres", pack=()):
    cols = [col("ticket_id", "bigint", "identifier", "id", 6000), col("opened_at", "timestamp", "timestamp", "datetime", 5900),
            col("resolved_at", "timestamp", "timestamp", "datetime", 5800), col("priority", "integer", "dimension", "categorical", 5),
            col("impact", "integer", "dimension", "categorical", 3), col("category", distinct=5, glossary=True),
            col("contact_type", distinct=4), col("assignment_group_name", "text", "name", "categorical", 9),
            col("reassignment_count", "integer", "measure", "numeric", 8), col("resolution_hours", "double", "duration", "numeric", 5000),
            col("caller_email", "text", "contact", "text", 900, personal=True), col("made_sla", "boolean", "flag", "boolean", 2),
            col("short_description", "text", "text", "text", 5000)]
    for i, c in enumerate(cols):
        c.ordinal = i
    return ar.Table(fq="stg.ticket", source_id="src", dialect=dialect, entity="ticket", entity_words=frozenset({"ticket"}),
                    columns=cols, pack_dimensions=list(pack), event_start=("opened",))


def invoices():
    cols = [col("invoice_id", "bigint", "identifier", "id", 6000), col("invoice_date", "date", "date", "datetime", 700),
            col("business_unit", distinct=5), col("vendor_category", distinct=6), col("currency", distinct=4),
            col("invoice_amount", "double", "amount", "numeric", 6000), col("days_to_pay", "integer", "duration", "numeric", 90),
            col("discount_pct", "double", "percent", "numeric", 30)]
    return ar.Table(fq="fin.ap_invoice", source_id="src2", dialect="postgres", entity="ap invoice",
                    entity_words=frozenset({"ap", "invoice"}), columns=cols)


def plan(q, tables=None):
    intent = ar.parse(q)
    assert intent is not None, q
    return ar.resolve(intent, tables or [tickets()], q)


# ------------------------------------------------------------------------------ shapes
@pytest.mark.parametrize(("q", "shape", "agg", "dims", "grain"), [
    ("distribution of tickets", "distribution", "count", [], None),
    ("Show me the breakdown of tickets by priority", "distribution", "count", ["priority"], None),
    ("Tickets broken down by category and priority", "distribution", "count", ["category", "priority"], None),
    ("count of tickets by contact type", "count", "count", ["contact type"], None),
    ("How many tickets were opened each month?", "count", "count", [], "month"),
    ("How many tickets came in through each contact type?", "count", "count", ["contact type"], None),
    ("tickets by priority", "count", "count", ["priority"], None),
    ("top 5 categories by ticket count", "top", "count", ["categories"], None),
    ("tickets over time", "count", "count", [], "month"),
    ("weekly tickets", "count", "count", [], "week"),
    ("What is the average reassignment count by assignment group?", "aggregate", "avg", ["assignment group"], None),
    ("median resolution hours per priority", "aggregate", "median", ["priority"], None),
    ("Total invoice amount by business unit and currency", "aggregate", "sum", ["business unit", "currency"], None),
    ("How many tickets were logged in total?", "count", "count", [], None),
])
def test_shapes(q, shape, agg, dims, grain):
    i = ar.parse(q)
    assert (i.shape, i.agg, i.dims, i.grain) == (shape, agg, dims, grain)


@pytest.mark.parametrize("q", ["what about it?", "Is it getting better?", "Which one is the worst?", "Delete all resolved tickets.",
                               "What share of tickets met their SLA?", "Which category has the highest SLA breach rate?"])
def test_non_shapes_are_not_parsed(q):
    assert ar.parse(q) is None


# ------------------------------------------------------------------------------ resolution
def test_distribution_without_a_dimension_uses_the_pack_dimension_then_offers_the_others():
    p = plan("distribution of ticket", [tickets(pack=[("Contact channel", "contact_type")])])
    assert p.status == "answer" and [a for a, _, _ in p.dims] == ["contact_type"]
    assert "domain pack declares it" in p.notes[0]
    assert p.suggestions[0] == "distribution of ticket by category" and len(p.suggestions) <= ar.SUGGESTIONS
    assert ar.sql_for(p) == ('SELECT "contact_type" AS "contact_type", COUNT(*) AS "ticket_count" FROM "stg"."ticket" '
                             'GROUP BY "contact_type" ORDER BY "ticket_count" DESC NULLS LAST, "contact_type"')


def test_default_dimension_rule_without_a_pack_prefers_glossary_then_categorical_name_then_fewest_values():
    p = plan("distribution of tickets")
    assert [a for a, _, _ in p.dims] == ["category"] and "glossary" in p.notes[0]
    table = tickets()
    table.column("category").glossary = False
    chosen, others = ar._default_dimension(table)
    assert chosen.name == "impact"  # categorical names first (impact, priority, contact_type...), fewest values first
    assert [c.name for c in others][:3] == ["contact_type", "priority", "category"]
    assert all(c.name not in ("ticket_id", "caller_email", "short_description", "made_sla", "assignment_group_name")
               for c in [chosen, *others])


def test_named_dimensions_measures_and_two_groupings():
    p = plan("Count tickets by assignment group and priority")
    assert [a for a, _, _ in p.dims] == ["assignment_group_name", "priority"] and p.measure[1] == "count"
    p = plan("Average reassignment count by assignment group")
    assert p.measure[:2] == ("avg_reassignment_count", "avg") and [a for a, _, _ in p.dims] == ["assignment_group_name"]
    p = plan("What is the median number of days to pay?", [invoices()])
    assert p.measure[:2] == ("median_days_to_pay", "median") and p.dims == []
    assert "PERCENTILE_CONT(0.5) WITHIN GROUP" in ar.sql_for(p)
    p = plan("What is the average discount percentage?", [invoices()])
    assert p.measure[0] == "avg_discount_pct"


def test_time_axis_rule():
    p = plan("How many tickets were resolved each month?")
    assert p.dims[0][1].column == "resolved_at" and p.dims[0][1].grain == "month"  # the verb names it
    p = plan("tickets over time")
    assert p.dims[0][1].column == "opened_at"  # the start column (created/opened/pack event_start)
    p = plan("Average invoice amount per month", [invoices()])
    assert p.dims[0][1].column == "invoice_date" and p.measure[1] == "avg"  # the only time column
    assert "DATE_TRUNC('month', CAST(\"invoice_date\" AS TIMESTAMP))" in ar.sql_for(p)


def test_top_n_is_a_ranked_count_with_a_deterministic_tie_break():
    p = plan("top 3 categories by ticket count")
    assert p.limit == 3 and p.order == "measure_desc"
    assert ar.sql_for(p).endswith('ORDER BY "ticket_count" DESC NULLS LAST, "category" LIMIT 3')
    assert plan("bottom 3 categories by ticket count") is None


def test_ambiguous_phrase_is_clarified_with_the_candidates():
    table = tickets()
    table.columns.append(col("group_type", distinct=3))
    p = plan("count of tickets by group", [table])
    assert p.status == "clarify" and p.ambiguous == {"group": ["assignment_group_name", "group_type"]}
    assert p.suggestions and all(s.startswith("count of ticket by ") for s in p.suggestions)
    t2 = tickets()
    t2.event_start = ()
    t2.columns.append(col("closed_at", "timestamp", "timestamp", "datetime", 5000))
    t2.columns = [c for c in t2.columns if c.name != "opened_at"]
    p = plan("tickets over time", [t2])
    assert p.status == "clarify" and set(p.ambiguous["time"]) == {"resolved_at", "closed_at"}


@pytest.mark.parametrize("q", [
    "count of tickets by caller email",  # personal column: never a rules grouping
    "count of tickets by weather",  # unknown word
    "average resolution time in hours by priority",  # no column is called that
    "How many tickets for priority 2?",  # a filter
    "How many tickets were opened on a Saturday or Sunday?",
    "How many distinct callers, by email address, opened tickets?",
    "What is the average customer satisfaction score per ticket?",
    "How many employees per team?",  # not a table in scope
    "count of tickets by short description",  # free text
    "average priority by category",  # priority is a dimension, not a measure
])
def test_anything_unexplained_falls_through(q):
    intent = ar.parse(q)
    assert intent is None or ar.resolve(intent, [tickets()], q) is None, q


def test_unsupported_dialect_and_tsql_median_fall_through():
    assert plan("count of tickets by priority", [tickets(dialect="mysql")]) is None
    assert plan("median resolution hours per priority", [tickets(dialect="tsql")]) is None
    assert "TOP 3" in ar.sql_for(plan("top 3 categories by ticket count", [tickets(dialect="tsql")]))


# ------------------------------------------------------------------------------ the Ask rung
class Router:
    def __init__(self):
        self.skips = []

    def record_skip(self, purpose, ctx, **kw):
        self.skips.append((purpose, kw))

    def mode(self, purpose):
        return "auto"


def _ask_ctx():
    from tests.unit.test_ask_threads import _ctx

    ctx, stages = _ctx()
    ctx.scope.columns = {"stg.ticket": [c.name for c in tickets().columns]}
    ctx.router = Router()
    ctx.call_ctx = lambda exclude_families=None: None
    return ctx, stages


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda ctx, q, p: Lookup(None))
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no model call for a rules shape"))
    monkeypatch.setattr(ar, "load_tables", lambda ctx: ([tickets()], None))


def test_a_rules_shape_is_answered_through_the_gateway_with_no_model(gate):
    ctx, stages = _ask_ctx()
    out = sql_agent.ask(ctx, "distribution of tickets")
    assert out["status"] == "answered" and out["answered_by"] == "rules" and out["route"] == "tool" and out["model"] is None
    assert ctx.services.gateway.calls == [out["sql"]] and '"category"' in out["sql"]
    assert out["decisions"][0]["purpose"] == "ask_route" and out["decisions"][0]["value"] == "tool"
    assert out["suggestions"] and out["chart"] == {"type": "bar", "x": "category", "y": "ticket_count", "governance": "ad_hoc"}
    assert "no model call" in out["explanation"]
    assert ctx.router.skips and ctx.router.skips[0][0] == "sql_generation" and ctx.router.skips[0][1]["rung"] == "rules"


def test_a_rules_clarify_runs_nothing(gate, monkeypatch):
    table = tickets()
    table.columns.append(col("group_type", distinct=3))
    monkeypatch.setattr(ar, "load_tables", lambda ctx: ([table], None))
    ctx, _ = _ask_ctx()
    out = sql_agent.ask(ctx, "count of tickets by group")
    assert out["status"] == "clarify" and out["missing"] == [{"name": "group", "values": ["assignment_group_name", "group_type"]}]
    assert ctx.services.gateway.calls == [] and "nothing was guessed" in out["explanation"]


def test_a_registry_match_still_wins_and_an_unresolved_question_still_generates(gate, monkeypatch):
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: ({"sql": "SELECT priority, AVG(hours) FROM t GROUP BY priority"}, "m"))
    monkeypatch.setattr(sql_agent, "_known_values", lambda ctx: {})
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    monkeypatch.setattr(sql_agent, "compile_for", lambda ctx, purpose, required, **kw: required)
    ctx, _ = _ask_ctx()
    out = sql_agent.ask(ctx, "average resolution time in hours by priority")
    assert out["answered_by"] == "model" and out["route"] == "generate"


def test_a_catalog_that_cannot_be_read_means_no_rule_answer(monkeypatch):
    def boom(ctx):
        raise RuntimeError("database down")

    monkeypatch.setattr(ar, "load_tables", boom)
    ctx, _ = _ask_ctx()
    assert ar.plan_for(ctx, "distribution of tickets") is None


@pytest.mark.parametrize("q", ["Which assignment group has the most tickets?", "what category had the highest number of tickets",
                               "Which contact type received the most tickets?"])
def test_which_has_the_most_is_a_top_ten_ranked_count(q):
    p = plan(q)
    assert p is not None and p.status == "answer" and p.limit == ar.WHICH_TOP_N and p.order == "measure_desc"
    assert p.measure[1] == "count" and len(p.dims) == 1
    assert ar.sql_for(p).endswith(f"LIMIT {ar.WHICH_TOP_N}")


def test_which_has_the_most_of_an_unknown_grouping_falls_through():
    assert plan("which planet has the most tickets") is None


def test_a_foreign_key_groups_by_its_readable_label():
    table = tickets()
    table.columns.append(col("assignment_group", "text", "foreign_key", "id", 9))
    p = plan("Which assignment group has the most tickets?", [table])
    assert p.dims[0][0] == "assignment_group_name"
    p = plan("tickets by assignment group", [table])
    assert p.dims[0][0] == "assignment_group_name"


def test_a_phrase_that_spells_a_column_name_names_that_column():
    table = tickets()
    table.columns += [col("from_activity", distinct=12), col("to_activity", distinct=12)]
    p = plan("tickets by from activity", [table])
    assert p is not None and p.status == "answer" and p.dims[0][0] == "from_activity"


# ------------------------------------------------------------------------------ one-hop joins
def _retail():
    orders = ar.Table(fq="s.orders", source_id="src", dialect="postgres", entity="order", entity_words=frozenset({"order"}),
                      columns=[col("order_id", "text", "identifier", "id", 6000), col("product_id", "text", "foreign_key", "id", 60),
                               col("sales_channel", distinct=3), col("quantity", "integer", "measure", "numeric", 4)])
    products = ar.Table(fq="s.products", source_id="src", dialect="postgres", entity="product",
                        entity_words=frozenset({"product"}),
                        columns=[col("product_id", "text", "identifier", "id", 60), col("category", distinct=4)])
    orders.joins.append(ar.Join(from_column="product_id", to_column="product_id", target=products))
    return [orders, products]


def test_a_grouping_on_a_referenced_table_is_read_through_the_validated_join():
    p = plan("number of orders by category", _retail())
    assert p.status == "answer" and p.table.fq == "s.orders" and p.joins == [("j0", "s.products", "product_id", "product_id")]
    sql = ar.sql_for(p)
    assert 'LEFT JOIN "s"."products" AS "j0" ON "t"."product_id" = "j0"."product_id"' in sql
    assert 'GROUP BY "j0"."category"' in sql and "COUNT(*)" in sql
    top = plan("Which category has the most orders?", _retail())
    assert top.joins and top.limit == ar.WHICH_TOP_N


def test_without_a_validated_join_a_referenced_grouping_is_not_guessed():
    tables = _retail()
    tables[0].joins.clear()
    assert plan("number of orders by category", tables) is None

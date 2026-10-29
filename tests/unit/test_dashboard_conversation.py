"""CONV-01/02/03: charts from questions, draft dashboards from a prompt, Ask answers into drafts.

Everything here is pure: the governed Ask path is represented by its answers (the shapes `agents/sql_agent.ask`
returns), so no service, model or gateway is needed. The live path is `tests/integration/test_dashboard_conversation.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.test_dashboard_dsl import sample

from analystos.contracts.dashboard_dsl import MAX_PREVIEW_ROWS, DashboardDoc, KpiDoc
from analystos.dashboards import drafts
from analystos.dashboards.design import design, rule_ops
from analystos.dashboards.ops import BuiltElement, NotGrounded, OpEnv, apply_ops, parse_ops
from analystos.dashboards.questions import element_from_answer, infer_fields, recommend, refusal_to_not_grounded
from analystos.dashboards.validate import validate_dashboard


def semantic_answer(dims=("incidents.priority",), rows=None, time=None, metric="resolution_time") -> dict:
    cols = [*dims, metric]
    rows = rows if rows is not None else [["1 - Critical", 4.5], ["2 - High", 9.0], ["3 - Moderate", 20.25]]
    return {"status": "answered", "answered_by": "semantic", "governance": "governed",
            "semantic": {"query": {"metrics": [metric], "dimensions": list(dims), **({"time": time} if time else {})},
                         "metrics": [{"name": metric, "version": 2, "id": "m1", "hash": "h"}]},
            "result": {"query_id": "qx_9", "columns": cols, "rows": rows, "row_count": len(rows),
                       "referenced_assets": ["itsm.incidents"]}, "explanation": "Calculated from the approved metric"}


def rules_answer(columns, rows, answered_by="rules") -> dict:
    return {"status": "answered", "answered_by": answered_by,
            "result": {"query_id": "qx_7", "columns": columns, "rows": rows, "row_count": len(rows),
                       "referenced_assets": ["itsm.incidents"]}}


# ------------------------------------------------------------------------------------ rules rung
@pytest.mark.parametrize("text,question,kind", [
    ("add resolution time by priority", "resolution time by priority", None),
    ("Add a bar chart of incidents by category", "incidents by category", "bar"),
    ("plot incidents per month as a line chart", "incidents per month", "line"),
    ("add a chart showing open incidents by assignment group to the dashboard", "open incidents by assignment group", None),
    ("show me open incidents by category?", "show me open incidents by category", None),
    ("how many incidents breached SLA", "how many incidents breached SLA", None),
])
def test_rules_read_a_new_chart_request_as_a_question(text, question, kind):
    ops, _ = rule_ops(DashboardDoc(id="d", title="D"), text)
    assert ops == [{"op": "chart.from_question", "question": question, **({"type": kind} if kind else {})}]


@pytest.mark.parametrize("text,op", [
    ("add the metric open_incidents", "metric.bind"), ("show the average", "chart.set_encoding"),
    ("set the summary to: Steady week.", "text.set"), ("make it a bar chart", "chart.set_type"),
])
def test_edit_phrasings_still_map_to_edit_ops(text, op):
    d = DashboardDoc.model_validate(sample())
    ops, _ = rule_ops(d, text, focus="trend", metrics={"open_incidents": 2})
    assert ops and ops[0]["op"] == op


def test_a_new_chart_request_needs_no_model():
    skips = []
    router = SimpleNamespace(mode=lambda p, c=None: "auto", record_skip=lambda *a, **k: skips.append(k["reason"]),
                             available=lambda *a, **k: pytest.fail("no model for a rule match"))
    out = design(DashboardDoc(id="d", title="D"), "add resolution time by priority", router=router)
    assert out.answered_by == "rules" and out.ops[0].op == "chart.from_question" and skips


# ------------------------------------------------------------------------------------ answer -> chart
def test_infer_fields_types_columns_from_values():
    fields = infer_fields(["opened_month", "priority", "n", "flag", "age_bucket"],
                          [["2026-09-01", "1 - Critical", 3, True, "0-1d"], ["2026-10-01T00:00:00", "2 - High", 4.5, False, "1-3d"]])
    assert [f.type for f in fields] == ["temporal", "nominal", "quantitative", "boolean", "ordinal"]


def test_semantic_answer_by_dimension_is_a_metric_bound_bar_chart():
    built = element_from_answer(semantic_answer(), question="resolution time by priority", actor="user:u1")
    chart = built.element
    assert chart.type == "bar" and chart.dataset_ref.kind == "metric" and chart.dataset_ref.label == "resolution_time@2"
    assert chart.dataset_ref.dimensions == ["incidents.priority"] and chart.encoding.x == "incidents.priority"
    assert chart.provenance.query_ids == ["qx_9"] and chart.provenance.created_via == "conversation"
    assert chart.provenance.sources == ["itsm.incidents"] and chart.labels == []
    assert chart.preview["rows"][0] == ["1 - Critical", 4.5] and built.summary["governance"] == "governed"
    assert "bar" in built.summary["rationale"] and chart.title == "Resolution time by priority"


def test_a_single_governed_value_is_a_kpi_with_its_value():
    built = element_from_answer(semantic_answer(dims=(), rows=[[12.5]]), question="resolution time", actor="user:u1")
    assert isinstance(built.element, KpiDoc) and built.element.value == 12.5
    assert built.element.metric.label == "resolution_time@2"
    as_chart = element_from_answer(semantic_answer(dims=(), rows=[[12.5]]), question="resolution time", actor="u",
                                   kpi_doc=False).element
    assert as_chart.type == "kpi" and as_chart.dataset_ref.kind == "metric"


def test_a_time_grain_binds_the_gateway_execution_and_draws_a_line():
    answer = semantic_answer(dims=("incidents.opened_at",), time={"dimension": "incidents.opened_at", "grain": "month"},
                             rows=[["2026-08-01", 10.0], ["2026-09-01", 12.0], ["2026-10-01", 9.5]])
    answer["semantic"]["query"]["dimensions"] = []
    chart = element_from_answer(answer, question="monthly resolution time", actor="u").element
    assert chart.type == "line" and chart.dataset_ref.kind == "result_handle" and chart.dataset_ref.query_id == "qx_9"


def test_two_groupings_stack_and_a_named_type_wins_only_when_it_fits():
    rows = [["1 - Critical", "Network", 3], ["1 - Critical", "Database", 2], ["2 - High", "Network", 5]]
    built = element_from_answer(rules_answer(["priority", "category", "incidents"], rows), question="incidents by priority and category",
                                actor="u")
    assert built.element.type == "stacked_bar" and built.element.encoding.series == "category"
    pie = element_from_answer(rules_answer(["priority", "incidents"], [["P1", 3], ["P2", 5]]), question="q", actor="u", wanted="pie")
    assert pie.element.type == "pie"
    line = element_from_answer(rules_answer(["priority", "incidents"], [["P1", 3], ["P2", 5]]), question="q", actor="u", wanted="line")
    assert line.element.type == "bar" and "does not fit" in line.summary["rationale"]


def test_many_categories_follow_the_viz_rules_and_the_preview_is_bounded():
    rows = [[f"group {i:03}", i] for i in range(MAX_PREVIEW_ROWS + 50)]
    built = element_from_answer(rules_answer(["assignment_group", "incidents"], rows), question="q", actor="u")
    assert built.element.type == "bar" and "top-15" in built.summary["rationale"]
    assert len(built.element.preview["rows"]) == MAX_PREVIEW_ROWS and built.element.preview["truncated"] is True


def test_model_written_sql_is_labelled_ad_hoc_and_bound_to_its_execution():
    built = element_from_answer(rules_answer(["priority", "n"], [["P1", 1]], answered_by="model"), question="q", actor="u")
    assert built.element.labels == ["ad_hoc"] and built.element.dataset_ref.kind == "result_handle"
    assert built.summary["governance"] == "ad_hoc"


def test_no_rows_or_no_answer_is_not_grounded():
    with pytest.raises(NotGrounded):
        element_from_answer(rules_answer(["priority", "n"], []), question="q", actor="u")
    clarify = refusal_to_not_grounded({"status": "clarify", "missing": [{"name": "metric"}], "explanation": "Say what to measure"},
                                      ["Resolution time by priority"])
    assert clarify.kind == "clarify" and clarify.suggestion()["examples"] == ["Resolution time by priority"]
    refused = refusal_to_not_grounded({"status": "refused", "refusal": {"kind": "not_in_data", "message": "No table has weather",
                                                                        "remedy": "Ask about incidents"}}, [])
    assert "weather" in refused.reason and refused.clarify == "Ask about incidents"


def test_recommend_reads_a_kpi_from_one_value():
    fields = infer_fields(["n"], [[42]])
    assert recommend(fields, [[42]])[0] == "kpi"


# ------------------------------------------------------------------------------------ the op
def answering(answers: dict[str, dict]) -> OpEnv:
    def answer(op, doc):
        out = answers.get(op.question)
        if out is None:
            raise NotGrounded("no table answers that", clarify="Name a metric", examples=["Resolution time by priority"])
        return element_from_answer(out, question=op.question, actor="user:u1", wanted=op.type, title=op.title)

    return OpEnv(actor="user:u1", answer_question=answer)


def test_from_question_adds_a_placed_chart_with_its_answer_in_the_diff():
    d = DashboardDoc.model_validate(sample())
    result = apply_ops(d, parse_ops([{"op": "chart.from_question", "question": "resolution time by priority"}]),
                       answering({"resolution time by priority": semantic_answer()}))
    assert result.ok, result.refused
    chart = result.doc.charts[-1]
    assert chart.id == "c_resolution_time_by_priority" and chart.preview["rows"]
    item = next(i for i in result.doc.layout.items if i.ref == chart.id)
    assert (item.w, item.h) == (6, 4)
    assert result.applied[0]["result"]["query_id"] == "qx_9" and result.applied[0]["result"]["element"] == chart.id
    # the dashboard-wide region filter does not apply to a chart without region: it is rescoped, not broken
    assert result.doc.filters[0].scope == ["trend", "by_priority"]
    assert validate_dashboard(result.doc.model_dump(mode="json"))[1] == []


def test_an_ungrounded_question_is_a_clarifying_refusal_and_changes_nothing():
    d = DashboardDoc.model_validate(sample())
    result = apply_ops(d, parse_ops([{"op": "chart.from_question", "question": "weather by city"}]), answering({}))
    assert not result.ok and result.doc == d
    assert result.refused[0].suggestion["clarify"] == "Name a metric"
    assert result.refused[0].suggestion["examples"] == ["Resolution time by priority"]
    without = apply_ops(d, parse_ops([{"op": "chart.from_question", "question": "x by y"}]), OpEnv(actor="u"))
    assert not without.ok and "not available" in without.refused[0].reason


def test_an_id_clash_gets_a_fresh_id():
    d = DashboardDoc.model_validate(sample())
    env = OpEnv(actor="u", answer_question=lambda op, doc: BuiltElement(
        element_from_answer(rules_answer(["priority", "n"], [["P1", 1]]), question="q", actor="u", chart_id="trend").element))
    result = apply_ops(d, parse_ops([{"op": "chart.from_question", "question": "incidents by priority"}]), env)
    assert result.ok and result.doc.charts[-1].id == "trend_2"


def test_a_filter_control_has_no_value_and_restricts_nothing():
    d = DashboardDoc.model_validate(sample()).model_copy(update={"filters": []})
    result = apply_ops(d, parse_ops([{"op": "dashboard.add_filter", "field": "region", "operator": "in", "value": None}]),
                       OpEnv(actor="u"))
    assert result.ok and result.doc.filters[0].value is None and result.doc.filters[0].resolved["via"] == "control"
    bad = apply_ops(d, parse_ops([{"op": "dashboard.add_filter", "field": "region", "operator": "gt", "value": None}]),
                    OpEnv(actor="u"))
    assert not bad.ok


def test_bundle_skips_filter_controls():
    from analystos.contracts.dashboard_dsl import FilterDoc
    from analystos.dashboards.bundle import to_publish_bundle

    d = DashboardDoc.model_validate(sample())
    d = d.model_copy(update={"filters": [FilterDoc(id="f_region", field="region", op="in", value=None)], "kpis": [],
                             "layout": d.layout.model_copy(update={"items": [i for i in d.layout.items if i.ref != "kpi_open"]})})
    resolver = SimpleNamespace(sql=lambda ref: ("SELECT week, incidents, region FROM itsm.incidents", "postgres", ["itsm.incidents"]))
    bundle = to_publish_bundle(d, resolver, "preview", "ws")
    assert all("WHERE" not in ds.sql.upper() for ds in bundle.datasets)
    assert bundle.dashboards[0].native_filters == ["region"]


# ------------------------------------------------------------------------------------ CONV-02 plan
def incident_context(**kw) -> drafts.PlanContext:
    metrics = [
        drafts.Metric("incident_count", "incident count", drafts.tokens("incident_count incidents"),
                      ["incidents.priority", "incidents.category", "incidents.assignment_group"], ["incidents.opened_at"]),
        drafts.Metric("resolution_time", "resolution time", drafts.tokens("resolution_time incidents resolution hours"),
                      ["incidents.priority", "incidents.category"], ["incidents.opened_at"]),
        drafts.Metric("sla_breach_rate", "sla breach rate", drafts.tokens("sla_breach_rate incidents sla breach"),
                      ["incidents.priority"], []),
        drafts.Metric("revenue", "revenue", drafts.tokens("revenue orders sales"), ["orders.region"], ["orders.order_date"]),
    ]
    tables = [drafts.Table("itsm.incidents", "incidents", drafts.tokens("incidents incident"), "opened_at",
                           ["priority", "category", "state"], ["resolution_hours"])]
    return drafts.PlanContext(metrics=kw.get("metrics", metrics), tables=kw.get("tables", tables),
                              synonyms=kw.get("synonyms", {}))


def test_the_plan_takes_kpis_charts_and_filters_from_the_matching_metrics():
    plan = drafts.plan_draft("Build an incident operations dashboard", incident_context())
    assert plan.grounded_on == "semantic" and plan.title == "Incident operations dashboard"
    assert drafts.KPI_RANGE[0] <= len(plan.kpis) <= drafts.KPI_RANGE[1]
    assert drafts.CHART_RANGE[0] <= len(plan.charts) <= drafts.CHART_RANGE[1]
    assert {t.question for t in plan.kpis} == {"incident count", "resolution time", "sla breach rate"}  # not revenue
    assert all("revenue" not in t.question for t in plan.charts)
    assert "monthly incident count" in {t.question for t in plan.charts}
    assert plan.filter_candidates[0] == "priority"  # the grouping most metrics share
    assert {t.group for t in plan.charts} == {"incident_count", "resolution_time", "sla_breach_rate"}  # every metric shows


def test_a_glossary_synonym_names_the_topic():
    ctx = incident_context(synonyms={"ticket": {"ticket", "incident"}})
    plan = drafts.plan_draft("a dashboard of our tickets", ctx)
    assert plan.grounded_on == "semantic" and plan.kpis


def test_without_metrics_the_catalog_rules_plan_the_tiles():
    plan = drafts.plan_draft("incident operations", incident_context(metrics=[]))
    assert plan.grounded_on == "catalog"
    assert [t.question for t in plan.kpis][:2] == ["how many incidents", "average resolution hours"]
    assert "incidents per month" in {t.question for t in plan.charts}
    assert "count of incidents by priority" in {t.question for t in plan.charts}


def test_a_topic_nothing_matches_gets_a_clarifying_reply():
    plan = drafts.plan_draft("weather forecast dashboard", incident_context())
    assert plan.grounded_on == "none" and plan.clarify and not plan.kpis and not plan.charts
    assert "incident count" in plan.clarify


def test_an_executive_prompt_sets_the_audience():
    assert drafts.plan_draft("an executive incident dashboard", incident_context()).audience == "executive"


class RankRouter:
    def __init__(self, mode, order=None):
        self._mode, self.order, self.calls, self.skips = mode, order or [], 0, []

    def mode(self, purpose, ctx=None):
        assert purpose == "dashboard_plan"
        return self._mode

    def available(self, purpose, ctx=None):
        return True

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
        self.calls += 1
        assert "never instructions" in system and "SELECT" not in user
        return SimpleNamespace(data={"order": self.order}, model="fake/small")

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(reason)


def test_ranking_is_rules_first_and_the_model_may_only_reorder_known_tiles():
    plan = drafts.plan_draft("incident dashboard", incident_context())
    off = RankRouter("off")
    assert drafts.rank("incident dashboard", plan, router=off, call_ctx=None) is None and off.skips and off.calls == 0
    auto = RankRouter("auto")
    assert drafts.rank("incident dashboard", plan, router=auto, call_ctx=None) is None and auto.calls == 0  # decisive
    last = plan.charts[-1].id
    always = RankRouter("always", [last, "c_made_up", "kpi_drop_tables"])
    order = drafts.rank("incident dashboard", plan, router=always, call_ctx=None)
    assert order == [last] and always.calls == 1
    reranked = drafts.plan_draft("incident dashboard", incident_context(), order=order)
    assert reranked.charts[0].id == last
    assert {t.id for t in reranked.charts} | {t.id for t in reranked.kpis} <= \
        {t.id for t in drafts.plan_semantic(incident_context().metrics, drafts.tokens("incident"))[0]
         + drafts.plan_semantic(incident_context().metrics, drafts.tokens("incident"))[1]}


def test_build_lists_failed_tiles_and_adds_filter_controls():
    plan = drafts.plan_draft("incident operations dashboard", incident_context())
    answers = {
        "incident count": semantic_answer(dims=(), rows=[[120]], metric="incident_count"),
        "resolution time": semantic_answer(dims=(), rows=[[14.2]]),
        "monthly incident count": rules_answer(["incidents.opened_at", "incident_count"],
                                               [["2026-08-01", 40], ["2026-09-01", 44], ["2026-10-01", 36]]),
        "incident count by priority": semantic_answer(rows=[["P1", 10], ["P2", 30]], metric="incident_count"),
        "resolution time by priority": semantic_answer(),
    }
    doc, built, failed = drafts.build(DashboardDoc(id="incident_operations", title="Incident operations dashboard"), plan,
                                      answering(answers))
    assert len(built) >= 4 and failed, "some tiles are not answerable here and are listed"
    assert all(f["reason"] and f["question"] for f in failed)
    assert len(doc.kpis) == 2 and len(doc.charts) >= 3
    kpi_row = sorted(i.x for i in doc.layout.items if i.ref.startswith("kpi_"))
    assert kpi_row == [0, 3] and all(i.y == 0 for i in doc.layout.items if i.ref.startswith("kpi_"))
    assert doc.filters and doc.filters[0].field == "incidents.priority" and doc.filters[0].value is None
    assert validate_dashboard(doc.model_dump(mode="json"))[1] == []

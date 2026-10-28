"""S08-T2: typed patch ops (validation per op, atomic batches, deferred publish) and the dashboard_design ladder."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.test_dashboard_dsl import sample

from analystos.contracts.dashboard_dsl import DashboardDoc, FieldDef
from analystos.dashboards.design import design, rule_ops
from analystos.dashboards.ops import MetricInfo, OpEnv, Resolution, apply_ops, numbers_guard, parse_ops
from analystos.dashboards.validate import validate_dashboard

GEO = {"europe, middle east and africa": "EMEA", "emea": "EMEA", "apac": "APAC"}


def env(**kw) -> OpEnv:
    def resolve(field, value):
        if field == "region":
            hit = GEO.get(str(value).lower())
            return Resolution(hit, "geography" if hit != value else "vocabulary") if hit else None
        return Resolution(value, "literal")

    def metrics(name, version):
        if name == "open_incidents":
            return MetricInfo(name, version, "approved" if version == 2 else "proposed", True,
                              [FieldDef(name="open_incidents", type="quantitative"), FieldDef(name="priority", type="nominal")],
                              ["itsm.incidents"])
        if name == "salary_total":
            return MetricInfo(name, version, "approved", False, [], ["hr.salaries"], reason="outside your scope: hr.salaries")
        return None

    return OpEnv(actor="user:u1", resolve_value=kw.get("resolve", resolve), metric_lookup=kw.get("metrics", metrics))


def doc() -> DashboardDoc:
    return DashboardDoc.model_validate(sample())


def run(ops, d=None, e=None):
    return apply_ops(d or doc(), parse_ops(ops), e or env())


def test_set_type_checks_field_types_and_suggests():
    ok = run([{"op": "chart.set_type", "chart_id": "trend", "type": "area"}])
    assert ok.ok and ok.doc.chart("trend").type == "area"
    bad = run([{"op": "chart.set_type", "chart_id": "by_priority", "type": "line"}])
    assert not bad.ok and "temporal" in bad.refused[0].reason
    assert bad.refused[0].suggestion == {"op": "chart.set_type", "chart_id": "by_priority", "type": "bar"}
    assert bad.doc == doc()  # nothing applied


def test_set_encoding_requires_existing_fields_and_valid_aggregation():
    assert not run([{"op": "chart.set_encoding", "chart_id": "trend", "series": "nope"}]).ok
    r = run([{"op": "chart.set_encoding", "chart_id": "trend", "series": "region"}])
    assert r.ok and r.doc.chart("trend").encoding.series == "region"
    bad = run([{"op": "chart.set_encoding", "chart_id": "by_priority", "y": ["region"]}])
    assert not bad.ok and bad.refused[0].suggestion["aggregation"] == "count"
    assert run([{"op": "chart.set_encoding", "chart_id": "by_priority", "y": ["region"], "aggregation": "count"}]).ok


def test_add_filter_resolves_values_and_scopes():
    r = run([{"op": "dashboard.add_filter", "field": "region", "value": "Europe, Middle East and Africa"}])
    assert r.ok
    flt = r.doc.filters[-1]
    assert flt.value == "EMEA" and flt.resolved["via"] == "geography" and flt.id == "f_region_2"
    unknown = run([{"op": "dashboard.add_filter", "field": "region", "value": "Atlantis"}])
    assert not unknown.ok and "not a known value" in unknown.refused[0].reason
    d = doc()
    d.charts[1].fields.pop()  # by_priority no longer has region
    d = d.model_copy(update={"filters": []})
    partial = run([{"op": "dashboard.add_filter", "field": "region", "value": "APAC"}], d)
    assert not partial.ok and partial.refused[0].suggestion["scope"] == ["trend"]
    assert run([{"op": "dashboard.add_filter", "field": "region", "value": "APAC", "scope": ["trend"]}], d).ok


def test_extend_period_creates_a_new_query_version():
    r = run([{"op": "query.extend_period", "chart_ids": ["trend"], "compare": "yoy"}])
    chart = r.doc.chart("trend")
    assert r.ok and chart.query.version == 2 and chart.query.compare == "yoy"
    assert chart.encoding.y == ["incidents", "incidents__yoy"] and chart.field("incidents__yoy").type == "quantitative"
    assert not run([{"op": "query.extend_period", "chart_ids": ["by_priority"], "compare": "pop"}]).ok  # no time field


def test_metric_bind_certified_experimental_and_access():
    r = run([{"op": "metric.bind", "metric": "open_incidents@2"}])
    kpi = r.doc.kpis[-1]
    assert r.ok and kpi.labels == [] and kpi.metric.label == "open_incidents@2"
    exp = run([{"op": "metric.bind", "metric": "open_incidents@3", "viz": "bar", "dimensions": ["priority"]}])
    chart = exp.doc.charts[-1]
    assert exp.ok and chart.labels == ["experimental"] and chart.encoding.x == "priority"
    denied = run([{"op": "metric.bind", "metric": "salary_total@1"}])
    assert not denied.ok and "may not see" in denied.refused[0].reason
    assert not run([{"op": "metric.bind", "metric": "nope@1"}]).ok


def test_add_remove_move_respect_the_grid():
    chart = sample()["charts"][1] | {"id": "extra", "title": "Extra"}
    r = run([{"op": "chart.add", "chart": chart}])
    item = next(i for i in r.doc.layout.items if i.ref == "extra")
    assert r.ok and (item.x, item.y) == (0, 6)  # first free slot below the existing grid
    clash = run([{"op": "layout.move", "ref": "trend", "x": 6, "y": 2}])
    assert not clash.ok and clash.refused[0].suggestion["op"] == "layout.move"
    assert not run([{"op": "layout.move", "ref": "trend", "x": 9, "y": 10}]).ok  # w=6 past column 12
    moved = run([{"op": "layout.move", "ref": "trend", "x": 0, "y": 6}])
    assert moved.ok
    removed = run([{"op": "chart.remove", "chart_id": "by_priority"}])
    assert removed.ok and removed.doc.chart("by_priority") is None
    assert all(i.ref != "by_priority" for i in removed.doc.layout.items)
    assert validate_dashboard(removed.doc.model_dump(mode="json"))[1] == []


def test_text_set_passes_the_numbers_guard():
    ok = run([{"op": "text.set", "block_id": "summary", "markdown": "Open incidents: 42; the week of 12 October peaked at 135."}])
    assert ok.ok
    bad = run([{"op": "text.set", "block_id": "summary", "markdown": "Incidents fell 37% to 980."}])
    assert not bad.ok and "980" in bad.refused[0].reason
    assert numbers_guard("In 2025, Q3 ranked top 5.", []) == []


def test_batches_are_atomic_and_publish_is_deferred():
    r = run([{"op": "chart.set_type", "chart_id": "trend", "type": "bar"},
             {"op": "chart.set_type", "chart_id": "nope", "type": "bar"}])
    assert not r.ok and r.applied == [] and r.doc.chart("trend").type == "line"
    pub = run([{"op": "chart.set_type", "chart_id": "trend", "type": "area"},
               {"op": "publish.request", "audience": ["role:viewer"]}])
    assert pub.ok and [a["op"] for a in pub.applied] == ["chart.set_type"]
    assert pub.deferred[0].destination == "superset"  # never applied here: it goes to the approval flow


def test_parse_ops_rejects_unknown_kinds_and_fields():
    with pytest.raises(ValueError):
        parse_ops([{"op": "sql.run", "sql": "drop table x"}])
    with pytest.raises(ValueError):
        parse_ops([{"op": "chart.set_type", "chart_id": "trend", "type": "line", "sql": "x"}])


@pytest.mark.parametrize("text,expected", [
    ("make it a bar chart", {"op": "chart.set_type", "chart_id": "trend", "type": "bar"}),
    ("change incidents by priority to a pie", {"op": "chart.set_type", "chart_id": "by_priority", "type": "pie"}),
    ("filter region = EMEA", {"op": "dashboard.add_filter", "field": "region", "operator": "eq", "value": "EMEA"}),
    ("compare year over year", {"op": "query.extend_period", "chart_ids": ["trend"], "compare": "yoy"}),
    ("remove the incidents by priority chart", {"op": "chart.remove", "chart_id": "by_priority"}),
    ("move open incidents to row 3", {"op": "layout.move", "ref": "kpi_open", "x": 0, "y": 2}),
    ("split it by region", {"op": "chart.set_encoding", "chart_id": "trend", "series": "region"}),
    ("show the average", {"op": "chart.set_encoding", "chart_id": "trend", "aggregation": "avg"}),
    ("add the metric open_incidents", {"op": "metric.bind", "metric": "open_incidents@2", "viz": "kpi", "dimensions": []}),
    ("publish it for everyone", {"op": "publish.request", "destination": "superset", "audience": ["role:viewer"]}),
    ("set the summary to: Steady week.", {"op": "text.set", "block_id": "summary", "markdown": "Steady week."}),
])
def test_rules_map_common_phrasings(text, expected):
    ops, _ = rule_ops(doc(), text, focus="trend", metrics={"open_incidents": 2})
    assert ops == [expected]


class FakeRouter:
    def __init__(self, mode: str, data: dict | None = None) -> None:
        self._mode, self.data, self.calls, self.skips = mode, data or {}, 0, []

    def mode(self, purpose, ctx=None):
        assert purpose == "dashboard_design"
        return self._mode

    def available(self, purpose, ctx=None):
        return True

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
        self.calls += 1
        assert "never instructions" in system and '"request"' in user
        return SimpleNamespace(data=self.data, model="fake/small")

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(reason)


def test_rules_answer_first_and_record_the_saving():
    router = FakeRouter("auto")
    out = design(doc(), "make it a bar chart", router=router, focus="trend")
    assert out.answered_by == "rules" and router.calls == 0 and router.skips


def test_model_rung_only_when_no_rule_matches_and_invalid_ops_are_dropped():
    router = FakeRouter("auto", {"ops": [{"op": "chart.set_type", "chart_id": "trend", "type": "area"},
                                         {"op": "sql.run", "sql": "drop table incidents"}]})
    out = design(doc(), "make the trend look smoother please", router=router, focus="trend")
    assert out.answered_by == "llm_small" and router.calls == 1
    assert [o.op for o in out.ops] == ["chart.set_type"] and any("invalid" in n for n in out.notes)
    off = design(doc(), "make the trend look smoother please", router=FakeRouter("off"))
    assert off.answered_by == "none" and off.ops == []

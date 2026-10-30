"""Chart selection (§34), dashboard layout (§33), native filters and the skills registry."""
from __future__ import annotations

import pytest

from analystos.contracts.bi import ChartSpec
from analystos.skills.dashboards import build_layout, choose_native_filters
from analystos.skills.registry import CATEGORIES, SKILLS, get_skill, resolve_skill
from analystos.skills.viz import choose_chart, recommend_chart


@pytest.mark.parametrize("intent,dim,card,n,expected", [
    ("trend", "datetime", 18, 1, "line"),
    ("comparison", "categorical", 6, 1, "bar"),
    ("distribution", "numeric", None, 1, "histogram"),
    ("relationship", "numeric", None, 2, "scatter"),
    ("part_to_whole", "categorical", 8, 1, "stacked_bar"),
    ("part_to_whole", "categorical", 4, 1, "pie"),
    ("part_to_whole", "categorical", 4, 2, "stacked_bar"),
    ("part_to_whole", "categorical", 30, 1, "treemap"),
    ("detail", None, None, 5, "table"),
    ("correlation", "numeric", 6, 6, "heatmap"),
    ("kpi", None, None, 1, "kpi"),
    ("aging", "categorical", 5, 1, "bar"),
    ("cohort", "datetime", 12, 1, "heatmap"),
    ("distribution", "categorical", 7, 1, "bar"),
    ("trend", "categorical", 7, 1, "bar"),
    ("comparison", "datetime", 12, 1, "line"),
    ("geography", "categorical", 10, 1, "bar"),
    ("something_else", None, None, 1, "table"),
])
def test_chart_rules(intent, dim, card, n, expected):
    chart, why = choose_chart(intent, dim, card, n)
    assert chart == expected and why


def test_overrides():
    r = recommend_chart("comparison", "categorical", 140, 1)
    assert r["chart_type"] == "bar" and r["limit"] == 15 and "top-15" in r["rationale"]
    r = recommend_chart("trend", "datetime", 24, 4)
    assert r["chart_type"] == "line" and r["series_by_metric"] is True


def _c(key, chart_type, intent, **kw):
    return ChartSpec(key=key, title=kw.pop("title", key), chart_type=chart_type, intent=intent, dataset="ds", **kw)


def test_executive_layout():
    charts = [_c("k1", "kpi", "kpi"), _c("k2", "kpi", "kpi"), _c("k3", "kpi", "kpi"), _c("k4", "kpi", "kpi"),
              _c("k5", "kpi", "kpi"), _c("trend", "line", "trend"), _c("d1", "bar", "comparison"),
              _c("d2", "bar", "comparison"), _c("risk", "bar", "comparison", title="Top SLA risk"),
              _c("detail", "table", "detail")]
    cells = build_layout("executive", charts)
    by = {c["chart"]: c for c in cells if c["kind"] == "chart"}
    assert [by[f"k{i}"]["width"] for i in range(1, 5)] == [3, 3, 3, 3]
    assert {by[f"k{i}"]["row"] for i in range(1, 5)} == {0}
    assert [by[f"k{i}"]["col"] for i in range(1, 5)] == [0, 3, 6, 9]
    assert by["k5"]["row"] > 0  # fifth KPI wraps
    assert by["trend"]["width"] == 12 and by["trend"]["row"] > by["k5"]["row"]
    assert by["d1"]["width"] == by["d2"]["width"] == 6 and by["d1"]["row"] == by["d2"]["row"] > by["trend"]["row"]
    assert by["risk"]["width"] == 12 and by["risk"]["row"] > by["d1"]["row"]
    assert cells[-1]["kind"] == "markdown" and cells[-1]["width"] == 12
    assert by["detail"]["row"] > by["risk"]["row"]
    _no_overlap(cells)


def test_operational_layout():
    charts = [_c("detail", "table", "detail"), _c("heat", "heatmap", "correlation"), _c("aging", "bar", "aging"),
              _c("queue", "bar", "comparison", title="Open queue by group"), _c("other", "line", "trend")]
    cells = build_layout("operational", charts)
    assert cells[0]["kind"] == "filters" and cells[0]["width"] == 12
    by = {c["chart"]: c for c in cells if c["kind"] == "chart"}
    assert by["heat"]["width"] == 6 and by["heat"]["row"] == by["aging"]["row"]
    assert by["detail"]["width"] == 12 and by["detail"]["row"] == max(c["row"] for c in cells)
    _no_overlap(cells)
    with pytest.raises(ValueError):
        build_layout("board", charts)


def _no_overlap(cells):
    occupied = set()
    for c in cells:
        assert c["col"] >= 0 and c["col"] + c["width"] <= 12
        for r in range(c["row"], c["row"] + c["height"]):
            for col in range(c["col"], c["col"] + c["width"]):
                assert (r, col) not in occupied
                occupied.add((r, col))


def test_native_filters():
    charts = [_c("a", "bar", "comparison", dimension="priority"), _c("b", "bar", "comparison", dimension="sys_id"),
              _c("c", "line", "trend", dimension="opened_at", series="category")]
    cols = [{"name": "opened_at", "semantic_type": "datetime"}, {"name": "priority", "semantic_type": "categorical", "cardinality": 4},
            {"name": "category", "semantic_type": "categorical", "cardinality": 5},
            {"name": "sys_id", "semantic_type": "id"}, {"name": "app", "semantic_type": "categorical", "cardinality": 400},
            {"name": "state", "semantic_type": "categorical", "cardinality": 7}, {"name": "breached", "semantic_type": "boolean"},
            {"name": "duration", "semantic_type": "numeric"}]
    f = choose_native_filters(charts, cols)
    assert f[:3] == ["opened_at", "priority", "category"]
    assert "sys_id" not in f and "app" not in f and "duration" not in f
    assert set(f) == {"opened_at", "priority", "category", "state", "breached"}


def test_registry_entries_resolve():
    ids = [s["id"] for s in SKILLS]
    assert len(ids) == len(set(ids))
    for s in SKILLS:
        assert s["category"] in CATEGORIES and s["deterministic"] is True and s["description"]
        assert callable(resolve_skill(s["id"]))
    assert {s["category"] for s in SKILLS} == set(CATEGORIES)
    assert get_skill("chi_square")["function"].endswith("stats.chi_square_rates")
    from analystos.contracts.registry import SkillSpec

    for s in SKILLS:  # compatible with the platform SkillSpec contract
        SkillSpec(id=s["id"], category=s["category"], description=s["description"], deterministic=s["deterministic"])


@pytest.mark.parametrize("name,semantic_type,role,distinct,coded", [
    ("priority", "numeric", "dimension", 5, True),       # the rehearsal's "Average priority" KPI
    ("incident.priority", None, None, None, True),       # the name alone
    ("status", "numeric", None, 7, True),
    ("severity_level", "numeric", None, None, True),
    ("risk_code", "code", None, 12, True),
    ("store_type", "category", None, 4, True),
    ("reopen_count", "numeric", "measure", 4, False),    # a count is a measure even with few values
    ("priority", "numeric", "dimension", 400, False),    # too many values to be codes
    ("resolution_hours", "numeric", "duration", 900, False),
    ("amount", "numeric", None, 3, False),
])
def test_coded_numbers_are_dimensions(name, semantic_type, role, distinct, coded):
    from analystos.skills.viz import is_coded_number

    assert is_coded_number(name, semantic_type=semantic_type, role=role, distinct=distinct) is coded

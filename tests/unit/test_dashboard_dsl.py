"""S08-T1: chart/dashboard JSON Schemas and the validator's rules."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from analystos.contracts.dashboard_dsl import (
    DashboardDoc,
    chart_json_schema,
    dashboard_json_schema,
    derive_sensitivity,
    max_sensitivity,
)
from analystos.dashboards.validate import suggest_type, suitability, validate_dashboard

ROOT = Path(__file__).resolve().parents[2]


def sample() -> dict:
    prov = {"sources": ["itsm.incidents"], "created_by": "user:u1", "query_ids": ["qx_1"]}
    return {
        "id": "ops_overview", "title": "Operations overview", "audience": "operational",
        "charts": [
            {"id": "trend", "title": "Incidents per week", "type": "line",
             "dataset_ref": {"kind": "verified_query", "name": "weekly_incidents", "version": 1},
             "fields": [{"name": "week", "type": "temporal"}, {"name": "incidents", "type": "quantitative"},
                        {"name": "region", "type": "nominal"}],
             "encoding": {"x": "week", "y": ["incidents"]}, "provenance": prov,
             "preview": {"columns": ["week", "incidents", "region"],
                         "rows": [["2026-10-05", 120, "EMEA"], ["2026-10-12", 135, "APAC"]]}},
            {"id": "by_priority", "title": "Incidents by priority", "type": "bar",
             "dataset_ref": {"kind": "result_handle", "handle_id": "rh_1", "query_id": "qx_2"},
             "fields": [{"name": "priority", "type": "nominal"}, {"name": "incidents", "type": "quantitative"},
                        {"name": "region", "type": "nominal"}],
             "encoding": {"x": "priority", "y": ["incidents"]}, "provenance": prov},
        ],
        "kpis": [{"id": "kpi_open", "title": "Open incidents", "metric": {"name": "open_incidents", "version": 2},
                  "value": 42, "provenance": prov}],
        "text_blocks": [{"id": "summary", "markdown": "Incidents rose to 135 in the week of 12 October."}],
        "layout": {"items": [{"ref": "kpi_open", "x": 0, "y": 0, "w": 3, "h": 2},
                             {"ref": "summary", "x": 3, "y": 0, "w": 9, "h": 2},
                             {"ref": "trend", "x": 0, "y": 2, "w": 6, "h": 4},
                             {"ref": "by_priority", "x": 6, "y": 2, "w": 6, "h": 4}]},
        "filters": [{"id": "f_region", "field": "region", "op": "eq", "value": "EMEA"}],
    }


def codes(raw: dict) -> list[str]:
    return [p.code for p in validate_dashboard(raw)[1]]


def test_valid_document_passes():
    doc, problems = validate_dashboard(sample())
    assert problems == [] and isinstance(doc, DashboardDoc)
    assert doc.charts[0].dataset_ref.kind == "verified_query" and doc.kpis[0].metric.label == "open_incidents@2"


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["charts"][0].pop("provenance"), "schema"),  # provenance is required
    (lambda d: d["charts"][0]["provenance"].update(sources=[]), "schema"),
    (lambda d: d["charts"][0].update(dataset_ref={"kind": "table", "name": "x"}), "schema"),
    (lambda d: d["charts"][0].update(id="Bad Id"), "schema"),
    (lambda d: d["charts"][0]["encoding"].update(y=["nope"]), "unknown_field"),
    (lambda d: d["charts"][1]["encoding"].update(aggregation="avg", y=["region"]), "invalid_aggregation"),
    (lambda d: d["charts"][1].update(type="line"), "unsuitable_type"),  # nominal x
    (lambda d: d["layout"]["items"][3].update(x=4), "grid_overlap"),
    (lambda d: d["layout"]["items"][3].update(x=8), "grid_overflow"),
    (lambda d: d["layout"]["items"].pop(), "unplaced"),
    (lambda d: d["layout"]["items"].append({"ref": "ghost", "x": 0, "y": 9, "w": 1, "h": 1}), "unknown_ref"),
    (lambda d: d["layout"]["items"][0].update(tab="nope"), "unknown_tab"),
    (lambda d: d["filters"][0].update(field="assignee"), "filter_field_missing"),
    (lambda d: d["filters"][0].update(op="in", value="EMEA"), "invalid_value"),
    (lambda d: d["kpis"][0].update(id="trend"), "duplicate_id"),
])
def test_rule_violations_are_reported_with_a_code(mutate, code):
    raw = copy.deepcopy(sample())
    mutate(raw)
    assert code in codes(raw)


def test_problems_carry_json_paths():
    raw = sample()
    raw["charts"][0]["encoding"]["y"] = ["nope"]
    problem = validate_dashboard(raw)[1][0]
    assert problem.path == "$.charts[0].encoding.y" and "nope" in problem.message


def test_sensitivity_is_the_maximum_over_inputs():
    raw = sample()
    raw["charts"][0]["fields"][1]["sensitivity"] = "confidential"
    assert "sensitivity_below_inputs" in codes(raw)  # declared internal, reads confidential
    doc = derive_sensitivity(DashboardDoc.model_validate(raw))
    assert doc.charts[0].sensitivity == "confidential" and doc.sensitivity == "confidential"
    assert doc.charts[1].sensitivity == "internal"
    assert validate_dashboard(doc.model_dump(mode="json"))[1] == []
    raw["kpis"][0]["metric"]["sensitivity"] = "restricted"
    assert derive_sensitivity(DashboardDoc.model_validate(raw)).sensitivity == "restricted"
    assert max_sensitivity("public", "bogus") == "internal"  # unknown never reads as public


def test_suitability_and_suggestion():
    chart = DashboardDoc.model_validate(sample()).charts[1]
    assert suitability("line", chart) and suitability("bar", chart) is None
    assert suitability("scatter", chart) and suggest_type(chart) == "bar"


def test_committed_json_schemas_match_the_models():
    """`analystos export-contracts` output is committed; a model change without regenerating fails here."""
    assert json.loads((ROOT / "contracts" / "chart.schema.json").read_text(encoding="utf-8")) == chart_json_schema()
    assert json.loads((ROOT / "contracts" / "dashboard.schema.json").read_text(encoding="utf-8")) == dashboard_json_schema()

"""S08: a builder dashboard becomes a publish bundle whose dataset SQL is built only from governed sources."""
from __future__ import annotations

import pytest
from tests.unit.test_dashboard_dsl import sample

from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.core.errors import InvalidInput
from analystos.dashboards.bundle import to_publish_bundle
from analystos.publishing.preview import validate_bundle

BASE = "SELECT week, incidents, region, priority FROM itsm.weekly"


class Resolver:
    def __init__(self, fail: str | None = None) -> None:
        self.fail, self.seen = fail, []

    def sql(self, ref):
        self.seen.append(ref.kind)
        if ref.kind == self.fail:
            raise InvalidInput(f"{ref.kind} not usable")
        return BASE, "postgres", ["itsm.weekly"]


def doc(**changes) -> DashboardDoc:
    raw = sample()
    raw.update(changes)
    return DashboardDoc.model_validate(raw)


def test_bundle_is_valid_and_carries_filters_comparisons_and_layout():
    d = doc()
    trend = d.charts[0].model_copy(update={"query": d.charts[0].query.model_copy(update={"compare": "yoy", "version": 2}),
                                           "encoding": d.charts[0].encoding.model_copy(update={"y": ["incidents", "incidents__yoy"]})})
    d = d.model_copy(update={"charts": [trend, d.charts[1]]})
    bundle = to_publish_bundle(d, Resolver(), "superset", "ws_1")
    assert validate_bundle(bundle) == []
    ds = {x.name: x for x in bundle.datasets}
    assert set(ds) == {"ds_trend", "ds_by_priority", "ds_kpi_open"}
    assert "WHERE \"region\" = 'EMEA'" in ds["ds_by_priority"].sql  # the dashboard filter, as a literal
    assert "INTERVAL '1 YEAR'" in ds["ds_trend"].sql and "incidents__yoy" in ds["ds_trend"].sql
    assert ds["ds_trend"].time_column == "week" and ds["ds_trend"].source_assets == ["itsm.incidents", "itsm.weekly"]
    charts = {c.key: c for c in bundle.charts}
    assert charts["trend"].chart_type == "line" and charts["trend"].dimension is None
    assert charts["by_priority"].dimension == "priority" and charts["kpi_open"].chart_type == "kpi"
    metrics = {m.name: m.sql_expression for m in bundle.metrics}
    assert metrics["trend__incidents"] == 'SUM("incidents")'
    dash = bundle.dashboards[0]
    assert dash.charts == ["kpi_open", "trend", "by_priority"] and dash.native_filters == ["region"]
    assert dash.summary_markdown.startswith("Incidents rose")


def test_filter_values_are_literals_never_sql():
    raw = sample()
    raw["filters"][0]["value"] = "EMEA' OR 1=1 --"
    bundle = to_publish_bundle(DashboardDoc.model_validate(raw), Resolver(), "superset", "ws_1")
    sql = next(d.sql for d in bundle.datasets if d.name == "ds_by_priority")
    assert "'EMEA'' OR 1=1 --'" in sql and "OR 1=1 --'" in sql.split("WHERE", 1)[1]


def test_an_unresolvable_dataset_refuses_the_bundle():
    with pytest.raises(InvalidInput):
        to_publish_bundle(doc(), Resolver(fail="metric"), "superset", "ws_1")

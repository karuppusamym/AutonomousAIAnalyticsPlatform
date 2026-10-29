"""CONV-06: cross-filtering and drill-down in the dashboard preview, every re-query governed.

The gateway here is the real validator (`gateway/validator.validate_sql` under the viewer's scope: denied columns,
row filters, star expansion) followed by in-memory DuckDB, so each statement the explorer builds is both admitted
by the gateway's rules and checked for the numbers it returns. Semantic tiles compile through the real compiler.
The live path over HTTP and compose Postgres is `tests/integration/test_dashboard_interact.py`.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
import sqlglot

from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.contracts.policy import DataScope
from analystos.core.errors import InvalidInput, NotFound
from analystos.dashboards import interact
from analystos.dashboards.interact import DrillStepIn, Explorer, ViewFilterIn, member_label, member_window, regrain
from analystos.dashboards.ops import OpEnv, Resolution, apply_ops, parse_ops
from analystos.gateway.validator import validate_sql

ASSET = "shop.orders"
COLUMNS = ["number", "priority", "region", "category", "opened_at", "resolution_hours", "customer_email"]


def frame() -> pd.DataFrame:
    rows = []
    for i in range(36):
        rows.append({"number": f"ORD{i:04}", "priority": ["1 - Critical", "2 - High", "3 - Moderate"][i % 3],
                     "region": ["EMEA", "APAC", "AMER"][(i // 3) % 3], "category": ["Network", "Billing"][i % 2],
                     "opened_at": datetime(2025, 11, 3) + pd.Timedelta(days=i * 3),
                     "resolution_hours": float(2 + i % 5), "customer_email": f"c{i}@example.com"})
    return pd.DataFrame(rows)


DATA = frame()


class Gateway:
    """validate_sql under the caller's scope, then DuckDB: the gateway's admission rules without services."""

    def __init__(self, delay: float = 0.0) -> None:
        self.con = duckdb.connect()
        self.con.execute("CREATE SCHEMA shop")
        self.con.register("frame", DATA)
        self.con.execute("CREATE TABLE shop.orders AS SELECT * FROM frame")
        self.calls: list[dict] = []
        self.delay, self.active, self.peak = delay, 0, 0
        self.lock = threading.Lock()

    def execute(self, scope, sql, *, actor, purpose, max_rows, **_):
        validated = validate_sql(scope, sql, max_rows=max_rows)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            n = len(self.calls)
            self.calls.append({"sql": sql, "purpose": purpose, "actor": actor, "max_rows": max_rows,
                               "executable": validated.executable_sql})
        try:
            if self.delay:
                time.sleep(self.delay)
            cur = self.con.cursor()
            cur.execute(sqlglot.transpile(validated.executable_sql, read="postgres", write="duckdb")[0])
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        finally:
            with self.lock:
                self.active -= 1
        return SimpleNamespace(query_id=f"qx_{n}", columns=cols, rows=rows[:max_rows], row_count=len(rows[:max_rows]),
                               truncated=len(rows) > max_rows, cache_hit=False)


def viewer_scope(**over) -> DataScope:
    base = {"workspace_id": "ws", "user_id": "usr_viewer", "role": "viewer", "source_ids": ["src_1"], "assets": [ASSET],
            "asset_sources": {ASSET: "src_1"}, "columns": {ASSET: COLUMNS}, "source_dialects": {"src_1": "postgres"},
            "denied_columns": [f"{ASSET}.customer_email"]}
    return DataScope(**{**base, **over})


def _field(name, *, dimension=False, time=False):
    return {"name": name, "expressions": [{"expression": name}], **({"dimension": {"is_time": time}} if dimension or time else {})}


DIMS = ["orders.priority", "orders.category", "orders.region", "orders.opened_at"]
CATALOG = {
    "id": "sm_1", "version": 1, "hash": "h", "relationships": [], "synonyms": {},
    "datasets": [{"name": "orders", "source": ASSET, "primary_key": ["number"],
                  "fields": [_field("number"), _field("priority", dimension=True), _field("region", dimension=True),
                             _field("category", dimension=True), _field("opened_at", time=True), _field("resolution_hours"),
                             _field("customer_email")]}],
    "metrics": {
        "resolution_time": {"id": "m1", "version": 1, "hash": "h1", "definition": {
            "name": "resolution_time", "expressions": [{"dialect": "ANSI_SQL", "expression": "AVG(resolution_hours)"}],
            "dataset": "orders", "dimensions": DIMS, "display_name": "Resolution time"}},
        "order_count": {"id": "m2", "version": 1, "hash": "h2", "definition": {
            "name": "order_count", "expressions": [{"dialect": "ANSI_SQL", "expression": "COUNT(number)"}],
            "dataset": "orders", "dimensions": DIMS, "display_name": "Order count"}},
    },
}

SQL = {
    "qx_pri": 'SELECT "priority" AS "priority", COUNT(*) AS "orders" FROM "shop"."orders" GROUP BY "priority" ORDER BY "priority"',
    "qx_rp": 'SELECT "region" AS "region", "priority" AS "priority", COUNT(*) AS "orders" FROM "shop"."orders" '
             'GROUP BY "region", "priority" ORDER BY "region", "priority"',
    "qx_trend": "SELECT DATE_TRUNC('month', CAST(\"opened_at\" AS TIMESTAMP)) AS \"month\", COUNT(*) AS \"orders\" "
                "FROM \"shop\".\"orders\" GROUP BY DATE_TRUNC('month', CAST(\"opened_at\" AS TIMESTAMP)) ORDER BY \"month\"",
    "qx_year": "SELECT DATE_TRUNC('year', CAST(\"opened_at\" AS TIMESTAMP)) AS \"year\", COUNT(*) AS \"orders\" "
               "FROM \"shop\".\"orders\" GROUP BY DATE_TRUNC('year', CAST(\"opened_at\" AS TIMESTAMP)) ORDER BY \"year\"",
}


def _prov(q):
    return {"sources": [ASSET], "query_ids": [q], "created_by": "user:usr_author", "created_via": "conversation"}


def _rh(id_, title, type_, fields, q, enc, preview):
    return {"id": id_, "title": title, "type": type_, "dataset_ref": {"kind": "result_handle", "handle_id": q, "query_id": q},
            "fields": fields, "encoding": enc, "provenance": _prov(q), "preview": preview}


def counts(by: list[str], df: pd.DataFrame = DATA) -> dict:
    return {tuple(k) if isinstance(k, tuple) else (k,): int(v) for k, v in df.groupby(by).size().items()}


def document() -> DashboardDoc:
    nominal = lambda n: {"name": n, "type": "nominal", "label": n.split(".")[-1].capitalize()}  # noqa: E731
    qty = lambda n: {"name": n, "type": "quantitative"}  # noqa: E731
    pri = sorted(counts(["priority"]).items())
    charts = [
        _rh("c_by_priority", "Orders by priority", "bar", [nominal("priority"), qty("orders")], "qx_pri",
            {"x": "priority", "y": ["orders"]}, {"columns": ["priority", "orders"], "rows": [[k[0], v] for k, v in pri]}),
        _rh("c_region_priority", "Orders by region and priority", "stacked_bar",
            [nominal("region"), nominal("priority"), qty("orders")], "qx_rp", {"x": "region", "y": ["orders"], "series": "priority"},
            {"columns": ["region", "priority", "orders"], "rows": [[*k, v] for k, v in sorted(counts(["region", "priority"]).items())]}),
        _rh("c_trend", "Orders per month", "line", [{"name": "month", "type": "temporal"}, qty("orders")], "qx_trend",
            {"x": "month", "y": ["orders"]}, {"columns": ["month", "orders"], "rows": [["2025-11-01", 10]]}),
        {"id": "c_resolution_by_category", "title": "Resolution time by category", "type": "bar",
         "dataset_ref": {"kind": "metric", "name": "resolution_time", "version": 1, "dimensions": ["orders.category"]},
         "fields": [nominal("orders.category"), qty("resolution_time")], "encoding": {"x": "orders.category", "y": ["resolution_time"]},
         "provenance": _prov("qx_m"), "preview": {"columns": ["orders.category", "resolution_time"], "rows": [["Network", 3.9], ["Billing", 4.1]]}},
    ]
    kpis = [{"id": "kpi_orders", "title": "Order count", "metric": {"name": "order_count", "version": 1}, "value": 36.0,
             "provenance": _prov("qx_k")}]
    items = [{"ref": "kpi_orders", "x": 0, "y": 0, "w": 3, "h": 2}, {"ref": "c_by_priority", "x": 0, "y": 2, "w": 6, "h": 4},
             {"ref": "c_region_priority", "x": 6, "y": 2, "w": 6, "h": 4}, {"ref": "c_trend", "x": 0, "y": 6, "w": 6, "h": 4},
             {"ref": "c_resolution_by_category", "x": 6, "y": 6, "w": 6, "h": 4}]
    return DashboardDoc.model_validate({"id": "ops", "title": "Ops", "charts": charts, "kpis": kpis, "layout": {"items": items}})


GLOSSARY = {"critical": "1 - Critical", "p1": "1 - Critical"}


def resolver(doc: DashboardDoc):
    """The typed-filter resolver's contract (`dashboards/env.py`): vocabulary from the previews, then glossary."""
    def resolve(name, value):
        known = {str(r[c.preview["columns"].index(name)]) for c in doc.charts if name in (c.preview or {}).get("columns", [])
                 for r in c.preview["rows"]}
        if name in DATA.columns:  # the profiled vocabulary of a source column
            known |= {str(v) for v in DATA[name].unique()}
        if not known:
            return Resolution(value=value, via="unverified")
        if str(value) in known:
            return Resolution(value=str(value), via="vocabulary")
        term = GLOSSARY.get(str(value).lower())
        return Resolution(value=term, via="glossary", note=f"{value!r} means {term!r}") if term in known else None

    return resolve


def explorer(doc: DashboardDoc | None = None, scope: DataScope | None = None, gateway: Gateway | None = None, **kw) -> Explorer:
    doc = doc or document()
    return Explorer(doc=doc, scope=scope or viewer_scope(), actor="user:usr_viewer", gateway=gateway or Gateway(),
                    resolve_value=resolver(doc), dataset_sql=lambda ref: (SQL[ref.handle_id], "postgres", [ASSET]),
                    catalog=CATALOG, **kw)


def tiles(out: dict) -> dict:
    return {t["id"]: t for t in out["tiles"]}


# ------------------------------------------------------------------------------------------- cross-filter
def test_a_click_filters_every_tile_bound_to_the_field_through_its_governed_path():
    gw = Gateway()
    out = explorer(gateway=gw).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    t = tiles(out)
    crit = DATA[DATA.priority == "1 - Critical"]
    assert out["filters"] == [{"field": "priority", "op": "eq", "value": "1 - Critical", "label": "Priority", "via": "vocabulary",
                               "source": "c_by_priority"}]
    assert t["c_by_priority"]["status"] == "source" and "preview" not in t["c_by_priority"]  # not filtered by its own click
    rp = t["c_region_priority"]
    assert rp["status"] == "filtered" and rp["filtered_by"] == ["Priority"]
    assert {(r[0], r[1]): r[2] for r in rp["preview"]["rows"]} == {k: v for k, v in counts(["region", "priority"], crit).items()}
    assert t["c_trend"] == {"id": "c_trend", "kind": "chart", "status": "not_filtered", "reason": "This chart has no Priority field."}
    # a metric chart is filtered by a dimension it does not show: the semantic compiler adds the predicate
    metric = t["c_resolution_by_category"]
    assert metric["status"] == "filtered"
    assert {r[0]: pytest.approx(r[1]) for r in metric["preview"]["rows"]} == crit.groupby("category")["resolution_hours"].mean().to_dict()
    assert t["kpi_orders"]["status"] == "filtered" and t["kpi_orders"]["value"] == len(crit)
    assert {c["purpose"] for c in gw.calls} == {"dashboard.cross_filter"} and len(gw.calls) == 3
    assert all(c["actor"] == "user:usr_viewer" and c["max_rows"] == interact.MAX_PREVIEW_ROWS for c in gw.calls)
    assert out["apply"] == [{"op": "dashboard.add_filter", "field": "priority", "operator": "eq", "value": "1 - Critical",
                             "scope": ["c_by_priority", "c_region_priority"]}]
    assert [x["id"] for x in out["tiles"]] == ["c_by_priority", "c_region_priority", "c_trend", "c_resolution_by_category", "kpi_orders"]


def test_two_filters_from_two_charts_each_leave_their_own_chart_unfiltered_by_them():
    gw = Gateway()
    out = explorer(gateway=gw).view([ViewFilterIn(field="priority", value="2 - High", source="c_by_priority"),
                                     ViewFilterIn(field="orders.category", value="Network", source="c_resolution_by_category")])
    t = tiles(out)
    high_net = DATA[(DATA.priority == "2 - High") & (DATA.category == "Network")]
    assert t["kpi_orders"]["value"] == len(high_net)
    assert t["c_by_priority"]["status"] == "source"  # it has no category field, and its own click does not filter it
    by_cat = t["c_resolution_by_category"]
    assert by_cat["filtered_by"] == ["Priority"]  # filtered by the other chart's click, not its own


@pytest.mark.parametrize("raw,message", [
    ({"field": "customer_segment", "value": "x"}, "not a field of this dashboard"),
    ({"field": "orders", "value": 3}, "is a measure"),
    ({"field": "month", "value": "2026-01-01"}, "drill into it"),
    ({"field": "priority", "value": "x' OR 1=1 --"}, "not a known value"),
    ({"field": "priority", "value": ["1 - Critical"]}, "use 'in'"),
    ({"field": "priority", "op": "in", "value": []}, "choose 1 to"),
    ({"field": "priority", "value": "1 - Critical", "source": "c_nope"}, "no chart"),
])
def test_the_client_predicate_is_never_trusted(raw, message):
    gw = Gateway()
    with pytest.raises(InvalidInput, match=message):
        explorer(gateway=gw).view([ViewFilterIn(**raw)])
    assert gw.calls == []  # refused before any statement


def test_a_request_is_bounded_and_values_resolve_like_typed_filters():
    with pytest.raises(InvalidInput, match="at most"):
        explorer().validate([ViewFilterIn(field="priority", value="1 - Critical")] * 6)
    with pytest.raises(InvalidInput, match="twice"):
        explorer().validate([ViewFilterIn(field="priority", value="1 - Critical"), ViewFilterIn(field="orders.priority", value="2 - High")])
    [f] = explorer().validate([ViewFilterIn(field="priority", value="P1")])
    assert (f.value, f.via, f.note) == ("1 - Critical", "glossary", "'P1' means '1 - Critical'")
    with pytest.raises(Exception):  # noqa: B017 - the request model refuses SQL-shaped extras
        ViewFilterIn(field="priority", value="1", sql="1=1")


def test_the_viewers_row_filter_and_denied_columns_apply_on_every_requery():
    gw = Gateway()
    scope = viewer_scope(row_filters={ASSET: ["region = 'EMEA'"]})
    out = explorer(scope=scope, gateway=gw).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    t = tiles(out)
    emea = DATA[(DATA.region == "EMEA") & (DATA.priority == "1 - Critical")]
    assert t["kpi_orders"]["value"] == len(emea)
    assert {r[0] for r in t["c_region_priority"]["preview"]["rows"]} == {"EMEA"}
    assert all("EMEA" in c["executable"] for c in gw.calls)  # the gateway wrapped every read of the table
    detail = explorer(scope=scope, gateway=gw).drill("c_by_priority", [DrillStepIn(field="priority", value="2 - High")], [], detail=True)
    rows = detail["detail"]
    assert "customer_email" not in rows["columns"] and rows["hidden_columns"] == 1
    region, priority = rows["columns"].index("region"), rows["columns"].index("priority")
    assert rows["rows"] and {(r[region], r[priority]) for r in rows["rows"]} == {("EMEA", "2 - High")}
    assert len(rows["rows"]) == len(DATA[(DATA.region == "EMEA") & (DATA.priority == "2 - High")])
    assert gw.calls[-1]["purpose"] == "dashboard.detail" and gw.calls[-1]["max_rows"] == interact.DETAIL_ROWS


def test_a_withheld_table_refuses_the_tile_and_keeps_the_others():
    scope = viewer_scope(withheld_assets={ASSET: "missing attribute regions"}, assets=[], columns={})
    out = explorer(scope=scope).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    t = tiles(out)
    assert t["c_region_priority"]["status"] == "refused" and t["kpi_orders"]["status"] == "refused"
    assert t["c_trend"]["status"] == "not_filtered"


# ------------------------------------------------------------------------------------------- drill-down
def test_time_drills_one_level_per_click_inside_the_clicked_member():
    gw = Gateway()
    ex = explorer(gateway=gw)
    plan = ex.drill_plan(ex.doc.chart("c_trend"))
    assert plan["kind"] == "time" and [lv["grain"] for lv in plan["levels"]] == ["month", "day"]
    out = ex.drill("c_trend", [DrillStepIn(field="month", value="2026-01-01T00:00:00")], [])
    jan = DATA[(DATA.opened_at >= "2026-01-01") & (DATA.opened_at < "2026-02-01")]
    assert out["path"] == [{"field": "month", "value": "2026-01-01T00:00:00", "grain": "month", "label": "Jan 2026"}]
    assert out["level"]["grain"] == "day" and gw.calls[-1]["purpose"] == "dashboard.drill"
    assert sum(r[1] for r in out["preview"]["rows"]) == len(jan) and len(out["preview"]["rows"]) == len(jan)
    assert all(str(r[0]).startswith("2026-01") for r in out["preview"]["rows"])
    # a day is the last level: no further chart, but its rows
    last = ex.drill("c_trend", [DrillStepIn(field="month", value="2026-01-01"), DrillStepIn(field="month", value=jan.opened_at.iloc[0].isoformat())],
                    [], detail=True)
    assert last["level"] is None and "preview" not in last and len(last["detail"]["rows"]) == 1
    with pytest.raises(InvalidInput, match="deeper"):
        ex.drill("c_trend", [DrillStepIn(field="month", value="2026-01-01")] * 3, [])
    with pytest.raises(InvalidInput, match="not a date"):
        ex.drill("c_trend", [DrillStepIn(field="month", value="soon")], [])
    with pytest.raises(NotFound):
        ex.drill("c_nope", [], [])


def test_a_yearly_axis_drills_through_quarter_and_month():
    doc = document()
    year = doc.charts[2].model_copy(update={"id": "c_year", "dataset_ref": doc.charts[2].dataset_ref.model_copy(
        update={"handle_id": "qx_year", "query_id": "qx_year"}), "fields": [doc.charts[2].fields[0].model_copy(update={"name": "year"}),
                                                                            doc.charts[2].fields[1]],
        "encoding": doc.charts[2].encoding.model_copy(update={"x": "year"})})
    doc = doc.model_copy(update={"charts": [*doc.charts, year], "layout": doc.layout.model_copy(update={"items": [
        *doc.layout.items, doc.layout.items[0].model_copy(update={"ref": "c_year", "y": 10})]})})
    ex = explorer(doc)
    assert [lv["grain"] for lv in ex.drill_plan(ex.doc.chart("c_year"))["levels"]] == ["year", "quarter", "month", "day"]
    q = ex.drill("c_year", [DrillStepIn(field="year", value="2025-01-01")], [])
    assert [str(r[0])[:10] for r in q["preview"]["rows"]] == ["2025-10-01"]  # only Q4 2025 has 2025 orders
    m = ex.drill("c_year", [DrillStepIn(field="year", value="2025-01-01"), DrillStepIn(field="year", value="2025-10-01")], [])
    assert [str(r[0])[:7] for r in m["preview"]["rows"]] == ["2025-11", "2025-12"] and m["path"][1]["label"] == "Q4 2025"


def test_a_metric_chart_drills_along_the_approved_dimensions():
    ex = explorer()
    plan = ex.drill_plan(ex.doc.chart("c_resolution_by_category"))
    assert plan["kind"] == "dimension" and [lv["field"] for lv in plan["levels"]] == ["orders.category", "orders.priority", "orders.region"]
    out = ex.drill("c_resolution_by_category", [DrillStepIn(field="orders.category", value="Network")],
                   [ViewFilterIn(field="region", value="APAC", source="c_region_priority")])
    sub = DATA[(DATA.category == "Network") & (DATA.region == "APAC")]
    assert out["filtered_by"] == ["Region"] and out["level"]["field"] == "orders.priority"
    assert {r[0]: pytest.approx(r[1]) for r in out["preview"]["rows"]} == sub.groupby("priority")["resolution_hours"].mean().to_dict()
    with pytest.raises(InvalidInput, match="level 1 of this drill path is"):
        ex.drill("c_resolution_by_category", [DrillStepIn(field="orders.region", value="APAC")], [])
    detail = ex.drill("c_resolution_by_category", [DrillStepIn(field="orders.category", value="Network")], [], detail=True)
    cat = detail["detail"]["columns"].index("category")
    assert {r[cat] for r in detail["detail"]["rows"]} == {"Network"} and "customer_email" not in detail["detail"]["columns"]


def test_a_chart_without_a_hierarchy_still_lists_the_rows_behind_a_bar():
    ex = explorer()
    plan = ex.drill_plan(ex.doc.chart("c_by_priority"))
    assert plan["kind"] == "none" and plan["levels"] == [{"field": "priority", "label": "Priority"}]
    out = ex.drill("c_by_priority", [DrillStepIn(field="priority", value="critical")], [], detail=True)
    assert out["path"][0]["value"] == "1 - Critical" and "preview" not in out
    assert len(out["detail"]["rows"]) == len(DATA[DATA.priority == "1 - Critical"])


def test_regrain_rewrites_the_projection_group_and_window_only():
    sql = regrain(SQL["qx_trend"], "postgres", "month", "day", member_window("2026-01-15", "month"))
    tree = sqlglot.parse_one(sql, read="postgres")
    assert "DATE_TRUNC('MONTH'" not in sql and sql.count("DATE_TRUNC('DAY'") == 2
    assert tree.args["where"] is not None and "2026-01-01 00:00:00" in sql and "2026-02-01 00:00:00" in sql
    assert len(list(tree.find_all(sqlglot.exp.TimestampTrunc))) == 2  # projection + group by


def test_member_windows_and_labels():
    assert member_window("2026-05-17", "quarter") == (datetime(2026, 4, 1), datetime(2026, 7, 1))
    assert member_window("2026-12-03T10:00:00Z", "month") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert member_window("2026-12-31", "year") == (datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert member_window("2026-10-29", "week") == (datetime(2026, 10, 26), datetime(2026, 11, 2))
    assert [member_label("2026-05-01", g) for g in ("year", "quarter", "month", "day")] == ["2026", "Q2 2026", "May 2026", "2026-05-01"]


# ------------------------------------------------------------------------------------------- bounds
def _wide(n: int) -> DashboardDoc:
    doc = document()
    base = doc.charts[1]
    extra = [base.model_copy(update={"id": f"c_rp_{i}", "dataset_ref": base.dataset_ref.model_copy(update={"handle_id": f"qx_rp{i}"})})
             for i in range(n)]
    items = [doc.layout.items[0].model_copy(update={"ref": c.id, "y": 20 + i * 2}) for i, c in enumerate(extra)]
    return doc.model_copy(update={"charts": [*doc.charts, *extra],
                                  "layout": doc.layout.model_copy(update={"items": [*doc.layout.items, *items]})})


def test_concurrent_tile_queries_are_capped_and_identical_statements_run_once(monkeypatch):
    for i in range(8):
        SQL[f"qx_rp{i}"] = SQL["qx_rp"] + ("" if i % 2 else " LIMIT 500") + (f" OFFSET {i}" if i > 3 else "")
    gw = Gateway(delay=0.05)
    out = explorer(_wide(8), gateway=gw).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    assert gw.peak <= interact.MAX_CONCURRENT
    statements = [c["sql"] for c in gw.calls]
    assert len(statements) == len(set(statements))  # identical statements ran once
    assert all(t["status"] in ("filtered", "source", "not_filtered") for t in out["tiles"])
    monkeypatch.setattr(interact, "MAX_TILE_QUERIES", 2)
    capped = explorer(_wide(8)).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    assert sum(t["status"] == "skipped" for t in capped["tiles"]) >= 5


def test_a_dashboard_at_its_concurrency_limit_reports_busy(monkeypatch):
    monkeypatch.setattr(interact, "GATE_WAIT_SECONDS", 0.01)
    gate = threading.BoundedSemaphore(1)
    gate.acquire()
    out = explorer(gate=gate).view([ViewFilterIn(field="priority", value="1 - Critical", source="c_by_priority")])
    assert tiles(out)["kpi_orders"]["status"] == "busy" and "try again" in tiles(out)["kpi_orders"]["reason"]
    gate.release()


def test_gates_are_per_dashboard_and_bounded(monkeypatch):
    monkeypatch.setattr(interact, "_MAX_GATES", 3)
    interact._GATES.clear()
    first = interact.gate_for("art_a")
    assert interact.gate_for("art_a") is first
    for x in "bcd":
        interact.gate_for(f"art_{x}")
    assert len(interact._GATES) == 3 and "art_a" not in interact._GATES


# ------------------------------------------------------------------------------------------- apply as dashboard filter
def test_apply_as_dashboard_filter_is_the_typed_op_and_the_saved_filter_then_applies_on_requery():
    doc = document()
    ex = explorer(doc)
    op = ex.view([ViewFilterIn(field="priority", value="critical", source="c_by_priority")])["apply"][0]
    assert op["value"] == "1 - Critical"
    result = apply_ops(doc, parse_ops([op]), OpEnv(actor="user:usr_editor", resolve_value=resolver(doc)))
    assert result.ok and result.doc.filters[0].value == "1 - Critical" and result.applied[0]["op"] == "dashboard.add_filter"
    gw = Gateway()
    saved = explorer(result.doc, gateway=gw).view([], refresh_saved=True)
    t = tiles(saved)
    assert t["c_by_priority"]["status"] == "saved_filter" and {r[0] for r in t["c_by_priority"]["preview"]["rows"]} == {"1 - Critical"}
    assert t["c_trend"]["status"] == "not_filtered" and len(gw.calls) == 2
    explore = explorer(result.doc).explore()
    by_id = {c["id"]: c for c in explore["charts"]}
    assert by_id["c_region_priority"]["filter_fields"] == ["region", "priority"] and by_id["c_trend"]["drill"]["kind"] == "time"
    assert explore["kpis"] == [{"id": "kpi_orders", "dimensions": DIMS}]

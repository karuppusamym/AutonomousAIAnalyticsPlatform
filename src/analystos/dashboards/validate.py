"""Dashboard DSL validator (S08-T1).

Two layers: the JSON Schema (`contracts/dashboard.schema.json`, the published contract any client can
check against) and the rules a schema cannot express — layout grid, encodings against the bound
fields, chart-type suitability, filter scope and derived sensitivity. Every problem has a JSON path,
a code and a message; the patch-ops engine refuses a batch whose result has any.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from analystos.contracts.dashboard_dsl import (
    GRID_COLUMNS,
    MAX_PREVIEW_ROWS,
    SENSITIVITY_ORDER,
    ChartDoc,
    DashboardDoc,
    chart_sensitivity,
    dashboard_json_schema,
    dashboard_sensitivity,
)

NUMERIC_AGGREGATIONS = frozenset({"sum", "avg", "min", "max"})


@dataclass(frozen=True)
class Problem:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@lru_cache(maxsize=1)
def _validator() -> Any:
    from jsonschema import Draft202012Validator

    return Draft202012Validator(dashboard_json_schema())


def _json_path(parts: Any) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


# -- chart-type suitability ---------------------------------------------------------------------------
def suitability(kind: str, chart: ChartDoc) -> str | None:
    """Why `kind` does not suit the chart's encoding and fields, or None when it does."""
    x = chart.field(chart.encoding.x)
    ys = [chart.field(y) for y in chart.encoding.y]
    numeric_y = all(f is not None and f.type == "quantitative" for f in ys) or chart.encoding.aggregation in (
        "count", "count_distinct")
    if kind in ("line", "area"):
        if x is None or x.type not in ("temporal", "ordinal"):
            return f"a {kind} chart needs a temporal (or ordered) x field"
        if not ys or not numeric_y:
            return f"a {kind} chart needs a numeric y"
    elif kind in ("bar", "stacked_bar"):
        if x is None or x.type == "quantitative":
            return f"a {kind} chart needs a categorical or temporal x field"
        if not ys or not numeric_y:
            return f"a {kind} chart needs a numeric y"
        if kind == "stacked_bar" and not chart.encoding.series:
            return "a stacked bar needs a series field"
    elif kind == "scatter":
        if x is None or x.type != "quantitative" or len(ys) != 1 or ys[0] is None or ys[0].type != "quantitative":
            return "a scatter plot needs a quantitative x and one quantitative y"
    elif kind in ("pie", "treemap"):
        if x is None or x.type not in ("nominal", "ordinal", "boolean") or len(ys) != 1 or not numeric_y:
            return f"a {kind} needs one categorical field and one numeric value"
    elif kind == "kpi":
        if x is not None or len(ys) != 1 or not numeric_y:
            return "a KPI shows one numeric value and no x field"
    elif kind == "histogram":
        if x is None or x.type != "quantitative":
            return "a histogram needs a quantitative x field"
    elif kind == "heatmap":
        if x is None or not chart.encoding.series or len(ys) != 1:
            return "a heatmap needs an x field, a series field and one value"
    return None


def suggest_type(chart: ChartDoc) -> str:
    """The first chart type that suits the chart's current encoding (table always does)."""
    for kind in ("line", "bar", "kpi", "scatter", "pie", "histogram"):
        if suitability(kind, chart) is None:
            return kind
    return "table"


# -- rules ---------------------------------------------------------------------------------------------
def _chart_problems(i: int, chart: ChartDoc) -> list[Problem]:
    base = f"$.charts[{i}]"
    out: list[Problem] = []
    names = [f.name for f in chart.fields]
    if len(set(names)) != len(names):
        out.append(Problem(f"{base}.fields", "duplicate_field", f"chart {chart.id} declares a field twice"))
    enc = chart.encoding
    for slot, value in (("x", enc.x), ("series", enc.series), ("color", enc.color), *(("y", y) for y in enc.y)):
        if value and chart.field(value) is None:
            out.append(Problem(f"{base}.encoding.{slot}", "unknown_field",
                               f"chart {chart.id}: field {value!r} is not in its dataset ({', '.join(names)})"))
    if enc.aggregation in NUMERIC_AGGREGATIONS:
        for y in enc.y:
            f = chart.field(y)
            if f is not None and f.type != "quantitative":
                out.append(Problem(f"{base}.encoding.aggregation", "invalid_aggregation",
                                   f"chart {chart.id}: {enc.aggregation} needs a quantitative field; {y} is {f.type}"))
    if chart.type != "table":
        reason = suitability(chart.type, chart)
        if reason:
            out.append(Problem(f"{base}.type", "unsuitable_type", f"chart {chart.id}: {reason} (try {suggest_type(chart)})"))
    if SENSITIVITY_ORDER[chart.sensitivity] < SENSITIVITY_ORDER[chart_sensitivity(chart)]:
        out.append(Problem(f"{base}.sensitivity", "sensitivity_below_inputs",
                           f"chart {chart.id} is labelled {chart.sensitivity} but reads {chart_sensitivity(chart)} data"))
    rows = (chart.preview or {}).get("rows") or []
    if len(rows) > MAX_PREVIEW_ROWS:
        out.append(Problem(f"{base}.preview.rows", "preview_too_large", f"a preview holds at most {MAX_PREVIEW_ROWS} rows"))
    if chart.dataset_ref.kind == "metric" and chart.dataset_ref.dimensions:
        missing = [d for d in chart.dataset_ref.dimensions if d not in names]
        if missing:
            out.append(Problem(f"{base}.dataset_ref.dimensions", "unknown_field",
                               f"chart {chart.id}: dimensions {missing} are not among its fields"))
    return out


def _layout_problems(doc: DashboardDoc) -> list[Problem]:
    out: list[Problem] = []
    ids = set(doc.element_ids())
    tabs = {t.id for t in doc.tabs}
    placed: dict[str, int] = {}
    cells: dict[tuple[str | None, int, int], str] = {}
    for i, item in enumerate(doc.layout.items):
        path = f"$.layout.items[{i}]"
        if item.ref not in ids:
            out.append(Problem(path, "unknown_ref", f"layout places {item.ref!r}, which is not a chart, KPI or text block"))
        placed[item.ref] = placed.get(item.ref, 0) + 1
        if item.x + item.w > GRID_COLUMNS:
            out.append(Problem(path, "grid_overflow", f"{item.ref} spans columns {item.x}..{item.x + item.w - 1}; "
                                                      f"the grid has {GRID_COLUMNS}"))
        if item.tab is not None and item.tab not in tabs:
            out.append(Problem(f"{path}.tab", "unknown_tab", f"{item.ref} is on tab {item.tab!r}, which does not exist"))
        for dx in range(item.w):
            for dy in range(item.h):
                cell = (item.tab, item.x + dx, item.y + dy)
                other = cells.get(cell)
                if other and other != item.ref:
                    out.append(Problem(path, "grid_overlap", f"{item.ref} overlaps {other}"))
                    break
                cells[cell] = item.ref
            else:
                continue
            break
    for ref in sorted(ids):
        if placed.get(ref, 0) == 0:
            out.append(Problem("$.layout.items", "unplaced", f"{ref} has no place in the layout"))
        elif placed[ref] > 1:
            out.append(Problem("$.layout.items", "placed_twice", f"{ref} is placed {placed[ref]} times"))
    return out


def _filter_problems(doc: DashboardDoc) -> list[Problem]:
    out: list[Problem] = []
    for i, flt in enumerate(doc.filters):
        path = f"$.filters[{i}]"
        unknown = [c for c in flt.scope if doc.chart(c) is None]
        if unknown:
            out.append(Problem(f"{path}.scope", "unknown_ref", f"filter {flt.id} is scoped to unknown charts {unknown}"))
        targets = [doc.chart(c) for c in flt.scope] if flt.scope else doc.charts
        lacking = [c.id for c in targets if c is not None and c.field(flt.field) is None]
        if lacking:
            out.append(Problem(f"{path}.field", "filter_field_missing",
                               f"filter {flt.id}: {flt.field!r} is not a field of {', '.join(lacking)}; scope the filter "
                               "to the charts that have it"))
        if flt.value is None and flt.op in ("eq", "in"):
            continue  # a control without a preset value
        if flt.op in ("in", "not_in") and not isinstance(flt.value, list):
            out.append(Problem(f"{path}.value", "invalid_value", f"filter {flt.id}: {flt.op} needs a list"))
        if flt.op == "between" and not (isinstance(flt.value, list) and len(flt.value) == 2):
            out.append(Problem(f"{path}.value", "invalid_value", f"filter {flt.id}: between needs [low, high]"))
    return out


def problems(doc: DashboardDoc) -> list[Problem]:
    """Rule problems of an already-parsed document."""
    out: list[Problem] = []
    seen: dict[str, str] = {}
    for kind, items in (("chart", doc.charts), ("kpi", doc.kpis), ("text", doc.text_blocks), ("tab", doc.tabs),
                        ("filter", doc.filters)):
        for item in items:
            if item.id in seen:
                out.append(Problem("$", "duplicate_id", f"{kind} id {item.id!r} is already used by a {seen[item.id]}"))
            seen[item.id] = kind
    for i, chart in enumerate(doc.charts):
        out.extend(_chart_problems(i, chart))
    out.extend(_layout_problems(doc))
    out.extend(_filter_problems(doc))
    if SENSITIVITY_ORDER[doc.sensitivity] < SENSITIVITY_ORDER[dashboard_sensitivity(doc)]:
        out.append(Problem("$.sensitivity", "sensitivity_below_inputs",
                           f"dashboard is labelled {doc.sensitivity} but shows {dashboard_sensitivity(doc)} data"))
    return out


def validate_dashboard(raw: dict[str, Any]) -> tuple[DashboardDoc | None, list[Problem]]:
    """(document, problems) for a raw JSON document; the document is None when it does not parse."""
    schema_errors = sorted(_validator().iter_errors(raw), key=lambda e: list(e.absolute_path))
    if schema_errors:
        return None, [Problem(_json_path(e.absolute_path), "schema", e.message[:300]) for e in schema_errors[:50]]
    try:
        doc = DashboardDoc.model_validate(raw)
    except ValidationError as exc:
        return None, [Problem(_json_path(err["loc"]), "schema", err["msg"]) for err in exc.errors()[:50]]
    return doc, problems(doc)

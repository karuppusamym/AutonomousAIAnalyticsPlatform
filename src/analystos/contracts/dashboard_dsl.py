"""Engine-neutral chart and dashboard DSL (spec S08 §3, v1 §8.1).

A dashboard is a document the conversation never edits directly: typed patch operations
(`dashboards/ops.py`) change it and every applied batch is a new artifact version. These models are the
contract; `analystos export-contracts` writes them to `contracts/chart.schema.json` and
`contracts/dashboard.schema.json`. The BI publish shapes (`contracts/bi.py`) are derived from them at
publish time.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "dashboard.v2"
GRID_COLUMNS = 12
MAX_PREVIEW_ROWS = 200

Sensitivity = Literal["public", "internal", "confidential", "restricted"]
SENSITIVITY_ORDER: dict[str, int] = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
FieldType = Literal["temporal", "quantitative", "nominal", "ordinal", "boolean"]
ChartKind = Literal["kpi", "line", "area", "bar", "stacked_bar", "histogram", "scatter", "heatmap", "table", "pie",
                    "treemap"]
Aggregation = Literal["sum", "avg", "min", "max", "count", "count_distinct", "none"]
FilterOp = Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "between"]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def max_sensitivity(*levels: str | None) -> str:
    """The most sensitive of `levels` (unknown or missing counts as internal: never public by accident)."""
    ranked = [lvl if lvl in SENSITIVITY_ORDER else "internal" for lvl in levels]
    return max(ranked or ["public"], key=SENSITIVITY_ORDER.__getitem__)


class FieldDef(_Strict):
    """One column of the bound dataset, as resolved when the chart was bound."""

    name: str = Field(min_length=1, max_length=200)
    type: FieldType
    label: str = ""
    sensitivity: Sensitivity = "internal"


class ResultHandleRef(_Strict):
    kind: Literal["result_handle"] = "result_handle"
    handle_id: str = Field(min_length=1, max_length=120)
    query_id: str | None = None  # the gateway execution that produced it
    sensitivity: Sensitivity = "internal"


class VerifiedQueryRef(_Strict):
    kind: Literal["verified_query"] = "verified_query"
    name: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    sensitivity: Sensitivity = "internal"


class MetricRef(_Strict):
    kind: Literal["metric"] = "metric"
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    dimensions: list[str] = Field(default_factory=list, max_length=10)
    sensitivity: Sensitivity = "internal"

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


DatasetRef = Annotated[ResultHandleRef | VerifiedQueryRef | MetricRef, Field(discriminator="kind")]


class Provenance(_Strict):
    """Where the chart's numbers come from. Required: an unsourced chart is never valid."""

    sources: list[str] = Field(min_length=1)  # assets, verified queries or metrics it reads
    query_ids: list[str] = Field(default_factory=list)
    created_by: str = Field(min_length=1)
    created_via: Literal["user", "conversation", "agent", "import"] = "user"
    run_id: str | None = None
    turn_id: str | None = None


class Encoding(_Strict):
    x: str | None = None
    y: list[str] = Field(default_factory=list, max_length=8)
    series: str | None = None
    color: str | None = None
    aggregation: Aggregation = "sum"


class QueryBinding(_Strict):
    """The chart's query version. `query.extend_period` raises the version and sets the comparison."""

    version: int = Field(default=1, ge=1)
    compare: Literal["none", "yoy", "pop"] = "none"
    period: str | None = None


class ChartDoc(_Strict):
    id: Identifier
    title: str = Field(min_length=1, max_length=300)
    type: ChartKind
    dataset_ref: DatasetRef
    fields: list[FieldDef] = Field(min_length=1, max_length=200)
    encoding: Encoding = Field(default_factory=Encoding)
    query: QueryBinding = Field(default_factory=QueryBinding)
    sensitivity: Sensitivity = "internal"  # derived: never below its dataset or any field
    provenance: Provenance
    labels: list[str] = Field(default_factory=list)  # e.g. "experimental" for an uncertified metric
    description: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)  # {columns, rows}; bounded, never published

    def field(self, name: str | None) -> FieldDef | None:
        return next((f for f in self.fields if f.name == name), None) if name else None


class KpiDoc(_Strict):
    id: Identifier
    title: str = Field(min_length=1, max_length=300)
    metric: MetricRef
    value: float | None = None
    unit: str = ""
    sensitivity: Sensitivity = "internal"
    provenance: Provenance
    labels: list[str] = Field(default_factory=list)


class TextBlock(_Strict):
    id: Identifier
    markdown: str = Field(default="", max_length=20_000)


class Tab(_Strict):
    id: Identifier
    title: str = Field(min_length=1, max_length=120)


class LayoutItem(_Strict):
    ref: Identifier  # a chart, KPI or text block id
    x: int = Field(ge=0, lt=GRID_COLUMNS)
    y: int = Field(ge=0, le=1000)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=48)
    tab: str | None = None


class Layout(_Strict):
    columns: Literal[12] = GRID_COLUMNS
    items: list[LayoutItem] = Field(default_factory=list)


class FilterDoc(_Strict):
    id: Identifier
    field: str = Field(min_length=1, max_length=200)
    op: FilterOp = "eq"
    value: Any = None
    scope: list[str] = Field(default_factory=list)  # chart ids; empty = every chart
    resolved: dict[str, Any] = Field(default_factory=dict)  # how the value was resolved (glossary, geography)


class DashboardDoc(_Strict):
    schema_version: Literal["dashboard.v2"] = SCHEMA_VERSION
    id: Identifier
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    audience: Literal["executive", "operational"] = "operational"
    layout: Layout = Field(default_factory=Layout)
    tabs: list[Tab] = Field(default_factory=list)
    filters: list[FilterDoc] = Field(default_factory=list)
    charts: list[ChartDoc] = Field(default_factory=list, max_length=60)
    text_blocks: list[TextBlock] = Field(default_factory=list)
    kpis: list[KpiDoc] = Field(default_factory=list)
    sensitivity: Sensitivity = "internal"  # derived: the maximum over charts and KPIs

    def chart(self, chart_id: str) -> ChartDoc | None:
        return next((c for c in self.charts if c.id == chart_id), None)

    def element_ids(self) -> list[str]:
        return [*(c.id for c in self.charts), *(k.id for k in self.kpis), *(t.id for t in self.text_blocks)]


def chart_sensitivity(chart: ChartDoc) -> str:
    return max_sensitivity(chart.dataset_ref.sensitivity, *(f.sensitivity for f in chart.fields))


def dashboard_sensitivity(doc: DashboardDoc) -> str:
    return max_sensitivity("public", *(chart_sensitivity(c) for c in doc.charts),
                           *(max_sensitivity(k.sensitivity, k.metric.sensitivity) for k in doc.kpis))


def derive_sensitivity(doc: DashboardDoc) -> DashboardDoc:
    """Set every chart's and the dashboard's sensitivity to the maximum over their inputs (never lowered)."""
    charts = [c.model_copy(update={"sensitivity": max_sensitivity(c.sensitivity, chart_sensitivity(c))}) for c in doc.charts]
    kpis = [k.model_copy(update={"sensitivity": max_sensitivity(k.sensitivity, k.metric.sensitivity)}) for k in doc.kpis]
    out = doc.model_copy(update={"charts": charts, "kpis": kpis})
    return out.model_copy(update={"sensitivity": max_sensitivity(doc.sensitivity, dashboard_sensitivity(out))})


def chart_json_schema() -> dict[str, Any]:
    return ChartDoc.model_json_schema()


def dashboard_json_schema() -> dict[str, Any]:
    return DashboardDoc.model_json_schema()

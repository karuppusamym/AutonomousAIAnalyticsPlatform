"""Typed dashboard patch operations (S08-T2, FR-048).

The conversation never edits the dashboard JSON. A model or a rule proposes ops; this module validates
each against the current document and applies a batch atomically: one refused op refuses the batch and
the document is unchanged. The caller persists an applied batch as `ArtifactVersion + 1` with the op list
as its diff. `publish.request` is never applied here: it is returned as deferred and goes to the
hash-bound approval flow.

Everything the engine needs from the platform (glossary resolution, the metric catalog with its access
check, the numbers a text block may quote) comes in through `OpEnv`, so the engine is pure and the same
ops replay identically.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from analystos.contracts.dashboard_dsl import (
    GRID_COLUMNS,
    ChartDoc,
    ChartKind,
    DashboardDoc,
    Encoding,
    FieldDef,
    FilterDoc,
    FilterOp,
    Identifier,
    KpiDoc,
    LayoutItem,
    MetricRef,
    Provenance,
    QueryBinding,
    TextBlock,
    derive_sensitivity,
)
from analystos.dashboards.validate import problems, suggest_type, suitability


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChartSetType(_Op):
    op: Literal["chart.set_type"] = "chart.set_type"
    chart_id: str
    type: ChartKind


class ChartSetEncoding(_Op):
    op: Literal["chart.set_encoding"] = "chart.set_encoding"
    chart_id: str
    x: str | None = None
    y: list[str] | None = None
    series: str | None = None
    color: str | None = None
    aggregation: Literal["sum", "avg", "min", "max", "count", "count_distinct", "none"] | None = None


class DashboardAddFilter(_Op):
    op: Literal["dashboard.add_filter"] = "dashboard.add_filter"
    field: str
    operator: FilterOp = "eq"
    value: Any = None
    scope: list[str] = Field(default_factory=list)
    filter_id: str | None = None


class QueryExtendPeriod(_Op):
    op: Literal["query.extend_period"] = "query.extend_period"
    chart_ids: list[str] = Field(min_length=1)
    compare: Literal["yoy", "pop"]


class MetricBind(_Op):
    op: Literal["metric.bind"] = "metric.bind"
    metric: str = Field(pattern=r"^[A-Za-z_][\w.]*@\d+$")  # name@version
    viz: ChartKind = "kpi"
    chart_id: str | None = None  # bind into this chart; else a new element is added
    title: str | None = None
    dimensions: list[str] = Field(default_factory=list)


class ChartAdd(_Op):
    op: Literal["chart.add"] = "chart.add"
    chart: ChartDoc
    position: dict[str, int] | None = None  # {x, y, w, h}
    tab: str | None = None


class ChartFromQuestion(_Op):
    """A new chart or KPI from a business question (CONV-01). The engine never answers it: `OpEnv.answer_question`
    runs the governed Ask path (semantic compiler, verified query, rules, else generated SQL, then the gateway)
    and returns the built element; a question it cannot ground is refused with a clarifying reply."""

    op: Literal["chart.from_question"] = "chart.from_question"
    question: str = Field(min_length=3, max_length=500)
    type: ChartKind | None = None  # the person's chart type; else skills/viz decides from the data's shape
    title: str | None = Field(default=None, max_length=300)
    position: dict[str, int] | None = None
    tab: str | None = None


class ChartRemove(_Op):
    op: Literal["chart.remove"] = "chart.remove"
    chart_id: str


class LayoutMove(_Op):
    op: Literal["layout.move"] = "layout.move"
    ref: str
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int | None = Field(default=None, ge=1)
    h: int | None = Field(default=None, ge=1)
    tab: str | None = None


class TextSet(_Op):
    op: Literal["text.set"] = "text.set"
    block_id: Identifier
    markdown: str = Field(max_length=20_000)


class PublishRequest(_Op):
    op: Literal["publish.request"] = "publish.request"
    audience: list[str] = Field(default_factory=lambda: ["role:viewer"])
    destination: Literal["superset", "preview"] = "superset"


Op = Annotated[ChartSetType | ChartSetEncoding | DashboardAddFilter | QueryExtendPeriod | MetricBind | ChartAdd |
               ChartFromQuestion | ChartRemove | LayoutMove | TextSet | PublishRequest, Field(discriminator="op")]
OP_KINDS = ("chart.set_type", "chart.set_encoding", "dashboard.add_filter", "query.extend_period", "metric.bind",
            "chart.add", "chart.from_question", "chart.remove", "layout.move", "text.set", "publish.request")
_OPS = TypeAdapter(list[Op])



def op_guide() -> str:
    """One line per op kind with its fields (* = required), from the op models: the model rung is told the exact
    shape, so its proposals are not dropped for a guessed field name."""
    lines = []
    for model in (ChartSetType, ChartSetEncoding, DashboardAddFilter, QueryExtendPeriod, MetricBind, ChartAdd,
                  ChartFromQuestion, ChartRemove, LayoutMove, TextSet, PublishRequest):
        fields = [f"{name}{'*' if info.is_required() else ''}" for name, info in model.model_fields.items() if name != "op"]
        lines.append(f"- {model.model_fields['op'].default}: {', '.join(fields)}")
    return "\n".join(lines)

def parse_ops(raw: list[dict[str, Any]]) -> list[Any]:
    """Typed ops from JSON; raises ValueError naming the first invalid op."""
    try:
        return _OPS.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValueError(f"invalid op at {list(first['loc'])}: {first['msg']}") from None


# -- environment -------------------------------------------------------------------------------------
@dataclass
class MetricInfo:
    """What the metric catalog says about `name@version` for the acting user."""

    name: str
    version: int
    status: str  # approved | proposed | draft | deprecated | rejected
    allowed: bool  # the caller may read every asset the metric reads
    fields: list[FieldDef]
    sources: list[str]
    sensitivity: str = "internal"
    display_name: str = ""
    reason: str = ""


@dataclass
class Resolution:
    """A filter value resolved through the glossary or geography: what the user said and what it means."""

    value: Any
    via: str  # glossary | geography | literal
    note: str = ""


class NotGrounded(Exception):
    """A question the platform could not turn into a governed query. Carries the clarifying reply and
    questions it can answer instead; never a guessed chart."""

    def __init__(self, reason: str, *, clarify: str = "", examples: list[str] | None = None, kind: str = "not_grounded") -> None:
        super().__init__(reason)
        self.reason, self.clarify, self.examples, self.kind = reason, clarify, list(examples or []), kind

    def suggestion(self) -> dict[str, Any]:
        return {"clarify": self.clarify or self.reason, "kind": self.kind,
                **({"examples": self.examples[:5]} if self.examples else {})}


@dataclass
class BuiltElement:
    """What answering a question produced: a chart (or a metric KPI) with its preview and provenance."""

    element: ChartDoc | KpiDoc
    summary: dict[str, Any] = field(default_factory=dict)  # answered_by, governance, query_id, rationale (for the diff)


@dataclass
class OpEnv:
    actor: str
    resolve_value: Callable[[str, Any], Resolution | None] = lambda field_name, value: None
    metric_lookup: Callable[[str, int], MetricInfo | None] = lambda name, version: None
    known_numbers: Callable[[DashboardDoc], Iterable[float]] | None = None
    # CONV-01: question -> built element through the governed Ask path; raises NotGrounded. None = not available here.
    answer_question: Callable[[ChartFromQuestion, DashboardDoc], BuiltElement] | None = None


@dataclass
class Refusal:
    index: int
    op: str
    reason: str
    suggestion: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "op": self.op, "reason": self.reason,
                **({"suggestion": self.suggestion} if self.suggestion else {})}


@dataclass
class OpResult:
    doc: DashboardDoc
    applied: list[dict[str, Any]] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    deferred: list[PublishRequest] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused

    @property
    def changed(self) -> bool:
        return bool(self.applied)


class _Refused(Exception):
    def __init__(self, reason: str, suggestion: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason, self.suggestion = reason, suggestion


# -- helpers -----------------------------------------------------------------------------------------
def _chart(doc: DashboardDoc, chart_id: str) -> ChartDoc:
    chart = doc.chart(chart_id)
    if chart is None:
        raise _Refused(f"no chart {chart_id!r} on this dashboard (charts: {', '.join(c.id for c in doc.charts) or 'none'})")
    return chart


def _replace_chart(doc: DashboardDoc, chart: ChartDoc) -> DashboardDoc:
    return doc.model_copy(update={"charts": [chart if c.id == chart.id else c for c in doc.charts]})


def _free_slot(doc: DashboardDoc, w: int, h: int, tab: str | None) -> tuple[int, int]:
    """The first top-left position (row-major) where a w×h box fits on `tab`."""
    taken = {(item.x + dx, item.y + dy) for item in doc.layout.items if item.tab == tab
             for dx in range(item.w) for dy in range(item.h)}
    w = min(w, GRID_COLUMNS)
    for y in range(0, 1000):
        for x in range(0, GRID_COLUMNS - w + 1):
            if all((x + dx, y + dy) not in taken for dx in range(w) for dy in range(h)):
                return x, y
    raise _Refused("the dashboard grid is full")


def _place(doc: DashboardDoc, ref: str, position: dict[str, int] | None, tab: str | None, default: tuple[int, int]) -> DashboardDoc:
    w, h = (position or {}).get("w", default[0]), (position or {}).get("h", default[1])
    if position and "x" in position and "y" in position:
        x, y = position["x"], position["y"]
    else:
        x, y = _free_slot(doc, w, h, tab)
    item = LayoutItem(ref=ref, x=x, y=y, w=w, h=h, tab=tab)
    return doc.model_copy(update={"layout": doc.layout.model_copy(update={"items": [*doc.layout.items, item]})})


_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATES = re.compile(rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}|\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|"
                    r"\b(?:q[1-4]|h[12]|fy\d{2,4}|week\s+\d{1,2}|w\d{1,2})\b", re.IGNORECASE)


def _doc_numbers(doc: DashboardDoc) -> list[float]:
    values: list[float] = []
    for k in doc.kpis:
        if k.value is not None:
            values.append(float(k.value))
    for c in doc.charts:
        for row in (c.preview or {}).get("rows") or []:
            for v in row if isinstance(row, list) else (row.values() if isinstance(row, dict) else []):
                if isinstance(v, int | float) and not isinstance(v, bool):
                    values.append(float(v))
    return values


def numbers_guard(markdown: str, values: Iterable[float]) -> list[str]:
    """Numbers in a text block that no chart or KPI on the dashboard shows (within the quoted precision).
    Years and small ordinals in prose (2024, Q3, top 5) are not claims and are ignored."""
    known = list(values)
    unbound = []
    for token in _NUMBER.findall(_DATES.sub(" ", markdown or "")):  # dates and period labels are not claims
        raw = token.replace(",", "")
        percent = raw.endswith("%")
        try:
            number = float(raw.rstrip("%"))
        except ValueError:
            continue
        if not percent and number.is_integer() and (0 <= number <= 10 or 1900 <= number <= 2100):
            continue
        decimals = len(raw.rstrip("%").split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(v - number) <= tol or (percent and abs(v - number / 100) <= tol / 100) for v in known):
            unbound.append(token)
    return unbound


# -- appliers ------------------------------------------------------------------------------------------
def _set_type(doc: DashboardDoc, op: ChartSetType, env: OpEnv) -> DashboardDoc:
    chart = _chart(doc, op.chart_id)
    reason = suitability(op.type, chart) if op.type != "table" else None
    if reason:
        alt = suggest_type(chart)
        raise _Refused(f"{reason}; chart {chart.id} has x={chart.encoding.x!r}, y={chart.encoding.y}",
                       {"op": "chart.set_type", "chart_id": chart.id, "type": alt})
    return _replace_chart(doc, chart.model_copy(update={"type": op.type}))


def _set_encoding(doc: DashboardDoc, op: ChartSetEncoding, env: OpEnv) -> DashboardDoc:
    chart = _chart(doc, op.chart_id)
    changes = {k: v for k, v in op.model_dump(exclude={"op", "chart_id"}).items() if v is not None}
    if not changes:
        raise _Refused("chart.set_encoding changes nothing")
    for slot in ("x", "series", "color"):
        if changes.get(slot) and chart.field(changes[slot]) is None:
            raise _Refused(f"{changes[slot]!r} is not a field of chart {chart.id}",
                           {"fields": [f.name for f in chart.fields]})
    for y in changes.get("y") or []:
        if chart.field(y) is None:
            raise _Refused(f"{y!r} is not a field of chart {chart.id}", {"fields": [f.name for f in chart.fields]})
    encoding = Encoding.model_validate({**chart.encoding.model_dump(), **changes})
    candidate = chart.model_copy(update={"encoding": encoding})
    agg = encoding.aggregation
    if agg in ("sum", "avg", "min", "max"):
        bad = [y for y in encoding.y if (candidate.field(y) or FieldDef(name=y, type="nominal")).type != "quantitative"]
        if bad:
            raise _Refused(f"{agg} needs quantitative fields; {', '.join(bad)} are not",
                           {"op": "chart.set_encoding", "chart_id": chart.id, "aggregation": "count"})
    if candidate.type != "table" and suitability(candidate.type, candidate):
        candidate = candidate.model_copy(update={"type": suggest_type(candidate)})  # the new encoding decides the type
    return _replace_chart(doc, candidate)


def _add_filter(doc: DashboardDoc, op: DashboardAddFilter, env: OpEnv) -> DashboardDoc:
    if not doc.charts:
        raise _Refused("a filter needs at least one chart")
    unknown = [c for c in op.scope if doc.chart(c) is None]
    if unknown:
        raise _Refused(f"filter scope names unknown charts {unknown}")
    targets = [doc.chart(c) for c in op.scope] if op.scope else doc.charts
    having = [c.id for c in doc.charts if c.field(op.field) is not None]
    lacking = [c.id for c in targets if c is not None and c.field(op.field) is None]
    if not having:
        raise _Refused(f"no chart on this dashboard has a field {op.field!r}")
    if lacking:
        raise _Refused(f"{op.field!r} is not a field of {', '.join(lacking)}",
                       {"op": "dashboard.add_filter", "field": op.field, "operator": op.operator, "value": op.value,
                        "scope": having})
    # No value = a filter control the viewer sets (a draft's filters): nothing to resolve, nothing restricted yet.
    resolved = Resolution(None, "control") if op.value is None else env.resolve_value(op.field, op.value)
    if resolved is None:
        raise _Refused(f"the value {op.value!r} for {op.field} is not a known value (glossary, geography or the "
                       "field's profiled vocabulary); name it exactly")
    fid = op.filter_id or re.sub(r"[^a-z0-9_]+", "_", f"f_{op.field}".lower())[:60]
    existing = {f.id for f in doc.filters}
    base, n = fid, 2
    while fid in existing:
        fid, n = f"{base}_{n}", n + 1
    if op.value is None and op.operator not in ("eq", "in"):
        raise _Refused("a filter control without a value uses 'eq' or 'in'")
    flt = FilterDoc(id=fid, field=op.field, op=op.operator, value=resolved.value, scope=list(op.scope),
                    resolved={"input": op.value, "via": resolved.via, **({"note": resolved.note} if resolved.note else {})})
    return doc.model_copy(update={"filters": [*doc.filters, flt]})


def _extend_period(doc: DashboardDoc, op: QueryExtendPeriod, env: OpEnv) -> DashboardDoc:
    for chart_id in op.chart_ids:
        chart = _chart(doc, chart_id)
        temporal = [f for f in chart.fields if f.type == "temporal"]
        if not temporal:
            raise _Refused(f"chart {chart.id} has no time field to compare periods over")
        if not chart.encoding.y:
            raise _Refused(f"chart {chart.id} has no value to compare")
        suffix = f"__{op.compare}"
        added = [FieldDef(name=f"{y}{suffix}", type="quantitative", label=f"{y} ({op.compare.upper()})",
                          sensitivity=(chart.field(y) or chart.fields[0]).sensitivity)
                 for y in chart.encoding.y if not y.endswith(suffix) and chart.field(f"{y}{suffix}") is None]
        y = [*chart.encoding.y, *(f.name for f in added)]
        query = QueryBinding(version=chart.query.version + 1, compare=op.compare, period=chart.query.period)
        doc = _replace_chart(doc, chart.model_copy(update={
            "fields": [*chart.fields, *added], "encoding": chart.encoding.model_copy(update={"y": y}), "query": query}))
    return doc


def _bind_metric(doc: DashboardDoc, op: MetricBind, env: OpEnv) -> DashboardDoc:
    name, _, version = op.metric.rpartition("@")
    info = env.metric_lookup(name, int(version))
    if info is None:
        raise _Refused(f"metric {op.metric} does not exist in this workspace")
    if not info.allowed:
        raise _Refused(f"metric {op.metric} reads data you may not see{': ' + info.reason if info.reason else ''}")
    if info.status in ("deprecated", "rejected"):
        raise _Refused(f"metric {op.metric} is {info.status}; bind a current version")
    labels = [] if info.status == "approved" else ["experimental"]
    ref = MetricRef(name=name, version=int(version), dimensions=list(op.dimensions), sensitivity=info.sensitivity)  # type: ignore[arg-type]
    provenance = Provenance(sources=info.sources or [f"metric:{op.metric}"], created_by=env.actor, created_via="conversation")
    title = op.title or info.display_name or name.replace("_", " ").title()
    fields = list(info.fields) or [FieldDef(name=name, type="quantitative", sensitivity=info.sensitivity)]  # type: ignore[arg-type]
    missing = [d for d in op.dimensions if not any(f.name == d for f in fields)]
    if missing:
        raise _Refused(f"metric {op.metric} has no dimensions {missing}",
                       {"dimensions": [f.name for f in fields if f.type != "quantitative"]})
    value_field = next((f.name for f in fields if f.name == name), None) or next(
        (f.name for f in fields if f.type == "quantitative"), name)
    if op.chart_id:
        chart = _chart(doc, op.chart_id)
        rebound = chart.model_copy(update={"dataset_ref": ref, "fields": fields, "provenance": provenance,
                                           "labels": sorted(set(chart.labels) - {"experimental"} | set(labels)),
                                           "encoding": chart.encoding.model_copy(update={"y": [value_field]}),
                                           "query": QueryBinding(version=chart.query.version + 1)})
        if rebound.type != "table" and suitability(rebound.type, rebound):
            rebound = rebound.model_copy(update={"type": suggest_type(rebound)})
        return _replace_chart(doc, rebound)
    if op.viz == "kpi" and not op.dimensions:
        kid = _unique_id(doc, f"kpi_{name}")
        kpi = KpiDoc(id=kid, title=title, metric=ref, sensitivity=info.sensitivity, provenance=provenance, labels=labels)  # type: ignore[arg-type]
        doc = doc.model_copy(update={"kpis": [*doc.kpis, kpi]})
        return _place(doc, kid, None, None, (3, 2))
    x = op.dimensions[0] if op.dimensions else None
    chart = ChartDoc(id=_unique_id(doc, f"c_{name}"), title=title, type=op.viz, dataset_ref=ref, fields=fields,
                     encoding=Encoding(x=x, y=[value_field], series=op.dimensions[1] if len(op.dimensions) > 1 else None),
                     sensitivity=info.sensitivity, provenance=provenance, labels=labels)  # type: ignore[arg-type]
    if chart.type != "table" and suitability(chart.type, chart):
        raise _Refused(f"{suitability(chart.type, chart)}", {"op": "metric.bind", "metric": op.metric,
                                                              "viz": suggest_type(chart), "dimensions": op.dimensions})
    doc = _rescope_filters(doc.model_copy(update={"charts": [*doc.charts, chart]}), chart)
    return _place(doc, chart.id, None, None, (6, 4))


def _rescope_filters(doc: DashboardDoc, new: ChartDoc) -> DashboardDoc:
    """A dashboard-wide filter keeps applying to the charts it applied to; a new chart without its field is
    left out of its scope instead of making the batch invalid."""
    filters = []
    for f in doc.filters:
        if not f.scope and new.field(f.field) is None:
            f = f.model_copy(update={"scope": [c.id for c in doc.charts if c.id != new.id and c.field(f.field)]})
        filters.append(f)
    return doc.model_copy(update={"filters": filters})


def _unique_id(doc: DashboardDoc, base: str) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")[:56] or "el"
    if not base[0].isalpha():
        base = f"e_{base}"
    taken = set(doc.element_ids()) | {t.id for t in doc.tabs} | {f.id for f in doc.filters}
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    return out


unique_id = _unique_id  # a free element id for callers that build an element before the op (CONV-03)


def _add_chart(doc: DashboardDoc, op: ChartAdd, env: OpEnv) -> DashboardDoc:
    if op.chart.id in set(doc.element_ids()):
        raise _Refused(f"an element {op.chart.id!r} already exists")
    if op.tab is not None and op.tab not in {t.id for t in doc.tabs}:
        raise _Refused(f"no tab {op.tab!r}")
    doc = _rescope_filters(doc.model_copy(update={"charts": [*doc.charts, op.chart]}), op.chart)
    return _place(doc, op.chart.id, op.position, op.tab, (6, 4))


def _from_question(doc: DashboardDoc, op: ChartFromQuestion, env: OpEnv) -> tuple[DashboardDoc, dict[str, Any]]:
    if env.answer_question is None:
        raise _Refused("charts from questions are not available here; add a metric or a saved chart instead")
    if op.tab is not None and op.tab not in {t.id for t in doc.tabs}:
        raise _Refused(f"no tab {op.tab!r}")
    try:
        built = env.answer_question(op, doc)
    except NotGrounded as exc:
        raise _Refused(exc.reason, exc.suggestion()) from None
    element = built.element
    taken = set(doc.element_ids()) | {t.id for t in doc.tabs} | {f.id for f in doc.filters}
    if element.id in taken:
        element = element.model_copy(update={"id": _unique_id(doc, element.id)})
    if isinstance(element, KpiDoc):
        doc = doc.model_copy(update={"kpis": [*doc.kpis, element]})
        return _place(doc, element.id, op.position, op.tab, (3, 2)), {"element": element.id, **built.summary}
    if element.type != "table" and suitability(element.type, element):
        raise _Refused(f"{suitability(element.type, element)}",
                       {"op": "chart.from_question", "question": op.question, "type": suggest_type(element)})
    doc = _rescope_filters(doc.model_copy(update={"charts": [*doc.charts, element]}), element)
    w, h = (3, 2) if element.type == "kpi" else (6, 4)
    return _place(doc, element.id, op.position, op.tab, (w, h)), {"element": element.id, **built.summary}


def _remove_chart(doc: DashboardDoc, op: ChartRemove, env: OpEnv) -> DashboardDoc:
    _chart(doc, op.chart_id)
    filters = []
    for f in doc.filters:
        if op.chart_id in f.scope:
            scope = [c for c in f.scope if c != op.chart_id]
            if not scope:
                continue  # a filter scoped only to the removed chart goes with it
            f = f.model_copy(update={"scope": scope})
        filters.append(f)
    items = [i for i in doc.layout.items if i.ref != op.chart_id]
    return doc.model_copy(update={"charts": [c for c in doc.charts if c.id != op.chart_id], "filters": filters,
                                  "layout": doc.layout.model_copy(update={"items": items})})


def _move(doc: DashboardDoc, op: LayoutMove, env: OpEnv) -> DashboardDoc:
    current = next((i for i in doc.layout.items if i.ref == op.ref), None)
    if current is None:
        raise _Refused(f"{op.ref!r} is not on the dashboard")
    w, h = op.w or current.w, op.h or current.h
    tab = op.tab if op.tab is not None else current.tab
    if op.x + w > GRID_COLUMNS:
        raise _Refused(f"{op.ref} would span past column {GRID_COLUMNS}",
                       {"op": "layout.move", "ref": op.ref, "x": max(0, GRID_COLUMNS - w), "y": op.y})
    moved = LayoutItem(ref=op.ref, x=op.x, y=op.y, w=w, h=h, tab=tab)
    others = [i for i in doc.layout.items if i.ref != op.ref]
    cells = {(i.tab, i.x + dx, i.y + dy): i.ref for i in others for dx in range(i.w) for dy in range(i.h)}
    clash = sorted({cells[(tab, op.x + dx, op.y + dy)] for dx in range(w) for dy in range(h)
                    if (tab, op.x + dx, op.y + dy) in cells})
    if clash:
        stub = doc.model_copy(update={"layout": doc.layout.model_copy(update={"items": others})})
        fx, fy = _free_slot(stub, w, h, tab)
        raise _Refused(f"{op.ref} would overlap {', '.join(clash)}", {"op": "layout.move", "ref": op.ref, "x": fx, "y": fy})
    items = [moved if i.ref == op.ref else i for i in doc.layout.items]
    return doc.model_copy(update={"layout": doc.layout.model_copy(update={"items": items})})


def _set_text(doc: DashboardDoc, op: TextSet, env: OpEnv) -> DashboardDoc:
    values = list((env.known_numbers or _doc_numbers)(doc))
    unbound = numbers_guard(op.markdown, values)
    if unbound:
        raise _Refused(f"the text quotes numbers no chart or KPI on this dashboard shows: {', '.join(unbound[:5])}")
    block = next((t for t in doc.text_blocks if t.id == op.block_id), None)
    if block is not None:
        blocks = [TextBlock(id=t.id, markdown=op.markdown) if t.id == op.block_id else t for t in doc.text_blocks]
        return doc.model_copy(update={"text_blocks": blocks})
    if op.block_id in set(doc.element_ids()):
        raise _Refused(f"{op.block_id!r} is a chart or KPI, not a text block")
    doc = doc.model_copy(update={"text_blocks": [*doc.text_blocks, TextBlock(id=op.block_id, markdown=op.markdown)]})
    return _place(doc, op.block_id, None, None, (12, 2))


_APPLY: dict[str, Callable[[DashboardDoc, Any, OpEnv], DashboardDoc]] = {
    "chart.set_type": _set_type, "chart.set_encoding": _set_encoding, "dashboard.add_filter": _add_filter,
    "query.extend_period": _extend_period, "metric.bind": _bind_metric, "chart.add": _add_chart,
    "chart.remove": _remove_chart, "layout.move": _move, "text.set": _set_text,
}


def apply_ops(doc: DashboardDoc, ops: list[Any], env: OpEnv) -> OpResult:
    """Validate and apply `ops` in order, atomically. Sensitivity is re-derived after the batch and the
    whole result must pass the validator; otherwise nothing is applied."""
    current = doc
    applied: list[dict[str, Any]] = []
    refused: list[Refusal] = []
    deferred: list[PublishRequest] = []
    for i, op in enumerate(ops):
        if isinstance(op, PublishRequest):
            deferred.append(op)
            continue
        extra: dict[str, Any] = {}
        try:
            if isinstance(op, ChartFromQuestion):
                current, extra = _from_question(current, op, env)
            else:
                current = _APPLY[op.op](current, op, env)
        except _Refused as exc:
            refused.append(Refusal(i, op.op, exc.reason, exc.suggestion))
            continue
        except ValidationError as exc:
            refused.append(Refusal(i, op.op, exc.errors()[0]["msg"]))
            continue
        applied.append({**op.model_dump(mode="json", exclude_none=True), **({"result": extra} if extra else {})})
    if refused:
        return OpResult(doc, [], refused, deferred)
    current = derive_sensitivity(current)
    issues = problems(current)
    if issues:
        return OpResult(doc, [], [Refusal(len(ops) - 1, "batch", "; ".join(p.message for p in issues[:5]))], deferred)
    return OpResult(current, applied, [], deferred)

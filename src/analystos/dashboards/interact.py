"""Cross-filtering and drill-down in the in-platform dashboard preview (CONV-06).

Exploring a dashboard is a *view*, never an edit: no version is created. "Apply as dashboard filter" is the one
path from a view filter to the document, and it goes through the typed `dashboard.add_filter` op like any edit.

Every number a viewer sees after a click is a governed re-query of the tile's own dataset, as the viewer:

* the client sends names and values only (`field`, `op` eq/in, `value`), never SQL or a predicate. The field must
  be bound by a chart on this dashboard and categorical; the value resolves through `dashboards/env.py` exactly as a
  typed filter does (glossary, geography, profiled vocabulary, the previews) or the request is refused;
* the tile's SQL comes from where it was built: the semantic compiler for an approved `metric@v` (the filter is a
  `SemanticFilter`, compiled under the viewer's scope), else the verified query or the recorded gateway execution
  (`bundle.Resolver`), wrapped with predicates built from sqlglot nodes and `skills/sqlbuild` literals;
* every statement runs through `QueryGateway.execute` with the viewer's resolved scope, so denied columns, row
  filters, ABAC and budgets apply as usual. Charts that do not carry the field are left alone and say so.

Drill-down is one level per click: a time axis truncated to a grain (`DATE_TRUNC`) re-grains year → quarter → month
→ day inside the window of the clicked member; a metric chart walks the metric's approved dimensions (the semantic
model's drill path). "Show rows" lists the rows behind a member: the dataset's own FROM and WHERE with the member's
predicates, projecting only columns the viewer may read, at most `DETAIL_ROWS`.

Bounds (ADR-014): previews are `MAX_PREVIEW_ROWS` inline rows, at most `MAX_TILE_QUERIES` statements per request,
identical statements run once, at most `MAX_CONCURRENT` statements per dashboard at a time in this process (others
wait `GATE_WAIT_SECONDS`, then report busy) and the gateway's result cache answers repeated clicks.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from analystos.contracts.dashboard_dsl import MAX_PREVIEW_ROWS, ChartDoc, DashboardDoc, FieldDef, FilterDoc, KpiDoc
from analystos.core.errors import AnalystOSError, Conflict, InvalidInput, NotFound
from analystos.core.logging import get_logger
from analystos.skills import sqlbuild

log = get_logger(__name__)

MAX_VIEW_FILTERS = 5
MAX_IN_VALUES = 50
MAX_TILE_QUERIES = 24
MAX_CONCURRENT = 4
GATE_WAIT_SECONDS = 15.0
MAX_DRILL_DEPTH = 4
DETAIL_ROWS = 100
DETAIL_COLUMNS = 30
CATEGORICAL = frozenset({"nominal", "ordinal", "boolean"})
TIME_LEVELS = ("year", "quarter", "month", "day")
_FINER = {"year": "quarter", "quarter": "month", "month": "day", "week": "day"}
_SEMANTIC_OP = {"eq": "=", "neq": "!=", "in": "in", "not_in": "not_in", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
Scalar = str | int | float | bool


# ------------------------------------------------------------------------------------------- request shapes
class ViewFilterIn(BaseModel):
    """One click or chip: a field of the dashboard and the member(s) chosen. `source` is the chart it came from
    (that chart is not filtered by its own selection)."""

    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=200)
    op: Literal["eq", "in"] = "eq"
    value: Scalar | list[Scalar] | None = None
    source: str | None = Field(default=None, max_length=64)


class DrillStepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=200)
    value: Scalar | None = None


@dataclass
class ViewFilter:
    field: str
    op: str
    value: Any
    label: str
    via: str
    note: str = ""
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value, "label": self.label, "via": self.via,
                **({"note": self.note} if self.note else {}), **({"source": self.source} if self.source else {})}


class _Unavailable(Exception):
    """A drill or detail this chart cannot offer, in plain words."""


# ------------------------------------------------------------------------------------------- field binding
def canon(name: str) -> str:
    """`orders.priority` and `priority` name the same field for cross-filtering."""
    return name.rsplit(".", 1)[-1].strip().lower()


def chart_field(chart: ChartDoc, name: str) -> FieldDef | None:
    """The chart's field for `name`: an exact match, else the one field with the same last segment."""
    exact = chart.field(name)
    if exact is not None:
        return exact
    hits = [f for f in chart.fields if canon(f.name) == canon(name)]
    return hits[0] if len(hits) == 1 else None


def _label(f: FieldDef | None, name: str) -> str:
    return (f.label if f is not None and f.label else canon(name).replace("_", " ").capitalize()) or name


def metric_definition(catalog: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    entry = ((catalog or {}).get("metrics") or {}).get(name)
    return entry if isinstance(entry, dict) else None


def metric_dimension(catalog: dict[str, Any] | None, metric: str, name: str, *, time: bool = False) -> str | None:
    """The approved dimension of `metric` that `name` refers to (categorical unless `time`)."""
    from analystos.semantic.compiler import _is_time

    entry = metric_definition(catalog, metric)
    if entry is None:
        return None
    defn = entry.get("definition") or {}
    for dim in defn.get("dimensions") or []:
        if isinstance(dim, str) and canon(dim) == canon(name) and _is_time(catalog or {}, defn, dim) == time:
            return dim
    return None


# ------------------------------------------------------------------------------------------- predicates
def _condition(target: exp.Expression, op: str, value: Any, dialect: str) -> exp.Expression:
    """A predicate on `target`; values are sqlbuild literals, never SQL text."""
    if op in ("in", "not_in"):
        values = value if isinstance(value, list) else [value]
        node: exp.Expression = exp.In(this=target, expressions=[sqlbuild.lit(v, dialect) for v in values])
        return exp.Not(this=node) if op == "not_in" else node
    if op == "between":
        return exp.Between(this=target, low=sqlbuild.lit(value[0], dialect), high=sqlbuild.lit(value[1], dialect))
    if value is None and op in ("eq", "neq"):
        return sqlbuild.not_null(target) if op == "neq" else sqlbuild.is_null(target)
    klass = {"eq": exp.EQ, "neq": exp.NEQ, "gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}[op]
    return klass(this=target, expression=sqlbuild.lit(value, dialect))


def _window(target: exp.Expression, start: datetime, end: datetime, dialect: str) -> exp.Expression:
    def ts(v: datetime) -> exp.Expression:
        return sqlbuild.cast(exp.Literal.string(v.isoformat(sep=" ")), "timestamp", dialect)

    return exp.And(this=exp.GTE(this=target.copy(), expression=ts(start)), expression=exp.LT(this=target.copy(), expression=ts(end)))


def wrap_filters(sql: str, conditions: list[tuple[str, str, Any]], dialect: str) -> str:
    """`SELECT * FROM (<sql>) AS "f" WHERE ...` over output columns, like the publish bundle's dashboard filters."""
    if not conditions:
        return sql
    inner = exp.Subquery(this=sqlglot.parse_one(sql, read=dialect), alias=exp.TableAlias(this=sqlbuild.ident("f")))
    cond = sqlbuild.and_all([_condition(sqlbuild.col(name), op, value, dialect) for name, op, value in conditions])
    return exp.select("*").from_(inner).where(cond).sql(dialect=dialect)


# ------------------------------------------------------------------------------------------- time members
def parse_member(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            pass
    raise InvalidInput(f"{value!r} is not a date this time axis can drill into")


def member_window(value: Any, grain: str) -> tuple[datetime, datetime]:
    """[start, end) of the `grain` period that contains `value`."""
    d = parse_member(value)
    if grain == "year":
        start = datetime(d.year, 1, 1)
        return start, datetime(d.year + 1, 1, 1)
    if grain in ("quarter", "month"):
        first = d.month - (d.month - 1) % 3 if grain == "quarter" else d.month
        start = datetime(d.year, first, 1)
        months = 3 if grain == "quarter" else 1
        y, m = divmod(first - 1 + months, 12)
        return start, datetime(d.year + y, m + 1, 1)
    if grain == "week":
        start = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
        return start, start + timedelta(days=7)
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)


def member_label(value: Any, grain: str) -> str:
    try:
        d = parse_member(value)
    except InvalidInput:
        return str(value)
    if grain == "year":
        return str(d.year)
    if grain == "quarter":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if grain == "month":
        return d.strftime("%b %Y")
    return d.date().isoformat()


# ------------------------------------------------------------------------------------------- SQL shapes
def _select(sql: str, dialect: str) -> exp.Select:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise _Unavailable("its query cannot be read for drilling") from exc
    if not isinstance(tree, exp.Select):
        raise _Unavailable("its query combines several results")
    return tree


def _projection(tree: exp.Select, alias: str) -> exp.Expression | None:
    for p in tree.expressions:
        if p.alias_or_name == alias:
            return p
    low = canon(alias)
    hits = [p for p in tree.expressions if canon(p.alias_or_name) == low]
    return hits[0] if len(hits) == 1 else None


def _trunc(node: exp.Expression | None) -> exp.TimestampTrunc | None:
    inner = node.unalias() if node is not None else None
    return inner if isinstance(inner, exp.TimestampTrunc | exp.DateTrunc) else None  # type: ignore[return-value]


def time_grain(sql: str, dialect: str, x: str) -> str | None:
    """The grain the chart's time axis is truncated to in its query (year, quarter, month, week, day), if any."""
    try:
        trunc = _trunc(_projection(_select(sql, dialect), x))
    except _Unavailable:
        return None
    unit = trunc.args.get("unit") if trunc is not None else None
    grain = (unit.name if isinstance(unit, exp.Expression) else str(unit or "")).strip("'\"").lower()
    return grain if grain in (*TIME_LEVELS, "week") else None


def regrain(sql: str, dialect: str, x: str, grain: str, window: tuple[datetime, datetime] | None) -> str:
    """The same query with its time axis truncated to `grain` and restricted to `window` (the clicked member)."""
    tree = _select(sql, dialect)
    proj = _projection(tree, x)
    old = _trunc(proj)
    if proj is None or old is None:
        raise _Unavailable("its time axis is not grouped by a period")
    raw = old.this.copy()
    new = exp.TimestampTrunc(this=raw.copy(), unit=exp.var(grain.upper()))
    before = old.copy()
    old.replace(new)
    for key in ("group", "order"):
        clause = tree.args.get(key)
        for node in list(clause.find_all(type(before)) if clause is not None else []):
            if node == before:
                node.replace(new.copy())
    if window is not None:
        tree = tree.where(_window(raw, *window, dialect))
    return tree.sql(dialect=dialect)


@dataclass
class Detail:
    sql: str
    columns: list[str]
    hidden: int


def _denied(scope: Any, asset: str, column: str) -> bool:
    from analystos.semantic.compiler import _denied as denied

    return denied(scope, f"{asset}.{column}")


def _asset(scope: Any, table: exp.Table) -> str | None:
    want = f"{table.db}.{table.name}".lower() if table.db else table.name.lower()
    hits = [a for a in scope.assets if a.lower() == want or (not table.db and a.split(".", 1)[-1].lower() == want)]
    return hits[0] if len(hits) == 1 else None


def detail_sql(sql: str, dialect: str, conditions: list[tuple[str, str, Any, tuple[datetime, datetime] | None]],
               scope: Any) -> Detail:
    """Row-level detail for a member: the dataset's FROM and WHERE, the member's predicates on the expressions the
    chart groups by, and only columns the viewer may read. Joins, set operations and stars over subqueries are
    refused (the rows' grain would not be the table's)."""
    tree = _select(sql, dialect)
    if tree.args.get("joins"):
        raise _Unavailable("it combines several tables")
    source = tree.args.get("from_") or tree.args.get("from")
    base = source.this if source is not None else None
    hidden = 0
    if isinstance(base, exp.Table):
        asset = _asset(scope, base)
        if asset is None:
            raise _Unavailable("its table is not in your access scope")
        names = list(scope.columns.get(asset) or [])
        visible = [c for c in names if not _denied(scope, asset, c)]
        hidden = len(names) - len(visible)
    elif isinstance(base, exp.Subquery) and isinstance(base.this, exp.Select) \
            and not any(isinstance(p, exp.Star) for p in base.this.expressions):
        visible = [n for n in base.this.named_selects if n]
        inner_from = base.this.args.get("from_") or base.this.args.get("from")
        inner = inner_from.this if inner_from is not None else None
        if isinstance(inner, exp.Table) and (asset := _asset(scope, inner)) is not None:
            hidden = sum(1 for c in scope.columns.get(asset) or [] if _denied(scope, asset, c))
    else:
        raise _Unavailable("its rows do not come from one table")
    if not visible:
        raise _Unavailable("you may not see any of its columns")
    where: list[exp.Expression] = []
    if tree.args.get("where") is not None:
        where.append(tree.args["where"].this.copy())
    for alias, op, value, window in conditions:
        proj = _projection(tree, alias)
        if proj is None:
            raise _Unavailable(f"{alias} is not one of its groupings")
        target = proj.unalias()
        if any(target.find_all(exp.AggFunc)):
            raise _Unavailable(f"{alias} is a calculated total")
        trunc = _trunc(target)
        if window is not None:
            where.append(_window(trunc.this if trunc is not None else target, *window, dialect))
        else:
            where.append(_condition(target.copy(), op, value, dialect))
    cols = visible[:DETAIL_COLUMNS]
    out = exp.select(*[sqlbuild.col(c) for c in cols]).from_(base.copy())
    cond = sqlbuild.and_all(where)
    if cond is not None:
        out = out.where(cond)
    if tree.args.get("with") is not None:
        out.set("with", tree.args["with"].copy())
    return Detail(out.limit(DETAIL_ROWS).sql(dialect=dialect), cols, hidden + max(0, len(visible) - len(cols)))


# ------------------------------------------------------------------------------------------- concurrency gate
_GATES: OrderedDict[str, threading.BoundedSemaphore] = OrderedDict()
_GATES_LOCK = threading.Lock()
_MAX_GATES = 512


def gate_for(dashboard_id: str) -> threading.BoundedSemaphore:
    """At most MAX_CONCURRENT statements per dashboard at a time in this process (a bounded map: ADR-014)."""
    with _GATES_LOCK:
        gate = _GATES.pop(dashboard_id, None) or threading.BoundedSemaphore(MAX_CONCURRENT)
        _GATES[dashboard_id] = gate
        while len(_GATES) > _MAX_GATES:
            _GATES.popitem(last=False)
        return gate


class Busy(Exception):
    pass


# ------------------------------------------------------------------------------------------- the explorer
@dataclass
class Explorer:
    """One viewer exploring one dashboard version. Everything that touches the platform is injected, so the
    rules can be tested without services: `dataset_sql` = `bundle.Resolver.sql`, `resolve_value` = the typed
    filter resolver, `catalog` = the approved semantic catalog, `gateway.execute` = the one way to run SQL."""

    doc: DashboardDoc
    scope: Any
    actor: str
    gateway: Any
    resolve_value: Callable[[str, Any], Any]
    dataset_sql: Callable[[Any], tuple[str, str, list[str]]]
    catalog: dict[str, Any] | None = None
    gate: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(MAX_CONCURRENT))
    version: int = 1

    # ---------------------------------------------------------------- validation
    def bound_charts(self, name: str) -> list[ChartDoc]:
        return [c for c in self.doc.charts if chart_field(c, name) is not None]

    def field_def(self, name: str) -> FieldDef | None:
        for c in self.doc.charts:
            f = chart_field(c, name)
            if f is not None:
                return f
        return None

    def resolve(self, name: str, value: Any) -> Any:
        """A value through the typed-filter resolver; a dotted semantic name falls back to its column name when the
        dotted spelling has no vocabulary of its own."""
        r = self.resolve_value(name, value)
        if r is not None and r.via == "unverified" and canon(name) != name:
            return self.resolve_value(canon(name), value)
        return r

    def validate(self, raw: list[ViewFilterIn]) -> list[ViewFilter]:
        """Never trust the client: the field must be bound by a chart here and categorical, the value known."""
        if len(raw) > MAX_VIEW_FILTERS:
            raise InvalidInput(f"at most {MAX_VIEW_FILTERS} filters at a time")
        out: list[ViewFilter] = []
        seen: set[str] = set()
        for f in raw:
            fdef = self.field_def(f.field)
            if fdef is None:
                raise InvalidInput(f"{f.field!r} is not a field of this dashboard")
            if fdef.type not in CATEGORICAL:
                raise InvalidInput(f"{_label(fdef, f.field)} is a {fdef.type} field: drill into it instead of filtering"
                                   if fdef.type == "temporal" else f"{_label(fdef, f.field)} is a measure, not a filter")
            if canon(f.field) in seen:
                raise InvalidInput(f"{_label(fdef, f.field)} is filtered twice")
            seen.add(canon(f.field))
            if f.source is not None and self.doc.chart(f.source) is None:
                raise InvalidInput(f"no chart {f.source!r} on this dashboard")
            values = f.value if isinstance(f.value, list) else [f.value]
            if f.op == "in" and not 1 <= len(values) <= MAX_IN_VALUES:
                raise InvalidInput(f"choose 1 to {MAX_IN_VALUES} values")
            if f.op == "eq" and isinstance(f.value, list):
                raise InvalidInput("one value for 'eq'; use 'in' for several")
            resolved, vias, notes = [], [], []
            for v in values:
                if v is None:
                    resolved.append(None)
                    vias.append("literal")
                    continue
                r = self.resolve(fdef.name, v)
                if r is None:
                    raise InvalidInput(f"{v!r} is not a known value of {_label(fdef, f.field)}")
                resolved.append(r.value)
                vias.append(r.via)
                if r.note:
                    notes.append(r.note)
            value = resolved if f.op == "in" else resolved[0]
            via = "vocabulary" if set(vias) <= {"vocabulary"} else next(v for v in vias if v != "vocabulary")
            out.append(ViewFilter(field=fdef.name, op=f.op, value=value, label=_label(fdef, f.field), via=via,
                                  note="; ".join(notes), source=f.source))
        return out

    # ---------------------------------------------------------------- statements
    def _approved(self, metric: Any) -> dict[str, Any]:
        entry = metric_definition(self.catalog, metric.name)
        if not self.catalog or entry is None:
            raise InvalidInput(f"metric {metric.label} is not approved in this workspace")
        if int(entry["version"]) != int(metric.version):
            raise InvalidInput(f"metric {metric.label} is not the approved version (v{entry['version']}); rebind the chart")
        return entry

    def _dialect(self, assets: list[str], fallback: str) -> str:
        sources = {self.scope.asset_sources.get(a) for a in assets if a in self.scope.asset_sources}
        if len(sources) == 1:
            return self.scope.source_dialects.get(next(iter(sources)), fallback)  # type: ignore[arg-type]
        return fallback

    def _semantic(self, metric: Any, dimensions: list[str], filters: list[tuple[str, str, Any]], *,
                  limit: int = MAX_PREVIEW_ROWS) -> tuple[str, str]:
        from analystos.contracts.semantic import SemanticFilter, SemanticQuery
        from analystos.semantic.compiler import _dialect, compile_query

        self._approved(metric)
        sem: list[SemanticFilter] = []
        for name, op, value in filters:
            if op == "between":
                sem += [SemanticFilter(field=name, op=">=", value=value[0]), SemanticFilter(field=name, op="<=", value=value[1])]
            elif value is None and op in ("eq", "neq"):
                sem.append(SemanticFilter(field=name, op="is_null" if op == "eq" else "is_not_null"))
            else:
                sem.append(SemanticFilter(field=name, op=_SEMANTIC_OP[op], value=value))  # type: ignore[arg-type]
        query = SemanticQuery(metrics=[metric.name], dimensions=list(dict.fromkeys(dimensions)), filters=sem, limit=limit)
        compiled = compile_query(query, self.catalog, self.scope)  # type: ignore[arg-type]
        return compiled.sql, _dialect(self.scope)

    def saved_filters(self, chart: ChartDoc) -> list[FilterDoc]:
        """The document's own value filters, exactly as the publish bundle applies them."""
        return [f for f in self.doc.filters if f.value is not None and (not f.scope or chart.id in f.scope)
                and chart.field(f.field) is not None]

    def chart_sql(self, chart: ChartDoc, view: list[ViewFilter], *, dimensions: list[str] | None = None,
                  extra: list[tuple[str, str, Any]] | None = None, limit: int = MAX_PREVIEW_ROWS,
                  transform: Callable[[str, str], str] | None = None) -> tuple[str, str]:
        """The tile's governed statement with the saved filters, `view` and `extra` (semantic field, op, value).
        `transform` rewrites the dataset statement before any filter wraps it (a time drill re-grains it)."""
        from analystos.dashboards.bundle import _wrap_compare

        saved = self.saved_filters(chart)
        ref = chart.dataset_ref
        if ref.kind == "metric":
            conds: list[tuple[str, str, Any]] = []
            for f in saved:
                dim = metric_dimension(self.catalog, ref.name, f.field) or metric_dimension(self.catalog, ref.name, f.field, time=True)
                if dim is None:
                    raise InvalidInput(f"the saved filter on {f.field} is not a dimension of {ref.label}")
                conds.append((dim, f.op, f.value))
            for v in view:
                dim = metric_dimension(self.catalog, ref.name, v.field)
                if dim is None:
                    raise InvalidInput(f"{v.label} is not a dimension of {ref.label}")
                conds.append((dim, v.op, v.value))
            sql, dialect = self._semantic(ref, dimensions if dimensions is not None else list(ref.dimensions),
                                          conds + list(extra or []), limit=limit)
        else:
            sql, fallback, assets = self.dataset_sql(ref)
            dialect = self._dialect(assets, fallback)
            sql = transform(sql, dialect) if transform is not None else sql
            names = [(chart.field(f.field).name, f.op, f.value) for f in saved]  # type: ignore[union-attr]
            names += [(chart_field(chart, v.field).name, v.op, v.value) for v in view]  # type: ignore[union-attr]
            sql = wrap_filters(sql, names, dialect)
        return _wrap_compare(sql, chart, dialect), dialect

    def kpi_sql(self, kpi: KpiDoc, view: list[ViewFilter]) -> tuple[str, str]:
        conds = [(metric_dimension(self.catalog, kpi.metric.name, v.field), v.op, v.value) for v in view]
        return self._semantic(kpi.metric, [], [c for c in conds if c[0]], limit=1)  # type: ignore[misc]

    def applies(self, chart: ChartDoc | KpiDoc, view: list[ViewFilter]) -> list[ViewFilter]:
        out = []
        for v in view:
            if v.source == chart.id:
                continue
            if isinstance(chart, KpiDoc):
                if metric_dimension(self.catalog, chart.metric.name, v.field):
                    out.append(v)
            elif chart_field(chart, v.field) is not None or (
                    chart.dataset_ref.kind == "metric" and metric_dimension(self.catalog, chart.dataset_ref.name, v.field)):
                out.append(v)
        return out

    def run(self, statements: dict[str, str], purpose: str, *, max_rows: int = MAX_PREVIEW_ROWS) -> dict[str, Any]:
        """Each distinct statement once, through the gateway, at most MAX_CONCURRENT per dashboard at a time."""
        from analystos.tenancy.background import carry_context

        unique = list(dict.fromkeys(statements.values()))

        def one(sql: str) -> Any:
            if not self.gate.acquire(timeout=GATE_WAIT_SECONDS):
                return Busy()
            try:
                return self.gateway.execute(self.scope, sql, actor=self.actor, purpose=purpose, max_rows=max_rows)
            except AnalystOSError as exc:
                return exc
            except Exception as exc:  # noqa: BLE001 - one tile's failure must not hide the others
                log.warning("dashboard.requery_failed", error=exc.__class__.__name__)
                return exc
            finally:
                self.gate.release()

        if len(unique) <= 1:
            results = {sql: one(sql) for sql in unique}
        else:
            with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(unique)), thread_name_prefix="dash-view") as pool:
                futures = {sql: pool.submit(carry_context(one), sql) for sql in unique}
                results = {sql: fut.result() for sql, fut in futures.items()}
        return {key: results[sql] for key, sql in statements.items()}

    # ---------------------------------------------------------------- view (cross-filter)
    def view(self, raw: list[ViewFilterIn], *, refresh_saved: bool = False) -> dict[str, Any]:
        from analystos.dashboards.questions import bounded_preview

        view = self.validate(raw)
        tiles: dict[str, dict[str, Any]] = {}
        statements: dict[str, str] = {}
        for chart in self.doc.charts:
            applied = self.applies(chart, view)
            mine = [v for v in view if v.source == chart.id]
            if not applied and not (refresh_saved and self.saved_filters(chart)):
                status = "source" if mine else "not_filtered"
                names = ", ".join(v.label for v in view if v.source != chart.id)
                tiles[chart.id] = {"id": chart.id, "kind": "chart", "status": status,
                                   **({"reason": f"This chart has no {names} field."} if status == "not_filtered" and names else {})}
                continue
            tiles[chart.id] = {"id": chart.id, "kind": "chart", "status": "filtered" if applied else "saved_filter",
                               "filtered_by": [v.label for v in applied]}
            self._plan(tiles[chart.id], statements, lambda c=chart, a=applied: self.chart_sql(c, a))
        for kpi in self.doc.kpis:
            applied = self.applies(kpi, view)
            if not applied:
                tiles[kpi.id] = {"id": kpi.id, "kind": "kpi", "status": "not_filtered",
                                 **({"reason": "This figure has no " + ", ".join(v.label for v in view) + " breakdown."} if view else {})}
                continue
            tiles[kpi.id] = {"id": kpi.id, "kind": "kpi", "status": "filtered", "filtered_by": [v.label for v in applied]}
            self._plan(tiles[kpi.id], statements, lambda k=kpi, a=applied: self.kpi_sql(k, a))
        for tile_id, result in self.run(statements, "dashboard.cross_filter").items():
            tile = tiles[tile_id]
            if self._failed(tile, result):
                continue
            tile.update(query_id=result.query_id, cache_hit=bool(getattr(result, "cache_hit", False)))
            if tile["kind"] == "kpi":
                first = result.rows[0][0] if result.rows and result.rows[0] else None
                tile["value"] = float(first) if isinstance(first, int | float) and not isinstance(first, bool) else first
            else:
                tile["preview"] = bounded_preview(result.columns, result.rows, result.row_count)
        return {"version": self.version, "filters": [v.as_dict() for v in view],
                "tiles": [tiles[i] for i in self.doc.element_ids() if i in tiles],
                "apply": [self.apply_op(v) for v in view],
                "limits": {"max_concurrent": MAX_CONCURRENT, "max_queries": MAX_TILE_QUERIES}}

    def _plan(self, tile: dict[str, Any], statements: dict[str, str], build: Callable[[], tuple[str, str]]) -> None:
        if len(statements) >= MAX_TILE_QUERIES:
            tile.update(status="skipped", reason=f"Only {MAX_TILE_QUERIES} charts are refreshed at a time.")
            return
        try:
            statements[tile["id"]], _ = build()
        except AnalystOSError as exc:
            tile.update(status="refused", reason=exc.message)
        except _Unavailable as exc:
            tile.update(status="refused", reason=f"This chart cannot be filtered: {exc}.")

    @staticmethod
    def _failed(tile: dict[str, Any], result: Any) -> bool:
        if isinstance(result, Busy):
            tile.update(status="busy", reason="Too many charts are refreshing on this dashboard; try again in a moment.")
            return True
        if isinstance(result, AnalystOSError):
            tile.update(status="refused" if result.http_status in (400, 403, 404, 422) else "error", reason=result.message)
            return True
        if isinstance(result, Exception):
            tile.update(status="error", reason="The query for this chart failed.")
            return True
        return False

    def apply_op(self, v: ViewFilter) -> dict[str, Any]:
        """The typed op that saves this view filter on the dashboard (a new version, through the ops engine)."""
        scope = [c.id for c in self.doc.charts if c.field(v.field) is not None]
        return {"op": "dashboard.add_filter", "field": v.field, "operator": v.op, "value": v.value, "scope": scope}

    # ---------------------------------------------------------------- drill
    def drill_plan(self, chart: ChartDoc) -> dict[str, Any]:
        """What a click on this chart drills into: time levels, the metric's dimension path, or nothing."""
        x = chart.field(chart.encoding.x)
        ref = chart.dataset_ref
        if x is not None and x.type == "temporal":
            try:
                sql, fallback, assets = self.dataset_sql(ref) if ref.kind != "metric" else (None, "postgres", [])
            except AnalystOSError as exc:
                return {"kind": "none", "reason": exc.message}
            grain = time_grain(sql, self._dialect(assets, fallback), x.name) if sql else None
            if grain is None:
                return {"kind": "none", "levels": [{"field": x.name, "label": _label(x, x.name)}],
                        "reason": "The time axis is not grouped by a period."}
            levels, g = [grain], grain
            while g in _FINER:
                g = _FINER[g]
                levels.append(g)
            return {"kind": "time", "field": x.name, "levels": [{"field": x.name, "grain": lv, "label": lv.capitalize()} for lv in levels]}
        if ref.kind == "metric" and ref.dimensions and x is not None:
            entry = metric_definition(self.catalog, ref.name)
            if entry is None:
                return {"kind": "none", "reason": f"Metric {ref.label} is not approved."}
            from analystos.semantic.compiler import _is_time
            from analystos.semantic.compiler import _label as dim_label

            defn = entry.get("definition") or {}
            current = next((d for d in ref.dimensions if canon(d) == canon(x.name)), None)
            if current is None:
                return {"kind": "none", "reason": "The chart's grouping is not one of the metric's dimensions."}
            path = [current] + [d for d in defn.get("dimensions") or [] if isinstance(d, str) and d not in ref.dimensions
                                and not _is_time(self.catalog or {}, defn, d)]
            if len(path) < 2:
                return {"kind": "none", "reason": f"{ref.label} has no other dimension to drill into."}
            return {"kind": "dimension", "field": current,
                    "levels": [{"field": d, "label": str(dim_label(self.catalog or {}, defn, d)).replace("_", " ").capitalize()}
                               for d in path[:MAX_DRILL_DEPTH + 1]]}
        levels = [{"field": x.name, "label": _label(x, x.name)}] if x is not None else []
        return {"kind": "none", "levels": levels, "reason": "This chart has no hierarchy to drill into."}

    def _steps(self, chart: ChartDoc, plan: dict[str, Any], path: list[DrillStepIn]) -> list[dict[str, Any]]:
        levels = plan.get("levels") or []
        if len(path) > min(MAX_DRILL_DEPTH + 1, len(levels)):
            raise InvalidInput("cannot drill deeper than the last level")
        steps = []
        for i, step in enumerate(path):
            level = levels[i]
            if canon(step.field) != canon(level["field"]):
                raise InvalidInput(f"level {i + 1} of this drill path is {level['label']}, not {step.field}")
            if plan["kind"] == "time":
                start, end = member_window(step.value, level["grain"])
                steps.append({"field": level["field"], "value": start.isoformat(), "grain": level["grain"],
                              "label": member_label(start, level["grain"]), "window": (start, end)})
            else:
                value = step.value
                fdef = chart_field(chart, level["field"])
                if value is not None and (fdef is None or fdef.type in CATEGORICAL):
                    r = self.resolve(level["field"], value)
                    if r is None:
                        raise InvalidInput(f"{value!r} is not a known value of {level['label']}")
                    value = r.value
                steps.append({"field": level["field"], "value": value, "label": "(none)" if value is None else str(value)})
        return steps

    def drill(self, chart_id: str, path: list[DrillStepIn], raw: list[ViewFilterIn], *, detail: bool = False,
              preview: bool = True) -> dict[str, Any]:
        """One drill level for `path` (`preview`) and, with `detail`, the rows behind it."""
        from analystos.dashboards.questions import bounded_preview

        chart = self.doc.chart(chart_id)
        if chart is None:
            raise NotFound(f"no chart {chart_id!r} on this dashboard")
        view = [v for v in self.validate(raw) if v.source != chart.id]
        applied = self.applies(chart, view)
        plan = self.drill_plan(chart)
        steps = self._steps(chart, plan, path)
        levels = plan.get("levels") or []
        level = levels[len(steps)] if len(steps) < len(levels) else None
        out: dict[str, Any] = {"chart_id": chart.id, "kind": plan["kind"], "depth": len(steps),
                               "levels": levels, "path": [{k: v for k, v in s.items() if k != "window"} for s in steps],
                               "level": level, "filtered_by": [v.label for v in applied],
                               **({"reason": plan["reason"]} if plan.get("reason") else {})}
        statements: dict[str, str] = {}
        if preview and steps and level is not None:
            try:
                statements["preview"] = self._drilled_sql(chart, plan, steps, level, applied)
            except _Unavailable as exc:
                out.update(status="refused", reason=f"This chart cannot drill here: {exc}.")
            except AnalystOSError as exc:
                out.update(status="refused", reason=exc.message)
        if detail:
            try:
                d = self._detail(chart, plan, steps, applied)
                statements["detail"] = d.sql
                out["detail"] = {"columns": d.columns, "hidden_columns": d.hidden}
            except _Unavailable as exc:
                out["detail"] = {"unavailable": f"Rows cannot be listed for this chart: {exc}."}
            except AnalystOSError as exc:
                out["detail"] = {"unavailable": exc.message}
        results = self.run({"preview": statements["preview"]}, "dashboard.drill") if "preview" in statements else {}
        if "detail" in statements:
            results.update(self.run({"detail": statements["detail"]}, "dashboard.detail", max_rows=DETAIL_ROWS))
        if "preview" in results:
            tile: dict[str, Any] = {}
            if not self._failed(tile, results["preview"]):
                r = results["preview"]
                out.update(preview=bounded_preview(r.columns, r.rows, r.row_count), query_id=r.query_id)
            else:
                out.update(status=tile["status"], reason=tile["reason"])
        if "detail" in results:
            tile = {}
            r = results["detail"]
            if not self._failed(tile, r):
                out["detail"].update(rows=bounded_preview(r.columns, r.rows, r.row_count)["rows"], truncated=bool(r.truncated)
                                     or len(r.rows) >= DETAIL_ROWS, query_id=r.query_id)
            else:
                out["detail"] = {"unavailable": tile["reason"]}
        return out

    def _drilled_sql(self, chart: ChartDoc, plan: dict[str, Any], steps: list[dict[str, Any]], level: dict[str, Any],
                     view: list[ViewFilter]) -> str:
        if plan["kind"] == "time":
            window = steps[-1]["window"]
            sql, _ = self.chart_sql(chart, view, transform=lambda s, d: regrain(s, d, plan["field"], level["grain"], window))
            return sql
        extra = [(s["field"], "eq", s["value"]) for s in steps]
        sql, _ = self.chart_sql(chart, view, dimensions=[level["field"]], extra=extra)
        return sql

    def _detail(self, chart: ChartDoc, plan: dict[str, Any], steps: list[dict[str, Any]], view: list[ViewFilter]) -> Detail:
        ref = chart.dataset_ref
        conds: list[tuple[str, str, Any, tuple[datetime, datetime] | None]] = [
            (s["field"], "eq", s["value"], s.get("window")) for s in steps]
        if ref.kind == "metric":
            dims = list(dict.fromkeys([*ref.dimensions, *(s["field"] for s in steps)]))
            for v in view:
                dim = metric_dimension(self.catalog, ref.name, v.field)
                if dim is not None:
                    dims.append(dim)
                    conds.append((dim, v.op, v.value, None))
            for f in self.saved_filters(chart):
                dim = metric_dimension(self.catalog, ref.name, f.field)
                if dim is not None:
                    dims.append(dim)
                    conds.append((dim, f.op, f.value, None))
            sql, dialect = self._semantic(ref, list(dict.fromkeys(dims))[:10], [])
        else:
            sql, fallback, assets = self.dataset_sql(ref)
            dialect = self._dialect(assets, fallback)
            conds += [(chart.field(f.field).name, f.op, f.value, None) for f in self.saved_filters(chart)]  # type: ignore[union-attr]
            conds += [(chart_field(chart, v.field).name, v.op, v.value, None) for v in view]  # type: ignore[union-attr]
        return detail_sql(sql, dialect, conds, self.scope)

    # ---------------------------------------------------------------- what each chart offers
    def explore(self) -> dict[str, Any]:
        """Per chart: the fields a click filters by, the drill path and whether rows can be listed. No query runs."""
        charts = []
        for chart in self.doc.charts:
            filterable = [f.name for f in chart.fields if f.type in CATEGORICAL and f.name in ((chart.preview or {}).get("columns") or [])]
            charts.append({"id": chart.id, "filter_fields": filterable, "drill": self.drill_plan(chart)})
        kpis = [{"id": k.id, "dimensions": [d for d in ((metric_definition(self.catalog, k.metric.name) or {}).get("definition") or {})
                                            .get("dimensions") or [] if isinstance(d, str)]} for k in self.doc.kpis]
        return {"version": self.version, "charts": charts, "kpis": kpis,
                "limits": {"max_filters": MAX_VIEW_FILTERS, "max_concurrent": MAX_CONCURRENT, "detail_rows": DETAIL_ROWS}}


# ------------------------------------------------------------------------------------------- service entry points
def explorer(session: Any, user: Any, artifact_id: str, *, base_version: int | None = None, gateway: Any = None) -> Explorer:
    """The viewer's explorer for the dashboard's current version (any member may explore what they may view)."""
    from analystos.dashboards import service
    from analystos.dashboards.bundle import Resolver
    from analystos.dashboards.env import resolver
    from analystos.governance.policy import resolve_scope
    from analystos.semantic.compiler import load_catalog

    art = service.load(session, user, artifact_id)
    if base_version is not None and base_version != art.version:
        raise Conflict(f"dashboard is at version {art.version}, not {base_version}; reload and retry",
                       details={"version": art.version})
    doc = service.document(art)
    scope = resolve_scope(session, user, art.workspace_id, minimum_role="viewer")
    if gateway is None:
        from analystos.runtime.context import default_gateway

        gateway = default_gateway()
    return Explorer(doc=doc, scope=scope, actor=f"user:{user.id}", gateway=gateway,
                    resolve_value=resolver(session, art.workspace_id, doc),
                    dataset_sql=Resolver(session, user, art.workspace_id).sql,
                    catalog=load_catalog(session, art.workspace_id), gate=gate_for(art.id), version=art.version)

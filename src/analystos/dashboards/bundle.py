"""DSL dashboard → `PublishBundle` (the hash an approval binds to).

Each chart and KPI gets its own virtual dataset whose SQL comes only from governed sources: a verified
query (no parameters), a recorded gateway execution (result handle), or an approved metric compiled by
the semantic compiler under the requester's scope. Dashboard filters and period comparisons wrap that
SQL with sqlglot-built expressions (values are literals, never SQL text). Nothing here executes SQL.
"""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle
from analystos.contracts.dashboard_dsl import ChartDoc, DashboardDoc, Encoding, FieldDef, FilterDoc, KpiDoc, Provenance
from analystos.core.errors import InvalidInput

_INTENT = {"line": "trend", "area": "trend", "bar": "comparison", "stacked_bar": "comparison", "pie": "part_to_whole",
           "treemap": "part_to_whole", "kpi": "kpi", "scatter": "relationship", "heatmap": "relationship",
           "histogram": "distribution", "table": "detail"}
_AGG = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT", "count_distinct": "COUNT_DISTINCT",
        "none": "MAX"}
_ASSET = re.compile(r"^[A-Za-z_][\w$]*\.[A-Za-z_][\w$]*$")


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _condition(flt: FilterDoc) -> exp.Expression:
    col = exp.Column(this=_ident(flt.field))
    if flt.op in ("in", "not_in"):
        expr: exp.Expression = exp.In(this=col, expressions=[_literal(v) for v in flt.value])
        return exp.Not(this=expr) if flt.op == "not_in" else expr
    if flt.op == "between":
        return exp.Between(this=col, low=_literal(flt.value[0]), high=_literal(flt.value[1]))
    klass = {"eq": exp.EQ, "neq": exp.NEQ, "gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}[flt.op]
    return klass(this=col, expression=_literal(flt.value))


def _wrap_filters(sql: str, filters: list[FilterDoc], dialect: str) -> str:
    if not filters:
        return sql
    inner = exp.Subquery(this=sqlglot.parse_one(sql, read=dialect), alias=exp.TableAlias(this=_ident("f")))
    where = filters[0]
    cond = _condition(where)
    for f in filters[1:]:
        cond = exp.And(this=cond, expression=_condition(f))
    return exp.select("*").from_(inner).where(cond).sql(dialect=dialect)


def _wrap_compare(sql: str, chart: ChartDoc, dialect: str) -> str:
    """Add `<y>__yoy|pop` columns: the same measure one period earlier (pop: the previous row of the same
    series over time; yoy: the row one year earlier)."""
    compare = chart.query.compare
    if compare == "none":
        return sql
    t = next((f.name for f in chart.fields if f.type == "temporal" and f.name == chart.encoding.x), None) or next(
        (f.name for f in chart.fields if f.type == "temporal"), None)
    if t is None:
        raise InvalidInput(f"chart {chart.id} compares periods but has no time field")
    measures = [y for y in chart.encoding.y if not y.endswith(f"__{compare}")]
    keys = [k for k in (chart.encoding.series, chart.encoding.color) if k]
    base = sqlglot.parse_one(sql, read=dialect)
    if compare == "pop":
        window = lambda y: exp.Window(this=exp.Anonymous(this="LAG", expressions=[exp.column(y, "b", quoted=True)]),  # noqa: E731
                                      partition_by=[exp.column(k, "b", quoted=True) for k in keys] or None,
                                      order=exp.Order(expressions=[exp.Ordered(this=exp.column(t, "b", quoted=True))]))
        select = exp.select("b.*", *(exp.alias_(window(y), f"{y}__pop", quoted=True) for y in measures))
        query = select.from_(exp.Table(this=_ident("base"), alias=exp.TableAlias(this=_ident("b"))))
    else:
        on = exp.EQ(this=exp.column(t, "p", quoted=True),
                    expression=exp.Sub(this=exp.column(t, "b", quoted=True),
                                       expression=exp.Interval(this=exp.Literal.string("1"), unit=exp.var("YEAR"))))
        for k in keys:
            on = exp.And(this=on, expression=exp.EQ(this=exp.column(k, "p", quoted=True), expression=exp.column(k, "b", quoted=True)))
        query = (exp.select("b.*", *(exp.alias_(exp.column(y, "p", quoted=True), f"{y}__yoy", quoted=True) for y in measures))
                 .from_(exp.Table(this=_ident("base"), alias=exp.TableAlias(this=_ident("b"))))
                 .join(exp.Table(this=_ident("base"), alias=exp.TableAlias(this=_ident("p"))), on=on, join_type="left"))
    return query.with_("base", as_=base).sql(dialect=dialect)


class Resolver:
    """Dataset SQL for a chart's `dataset_ref`, under the requester's scope (deterministic for one
    workspace state, so the bundle rebuilt at execution hashes the same as the approved one)."""

    def __init__(self, session: Session, user: Any, workspace_id: str) -> None:
        self.session, self.user, self.workspace_id = session, user, workspace_id
        self._catalog: dict[str, Any] | None | bool = False
        self._scope: Any = None

    def _scope_for(self) -> Any:
        if self._scope is None:
            from analystos.governance.policy import resolve_scope

            self._scope = resolve_scope(self.session, self.user, self.workspace_id)
        return self._scope

    def sql(self, ref: Any) -> tuple[str, str, list[str]]:
        """(sql, dialect, source assets)."""
        from analystos.db.models import QueryExecution, VerifiedQuery

        if ref.kind == "verified_query":
            vq = self.session.scalar(select(VerifiedQuery).where(VerifiedQuery.workspace_id == self.workspace_id,
                                                                 VerifiedQuery.name == ref.name))
            if vq is None or vq.status != "active":
                raise InvalidInput(f"verified query {ref.name} is not active in this workspace")
            if vq.parameters:
                raise InvalidInput(f"verified query {ref.name} takes parameters; bind it with fixed values first")
            assets = [a for a in (vq.origin or {}).get("assets") or [] if isinstance(a, str)]
            return vq.sql_template, vq.dialect or "postgres", assets
        if ref.kind == "result_handle":
            qid = ref.query_id or ref.handle_id
            qx = self.session.get(QueryExecution, qid)
            if qx is None or qx.workspace_id != self.workspace_id or qx.status != "ok":
                raise InvalidInput(f"result {ref.handle_id} has no successful gateway execution in this workspace")
            return qx.sql, "postgres", list(qx.referenced_assets or [])
        if ref.kind == "metric":
            from analystos.contracts.semantic import SemanticQuery
            from analystos.semantic.compiler import compile_query, load_catalog

            if self._catalog is False:
                self._catalog = load_catalog(self.session, self.workspace_id)
            catalog = self._catalog
            if not catalog:
                raise InvalidInput("this workspace has no approved semantic model; metric charts cannot be published")
            approved = (catalog["metrics"] or {}).get(ref.name)  # type: ignore[index]
            if approved is None:
                raise InvalidInput(f"metric {ref.label} is not approved (experimental metrics are not published)")
            if int(approved["version"]) != ref.version:
                raise InvalidInput(f"metric {ref.label} is not the approved version (approved: v{approved['version']}); "
                                   "rebind the chart")
            compiled = compile_query(SemanticQuery(metrics=[ref.name], dimensions=list(ref.dimensions)), catalog,  # type: ignore[arg-type]
                                     self._scope_for())
            assets = [a for a in (compiled.provenance or {}).get("assets") or [] if isinstance(a, str)]
            return compiled.sql, "postgres", assets
        raise InvalidInput(f"unknown dataset reference {ref.kind}")


def _kpi_chart(kpi: KpiDoc) -> ChartDoc:
    return ChartDoc(id=kpi.id, title=kpi.title, type="kpi", dataset_ref=kpi.metric,
                    fields=[FieldDef(name=kpi.metric.name, type="quantitative", sensitivity=kpi.sensitivity)],
                    encoding=Encoding(y=[kpi.metric.name]), sensitivity=kpi.sensitivity,
                    provenance=kpi.provenance if kpi.provenance else Provenance(sources=[kpi.metric.label], created_by="-"),
                    labels=kpi.labels)


def _column_type(field: FieldDef) -> str:
    return {"temporal": "TIMESTAMP", "quantitative": "NUMERIC", "boolean": "BOOLEAN"}.get(field.type, "VARCHAR")


def to_publish_bundle(doc: DashboardDoc, resolver: Resolver, destination: str, workspace_id: str) -> PublishBundle:
    datasets: list[DatasetDef] = []
    metrics: list[MetricDef] = []
    charts: list[ChartSpec] = []
    for chart in [*doc.charts, *(_kpi_chart(k) for k in doc.kpis)]:
        sql, dialect, assets = resolver.sql(chart.dataset_ref)
        # a filter without a value is a control the viewer sets (a native filter), not a restriction of the dataset
        scoped = [f for f in doc.filters if (not f.scope or chart.id in f.scope) and chart.field(f.field) is not None
                  and f.value is not None]
        sql = _wrap_compare(_wrap_filters(sql, scoped, dialect), chart, dialect)
        x = chart.field(chart.encoding.x)
        time_column = x.name if x is not None and x.type == "temporal" else None
        ds_name = f"ds_{chart.id}"
        sources = sorted({*assets, *(s for s in chart.provenance.sources if _ASSET.match(s))})
        datasets.append(DatasetDef(name=ds_name, description=f"{chart.title} ({chart.dataset_ref.kind})", sql=sql,
                                   columns=[{"name": f.name, "type": _column_type(f), "business_name": f.label or f.name}
                                            for f in chart.fields],
                                   time_column=time_column, source_assets=sources))
        names = []
        for y in chart.encoding.y:
            agg = chart.encoding.aggregation
            col = exp.column(y, quoted=True).sql(dialect="postgres")
            expr = f"COUNT(DISTINCT {col})" if agg == "count_distinct" else f"{_AGG[agg]}({col})"
            name = f"{chart.id}__{y}"[:120]
            names.append(name)
            metrics.append(MetricDef(name=name, display_name=(chart.field(y) or FieldDef(name=y, type="quantitative")).label or y,
                                     definition=f"{agg} of {y} for {chart.title}", sql_expression=expr,
                                     source_columns=[y]))
        kind = {"area": "line"}.get(chart.type, chart.type)
        charts.append(ChartSpec(key=chart.id, title=chart.title, chart_type=kind, intent=_INTENT[chart.type],  # type: ignore[arg-type]
                                dataset=ds_name, metric=names[0] if names else None, metrics=names,
                                dimension=None if time_column else chart.encoding.x,
                                series=chart.encoding.series or chart.encoding.color, description=chart.description))
    keys = [c.key for c in charts]
    layout = [{"chart": i.ref, "row": i.y, "col": i.x, "width": i.w, "height": i.h}
              for i in sorted(doc.layout.items, key=lambda i: (i.y, i.x)) if i.ref in keys]
    summary = "\n\n".join(t.markdown for t in doc.text_blocks if t.markdown.strip())
    dashboard = DashboardSpec(key=doc.id, title=doc.title, audience=doc.audience, description=doc.description,
                              charts=[c["chart"] for c in layout], layout=layout,
                              native_filters=sorted({f.field for f in doc.filters}), summary_markdown=summary)
    return PublishBundle(workspace_id=workspace_id, destination=destination, datasets=datasets, metrics=metrics,  # type: ignore[arg-type]
                         charts=charts, dashboards=[dashboard])

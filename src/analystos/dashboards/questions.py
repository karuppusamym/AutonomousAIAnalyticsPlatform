"""A business question -> a dashboard chart with real data (CONV-01).

The builder never writes SQL and never asks a model for numbers. A question goes through the governed Ask
path unchanged (`agents/sql_agent.ask`: the semantic compiler for an approved metric, a verified query,
the schema-bound rules, else model-written SQL; every statement through the gateway validator and
`QueryGateway.execute`). This module turns that answer into a `ChartDoc` (or a metric `KpiDoc`):

* fields from the result's columns and values (temporal / quantitative / nominal);
* the chart type from `skills/viz.recommend_chart` (intent from the data's shape), unless the person named one
  that suits the data;
* `dataset_ref` that the publisher can re-derive without the answer: the approved `metric@v` + dimensions,
  a parameterless verified query, else the gateway execution (`result_handle` = its query id);
* a bounded preview (`MAX_PREVIEW_ROWS`) and provenance (assets, query id, how it was answered).

A question the Ask path refuses, asks back about, or answers with nothing chartable raises `NotGrounded`
with a clarifying reply and examples the workspace can answer: never a guessed chart.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from analystos.contracts.dashboard_dsl import (
    MAX_PREVIEW_ROWS,
    ChartDoc,
    Encoding,
    FieldDef,
    KpiDoc,
    MetricRef,
    Provenance,
    ResultHandleRef,
    VerifiedQueryRef,
    max_sensitivity,
)
from analystos.core.logging import get_logger
from analystos.dashboards.ops import BuiltElement, ChartFromQuestion, NotGrounded
from analystos.dashboards.validate import suggest_type, suitability

log = get_logger(__name__)

_DATE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_ORDINAL_NAME = re.compile(r"(?:_bucket|_band|_hour|_dow|_rank|_age)$")
_CLARIFY = ("Say what to measure and how to group it, for example \"order count by region\", \"average delivery "
            "days by carrier\" or \"orders per month\".")


# ------------------------------------------------------------------------------------ data shape
def _is_number(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def _is_temporal(v: Any) -> bool:
    return isinstance(v, datetime | date) or (isinstance(v, str) and bool(_DATE.match(v.strip())))


def infer_fields(columns: list[str], rows: list[Any], *, measures: set[str] | None = None,
                 sensitivity_of: Callable[[str], str] | None = None) -> list[FieldDef]:
    """One field per result column, typed from its values (the first rows only); `measures` (approved metric
    names) are quantitative whatever their values."""
    out: list[FieldDef] = []
    for i, name in enumerate(columns):
        values = [r[i] for r in rows[:MAX_PREVIEW_ROWS] if isinstance(r, list | tuple) and i < len(r) and r[i] is not None]
        if name in (measures or set()) or (values and all(_is_number(v) for v in values)):
            ftype = "quantitative"
        elif values and all(isinstance(v, bool) for v in values):
            ftype = "boolean"
        elif values and all(_is_temporal(v) for v in values):
            ftype = "temporal"
        elif _ORDINAL_NAME.search(name.lower()):
            ftype = "ordinal"
        else:
            ftype = "nominal"
        label = name.split(".")[-1].replace("_", " ").strip().capitalize() or name
        sens = sensitivity_of(name) if sensitivity_of else "internal"
        out.append(FieldDef(name=name, type=ftype, label=label[:200], sensitivity=sens))  # type: ignore[arg-type]
    return out


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def bounded_preview(columns: list[str], rows: list[Any], row_count: int | None = None) -> dict[str, Any]:
    kept = [[_json_value(v) for v in r] for r in rows[:MAX_PREVIEW_ROWS] if isinstance(r, list | tuple)]
    total = row_count if isinstance(row_count, int) else len(rows)
    return {"columns": list(columns), "rows": kept, **({"truncated": True, "row_count": total} if total > len(kept) else {})}


def _cardinality(rows: list[Any], i: int) -> int:
    return len({repr(r[i]) for r in rows if isinstance(r, list | tuple) and i < len(r)})


def recommend(fields: list[FieldDef], rows: list[Any], *, hint: dict[str, Any] | None = None,
              wanted: str | None = None) -> tuple[str, Encoding, str]:
    """(chart type, encoding, rationale). The encoding comes from the data's shape; the type from skills/viz;
    a type the person named wins when it suits the encoding."""
    from analystos.skills.viz import recommend_chart

    measures = [f for f in fields if f.type == "quantitative"]
    dims = [f for f in fields if f.type != "quantitative"]
    hinted_x = (hint or {}).get("x")
    temporal = [f for f in dims if f.type == "temporal"]
    x = next((f for f in dims if f.name == hinted_x), None) or (temporal[0] if temporal else (dims[0] if dims else None))
    series = next((f for f in dims if x is not None and f.name != x.name), None)
    ys = [f.name for f in measures if x is None or f.name != x.name][:8]
    if not ys and x is not None and len(fields) == 1:
        return "table", Encoding(x=x.name, aggregation="none"), "no numeric value to plot: a table shows the rows"
    if x is None:
        if len(rows) == 1 and len(ys) == 1:
            r = recommend_chart("kpi", None, 1, 1)
            return r["chart_type"], Encoding(y=ys), r["rationale"]
        return "table", Encoding(y=ys, aggregation="none"), "several values without a grouping: a table shows them"
    idx = fields.index(x)
    card = _cardinality(rows, idx)
    if x.type == "temporal":
        intent, dim_type = "trend", "datetime"
    elif series is not None:
        intent, dim_type = "comparison", "categorical"
    else:
        intent, dim_type = "comparison", "categorical"
    r = recommend_chart(intent, dim_type, card, len(ys) or 1)
    kind, why = r["chart_type"], r["rationale"]
    if series is not None and kind == "bar":
        kind, why = "stacked_bar", f"{why}; split by {series.label or series.name}"
    encoding = Encoding(x=x.name, y=ys, series=series.name if series is not None else None,
                        aggregation="sum" if ys else "count")
    probe = ChartDoc(id="probe", title="probe", type="table", dataset_ref=ResultHandleRef(handle_id="probe"), fields=fields,
                     encoding=encoding, provenance=Provenance(sources=["probe"], created_by="probe"))
    if wanted and wanted != kind:
        reason = suitability(wanted, probe) if wanted != "table" else None
        if reason is None:
            return wanted, encoding, f"{wanted} as asked (the rules suggested {kind}: {why})"
        why = f"{why}; a {wanted} chart does not fit this data ({reason})"
    if kind != "table" and suitability(kind, probe):
        kind = suggest_type(probe)
        why = f"{why}; adjusted to {kind} for these fields"
    return kind, encoding, why


# ------------------------------------------------------------------------------------ answer -> element
def element_id(question: str, prefix: str = "c") -> str:
    words = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")
    words = re.sub(r"^(?:add|show|plot|chart|me|a|an|the|of|for)_", "", words)
    out = f"{prefix}_{words}"[:56].rstrip("_")
    return out if re.match(r"^[a-z][a-z0-9_]*$", out) else f"{prefix}_chart"


def _title(question: str) -> str:
    q = re.sub(r"^\s*(?:please\s+)?(?:add|show(?: me)?|plot|chart|include|create)\s+(?:a\s+|an\s+|the\s+)?(?:chart\s+(?:of|showing)\s+)?",
               "", question.strip(), flags=re.I).rstrip("?.! ")
    return (q[:1].upper() + q[1:])[:300] if q else question[:300]


def _dataset_ref(answer: dict[str, Any], query_id: str | None, sensitivity: str) -> Any:
    semantic = answer.get("semantic") or {}
    query = semantic.get("query") or {}
    metrics = semantic.get("metrics") or []
    if (answer.get("answered_by") == "semantic" and len(metrics) == 1 and len(query.get("metrics") or []) == 1
            and not query.get("filters") and not (query.get("time") or {}).get("grain") and not query.get("time")):
        m = metrics[0]
        return MetricRef(name=m["name"], version=int(m["version"]), dimensions=list(query.get("dimensions") or []),
                         sensitivity=sensitivity)  # type: ignore[arg-type]
    vq = answer.get("verified_query") or {}
    if answer.get("answered_by") == "registry" and vq.get("name") and vq.get("version") and not answer.get("parameters"):
        return VerifiedQueryRef(name=str(vq["name"]), version=int(vq["version"]), sensitivity=sensitivity)  # type: ignore[arg-type]
    if not query_id:
        raise NotGrounded("the answer has no recorded query to bind the chart to", clarify=_CLARIFY)
    return ResultHandleRef(handle_id=query_id, query_id=query_id, sensitivity=sensitivity)  # type: ignore[arg-type]


def element_from_answer(answer: dict[str, Any], *, question: str, actor: str, wanted: str | None = None,
                        title: str | None = None, chart_id: str | None = None, turn_id: str | None = None,
                        sensitivity_of: Callable[[str], str] | None = None, kpi_doc: bool = True) -> BuiltElement:
    """Build the chart (or metric KPI) for an answered question. Pure: everything comes from the answer."""
    result = answer.get("result") or {}
    columns = list(result.get("columns") or [])
    rows = list(result.get("rows") or [])
    if not columns or not rows:
        raise NotGrounded("the question was answered but returned no rows to chart",
                          clarify="Try a wider period or a different grouping.", kind="empty")
    semantic = answer.get("semantic") or {}
    measures = set((semantic.get("query") or {}).get("metrics") or [])
    fields = infer_fields(columns, rows, measures=measures, sensitivity_of=sensitivity_of)
    kind, encoding, rationale = recommend(fields, rows, hint=answer.get("chart") if isinstance(answer.get("chart"), dict) else None,
                                          wanted=wanted)
    sensitivity = max_sensitivity(*(f.sensitivity for f in fields))
    query_id = result.get("query_id")
    ref = _dataset_ref(answer, query_id, sensitivity)
    assets = [a for a in result.get("referenced_assets") or [] if isinstance(a, str)]
    sources = assets or ([ref.label] if isinstance(ref, MetricRef) else [f"query:{query_id}"])
    governance = answer.get("governance") or ("governed" if answer.get("answered_by") == "semantic" else "ad_hoc")
    provenance = Provenance(sources=sources, query_ids=[query_id] if query_id else [], created_by=actor,
                            created_via="conversation", turn_id=turn_id)
    name = title or _title(question)
    labels = [] if governance == "governed" or answer.get("answered_by") in ("registry", "rules") else ["ad_hoc"]
    summary = {"answered_by": answer.get("answered_by"), "governance": governance, "query_id": query_id,
               "chart_type": kind, "rationale": rationale, "dataset": ref.kind, "rows": len(rows)}
    if kpi_doc and isinstance(ref, MetricRef) and kind == "kpi" and not ref.dimensions:
        value = rows[0][columns.index(encoding.y[0])] if encoding.y and encoding.y[0] in columns else None
        kpi = KpiDoc(id=chart_id or element_id(question, "kpi"), title=name, metric=ref,
                     value=float(value) if _is_number(value) else None, sensitivity=sensitivity,  # type: ignore[arg-type]
                     provenance=provenance)
        return BuiltElement(kpi, summary)
    chart = ChartDoc(id=chart_id or element_id(question), title=name, type=kind, dataset_ref=ref, fields=fields,  # type: ignore[arg-type]
                     encoding=encoding, sensitivity=sensitivity, provenance=provenance, labels=labels,  # type: ignore[arg-type]
                     description=str(answer.get("explanation") or "")[:2000],
                     preview=bounded_preview(columns, rows, result.get("row_count")))
    return BuiltElement(chart, summary)


# ------------------------------------------------------------------------------------ the Ask path
def refusal_to_not_grounded(answer: dict[str, Any], examples: list[str]) -> NotGrounded:
    """An Ask outcome that is not an answer, as a clarifying reply."""
    status = answer.get("status")
    if status == "clarify":
        missing = ", ".join(m.get("name", "") for m in answer.get("missing") or [] if isinstance(m, dict)) or ""
        return NotGrounded("the question is too open to chart safely" + (f" (missing: {missing})" if missing else ""),
                           clarify=str(answer.get("explanation") or _CLARIFY)[:500], examples=examples, kind="clarify")
    if status == "needs_input":
        return NotGrounded("the question needs a value before it can be charted",
                           clarify=str(answer.get("explanation") or _CLARIFY)[:500], examples=examples, kind="needs_input")
    refusal = answer.get("refusal") or {}
    reason = str(refusal.get("message") or answer.get("explanation") or "the question could not be answered from the data")
    return NotGrounded(reason[:400], clarify=str(refusal.get("remedy") or _CLARIFY)[:500], examples=examples,
                       kind=str(refusal.get("kind") or "not_grounded"))


def examples_for(catalog: dict[str, Any] | None, scope: Any = None, limit: int = 5) -> list[str]:
    """Questions the governed path answers without a model: approved metrics, by one of their dimensions."""
    if not catalog:
        return []
    try:
        from analystos.semantic.compiler import planner_catalog

        visible = planner_catalog(catalog, scope) if scope is not None else catalog
    except Exception:  # noqa: BLE001 - examples are a courtesy; a catalog problem must not hide the reply
        return []
    out: list[str] = []
    for name, entry in sorted((visible.get("metrics") or {}).items()):
        d = entry.get("definition") or {}
        label = (d.get("display_name") or name.replace("_", " ")).strip()
        dims = [x for x in d.get("dimensions") or [] if isinstance(x, str)]
        out.append(f"{label} by {dims[0].split('.')[-1].replace('_', ' ')}" if dims else label)
        if len(out) >= limit:
            break
    return out


def sensitivity_lookup(session: Any, workspace_id: str, assets: list[str]) -> Callable[[str], str]:
    """Column -> sensitivity from the catalog tags of the answer's tables (restricted > confidential for PII or
    sensitive tags); a computed column that matches no source column stays internal."""
    from sqlalchemy import select

    from analystos.db.models import SourceAsset, SourceColumn

    tags: dict[str, set[str]] = {}
    for fq in assets[:20]:
        schema, _, name = fq.partition(".")
        asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        if asset is None:
            continue
        for col in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id)):
            tags.setdefault(col.name.lower(), set()).update(t.lower() for t in col.tags or [])

    def of(column: str) -> str:
        found = tags.get(column.split(".")[-1].lower(), set())
        if "restricted" in found:
            return "restricted"
        if found & {"pii", "sensitive", "confidential"}:
            return "confidential"
        return "internal"

    return of


def context(user: Any, workspace_id: str) -> Any:
    """The SQL agent's ad-hoc context for `user` (scope, policy, approved semantic catalog), built in its own session:
    building it detaches everything it loaded, which must not touch the caller's session."""
    from analystos.db.base import session_scope
    from analystos.services.ask import adhoc_context

    with session_scope() as s:
        return adhoc_context(s, user, workspace_id)


def ask(user: Any, workspace_id: str, question: str, *, ask_fn: Callable[..., dict[str, Any]] | None = None,
        ctx: Any = None) -> tuple[dict[str, Any], list[str]]:
    """Run one question through the governed Ask path as `user`; returns (answer, grounded examples).
    Ask's own errors become a refusal answer, as in an Ask thread."""
    from analystos.agents.sql_agent import ask as sql_ask
    from analystos.core.errors import AnalystOSError
    from analystos.services.ask import refusal_for

    ctx = ctx if ctx is not None else context(user, workspace_id)
    examples = examples_for(ctx.semantic_catalog, ctx.scope)
    if not ctx.scope.assets:
        return {"status": "refused", "refusal": {"kind": "no_scope", "message": "No selected, ready tables are in your scope.",
                                                  "remedy": "Ask an owner to select tables for this workspace."}}, examples
    try:
        answer = (ask_fn or sql_ask)(ctx, question)
    except AnalystOSError as exc:
        answer = {"status": "refused", "refusal": refusal_for(exc)}
    return answer, examples


def question_answerer(session: Any, user: Any, workspace_id: str, *,
                      ask_fn: Callable[..., dict[str, Any]] | None = None) -> Callable[[ChartFromQuestion, Any], BuiltElement]:
    """`OpEnv.answer_question` for one user and workspace."""

    cached: dict[str, Any] = {}

    def answer(op: ChartFromQuestion, doc: Any) -> BuiltElement:
        if "ctx" not in cached:  # one context per batch or draft: the same scope and catalog for every tile
            cached["ctx"] = context(user, workspace_id)
        out, examples = ask(user, workspace_id, op.question, ask_fn=ask_fn, ctx=cached["ctx"])
        if out.get("status") != "answered":
            raise refusal_to_not_grounded(out, examples)
        assets = list((out.get("result") or {}).get("referenced_assets") or [])
        built = element_from_answer(out, question=op.question, actor=f"user:{user.id}", wanted=op.type, title=op.title,
                                    sensitivity_of=sensitivity_lookup(session, workspace_id, assets))
        return built

    return answer

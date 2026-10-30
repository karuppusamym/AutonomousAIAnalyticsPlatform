"""Chart selection rules (spec §34) with context overrides.

Defaults:
  trend -> line | comparison -> bar | distribution -> histogram | relationship -> scatter
  part_to_whole -> stacked_bar (treemap when > 12 categories; pie only when <= 5 categories and one metric)
  detail -> table | correlation -> heatmap | kpi -> kpi | aging -> bar (bucketed) | cohort -> heatmap
  geography / process_flow: map and sankey are not in the ChartType vocabulary; fall back to bar / table.

Overrides:
  * comparison / aging with high cardinality (> 20 categories) -> top-N bar (limit 15)
  * trend with > 2 metrics -> line with one series per metric
  * trend over a non-temporal dimension -> bar
  * distribution over a categorical dimension -> bar of counts
"""
from __future__ import annotations

import re
from typing import Any

HIGH_CARDINALITY = 20
# A number that names a category (priority 1-5, state 1-7) is a dimension: averaging it means nothing.
MAX_CODES = 20
CODE_ROLES = frozenset({"code", "dimension", "flag"})
CODE_TYPES = frozenset({"code", "category", "categorical", "ordinal", "boolean"})
CODE_WORDS = frozenset({"priority", "status", "state", "level", "severity", "urgency", "impact", "tier", "grade", "rank",
                        "code", "type", "class", "category", "stage", "phase", "band", "bucket"})
TOP_N_LIMIT = 15
PIE_MAX = 5
TREEMAP_MIN = 13

INTENT_DEFAULTS = {
    "trend": "line", "comparison": "bar", "distribution": "histogram", "relationship": "scatter",
    "part_to_whole": "stacked_bar", "detail": "table", "correlation": "heatmap", "kpi": "kpi", "aging": "bar",
    "cohort": "heatmap", "geography": "bar", "process_flow": "table",
}
_ALIASES = {"category_comparison": "comparison", "ranking": "comparison", "composition": "part_to_whole",
            "share": "part_to_whole", "operational_detail": "detail", "time_series": "trend", "scatter": "relationship"}


def recommend_chart(intent: str, dimension_semantic_type: str | None, cardinality: int | None, n_metrics: int = 1) -> dict[str, Any]:
    """Full recommendation: {chart_type, rationale, limit, series_by_metric}."""
    it = _ALIASES.get((intent or "").lower(), (intent or "").lower())
    dim = (dimension_semantic_type or "").lower()
    card = cardinality if cardinality is not None else 0
    n_metrics = max(1, int(n_metrics or 1))
    limit: int | None = None
    series = False
    if it not in INTENT_DEFAULTS:
        return {"chart_type": "table", "rationale": f"unknown intent '{intent}': a table shows the data without implying a shape",
                "limit": None, "series_by_metric": False}
    chart = INTENT_DEFAULTS[it]
    why = f"§34 default for {it}: {chart}"
    if it == "trend":
        if dim and dim != "datetime":
            chart, why = "bar", f"trend requested over a non-temporal dimension ({dim}); bar compares the ordered categories"
        elif n_metrics > 2:
            series, why = True, f"trend of {n_metrics} metrics: line with one series per metric (shared time axis)"
        else:
            why = "trend over time: line shows direction and change points"
    elif it in ("comparison", "aging"):
        if dim == "datetime" and it == "comparison":
            chart, why = "line", "comparison across a time dimension reads as a trend: line"
        elif card > HIGH_CARDINALITY:
            limit = TOP_N_LIMIT
            why = f"{card} categories is too many to compare; top-{TOP_N_LIMIT} bar sorted by value (rest summarised)"
        elif it == "aging":
            why = "aging: bar over ordered age buckets"
        else:
            why = f"category comparison across {card or 'few'} categories: bar"
    elif it == "distribution":
        if dim in ("categorical", "boolean", "id"):
            chart, why = "bar", f"distribution of a {dim} dimension: bar of counts per category"
        else:
            why = "distribution of a numeric measure: histogram"
    elif it == "part_to_whole":
        if card and card <= PIE_MAX and n_metrics == 1:
            chart, why = "pie", f"part-to-whole with {card} categories (<= {PIE_MAX}) and one metric: pie is readable"
        elif card >= TREEMAP_MIN:
            chart, why = "treemap", f"part-to-whole with {card} categories: treemap keeps small parts visible"
        else:
            why = "part-to-whole: stacked bar (pie only for <= 5 categories)"
    elif it == "geography":
        why = "geography: map is not a supported chart type; bar by region instead"
        if card > HIGH_CARDINALITY:
            limit = TOP_N_LIMIT
    elif it == "process_flow":
        why = "process flow: sankey is not a supported chart type; table of transitions instead"
    elif it == "cohort":
        why = "cohort: heatmap of cohort x period"
    elif it == "correlation":
        why = "correlation matrix: heatmap"
    return {"chart_type": chart, "rationale": why, "limit": limit, "series_by_metric": series}


def is_coded_number(name: str, *, semantic_type: str | None = None, role: str | None = None,
                    distinct: int | None = None) -> bool:
    """A numeric column that is a code (priority, status, level, ...): few distinct values and either the catalog
    says it is a code or dimension, or its name reads like one. Such a column is grouped by, never averaged; a
    count or measure role (a repeat count) or more than `MAX_CODES` values keeps it a measure."""
    if distinct is not None and distinct > MAX_CODES:
        return False
    if (role or "").lower() in ("measure", "amount", "duration", "percent"):
        return False
    if (role or "").lower() in CODE_ROLES or (semantic_type or "").lower() in CODE_TYPES:
        return True
    return bool(set(re.split(r"[^a-z0-9]+", name.rsplit(".", 1)[-1].lower())) & CODE_WORDS)


def choose_chart(intent: str, dimension_semantic_type: str | None, cardinality: int | None, n_metrics: int = 1) -> tuple[str, str]:
    """(chart_type, rationale) per §34 defaults and overrides; see `recommend_chart` for limit/series."""
    r = recommend_chart(intent, dimension_semantic_type, cardinality, n_metrics)
    return r["chart_type"], r["rationale"]

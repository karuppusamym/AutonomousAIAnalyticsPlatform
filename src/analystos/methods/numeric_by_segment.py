"""numeric_by_segment: a numeric outcome across segment groups.

primary       Mann-Whitney (two groups) / Kruskal-Wallis (more) on a deterministic row sample, with
              exact full-data summaries (n, mean, median, min, max) from an aggregate query
verification  bootstrap 95% CI of median(top) - median(baseline)
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods.base import (
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    cap,
    cap_sample,
    finish,
    first,
    no_data,
    nothing_to_verify,
    seg_label,
    seg_sort_key,
    select_segments,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

NUMERIC_OUT = {"column", "duration_hours"}


def numeric_data(rows: Rows, spec: AnalysisSpec) -> tuple[dict[str, list[float]], dict, list[str], dict]:
    summary, sample = rows["summary"], rows["primary"]
    full = {}
    null_seg = 0
    null_out = 0
    for r in summary:
        if r["segment"] is None:
            null_seg += int(r["n_rows"] or 0)
            continue
        null_out += int((r["n_rows"] or 0) - (r["n"] or 0))
        lab = seg_label(r["segment"], spec.segment)
        full[lab] = {"segment": lab, "n": int(r["n"] or 0), "mean": r.get("mean"), "median": r.get("median"),
                     "min": r.get("min"), "max": r.get("max"), "order": r.get("segment_order")}
    kept, warns, info = select_segments([g for g in full.values() if g["n"] > 0], spec)
    keep = {g["segment"] for g in kept}
    values: dict[str, list[float]] = {}
    for r in sample:
        lab = seg_label(r["segment"], spec.segment)
        if lab in keep:
            values.setdefault(lab, []).append(float(r["outcome"]))
    rows_nonnull = int(first(sample, "_rows_total", 0)) - int(first(sample, "_rows_excluded", 0))
    excl = {"excluded_null_segment_rows": null_seg, "excluded_null_outcome_rows": null_out,
            "rows_complete": rows_nonnull, "sample_size": len(sample),
            "sampled": rows_nonnull > len(sample)} | info
    ordered = sorted(values, key=lambda s: seg_sort_key(spec.segment, full[s].get("order"), s))
    return {s: values[s] for s in ordered}, full, warns, excl


class NumericBySegment(AnalysisMethod):
    name = "numeric_by_segment"
    vocabulary = "numeric outcome across groups of `segment`. outcome.type in [column (numeric), duration_hours]."
    purposes = ("summary", "primary")
    playbook = "measure_by_segment"
    segment_matrix = True
    outcome_types = frozenset({"numeric"})
    segment_types = frozenset({"categorical", "boolean"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = []
        if spec.segment is None:
            errors.append(f"{self.name} needs a segment")
        if spec.outcome is None or spec.outcome.type not in NUMERIC_OUT:
            errors.append(f"{self.name} needs a numeric outcome (column/duration_hours)")
        elif spec.outcome.type == "column" and semantic(spec.outcome) not in ("numeric", None):
            errors.append(f"outcome {spec.outcome.column} is not numeric")
        elif spec.outcome.type == "duration_hours" and not spec.outcome.end_column:
            errors.append("duration_hours needs end_column")
        return errors

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "outcome")
        seg_cols, ordered = sb.segment_cols(spec, dialect)
        out = spec.outcome
        assert out is not None
        notes: list[str] = []
        out_e = sb.as_double(sb.derive(out, dialect), dialect)
        if purpose == "primary":
            q = sb.sample_query(spec, dialect, [seg_cols[0], ("outcome", out_e)], sample_rows)
            notes.append(f"deterministic hash-ordered sample of at most {sample_rows} (segment, outcome) rows")
            return sb.CompiledQuery(sb.to_sql(q, dialect), {"segment": "segment", "outcome": "outcome",
                                                            "rows_total": "_rows_total", "rows_excluded": "_rows_excluded"},
                                    notes, purpose, dialect, sample_rows, "sample")
        inner = sb.base_select(spec, dialect, [*seg_cols, ("outcome", out_e)])
        group = [sb.ref(a) for a, _ in seg_cols]
        o = sb.ref("outcome")
        aggs = [sb.count_star().as_(sb.ident("n_rows")), exp.Count(this=o).as_(sb.ident("n")),
                exp.Avg(this=o.copy()).as_(sb.ident("mean")), exp.Min(this=o.copy()).as_(sb.ident("min")),
                exp.Max(this=o.copy()).as_(sb.ident("max"))]
        columns = {"segment": "segment", "n_rows": "n_rows", "n": "n", "mean": "mean", "min": "min", "max": "max"}
        if dialect != "tsql" and dialect not in sb.NO_EXACT_PERCENTILE:
            aggs.append(sb.median(o.copy(), dialect).as_(sb.ident("median")))
            columns["median"] = "median"
        else:
            notes.append(f"{dialect}: no exact grouped PERCENTILE_CONT; medians come from the row sample")
        if ordered:
            columns["segment_order"] = "segment_order"
        q = (exp.select(*group, *aggs).from_(sb.subquery(inner, "d")).group_by(*[g.copy() for g in group])
             .order_by(exp.Ordered(this=sb.ref("n_rows"), desc=True), *[g.copy() for g in group]).limit(sb.MAX_GROUPS))
        return sb.CompiledQuery(sb.to_sql(q, dialect), columns, notes, purpose, dialect, sb.MAX_GROUPS, "aggregate")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        values, full, warns, excl = numeric_data(rows, spec)
        values = {k: v for k, v in values.items() if len(v) >= 2}
        if len(values) < 2:
            out = no_data(spec, "need at least two segments with data", "mann_whitney_u")
            out.stat.warnings = warns + out.stat.warnings
            out.stat.details.update(excl)
            return out
        stat = st.compare_groups(values, alpha=alpha)
        stat.warnings = warns + stat.warnings
        for g in stat.groups:
            f = full.get(g["segment"], {})
            g.update({"full_n": f.get("n"), "full_mean": st._f(f.get("mean")), "full_median": st._f(f.get("median"))})
        stat.details.update(excl | {"outcome": sb.describe(spec.outcome), "segment": sb.describe(spec.segment)})
        stat.highlights.update({"outcome": sb.describe(spec.outcome), "segment_by": sb.describe(spec.segment),
                                "sampled": excl["sampled"]})
        if excl["sampled"]:
            stat.warnings.append(f"test computed on a deterministic sample of {excl['sample_size']} of "
                                 f"{excl['rows_complete']} complete rows; full_* columns are exact")
        cols = ["segment", "n", "median", "mean", "p25", "p75", "full_n", "full_mean", "full_median"]
        table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        values, _, _, _ = numeric_data(rows, spec)
        top, base = primary.highlights.get("top_segment"), primary.highlights.get("baseline_segment")
        if top not in values or base not in values or top == base:
            return nothing_to_verify(spec, primary, "bootstrap_median_difference",
                                     "primary top/baseline segments unavailable; nothing to verify")
        a, b = cap_sample(values[top], 20000, 1), cap_sample(values[base], 20000, 2)
        est, lo, hi = st.bootstrap_diff_ci(a, b, np.median, n=2000, seed=0, alpha=alpha)
        base_med = float(np.median(b))
        rel = est / abs(base_med) if base_med != 0 else None
        supported = lo > 0 or hi < 0
        v = StatResult(method=spec.method, test="bootstrap_median_difference", n=int(a.size + b.size), statistic=st._f(est),
                       effect_size=st._f(rel), effect_label="relative_median_difference", ci_low=st._f(lo), ci_high=st._f(hi),
                       highlights={"top_segment": top, "baseline_segment": base, "median_difference": st._q(est),
                                   "median_difference_ci": [st._q(lo), st._q(hi)], "relative_difference": st._q(rel)},
                       assumptions=["percentile bootstrap, 2000 resamples, seed 0"], supported=supported,
                       details={"alpha": alpha})
        table = {"columns": ["top_segment", "baseline_segment", "median_difference", "ci_low", "ci_high"],
                 "rows": [[top, base, st._f(est), st._f(lo), st._f(hi)]]}
        return finish(spec, primary, v, est > 0, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if "top_median" not in hl and "top_value" not in hl:
            return None
        seg, out, scope = text_parts(spec)
        top = hl.get("top_median", hl.get("top_value"))
        base = hl.get("baseline_median", hl.get("baseline_value"))
        title = f"{cap(out)} is higher for {seg} = {hl.get('top_segment')}"
        text = (f"Median {out} is {top:.1f} for {seg} = {hl.get('top_segment')} versus {base:.1f} for "
                f"{hl.get('baseline_segment')}" + (f" ({hl['ratio']:.1f}x)" if isinstance(hl.get("ratio"), (int, float)) else "")
                + f"{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="comparison", dimension="segment", measure="outcome")

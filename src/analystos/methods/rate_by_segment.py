"""rate_by_segment: a boolean outcome's rate across segment groups.

primary       chi-square test of independence + Cramér's V (aggregated table, all rows)
verification  logistic regression with segment dummies fitted as a binomial GLM on the aggregated
              table (identical to row-level logistic regression, so no row sample and no sampling
              error), plus a Monte Carlo permutation test of independence with fixed margins
              (exact-in-distribution, no chi-square asymptotics). Chosen over row-level logistic
              regression on a sample because it uses all rows and handles separation (0% segments)
              with a Haldane correction instead of diverging.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods.base import (
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    cap,
    finish,
    fmt_pct,
    nothing_to_verify,
    seg_label,
    seg_sort_key,
    select_segments,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

BOOLEAN_OUT = {"equals", "is_true", "after_hours", "later_than"}


def rate_groups(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[dict], dict]:
    null_seg = sum(int(r["n_rows"] or 0) for r in rows if r["segment"] is None)
    null_out = sum(int((r["n_rows"] or 0) - (r["n"] or 0)) for r in rows if r["segment"] is not None)
    groups = [{"segment": seg_label(r["segment"], spec.segment), "n": int(r["n"] or 0),
               "positives": int(r["positives"] or 0), "order": r.get("segment_order")}
              for r in rows if r["segment"] is not None and (r["n"] or 0) > 0]
    return groups, {"excluded_null_segment_rows": null_seg, "excluded_null_outcome_rows": null_out,
                    "rows_scanned": sum(int(r["n_rows"] or 0) for r in rows)}


class RateBySegment(AnalysisMethod):
    name = "rate_by_segment"
    vocabulary = "boolean outcome rate across groups of `segment`. outcome.type in [equals, is_true, after_hours, later_than]."
    playbook = "flag_by_segment"
    drill_down = True
    segment_matrix = True
    outcome_types = frozenset({"boolean"})
    segment_types = frozenset({"categorical", "boolean"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = []
        if spec.segment is None:
            errors.append(f"{self.name} needs a segment")
        if spec.outcome is None or spec.outcome.type not in BOOLEAN_OUT:
            errors.append(f"{self.name} needs a boolean outcome (equals/is_true/after_hours/later_than)")
        return errors

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "outcome")
        seg_cols, ordered = sb.segment_cols(spec, dialect)
        out = spec.outcome
        assert out is not None
        notes: list[str] = []
        if out.type not in sb.BOOLEAN_DERIVATIONS:
            out_e = sb.is_true_expr(sb.derive(out, dialect), dialect)
            notes.append(f"outcome {sb.describe(out)} interpreted as boolean (true/t/1/yes/y)")
        else:
            out_e = sb.derive(out, dialect)
        inner = sb.base_select(spec, dialect, [*seg_cols, ("outcome", out_e)])
        group = [sb.ref(a) for a, _ in seg_cols]
        q = (exp.select(*group, sb.count_star().as_(sb.ident("n_rows")), exp.Count(this=sb.ref("outcome")).as_(sb.ident("n")),
                        exp.Sum(this=sb.ref("outcome")).as_(sb.ident("positives")))
             .from_(sb.subquery(inner, "d")).group_by(*[g.copy() for g in group])
             .order_by(exp.Ordered(this=sb.ref("n_rows"), desc=True), *[g.copy() for g in group]).limit(sb.MAX_GROUPS))
        columns = {"segment": "segment", "n_rows": "n_rows", "n": "n", "positives": "positives"}
        if ordered:
            columns["segment_order"] = "segment_order"
        notes.append("rows with NULL segment form their own group; rows with NULL outcome counted as n_rows - n")
        return sb.CompiledQuery(sb.to_sql(q, dialect), columns, notes, purpose, dialect, sb.MAX_GROUPS, "aggregate")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        groups, excl = rate_groups(rows["primary"], spec)
        kept, warns, info = select_segments(groups, spec)
        stat = st.chi_square_rates(kept, alpha=alpha)
        stat.warnings = warns + stat.warnings
        stat.details.update(excl | info | {"outcome": sb.describe(spec.outcome), "segment": sb.describe(spec.segment)})
        stat.highlights.update({"outcome": sb.describe(spec.outcome), "segment_by": sb.describe(spec.segment)})
        order = {g["segment"]: g.get("order") for g in groups}
        stat.groups.sort(key=lambda g: seg_sort_key(spec.segment, order.get(g["segment"]), g["segment"]))
        if stat.groups and spec.segment is not None and (spec.segment.type in ("bucket", "hour_of_day", "day_of_week", "period")):
            # ordered segments: also quote the first bucket as the natural reference level
            ref = stat.groups[0]
            top_rate = stat.highlights.get("top_rate")
            stat.highlights.update({"reference_segment": ref["segment"], "reference_rate": st._q(ref["rate"]),
                                    "rate_ratio_vs_reference": st._f(top_rate / ref["rate"], 3)
                                    if ref["rate"] and top_rate is not None else None})
        cols = ["segment", "n", "positives", "rate", "ci_low", "ci_high"]
        table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        groups, _ = rate_groups(rows["primary"], spec)
        kept, _, _ = select_segments(groups, spec)
        top, base = primary.highlights.get("top_segment"), primary.highlights.get("baseline_segment")
        segs = {g["segment"] for g in kept}
        if len(kept) < 2 or top not in segs or base not in segs or top == base:
            return nothing_to_verify(spec, primary, "grouped_logistic_regression",
                                     "primary top/baseline segments unavailable; nothing to verify", sum(g["n"] for g in kept))
        gl = st.grouped_logistic(kept, baseline=base, alpha=alpha)
        perm = st.permutation_chi_square(kept, n_perm=2000, seed=0)
        o = gl["odds_ratios"][top]
        lr_p, perm_p = gl["lr_p_value"], perm["p_value"]
        direction = o["odds_ratio"] is not None and o["odds_ratio"] > 1
        ci_excl = o["ci_low"] is not None and o["ci_low"] > 1
        supported = st._sig(lr_p, alpha) and st._sig(perm_p, alpha) and ci_excl and o["odds_ratio"] >= st.MIN_ODDS_RATIO
        v = StatResult(method=spec.method, test="grouped_logistic_regression+permutation_chi_square",
                       n=sum(g["n"] for g in kept), statistic=gl["lr_statistic"], p_value=lr_p, effect_size=o["odds_ratio"],
                       effect_label="odds_ratio_top_vs_baseline", ci_low=o["ci_low"], ci_high=o["ci_high"],
                       groups=[{"segment": s, **d} for s, d in gl["odds_ratios"].items()],
                       highlights={"top_segment": top, "baseline_segment": base, "odds_ratio": st._q(o["odds_ratio"]),
                                   "odds_ratio_ci": [st._q(o["ci_low"]), st._q(o["ci_high"])], "lr_p_value": lr_p,
                                   "permutation_p_value": perm_p},
                       warnings=gl["warnings"], supported=supported,
                       details={"permutation": perm, "alpha": alpha, "threshold_odds_ratio": st.MIN_ODDS_RATIO})
        table = {"columns": ["segment", "odds_ratio", "ci_low", "ci_high", "p_value"],
                 "rows": [[s, d["odds_ratio"], d["ci_low"], d["ci_high"], d["p_value"]] for s, d in gl["odds_ratios"].items()]}
        return finish(spec, primary, v, direction, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if "top_rate" not in hl:
            return None
        seg, out, scope = text_parts(spec)
        title = f"{cap(out)} concentrates in {seg} = {hl.get('top_segment')}"
        text = (f"Records with {seg} = {hl.get('top_segment')} have a {out} rate of {fmt_pct(hl['top_rate'])} versus "
                f"{fmt_pct(hl.get('baseline_rate', 0))} for {seg} = {hl.get('baseline_segment')}"
                + (f" ({hl['rate_ratio']:.1f}x)" if isinstance(hl.get("rate_ratio"), (int, float)) else "") + f"{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="comparison", dimension="segment", measure="outcome")

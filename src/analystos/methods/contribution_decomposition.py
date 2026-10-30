"""contribution_decomposition: why a KPI changed between the last two `time` periods.

The KPI is the rate of a boolean outcome or the mean of a numeric outcome over the records of a
period. With segment weights w (share of records) and segment KPIs r, the change splits exactly into

  rate (within-segment) effect  sum_s w1_s * (r1_s - r0_s)      over segments present in both periods
  mix effect                    (K1 - K0) - rate effect          shifts in segment shares (and segments
                                                                 that appear or disappear)
  volume effect                 (N1 - N0) * K0                   on the KPI's total (sum), in outcome units

One aggregate query (period x segment -> n, sum, sum of squares) is pushed down; the smaller
segments beyond `top_k` are pooled as "(other)" (pooling keeps every KPI exact).

primary       z test of the rate effect with its direct-standardisation variance
              sum_s w1_s^2 (var1_s/n1_s + var0_s/n0_s); supported when significant and at least 5% of
              the earlier KPI. A KPI that moved only through mix is *not* supported: the claim is that
              the segments themselves changed.
verification  boolean KPI: Cochran-Mantel-Haenszel test of period x outcome stratified by segment
              (pooled odds ratio); numeric KPI: per-segment Welch z statistics combined with Stouffer's
              method, weighted by the later period's segment shares. Agrees when significant in the
              same direction.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps
from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.core.errors import InvalidInput
from analystos.methods.base import (
    OTHER,
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    cap,
    finish,
    fmt_pct,
    json_value,
    no_data,
    nothing_to_verify,
    seg_label,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

MIN_RELATIVE_EFFECT = 0.05  # the rate effect must move the KPI by >= 5% of its earlier value
MAX_CELLS = 5000
BOOLEAN_OUT = {"equals", "is_true", "after_hours", "later_than"}
NUMERIC_OUT = {"column", "duration_hours"}


def _cell(n: float, total: float, sumsq: float, boolean: bool) -> dict[str, float | None]:
    mean = total / n if n else None
    if mean is None:
        var = None
    elif boolean:
        var = mean * (1 - mean)
    else:
        var = max((sumsq - total * total / n) / (n - 1), 0.0) if n > 1 else None
    return {"n": n, "total": total, "sumsq": sumsq, "mean": mean, "var": var}


def periods(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[Any, Any, dict[str, dict], dict[str, dict], dict]:
    """(earlier period, later period, cells before, cells after, info); cells are per segment label."""
    assert spec.outcome is not None
    boolean = spec.outcome.type in BOOLEAN_OUT
    ps = sorted({r["period"] for r in rows if r["period"] is not None})
    info: dict[str, Any] = {"periods_seen": len(ps), "excluded_null_period_rows": sum(int(r["n_rows"] or 0) for r in rows
                                                                                      if r["period"] is None),
                            "excluded_null_outcome_rows": sum(int((r["n_rows"] or 0) - (r["n"] or 0)) for r in rows),
                            "truncated": len(rows) >= MAX_CELLS}
    if len(ps) < 2:
        return None, None, {}, {}, info
    p0, p1 = ps[-2], ps[-1]
    raw: dict[Any, dict[str, list[float]]] = {p0: {}, p1: {}}
    for r in rows:
        if r["period"] in raw and (r["n"] or 0) > 0:
            lab = seg_label(r["segment"], spec.segment)
            acc = raw[r["period"]].setdefault(lab, [0.0, 0.0, 0.0])
            acc[0] += float(r["n"])
            acc[1] += float(r["total"] or 0)
            acc[2] += float(r["sumsq"] or 0)
    size = {s: raw[p0].get(s, [0])[0] + raw[p1].get(s, [0])[0] for s in set(raw[p0]) | set(raw[p1])}
    keep = set(sorted(size, key=lambda s: (-size[s], s))[: spec.top_k])
    pooled: dict[Any, dict[str, list[float]]] = {p0: {}, p1: {}}
    for p in (p0, p1):
        for s, acc in raw[p].items():
            dst = pooled[p].setdefault(s if s in keep else OTHER, [0.0, 0.0, 0.0])
            for i in range(3):
                dst[i] += acc[i]
    info["pooled_segments"] = len(size) - len(keep)
    cells = [{s: _cell(*acc, boolean) for s, acc in pooled[p].items()} for p in (p0, p1)]
    return p0, p1, cells[0], cells[1], info


def _period(p: Any) -> Any:
    v = json_value(p)
    return v[:10] if isinstance(v, str) and v[10:] in ("", "T00:00:00") else v


def _article(word: str) -> str:
    return ("an " if word[:1] in "aeiou" else "a ") + word


def decompose(c0: dict[str, dict], c1: dict[str, dict]) -> dict[str, Any]:
    n0, n1 = sum(c["n"] for c in c0.values()), sum(c["n"] for c in c1.values())
    k0 = sum(c["total"] for c in c0.values()) / n0
    k1 = sum(c["total"] for c in c1.values()) / n1
    common = sorted(set(c0) & set(c1))
    contrib = {s: (c1[s]["n"] / n1) * (c1[s]["mean"] - c0[s]["mean"]) for s in common}
    rate = sum(contrib.values())
    var = sum((c1[s]["n"] / n1) ** 2 * ((c1[s]["var"] or 0) / c1[s]["n"] + (c0[s]["var"] or 0) / c0[s]["n"]) for s in common)
    return {"n0": n0, "n1": n1, "k0": k0, "k1": k1, "rate": rate, "mix": (k1 - k0) - rate, "volume": (n1 - n0) * k0,
            "var": var, "contrib": contrib, "common": common}


class ContributionDecomposition(AnalysisMethod):
    name = "contribution_decomposition"
    vocabulary = ("change of a KPI (rate of a boolean outcome, or mean of a numeric outcome) between the last two "
                  "`time` periods (time.type = date_trunc), split into within-`segment` rate, mix across `segment` "
                  "and volume effects.")
    outcome_types = frozenset({"boolean", "numeric"})
    segment_types = frozenset({"categorical", "boolean"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = []
        if spec.outcome is None or spec.outcome.type not in BOOLEAN_OUT | NUMERIC_OUT:
            errors.append(f"{self.name} needs a boolean (equals/is_true/after_hours/later_than) or numeric (column/duration_hours) outcome")
        elif spec.outcome.type == "column" and semantic(spec.outcome) not in ("numeric", None):
            errors.append(f"outcome {spec.outcome.column} is not numeric")
        if spec.segment is None:
            errors.append(f"{self.name} needs a segment")
        if spec.time is None or not ((spec.time.type == "date_trunc" and spec.time.grain) or
                                     (spec.time.type == "period" and len(spec.time.value or []) == 2)):
            errors.append(f"{self.name} needs time = date_trunc with a grain, or a period of two windows")
        return errors

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        if spec.outcome is None or spec.time is None:
            raise InvalidInput(f"{self.name} requires `outcome`, `segment` and `time`")
        seg_cols, _ = sb.segment_cols(spec, dialect)
        o = sb.derive(spec.outcome, dialect)
        out_e = o if spec.outcome.type in sb.BOOLEAN_DERIVATIONS else sb.as_double(o, dialect)
        inner = sb.base_select(spec, dialect, [("period", sb.time_period_expr(spec.time, dialect)), seg_cols[0],
                                               ("outcome", out_e)])
        y = sb.ref("outcome")
        q = (exp.select(sb.ref("period"), sb.ref("segment"), sb.count_star().as_(sb.ident("n_rows")),
                        exp.Count(this=y).as_(sb.ident("n")), exp.Sum(this=y.copy()).as_(sb.ident("total")),
                        exp.Sum(this=exp.Mul(this=y.copy(), expression=y.copy())).as_(sb.ident("sumsq")))
             .from_(sb.subquery(inner, "d")).group_by(sb.ref("period"), sb.ref("segment"))
             .order_by(sb.ref("period"), sb.ref("segment")).limit(MAX_CELLS))
        notes = [f"KPI = {'rate' if spec.outcome.type in BOOLEAN_OUT else 'mean'} of {sb.describe(spec.outcome)} per "
                 f"{spec.time.grain or 'month'}; the last two periods are compared",
                 "rows with NULL outcome are counted as n_rows - n and excluded from the KPI"]
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"period": "period", "segment": "segment", "n_rows": "n_rows", "n": "n",
                                                        "total": "total", "sumsq": "sumsq"},
                                notes, purpose, dialect, MAX_CELLS, "aggregate")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        p0, p1, c0, c1, info = periods(rows["primary"], spec)
        if p0 is None or not c0 or not c1:
            out = no_data(spec, "need two periods with data", "rate_effect_z")
            out.stat.details.update(info)
            return out
        assert spec.outcome is not None
        boolean = spec.outcome.type in BOOLEAN_OUT
        d = decompose(c0, c1)
        warns = []
        if not d["common"]:
            warns.append("no segment is present in both periods; the change is all mix")
        if d["n1"] < 0.5 * d["n0"]:
            warns.append("the later period has < 50% of the earlier period's records; it may be incomplete")
        se = math.sqrt(d["var"]) if d["var"] > 0 else None
        z = d["rate"] / se if se else None
        p = float(2 * sps.norm.sf(abs(z))) if z is not None else None
        rel = d["rate"] / abs(d["k0"]) if d["k0"] else None
        supported = bool(p is not None and st._sig(p, alpha) and rel is not None and abs(rel) >= MIN_RELATIVE_EFFECT)
        sign = 1 if d["rate"] >= 0 else -1
        top = max(d["contrib"], key=lambda s: (sign * d["contrib"][s], s)) if d["contrib"] else None
        unit = (lambda v: st._f(abs(v) * 100, 1)) if boolean else (lambda v: st._f(abs(v), 2))
        kpi = "rate" if boolean else "mean"
        groups = []
        for s in sorted(set(c0) | set(c1)):
            a, b = c0.get(s), c1.get(s)
            groups.append({"segment": s, "n_before": int(a["n"]) if a else 0, "n_after": int(b["n"]) if b else 0,
                           f"{kpi}_before": st._f(a["mean"]) if a else None, f"{kpi}_after": st._f(b["mean"]) if b else None,
                           "share_before": st._f(a["n"] / d["n0"]) if a else 0.0, "share_after": st._f(b["n"] / d["n1"]) if b else 0.0,
                           "rate_contribution": st._f(d["contrib"].get(s))})
        hl = {"period_before": _period(p0), "period_after": _period(p1), f"{kpi}_before": st._q(d["k0"]),
              f"{kpi}_after": st._q(d["k1"]), "direction": "increase" if d["k1"] >= d["k0"] else "decrease",
              "within_segment_direction": "increase" if d["rate"] >= 0 else "decrease",
              "within_segment_points" if boolean else "within_segment_change": unit(d["rate"]),
              "mix_points" if boolean else "mix_change": unit(d["mix"]),
              "mix_direction": "increase" if d["mix"] >= 0 else "decrease",
              "dominant_effect": "within-segment" if abs(d["rate"]) >= abs(d["mix"]) else "mix",
              "top_segment": top, "top_segment_contribution": st._q(d["contrib"].get(top)) if top else None,
              "volume_effect": st._q(d["volume"]), "n_before": int(d["n0"]), "n_after": int(d["n1"]),
              "outcome": sb.describe(spec.outcome), "segment_by": sb.describe(spec.segment), "p_value": st._f(p, 8)}
        stat = StatResult(method=spec.method, test="rate_effect_z", n=int(d["n0"] + d["n1"]), statistic=st._f(z),
                          p_value=st._f(p, 12), effect_size=st._f(rel), effect_label="rate_effect_relative_to_kpi_before",
                          ci_low=st._f(d["rate"] - st.Z95 * se) if se else None,
                          ci_high=st._f(d["rate"] + st.Z95 * se) if se else None,
                          groups=groups, highlights=hl, warnings=warns, supported=supported,
                          assumptions=["independent records", "normal approximation per segment",
                                       "ci is for the within-segment (rate) effect in KPI units"],
                          details=info | {"kpi": kpi, "rate_effect": st._f(d["rate"]), "mix_effect": st._f(d["mix"]),
                                          "volume_effect": st._f(d["volume"]), "alpha": alpha,
                                          "threshold_relative_effect": MIN_RELATIVE_EFFECT})
        cols = ["segment", "n_before", "n_after", f"{kpi}_before", f"{kpi}_after", "share_before", "share_after",
                "rate_contribution"]
        return AnalysisOutcome(method=spec.method, stat=stat, table={"columns": cols, "rows": [[g[c] for c in cols] for g in groups]})

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        p0, p1, c0, c1, _ = periods(rows["primary"], spec)
        common = sorted(set(c0) & set(c1)) if p0 is not None else []
        if not common:
            return nothing_to_verify(spec, primary, "stratified_period_test", "no segment present in both periods")
        assert spec.outcome is not None
        n1 = sum(c["n"] for c in c1.values())
        if spec.outcome.type in BOOLEAN_OUT:
            tables = [np.array([[c1[s]["total"], c1[s]["n"] - c1[s]["total"]], [c0[s]["total"], c0[s]["n"] - c0[s]["total"]]])
                      for s in common]
            tables = [t + 0.5 if (t == 0).any() else t for t in tables]
            mh = st.mantel_haenszel(tables, alpha=alpha)
            odds, lo, hi = mh["odds_ratio"], mh["ci_low"], mh["ci_high"]
            pv, stat_v = mh["p_value"], mh["statistic"]
            direction = odds > 1 if (primary.statistic or 0) >= 0 else odds < 1
            supported = st._sig(pv, alpha) and (lo > 1 or hi < 1)
            v = StatResult(method=spec.method, test="cochran_mantel_haenszel", n=int(sum(c0[s]["n"] + c1[s]["n"] for s in common)),
                           statistic=st._f(stat_v), p_value=st._f(pv, 12), effect_size=st._f(odds),
                           effect_label="pooled_odds_ratio_after_vs_before", ci_low=st._f(lo), ci_high=st._f(hi),
                           highlights={"pooled_odds_ratio": st._q(odds), "p_value": st._f(pv, 8)},
                           assumptions=["segments as strata", "common odds ratio across strata"], supported=supported,
                           details={"alpha": alpha, "strata": len(common)})
            table = {"columns": ["test", "statistic", "p_value", "pooled_odds_ratio", "ci_low", "ci_high"],
                     "rows": [["cochran_mantel_haenszel", st._f(stat_v), st._f(pv, 12), st._f(odds), st._f(lo), st._f(hi)]]}
            return finish(spec, primary, v, direction, table)
        zs, ws, per = [], [], []
        for s in common:
            a, b = c0[s], c1[s]
            if a["n"] < 2 or b["n"] < 2 or a["var"] is None or b["var"] is None:
                continue
            se = math.sqrt(a["var"] / a["n"] + b["var"] / b["n"])
            if se == 0:
                continue
            z = (b["mean"] - a["mean"]) / se
            zs.append(z)
            ws.append(b["n"] / n1)
            per.append([s, st._f(z)])
        if not zs:
            return nothing_to_verify(spec, primary, "stouffer_welch_z", "no segment with enough data in both periods")
        w = np.array(ws)
        zc = float((w * np.array(zs)).sum() / math.sqrt((w ** 2).sum()))
        pv = float(2 * sps.norm.sf(abs(zc)))
        direction = (zc > 0) == ((primary.statistic or 0) > 0)
        v = StatResult(method=spec.method, test="stouffer_welch_z", n=int(sum(c0[s]["n"] + c1[s]["n"] for s in common)),
                       statistic=st._f(zc), p_value=st._f(pv, 12), effect_size=st._f(zc), effect_label="combined_z",
                       highlights={"combined_z": st._q(zc), "p_value": st._f(pv, 8), "strata": len(zs)},
                       assumptions=["segments as strata", "Welch z per segment, Stouffer weights = later segment shares"],
                       supported=st._sig(pv, alpha), details={"alpha": alpha})
        return finish(spec, primary, v, direction, {"columns": ["segment", "welch_z"], "rows": per})

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if "period_before" not in hl or not hl.get("top_segment"):
            return None
        seg, out, scope = text_parts(spec)
        boolean = "rate_before" in hl
        if boolean:
            before, after = fmt_pct(hl["rate_before"]), fmt_pct(hl["rate_after"])
            within, mix = f"{hl['within_segment_points']} points", f"{hl['mix_points']} points"
        else:
            before, after = f"{hl['mean_before']:.2f}", f"{hl['mean_after']:.2f}"
            within, mix = f"{hl['within_segment_change']}", f"{hl['mix_change']}"
        title = f"{cap(out)} changed within {seg} groups, led by {hl['top_segment']}"
        text = (f"{cap(out)} moved from {before} in {hl['period_before']} to {after} in {hl['period_after']}{scope}: "
                f"the change within {seg} groups accounts for {_article(hl['within_segment_direction'])} of {within} "
                f"(largest in {seg} = {hl['top_segment']}), the shift in {seg} mix for {_article(hl['mix_direction'])} of {mix}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="comparison", dimension="segment", measure="outcome")

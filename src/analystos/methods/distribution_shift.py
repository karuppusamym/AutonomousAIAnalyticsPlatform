"""distribution_shift: did a numeric outcome's distribution move in the latest `time` period?

The latest complete period (periods dated after today are future-dated rows and left out) is compared
with every earlier period, on a deterministic bounded row sample (`sqlbuild.sample_query`).

primary       two-sample Kolmogorov-Smirnov test + population stability index (PSI) over ten quantile bins
              of the reference. Supported when p < alpha and PSI >= 0.1 (a material shift).
verification  k-sample Anderson-Darling test on the same two samples, more sensitive in the tails
"""
from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods._series import as_date
from analystos.methods.base import (
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Semantic,
    cap_sample,
    finish,
    first,
    json_value,
    no_data,
    nothing_to_verify,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

MIN_GROUP = 30
PSI_MATERIAL = 0.1
BINS = 10
NUMERIC_OUT = {"column", "duration_hours"}


def psi(reference: np.ndarray, current: np.ndarray, bins: int = BINS) -> float:
    """Population stability index over the reference's quantile bins (empty bins floored at 1e-4)."""
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if edges.size < 2:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(reference, edges)[0] / reference.size
    c = np.histogram(current, edges)[0] / current.size
    r, c = np.clip(r, 1e-4, None), np.clip(c, 1e-4, None)
    return float(np.sum((c - r) * np.log(c / r)))


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def split(rows: list[dict[str, Any]], *, today: str | None = None) -> tuple[Any, np.ndarray, np.ndarray, int]:
    """(latest period, reference values, current values, future rows left out)."""
    cut = today or _today()
    by: dict[str, list[float]] = {}
    future = 0
    for r in rows:
        if r.get("period") is None or r.get("outcome") is None:
            continue
        key = str(json_value(r["period"]))
        if key[:10] > cut:
            future += 1
            continue
        by.setdefault(key, []).append(float(r["outcome"]))
    if not by:
        return None, np.array([]), np.array([]), future
    latest = max(by)
    ref = [v for k, vals in by.items() if k != latest for v in vals]
    return latest, np.asarray(ref, dtype=float), np.asarray(by[latest], dtype=float), future


class DistributionShift(AnalysisMethod):
    name = "distribution_shift"
    vocabulary = ("shift in the distribution of a numeric outcome in the latest `time` period versus the periods before "
                  "it (time.type = date_trunc; outcome.type in [column (numeric), duration_hours]).")
    outcome_types = frozenset({"numeric"})

    def applicable(self, outcome, segment) -> bool:
        return segment is None and outcome == "numeric"

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = []
        if spec.time is None or spec.time.type != "date_trunc" or not spec.time.grain:
            errors.append(f"{self.name} needs time = date_trunc with a grain")
        if spec.outcome is None or spec.outcome.type not in NUMERIC_OUT:
            errors.append(f"{self.name} needs a numeric outcome (column/duration_hours)")
        elif spec.outcome.type == "column" and semantic(spec.outcome) not in ("numeric", None):
            errors.append(f"outcome {spec.outcome.column} is not numeric")
        return errors

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "time")
        sb.require(spec, "outcome")
        assert spec.time is not None and spec.outcome is not None
        period = sb.time_period_expr(spec.time, dialect)
        out_e = sb.as_double(sb.derive(spec.outcome, dialect), dialect)
        q = sb.sample_query(spec, dialect, [("period", period), ("outcome", out_e)], sample_rows)
        notes = [f"deterministic hash-ordered sample of at most {sample_rows} (period, outcome) rows"]
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"period": "period", "outcome": "outcome",
                                                        "rows_total": "_rows_total", "rows_excluded": "_rows_excluded"},
                                notes, purpose, dialect, sample_rows, "sample")

    def test(self, rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        sample = rows["primary"]
        latest, ref, cur, future = split(sample)
        if latest is None or ref.size < MIN_GROUP or cur.size < MIN_GROUP:
            out = no_data(spec, f"need at least {MIN_GROUP} values in the latest period and before it", "ks_2samp+psi")
            out.stat.n = int(ref.size + cur.size)
            return out
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ks = sps.ks_2samp(ref, cur)
        index = psi(ref, cur)
        ref_med, cur_med = float(np.median(ref)), float(np.median(cur))
        supported = st._sig(float(ks.pvalue), alpha) and index >= PSI_MATERIAL
        rows_total = int(first(sample, "_rows_total", 0)) - int(first(sample, "_rows_excluded", 0))
        d = as_date(latest)
        stat = StatResult(
            method=spec.method, test="ks_2samp+psi", n=int(ref.size + cur.size), statistic=st._f(ks.statistic),
            p_value=st._f(float(ks.pvalue), 12), effect_size=st._f(index), effect_label="population_stability_index",
            groups=[{"segment": "reference", "n": int(ref.size), "median": st._f(ref_med), "mean": st._f(float(ref.mean()))},
                    {"segment": "latest period", "n": int(cur.size), "median": st._f(cur_med), "mean": st._f(float(cur.mean()))}],
            highlights={"psi": st._q(index), "ks_statistic": st._q(ks.statistic), "latest_period": d.isoformat() if d else str(latest),
                        "reference_median": st._q(ref_med), "latest_median": st._q(cur_med),
                        "latest_n": int(cur.size), "reference_n": int(ref.size), "outcome": sb.describe(spec.outcome)},
            assumptions=["independent rows", f"PSI over {BINS} reference quantile bins", "latest period complete"],
            supported=supported,
            details={"alpha": alpha, "psi_material": PSI_MATERIAL, "excluded_future_rows": future,
                     "sampled": rows_total > len(sample), "sample_size": len(sample), "rows_complete": rows_total})
        if future:
            stat.warnings.append(f"{future} sampled row(s) dated after today were left out")
        if rows_total > len(sample):
            stat.warnings.append(f"computed on a deterministic sample of {len(sample)} of {rows_total} complete rows")
        table = {"columns": ["group", "n", "median", "mean"],
                 "rows": [[g["segment"], g["n"], g["median"], g["mean"]] for g in stat.groups]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        latest, ref, cur, _ = split(rows["primary"])
        if latest is None or ref.size < MIN_GROUP or cur.size < MIN_GROUP:
            return nothing_to_verify(spec, primary, "anderson_darling_ksamp", "too few values to compare")
        a, b = cap_sample(ref.tolist(), 20000, 1), cap_sample(cur.tolist(), 20000, 2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ad = sps.anderson_ksamp([a, b])
        p = float(ad.pvalue if hasattr(ad, "pvalue") else ad.significance_level)
        supported = st._sig(p, alpha)
        v = StatResult(method=spec.method, test="anderson_darling_ksamp", n=int(a.size + b.size), statistic=st._f(ad.statistic),
                       p_value=st._f(p, 12), highlights={"ad_statistic": st._q(ad.statistic), "p_value": st._f(p, 8)},
                       assumptions=["p-value interpolated and capped to [0.001, 0.25] by SciPy"], supported=supported,
                       details={"alpha": alpha})
        table = {"columns": ["statistic", "p_value"], "rows": [[st._f(ad.statistic), st._f(p, 12)]]}
        return finish(spec, primary, v, True if supported else None, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if not isinstance(hl.get("psi"), int | float):
            return None
        _, out, scope = text_parts(spec)
        title = f"The distribution of {out} shifted in the latest period"
        text = (f"In the latest period the median {out} moved from {hl['reference_median']:.1f} to "
                f"{hl['latest_median']:.1f}; the population stability index against earlier periods is "
                f"{hl['psi']:.2f}{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="distribution", dimension="time", measure="outcome")

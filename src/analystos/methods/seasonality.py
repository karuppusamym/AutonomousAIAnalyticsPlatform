"""seasonality: a repeating within-year (or within-week) pattern in volume or a numeric outcome over `time`.

primary       seasonal decomposition (STL with the `ml` extra, else classical) -> seasonal strength
              Fs = max(0, 1 - Var(R)/Var(S+R)); p-value: Kruskal-Wallis of the detrended values grouped by
              season position. Supported when p < alpha and Fs >= 0.3, with two full seasons of history.
verification  autocorrelation of the detrended series at the season lag (large-sample z test), an
              independent reading of the same periodicity
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods._series import SEASON, decompose, season_label, seasonal_strength, with_units
from analystos.methods.base import (
    AnalysisOutcome,
    ChartIntent,
    Semantic,
    cap,
    finish,
    json_value,
    no_data,
    nothing_to_verify,
    text_parts,
)
from analystos.methods.trend import Trend, series
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

MIN_STRENGTH = 0.3
MIN_SEASONS = 2


class Seasonality(Trend):
    name = "seasonality"
    vocabulary = ("seasonal pattern of volume (outcome null) or a numeric outcome over `time` (time.type = date_trunc, "
                  "grain day|week|month|quarter); needs two full seasons of periods.")
    playbook = None

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = super().validate(spec, semantic)
        if not errors and spec.time is not None and spec.time.grain not in SEASON:
            errors.append(f"{self.name} needs grain day, week, month or quarter")
        return errors

    def _data(self, rows, spec: AnalysisSpec):
        periods, y, raw, excl = series(rows["primary"], spec)
        m = SEASON.get(spec.time.grain if spec.time else "", 12)
        return periods, np.asarray(y, dtype=float), raw, excl, m

    def test(self, rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, raw, excl, m = self._data(rows, spec)
        n = int(y.size)
        if n < MIN_SEASONS * m:
            out = no_data(spec, f"need two full seasons ({MIN_SEASONS * m} periods) to read seasonality; have {n}",
                          "seasonal_decomposition")
            out.stat.n = n
            return out
        dec = decompose(y, m)
        fs = seasonal_strength(dec["seasonal"], dec["resid"])
        detr = y - dec["trend"]
        pos = np.arange(n) % m
        groups = [detr[pos == k] for k in range(m) if np.sum(pos == k) >= 2]
        try:
            h, p = sps.kruskal(*groups) if len(groups) >= 2 and np.ptp(detr) > 0 else (0.0, 1.0)
        except ValueError:  # every value identical within the positions
            h, p = 0.0, 1.0
        season = dec["seasonal"]
        k_hi, k_lo = int(np.argmax(season[:m])), int(np.argmin(season[:m]))
        grain = spec.time.grain if spec.time else "month"
        peak, trough = season_label(periods[k_hi], grain), season_label(periods[k_lo], grain)
        level = float(np.mean(np.abs(y))) or 1.0
        amplitude = float(season.max() - season.min())
        supported = st._sig(p, alpha) and fs >= MIN_STRENGTH
        metric = "count" if spec.outcome is None else f"mean({sb.describe(spec.outcome)})"
        stat = StatResult(
            method=spec.method, test="seasonal_decomposition+kruskal_wallis", n=n, statistic=st._f(h), p_value=st._f(p, 12),
            effect_size=st._f(fs), effect_label="seasonal_strength",
            highlights={"seasonal_strength": st._q(fs), "season_length": m, "peak_season": peak, "trough_season": trough,
                        "seasonal_amplitude_share": st._q(amplitude / level), "n_periods": n, "metric": metric},
            assumptions=["additive seasonality", f"season of {m} periods ({grain})", "two or more full seasons",
                         "seasonal strength = 1 - Var(remainder) / Var(seasonal + remainder)"],
            supported=supported,
            details={"engine": dec["engine"], "alpha": alpha, "min_strength": MIN_STRENGTH, "metric": metric, **excl,
                     "seasonal_profile": [st._f(v) for v in season[:m]]})
        if dec["engine"] != "stl":
            stat.warnings.append("STL needs the `ml` extra; a classical moving-average decomposition was used")
        cols = ["period", "value", "trend", "seasonal", "remainder"]
        table = {"columns": cols, "rows": [[json_value(pp), st._f(v), st._f(t), st._f(s), st._f(r)] for pp, v, t, s, r in
                                           zip(periods, y, dec["trend"], season, dec["resid"], strict=False)]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, _, _, m = self._data(rows, spec)
        n = int(y.size)
        if n < MIN_SEASONS * m:
            return nothing_to_verify(spec, primary, "seasonal_autocorrelation", "fewer than two full seasons", n)
        x = y - decompose(y, m)["trend"]
        x = x - x.mean()
        denom = float(np.dot(x, x))
        r = float(np.dot(x[:-m], x[m:]) / denom) if denom > 0 else 0.0
        z = r * math.sqrt(n)
        p = float(2 * sps.norm.sf(abs(z)))
        supported = st._sig(p, alpha) and r > 0
        v = StatResult(method=spec.method, test="seasonal_autocorrelation", n=n, statistic=st._f(z), p_value=st._f(p, 12),
                       effect_size=st._f(r), effect_label="acf_at_season_lag",
                       highlights={"acf_at_season_lag": st._q(r), "season_length": m, "p_value": st._f(p, 8)},
                       assumptions=["large-sample standard error 1/sqrt(n)", "detrended with the same decomposition"],
                       supported=supported, details={"alpha": alpha})
        table = {"columns": ["lag", "acf", "z", "p_value"], "rows": [[m, st._f(r), st._f(z), st._f(p, 12)]]}
        return finish(spec, primary, v, r > 0, table)

    def facts(self, spec, stat, *, extra=None, query_ids=(), result_hashes=()):
        return with_units(super().facts(spec, stat, extra=extra, query_ids=query_ids, result_hashes=result_hashes),
                          {"season_length": ("count", "count"), "seasonal_amplitude_share": ("fraction", "share")})

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if hl.get("seasonal_strength") is None:
            return None
        _, out, scope = text_parts(spec)
        title = f"{cap(out)} follows a seasonal pattern"
        text = (f"{cap(out)} repeats a cycle of {hl['season_length']} periods, peaking in {hl['peak_season']} and lowest "
                f"in {hl['trough_season']}; its seasonal strength is {hl['seasonal_strength']:.2f}{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="trend", dimension="time", measure="volume" if spec.outcome is None else "outcome")

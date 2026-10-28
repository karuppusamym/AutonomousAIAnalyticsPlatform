"""forecast_backtest: a forecast of volume or a numeric outcome over `time`, judged by how it would have done.

primary       ETS (statsmodels Holt-Winters, additive damped trend, additive season with two full seasons;
              `skills.forecast`) evaluated by a rolling-origin backtest: K non-overlapping folds, each fitted
              on everything before its origin and forecasting `h` periods. Reported per fold and overall:
              MAE, the naive MAE (last value, or one season back when that fits the training data better), their ratio, and the share of
              actuals inside the prediction interval. Significance: a one-sided Diebold-Mariano test on the
              absolute-error differential (does the model beat naive?). Supported when p < alpha and the
              relative MAE is below 1. The forecast itself (point + interval for `h` periods) is the result.
verification  ARIMA(1,1,1) (seasonal (0,1,1) with two full seasons; statsmodels) on the same folds, an
              independent model family: agrees when it
              also beats naive. Without statsmodels a drift model stands in, and says so.
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods._series import SEASON, with_units
from analystos.methods.base import (
    AnalysisOutcome,
    ChartIntent,
    cap,
    finish,
    fmt_pct,
    json_value,
    no_data,
    nothing_to_verify,
    text_parts,
)
from analystos.methods.trend import Trend, series
from analystos.skills import forecast as fc
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

MIN_PERIODS = 12
MAX_FOLDS = 5
LEVEL = 0.9


def horizon_for(n: int) -> int:
    return max(1, min(4, n // 6))


def _season(grain: str | None, n_train: int) -> int | None:
    m = SEASON.get(grain or "")
    return m if m and n_train >= 2 * m else None


def _naive(train: np.ndarray, h: int, m: int | None) -> np.ndarray:
    """The baseline: the last value, or the value one season back when that is the better simple forecast on
    the training data itself (so a random walk is compared with the naive forecast it cannot beat)."""
    last = np.full(h, train[-1], dtype=float)
    if not m or train.size <= m + 1:
        return last
    seasonal_err = float(np.mean(np.abs(train[m:] - train[:-m])))
    naive_err = float(np.mean(np.abs(train[1:] - train[:-1])))
    if seasonal_err < naive_err:
        return np.array([train[-m + (i % m)] for i in range(h)], dtype=float)
    return last


def _arima(train: np.ndarray, h: int, m: int | None = None) -> tuple[np.ndarray, str]:
    """ARIMA(1,1,1), seasonal (0,1,1)_m when two full seasons are there to fit it."""
    try:
        from analystos.core.profiles import require_extra

        require_extra("ml", "ARIMA forecasting")
        from statsmodels.tsa.arima.model import ARIMA

        seasonal = (0, 1, 1, m) if m else (0, 0, 0, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit = ARIMA(train, order=(1, 1, 1), seasonal_order=seasonal).fit()
        point = np.asarray(fit.forecast(h), dtype=float)
        if np.all(np.isfinite(point)):
            return point, f"sarima_1_1_1_0_1_1_{m}" if m else "arima_1_1_1"
    except Exception:  # noqa: BLE001 - no statsmodels, or a numerical failure: drift stands in
        pass
    drift = float(np.diff(train).mean()) if train.size > 1 else 0.0
    return train[-1] + drift * np.arange(1, h + 1), "drift"


def backtest(y: np.ndarray, grain: str | None, *, model: str = "ets", level: float = LEVEL) -> dict[str, Any]:
    """Rolling-origin evaluation: folds of `h` periods at the end of the series, oldest first."""
    n = int(y.size)
    h = horizon_for(n)
    k = max(1, min(MAX_FOLDS, (n - MIN_PERIODS // 2) // h))
    folds, e_model, e_naive, covered, inside = [], [], [], 0, 0
    engines: set[str] = set()
    for f in range(k):
        origin = n - (k - f) * h
        train, actual = y[:origin], y[origin:origin + h]
        m = _season(grain, int(train.size))
        if model == "ets":
            r = fc._fit(train, h, m, level)
            point, lower, upper, engine = r["point"], r["lower"], r["upper"], r["method"]
        else:
            point, engine = _arima(train, h, m)
            lower = upper = None
        engines.add(engine)
        base = _naive(train, h, m)
        em, en = np.abs(actual - point[:actual.size]), np.abs(actual - base[:actual.size])
        e_model += em.tolist()
        e_naive += en.tolist()
        hit = None
        if lower is not None:
            ok = (actual >= lower[:actual.size]) & (actual <= upper[:actual.size])
            inside += int(ok.sum())
            covered += int(actual.size)
            hit = float(ok.mean())
        folds.append({"fold": f + 1, "origin": origin, "train_n": int(train.size), "horizon": int(actual.size),
                      "mae": st._f(float(em.mean())), "naive_mae": st._f(float(en.mean())), "coverage": st._f(hit),
                      "engine": engine, "seasonal_periods": m})
    em, en = np.asarray(e_model), np.asarray(e_naive)
    mae, naive_mae = float(em.mean()), float(en.mean())
    rel = mae / naive_mae if naive_mae > 0 else (0.0 if mae == 0 else math.inf)
    d = em - en  # < 0 where the model was closer
    if d.size >= 2 and float(d.std(ddof=1)) > 0:
        dm = float(d.mean() / (d.std(ddof=1) / math.sqrt(d.size)))
        p = float(sps.t.cdf(dm, df=d.size - 1))  # one-sided: the model's errors are smaller
    else:
        dm, p = 0.0, (0.0 if mae < naive_mae else 1.0)
    return {"folds": folds, "horizon": h, "n_folds": k, "mae": mae, "naive_mae": naive_mae, "relative_mae": rel,
            "coverage": (inside / covered) if covered else None, "dm_statistic": dm, "p_value": p, "level": level,
            "engines": sorted(engines), "errors": int(em.size)}


class ForecastBacktest(Trend):
    name = "forecast_backtest"
    vocabulary = ("forecast of volume (outcome null) or a numeric outcome over `time` (time.type = date_trunc), judged "
                  "by a rolling-origin backtest against a naive forecast; needs 12+ periods.")
    playbook = None

    def test(self, rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, raw, excl = series(rows["primary"], spec)
        arr = np.asarray(y, dtype=float)
        n = int(arr.size)
        if n < MIN_PERIODS:
            out = no_data(spec, f"need at least {MIN_PERIODS} periods for a backtested forecast; have {n}", "ets_backtest")
            out.stat.n = n
            return out
        grain = spec.time.grain if spec.time else None
        bt = backtest(arr, grain)
        h = bt["horizon"]
        future = fc.forecast_series(arr, [json_value(p) for p in periods], horizon=h,
                                    seasonal_periods=_season(grain, n), level=LEVEL)
        nxt = future.highlights
        supported = st._sig(bt["p_value"], alpha) and bt["relative_mae"] < 1
        metric = "count" if spec.outcome is None else f"mean({sb.describe(spec.outcome)})"
        stat = StatResult(
            method=spec.method, test="ets_rolling_origin_backtest+diebold_mariano", n=n,
            statistic=st._f(bt["dm_statistic"]), p_value=st._f(bt["p_value"], 12),
            effect_size=st._f(1 - bt["relative_mae"]) if math.isfinite(bt["relative_mae"]) else None,
            effect_label="skill_vs_naive", ci_low=future.ci_low, ci_high=future.ci_high,
            groups=future.groups,
            highlights={"backtest_folds": bt["n_folds"], "horizon": h, "relative_mae": st._q(bt["relative_mae"]),
                        "interval_coverage": st._q(bt["coverage"]), "backtest_mae": st._q(bt["mae"]),
                        "naive_mae": st._q(bt["naive_mae"]), "next_value": nxt.get("next_value"),
                        "next_lower": nxt.get("lower"), "next_upper": nxt.get("upper"),
                        "forecast_direction": nxt.get("trend_direction"), "n_periods": n, "metric": metric},
            assumptions=[*future.assumptions, "rolling origin: each fold fitted only on earlier periods",
                         "Diebold-Mariano on absolute errors, one-sided"],
            warnings=list(future.warnings),
            supported=supported,
            details={"backtest": {k: (st._f(v) if isinstance(v, float) else v) for k, v in bt.items()},
                     "forecast": {"method": nxt.get("method"), "horizon": h, "level": LEVEL,
                                  "next_period": nxt.get("next_period")}, "alpha": alpha, "metric": metric, **excl})
        if bt["coverage"] is not None and bt["coverage"] < LEVEL - 0.25:
            stat.warnings.append(f"backtest interval coverage {fmt_pct(bt['coverage'])} is well below the nominal "
                                 f"{fmt_pct(LEVEL)}: the intervals are too narrow")
        cols = ["fold", "origin", "train_n", "horizon", "mae", "naive_mae", "coverage", "engine"]
        table = {"columns": cols, "rows": [[f[c] for c in cols] for f in bt["folds"]]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        _, y, _, _ = series(rows["primary"], spec)
        arr = np.asarray(y, dtype=float)
        if arr.size < MIN_PERIODS:
            return nothing_to_verify(spec, primary, "arima_backtest", f"fewer than {MIN_PERIODS} periods", int(arr.size))
        bt = backtest(arr, spec.time.grain if spec.time else None, model="arima")
        supported = bt["relative_mae"] < 1 and st._sig(bt["p_value"], alpha)
        v = StatResult(method=spec.method, test=f"{'+'.join(bt['engines'])}_rolling_origin_backtest", n=int(arr.size),
                       statistic=st._f(bt["dm_statistic"]), p_value=st._f(bt["p_value"], 12),
                       effect_size=st._f(1 - bt["relative_mae"]) if math.isfinite(bt["relative_mae"]) else None,
                       effect_label="skill_vs_naive",
                       highlights={"relative_mae": st._q(bt["relative_mae"]), "backtest_folds": bt["n_folds"]},
                       assumptions=["same folds as the primary backtest", "an independent model family"],
                       supported=supported, details={"alpha": alpha, "backtest": {k: v for k, v in bt.items() if k != "folds"},
                                                     "folds": bt["folds"]})
        if "drift" in bt["engines"]:
            v.warnings.append("ARIMA needs the `ml` extra (statsmodels); a drift model stood in")
        table = {"columns": ["fold", "mae", "naive_mae", "engine"],
                 "rows": [[f["fold"], f["mae"], f["naive_mae"], f["engine"]] for f in bt["folds"]]}
        return finish(spec, primary, v, True if supported else None, table)

    def facts(self, spec, stat, *, extra=None, query_ids=(), result_hashes=()):
        return with_units(super().facts(spec, stat, extra=extra, query_ids=query_ids, result_hashes=result_hashes),
                          {"interval_coverage": ("fraction", "share"), "backtest_folds": ("count", "count"),
                           "relative_mae": ("ratio", "statistic")})

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        rel, cov = hl.get("relative_mae"), hl.get("interval_coverage")
        if not isinstance(rel, int | float):
            return None
        _, out, scope = text_parts(spec)
        title = f"{cap(out)} can be forecast better than a naive guess"
        text = (f"Across {hl.get('backtest_folds')} rolling-origin backtest folds the {out} forecast's average error was "
                f"{rel:.2f} times a naive forecast's")
        if isinstance(cov, int | float):
            text += f", and {fmt_pct(cov)} of actual values fell inside its prediction interval"
        if all(isinstance(hl.get(k), int | float) for k in ("next_value", "next_lower", "next_upper")):
            text += (f"; the next period is forecast at {hl['next_value']:.1f} (interval {hl['next_lower']:.1f} to "
                     f"{hl['next_upper']:.1f})")
        return title, text + f"{scope}."

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="trend", dimension="time", measure="volume" if spec.outcome is None else "outcome")

"""Deterministic forecasting skills: Holt-Winters with honest intervals, and forecast deviation.

``forecast_series`` fits statsmodels ``ExponentialSmoothing`` (additive damped trend; additive
seasonality only when the series holds at least two full seasons) and derives prediction intervals
by simulating the fitted state-space model with a fixed seed, so the same input always gives the
same numbers. Short series (< 8 points) fall back to naive / drift forecasts with the textbook
interval widths; constant or degenerate series return a flat forecast instead of raising.

``forecast_deviation`` answers "is the latest point unusual given the history?": it fits on all
but the last ``holdout`` points and checks whether the actual values fall outside the interval.
``supported=True`` means a deviation was detected.
"""
from __future__ import annotations

import math
import re
import warnings
from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import StatResult
from analystos.core.errors import FeatureUnavailable
from analystos.skills.stats import _f, _period_str, _q

MIN_HW_POINTS = 8
SIM_REPETITIONS = 1000
SEED = 0
FLAT_TREND_PCT = 0.01  # |forecast change over the horizon| below 1% of the level reads as "flat"


# --------------------------------------------------------------------------------------------
# periods
# --------------------------------------------------------------------------------------------
_YM = re.compile(r"^(\d{4})-(\d{2})$")
_YQ = re.compile(r"^(\d{4})-?Q([1-4])$", re.I)


def _infer_step(periods: Sequence[Any]) -> tuple[str | None, Any]:
    """('month' | 'quarter' | pandas freq | None, parsed last period) for regular period labels."""
    import pandas as pd

    labels = [str(_period_str(p)) for p in periods]
    if labels and all(_YM.match(x) for x in labels):
        return "month", labels[-1]
    if labels and all(_YQ.match(x) for x in labels):
        return "quarter", labels[-1]
    if len(labels) < 3:
        return None, None
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(labels))
        freq = pd.infer_freq(idx)
    except (ValueError, TypeError):
        return None, None
    return (freq, idx[-1]) if freq else (None, None)


def _future_periods(periods: Sequence[Any] | None, n: int, horizon: int) -> list[Any]:
    if periods is None or len(periods) != n:
        return [f"t+{i}" for i in range(1, horizon + 1)]
    step, last = _infer_step(periods)
    if step == "month":
        y, m = map(int, _YM.match(last).groups())  # type: ignore[union-attr]
        out = []
        for _ in range(horizon):
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            out.append(f"{y:04d}-{m:02d}")
        return out
    if step == "quarter":
        y, q = map(int, _YQ.match(last).groups())  # type: ignore[union-attr]
        out = []
        for _ in range(horizon):
            y, q = (y + 1, 1) if q == 4 else (y, q + 1)
            out.append(f"{y:04d}-Q{q}")
        return out
    if step:
        import pandas as pd

        rng = pd.date_range(start=last, periods=horizon + 1, freq=step)[1:]
        date_only = all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(_period_str(p))) for p in periods)
        return [d.date().isoformat() if date_only else d.isoformat() for d in rng]
    return [f"{_period_str(periods[-1])}+{i}" for i in range(1, horizon + 1)]


def _season_from_periods(periods: Sequence[Any] | None) -> int | None:
    """Default season length from the period spacing: monthly 12, quarterly 4, weekly 52, daily 7."""
    if not periods:
        return None
    step, _ = _infer_step(periods)
    if step is None:
        return None
    s = str(step).upper()
    if step == "month" or s.startswith("M"):
        return 12
    if step == "quarter" or s.startswith("Q"):
        return 4
    if s.startswith("W"):
        return 52
    if s in {"D", "B"}:
        return 7
    if s in {"H", "h"}:
        return 24
    return None


# --------------------------------------------------------------------------------------------
# core fit
# --------------------------------------------------------------------------------------------
def _clean(values: Sequence[float], periods: Sequence[Any] | None) -> tuple[np.ndarray, list[Any] | None]:
    y = np.asarray([np.nan if v is None else v for v in values], dtype=float)
    ok = np.isfinite(y)
    per = [p for p, k in zip(periods, ok, strict=False) if k] if periods is not None and len(periods) == len(y) else None
    return y[ok], per


def _fit(y: np.ndarray, horizon: int, seasonal_periods: int | None, level: float) -> dict[str, Any]:
    """Point forecast + interval for `horizon` steps. Never raises for finite input with n >= 1."""
    n = int(y.size)
    zq = float(sps.norm.ppf(0.5 + level / 2))
    warns: list[str] = []
    if n == 0:
        raise ValueError("empty series")
    if np.ptp(y) == 0:
        pt = np.full(horizon, y[-1])
        return {"method": "constant", "point": pt, "lower": pt.copy(), "upper": pt.copy(), "fitted": np.full(n, y[-1]),
                "sigma": 0.0, "seasonal_periods": None, "trend": 0.0,
                "warnings": ["series is constant; interval has zero width"]}
    if n >= MIN_HW_POINTS:
        try:
            return _fit_holt_winters(y, horizon, seasonal_periods, level, warns)
        except FeatureUnavailable:  # a lite install without the `ml` extra: a stated, labelled degradation
            warns.append("Holt-Winters needs the `ml` extra (statsmodels), which this installation lacks; used drift")
        except Exception as e:  # noqa: BLE001 - numerical failure falls back to drift, never to the caller
            warns.append(f"Holt-Winters fit failed ({type(e).__name__}); used drift")
    else:
        warns.append(f"fewer than {MIN_HW_POINTS} points; used a {'drift' if n >= 3 else 'naive'} forecast")
    h = np.arange(1, horizon + 1, dtype=float)
    if n >= 3:
        d = np.diff(y)
        drift = float(d.mean())
        sigma = float(d.std(ddof=1)) if d.size > 1 else 0.0
        pt = y[-1] + drift * h
        se = sigma * np.sqrt(h * (1 + h / (n - 1)))
        fitted = np.concatenate([[y[0]], y[:-1] + drift])
        method = "drift"
        trend = drift
    else:
        sigma = float(abs(y[-1] - y[0])) if n == 2 else 0.0
        pt = np.full(horizon, y[-1])
        se = sigma * np.sqrt(h)
        fitted = np.concatenate([[y[0]], y[:-1]])
        method = "naive"
        trend = 0.0
        if n == 1:
            warns.append("single observation; interval has zero width")
    return {"method": method, "point": pt, "lower": pt - zq * se, "upper": pt + zq * se, "fitted": fitted,
            "sigma": sigma, "seasonal_periods": None, "trend": trend, "warnings": warns}


def _fit_holt_winters(y: np.ndarray, horizon: int, seasonal_periods: int | None, level: float,
                      warns: list[str]) -> dict[str, Any]:
    from analystos.core.profiles import require_extra

    require_extra("ml", "Holt-Winters forecasting")
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    n = int(y.size)
    sp = seasonal_periods if seasonal_periods and seasonal_periods >= 2 and n >= 2 * seasonal_periods else None
    if seasonal_periods and sp is None:
        warns.append(f"fewer than two full seasons of {seasonal_periods}; seasonality not modelled")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model = ExponentialSmoothing(y, trend="add", damped_trend=True, seasonal="add" if sp else None,
                                     seasonal_periods=sp, initialization_method="estimated")
        fit = model.fit(optimized=True)
        point = np.asarray(fit.forecast(horizon), dtype=float)
        sims = np.asarray(fit.simulate(horizon, repetitions=SIM_REPETITIONS, error="add", anchor="end",
                                       random_state=SEED), dtype=float).reshape(horizon, -1)
    if any("converge" in str(w.message).lower() for w in caught):
        warns.append("optimizer reported non-convergence; interval may be too narrow")
    if not np.all(np.isfinite(point)):
        raise FloatingPointError("non-finite forecast")
    fitted = np.asarray(fit.fittedvalues, dtype=float)
    resid = y - fitted
    sigma = float(np.sqrt(np.mean(resid ** 2)))
    tail = (1 - level) / 2
    lo = np.quantile(sims, tail, axis=1)
    hi = np.quantile(sims, 1 - tail, axis=1)
    # simulation noise must not make the interval exclude its own point forecast
    lo, hi = np.minimum(lo, point), np.maximum(hi, point)
    trend_comp = getattr(fit, "trend", None)
    trend = float(np.asarray(trend_comp)[-1]) if trend_comp is not None and len(np.asarray(trend_comp)) else 0.0
    return {"method": "holt_winters", "point": point, "lower": lo, "upper": hi, "fitted": fitted, "sigma": sigma,
            "seasonal_periods": sp, "trend": trend, "warnings": warns,
            "params": {k: _f(v, 6) for k, v in fit.params.items()
                       if k in {"smoothing_level", "smoothing_trend", "smoothing_seasonal", "damping_trend"}}}


def _direction(point: np.ndarray, y: np.ndarray) -> str:
    scale = max(float(np.mean(np.abs(y))), 1e-12)
    change = float(point[-1] - y[-1]) if point.size else 0.0
    if point.size > 1:
        change = float(point[-1] - point[0]) if abs(point[-1] - point[0]) > 0 else change
    if abs(change) < FLAT_TREND_PCT * scale:
        return "flat"
    return "increasing" if change > 0 else "decreasing"


# --------------------------------------------------------------------------------------------
# public skills
# --------------------------------------------------------------------------------------------
def forecast_series(values: Sequence[float], periods: Sequence[Any] | None = None, *, horizon: int = 4,
                    seasonal_periods: int | None = None, level: float = 0.95) -> StatResult:
    """Holt-Winters (additive damped trend, optional additive season) forecast with prediction intervals.

    ``seasonal_periods`` defaults from the period spacing (monthly 12, quarterly 4, weekly 52, daily 7)
    and is only used with at least two full seasons. Intervals: simulated from the fitted model
    (1000 seeded paths) for Holt-Winters; drift/naive closed forms otherwise.
    """
    horizon = max(1, int(horizon))
    y, per = _clean(values, periods)
    n = int(y.size)
    if n == 0:
        return StatResult(method="forecast", test="none", n=0, supported=False, warnings=["no finite observations"],
                          highlights={"method": "none", "horizon": horizon})
    sp = seasonal_periods if seasonal_periods is not None else _season_from_periods(per)
    r = _fit(y, horizon, sp, level)
    fut = _future_periods(per, n, horizon)
    hist_periods = per if per is not None else list(range(n))
    groups = [{"period": _period_str(p), "kind": "history", "value": _f(v), "fitted": _f(fv)}
              for p, v, fv in zip(hist_periods, y, r["fitted"], strict=False)]
    groups += [{"period": p, "kind": "forecast", "forecast": _f(v), "lower": _f(lo), "upper": _f(hi)}
               for p, v, lo, hi in zip(fut, r["point"], r["lower"], r["upper"], strict=False)]
    trend_dir = _direction(r["point"], y)
    hl = {"next_period": fut[0], "next_value": _q(r["point"][0]), "lower": _q(r["lower"][0]),
          "upper": _q(r["upper"][0]), "last_value": _q(y[-1]), "horizon_value": _q(r["point"][-1]),
          "trend_direction": trend_dir, "method": r["method"], "horizon": horizon, "level": level,
          "seasonal_periods": r["seasonal_periods"], "n_periods": n}
    test = {"holt_winters": "holt_winters_damped_additive", "drift": "drift", "naive": "naive",
            "constant": "constant"}[r["method"]]
    assumptions = ["equally spaced periods", "additive errors", f"{int(level * 100)}% prediction interval"]
    if r["method"] == "holt_winters":
        assumptions.append(f"interval from {SIM_REPETITIONS} simulated paths (seed {SEED})")
    return StatResult(method="forecast", test=test, n=n, statistic=_f(r["point"][0]), effect_size=_f(r["trend"]),
                      effect_label="trend_per_period", ci_low=_f(r["lower"][0]), ci_high=_f(r["upper"][0]),
                      groups=groups, highlights=hl, assumptions=assumptions, warnings=list(r["warnings"]),
                      supported=None,
                      details={"residual_sigma": _f(r["sigma"]), "params": r.get("params", {}),
                               "seasonal_periods_requested": seasonal_periods})


def forecast_deviation(values: Sequence[float], periods: Sequence[Any] | None = None, *, holdout: int = 1,
                       z: float = 2.0, seasonal_periods: int | None = None) -> StatResult:
    """Fit on all but the last ``holdout`` points; flag the latest actual if it falls outside the
    two-sided interval with coverage 2*Phi(z)-1 (z=2 -> 95.4%). ``supported=True`` means deviation.

    effect_size is the standardized deviation of the last point: (actual - forecast) / (half-width / z).
    """
    holdout = max(1, int(holdout))
    y, per = _clean(values, periods)
    n = int(y.size)
    train, actual = y[:-holdout], y[-holdout:]
    if train.size < 3:
        return StatResult(method="forecast_deviation", test="none", n=n, supported=False,
                          warnings=[f"need at least 3 points before the {holdout} held-out point(s)"],
                          highlights={"deviation": False, "holdout": holdout})
    level = float(2 * sps.norm.cdf(z) - 1)
    train_per = per[:-holdout] if per is not None else None
    sp = seasonal_periods if seasonal_periods is not None else _season_from_periods(train_per)
    r = _fit(train, holdout, sp, level)
    hold_periods = per[-holdout:] if per is not None else list(range(train.size, n))
    groups = []
    for p, a, f, lo, hi in zip(hold_periods, actual, r["point"], r["lower"], r["upper"], strict=False):
        outside = bool(a < lo or a > hi) if hi > lo else bool(not math.isclose(a, f, rel_tol=1e-9, abs_tol=1e-12))
        groups.append({"period": _period_str(p), "actual": _f(a), "forecast": _f(f), "lower": _f(lo), "upper": _f(hi),
                       "outside": outside, "direction": "above" if a > f else ("below" if a < f else "on")})
    last = groups[-1]
    a, f, lo, hi = actual[-1], r["point"][-1], r["lower"][-1], r["upper"][-1]
    half = (hi - lo) / 2
    zscore = (a - f) / (half / z) if half > 0 else (0.0 if a == f else math.copysign(math.inf, a - f))
    dev_pct = (a - f) / abs(f) if f != 0 else None
    warns = list(r["warnings"])
    if half == 0:
        warns.append("zero-width interval (constant or tiny history): any change counts as a deviation")
    hl = {"deviation": last["outside"], "period": last["period"], "actual": _q(a), "expected": _q(f),
          "lower": _q(lo), "upper": _q(hi), "direction": last["direction"], "deviation_pct": _q(dev_pct),
          "z_score": _q(zscore), "method": r["method"], "holdout": holdout, "z": z,
          "any_holdout_outside": any(g["outside"] for g in groups)}
    return StatResult(method="forecast_deviation", test=f"{r['method']}_interval", n=n, statistic=_f(zscore),
                      effect_size=_f(dev_pct), effect_label="pct_deviation_from_forecast", ci_low=_f(lo), ci_high=_f(hi),
                      groups=groups, highlights=hl,
                      assumptions=[f"interval coverage {level:.3f} (z={z})", "model fitted without the held-out points"],
                      warnings=warns, supported=bool(last["outside"]),
                      details={"train_n": int(train.size), "residual_sigma": _f(r["sigma"]),
                               "seasonal_periods": r["seasonal_periods"]})


# --------------------------------------------------------------------------------------------
# the `ml`-extra models the time-series method pack uses (S07-T6); methods never import statsmodels
# --------------------------------------------------------------------------------------------
def stl_components(y: Sequence[float], period: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Robust STL (trend, seasonal, remainder). Raises FeatureUnavailable without the `ml` extra."""
    from analystos.core.profiles import require_extra

    require_extra("ml", "STL decomposition")
    from statsmodels.tsa.seasonal import STL

    fit = STL(np.asarray(y, dtype=float), period=int(period), robust=True).fit()
    return np.asarray(fit.trend), np.asarray(fit.seasonal), np.asarray(fit.resid)


def arima_point(train: Sequence[float], horizon: int, seasonal_periods: int | None = None) -> np.ndarray:
    """ARIMA(1,1,1) point forecast, seasonal (0,1,1)_m when `seasonal_periods` is given. Raises
    FeatureUnavailable without the `ml` extra, FloatingPointError on a non-finite forecast."""
    from analystos.core.profiles import require_extra

    require_extra("ml", "ARIMA forecasting")
    from statsmodels.tsa.arima.model import ARIMA

    seasonal = (0, 1, 1, int(seasonal_periods)) if seasonal_periods else (0, 0, 0, 0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ARIMA(np.asarray(train, dtype=float), order=(1, 1, 1), seasonal_order=seasonal).fit()
    point = np.asarray(fit.forecast(int(horizon)), dtype=float)
    if not np.all(np.isfinite(point)):
        raise FloatingPointError("non-finite ARIMA forecast")
    return point

"""Shared pieces of the time-series method pack (spec S07-T6): season lengths, season labels without
digits (a label is masked by the numbers guard, so it must not carry numbers of its own), a seasonal
decomposition (statsmodels STL when the `ml` extra is installed, else classical moving-average
decomposition, both deterministic), and typed-fact unit overrides for statistics the shared highlight
vocabulary does not know."""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

SEASON = {"day": 7, "week": 52, "month": 12, "quarter": 4}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
QUARTERS = ("the first quarter", "the second quarter", "the third quarter", "the fourth quarter")


def as_date(p: Any) -> _dt.date | None:
    if isinstance(p, _dt.datetime):
        return p.date()
    if isinstance(p, _dt.date):
        return p
    try:
        return _dt.date.fromisoformat(str(p)[:10])
    except ValueError:
        return None


def season_label(period: Any, grain: str) -> str:
    """The season a period falls in, in words: month name, weekday, or quarter (never a number)."""
    d = as_date(period)
    if d is None:
        return "an unlabelled period"
    if grain == "day":
        return WEEKDAYS[d.weekday()]
    if grain == "quarter":
        return QUARTERS[(d.month - 1) // 3]
    return MONTHS[d.month - 1]


def _classical(y: np.ndarray, m: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Additive classical decomposition: centred moving average (2 x m for an even m), per-position
    seasonal means centred on zero, edges of the trend filled by the nearest computed value."""
    n = y.size
    w = np.r_[0.5, np.ones(m - 1), 0.5] / m if m % 2 == 0 else np.ones(m) / m
    trend = np.convolve(y, w, mode="same")
    half = len(w) // 2
    if n > 2 * half:
        trend[:half] = trend[half]
        trend[n - half:] = trend[n - half - 1]
    detr = y - trend
    pos = np.arange(n) % m
    means = np.array([detr[pos == k].mean() if np.any(pos == k) else 0.0 for k in range(m)])
    means -= means.mean()
    seasonal = means[pos]
    return trend, seasonal, y - trend - seasonal


def decompose(y: Sequence[float], m: int) -> dict[str, Any]:
    """trend, seasonal, resid and the engine used. STL is robust to outliers; it needs the `ml` extra."""
    arr = np.asarray(y, dtype=float)
    try:
        from analystos.skills.forecast import stl_components

        trend, seasonal, resid = stl_components(arr, m)
        return {"trend": trend, "seasonal": seasonal, "resid": resid, "engine": "stl"}
    except Exception:  # noqa: BLE001 - no statsmodels (lite) or a numerical failure: the classical decomposition
        trend, seasonal, resid = _classical(arr, m)
        return {"trend": trend, "seasonal": seasonal, "resid": resid, "engine": "classical"}


def seasonal_strength(seasonal: np.ndarray, resid: np.ndarray) -> float:
    """Hyndman & Athanasopoulos: max(0, 1 - Var(R) / Var(S + R)); 0 none, 1 entirely seasonal."""
    total = float(np.var(seasonal + resid))
    return 0.0 if total <= 0 else max(0.0, 1.0 - float(np.var(resid)) / total)


def with_units(facts: Iterable[Any], units: Mapping[str, tuple[str, str]]) -> list[Any]:
    """Give pack statistics their meaning: role -> (unit, kind). A coverage is a fraction, a season length
    a count; everything else keeps what the shared vocabulary decided."""
    out = []
    for f in facts:
        if f.role in units:
            unit, kind = units[f.role]
            f = f.model_copy(update={"unit": unit, "kind": kind})
        out.append(f)
    return out

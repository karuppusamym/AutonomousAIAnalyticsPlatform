"""level_shift: one sustained shift in the level of volume or a numeric outcome over `time`.

Change-point detection by CUSUM (spec S07 §3.5: `ruptures` is not a dependency, so the CUSUM fallback
is the method; it needs nothing beyond numpy).

primary       CUSUM of deviations from the mean: the split k* maximising |S_k|; significance by a seeded
              permutation test of max|S_k| (999 shuffles). Supported when p < alpha, the shift is at least
              10% of the earlier level and both sides hold at least 3 periods.
verification  binary segmentation least-squares split (`skills.stats.change_point`), an independent search:
              agrees when it finds a change within 2 periods of k* in the same direction
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from analystos.contracts.analysis import AnalysisSpec, StatResult
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
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

MIN_SIDE = 3
MIN_PERIODS = 8
PERMUTATIONS = 999
SEED = 0
NEAR = 2


def cusum(y: np.ndarray) -> tuple[int, float]:
    """(k*, max|S_k|): the split after index k* (1..n-1) and the CUSUM extreme, on the centred series."""
    s = np.cumsum(y - y.mean())[:-1]
    k = int(np.argmax(np.abs(s)))
    return k + 1, float(np.abs(s[k]))


def _split(y: np.ndarray) -> tuple[int, float, float]:
    """The CUSUM split restricted to splits leaving MIN_SIDE periods on each side."""
    s = np.abs(np.cumsum(y - y.mean())[:-1])
    lo, hi = MIN_SIDE - 1, y.size - MIN_SIDE - 1
    k = lo + int(np.argmax(s[lo:hi + 1]))
    return k + 1, float(y[:k + 1].mean()), float(y[k + 1:].mean())


class LevelShift(Trend):
    name = "level_shift"
    vocabulary = ("one sustained shift in the level of volume (outcome null) or a numeric outcome over `time` "
                  "(time.type = date_trunc, grain week|month); CUSUM change point.")
    playbook = None

    def test(self, rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, raw, excl = series(rows["primary"], spec)
        arr = np.asarray(y, dtype=float)
        n = int(arr.size)
        if n < MIN_PERIODS:
            out = no_data(spec, f"need at least {MIN_PERIODS} periods to detect a level shift; have {n}", "cusum")
            out.stat.n = n
            return out
        k, before, after = _split(arr)
        _, observed = cusum(arr)
        if np.ptp(arr) == 0:
            p = 1.0
        else:
            rng = np.random.default_rng(SEED)
            hits = sum(cusum(rng.permutation(arr))[1] >= observed - 1e-12 for _ in range(PERMUTATIONS))
            p = (hits + 1) / (PERMUTATIONS + 1)
        pct = (after - before) / abs(before) if before else None
        supported = st._sig(p, alpha) and pct is not None and abs(pct) >= st.MIN_SHIFT_PCT
        sigma = float(arr.std(ddof=1)) or 1.0
        metric = "count" if spec.outcome is None else f"mean({sb.describe(spec.outcome)})"
        stat = StatResult(
            method=spec.method, test="cusum_permutation", n=n, statistic=st._f(observed / (sigma * np.sqrt(n))),
            p_value=st._f(p, 12), effect_size=st._f(pct), effect_label="pct_change_level",
            highlights={"change_period": str(json_value(periods[k])), "change_before_mean": st._q(before),
                        "change_after_mean": st._q(after), "pct_change": st._q(pct), "n_before": k, "n_after": n - k,
                        "n_periods": n, "metric": metric,
                        "direction": "increase" if after > before else "decrease" if after < before else "equal"},
            assumptions=["one shift in the mean", f"permutation p-value ({PERMUTATIONS} seeded shuffles)",
                         "periods exchangeable under no change"],
            supported=supported, details={"alpha": alpha, "split_index": k, "metric": metric, **excl})
        cols = ["period", "value", "segment"]
        table = {"columns": cols, "rows": [[json_value(pp), st._f(v), "before" if i < k else "after"]
                                           for i, (pp, v) in enumerate(zip(periods, arr, strict=False))]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, _, _ = series(rows["primary"], spec)
        if len(y) < MIN_PERIODS:
            return nothing_to_verify(spec, primary, "binary_segmentation", f"fewer than {MIN_PERIODS} periods", len(y))
        v = st.change_point(y, periods, alpha=alpha, min_size=MIN_SIDE, max_points=1)
        v.method = spec.method
        k = int((primary.details or {}).get("split_index") or 0)
        found = [int(cp.get("index", -1)) for cp in v.groups or [] if isinstance(cp, dict)]
        near = any(abs(i - k) <= NEAR for i in found) if found else False
        before, after = v.highlights.get("before_mean"), v.highlights.get("after_mean")
        same = None
        if isinstance(before, int | float) and isinstance(after, int | float):
            same = (after > before) == (((primary.highlights or {}).get("pct_change") or 0) > 0)
        v.supported = bool(v.supported) and near
        table = {"columns": ["change_points", "near_cusum_split", "before_mean", "after_mean"],
                 "rows": [[found, near, before, after]]}
        return finish(spec, primary, v, same, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        pct = hl.get("pct_change")
        if not isinstance(pct, int | float) or pct == 0:
            return None
        _, out, scope = text_parts(spec)
        title = f"{cap(out)} shifted to a new level"
        text = (f"{cap(out)} {'rose' if pct > 0 else 'fell'} by {fmt_pct(abs(pct))} from one sustained level to another, "
                f"with the shift starting at {hl.get('change_period')}{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="trend", dimension="time", measure="volume" if spec.outcome is None else "outcome")

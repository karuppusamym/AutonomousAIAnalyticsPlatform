"""How far a supported finding clears its method's bars (P8-15).

A finding that only just passes is labelled ``weak`` so no reader takes it for an established fact. Seen
live: unit price "differed by order status" with rank-biserial 0.1175 against the 0.10 minimum and
q = 0.0028, on data where status was assigned at random. The label is computed from the statistic
alone, never by a model:

* ``margin`` = effect / the method's minimum effect for that effect measure (`skills.stats` thresholds);
  ratio measures (odds ratios, share ratios) compare on the log scale, so an odds ratio of 1.44 is
  2x the 1.2 minimum.
* ``weak``     margin < 1.5, or q >= alpha / 10 (q within a factor of 10 of alpha);
* ``strong``   margin >= 3 and q < alpha / 100;
* ``moderate`` otherwise. A measure with no minimum (Gini, robust z) is graded on q alone and is never
  ``strong``; with neither a threshold nor a p-value there is no grade (None).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from analystos.contracts.evidence import Strength
from analystos.methods.contribution_decomposition import MIN_RELATIVE_EFFECT
from analystos.skills import stats as st

WEAK_EFFECT_MARGIN = 1.5
STRONG_EFFECT_MARGIN = 3.0
WEAK_Q_FACTOR = 10.0  # q >= alpha / 10 is weak
STRONG_Q_FACTOR = 100.0  # strong needs q < alpha / 100

# effect_label -> (the method's minimum effect, scale). Primary statistics only: that is what a finding reports.
EFFECT_THRESHOLDS: dict[str, tuple[float, str]] = {
    "cramers_v": (st.MIN_CRAMERS_V, "abs"),
    "rank_biserial": (st.MIN_RANK_BISERIAL, "abs"),
    "epsilon_squared": (st.MIN_EPSILON_SQ, "abs"),
    "hedges_g": (st.MIN_HEDGES_G, "abs"),
    "eta_squared": (st.MIN_ETA_SQ, "abs"),
    "spearman_rho": (st.MIN_ABS_RHO, "abs"),
    "pearson_r": (st.MIN_ABS_RHO, "abs"),
    "odds_ratio": (st.MIN_ODDS_RATIO, "log"),
    "odds_ratio_top_vs_baseline": (st.MIN_ODDS_RATIO, "log"),
    "odds_ratio_best_vs_worst_cohort": (st.MIN_ODDS_RATIO, "log"),
    "pct_change_fitted": (st.MIN_TREND_PCT, "abs"),
    "pct_change_theil_sen": (st.MIN_TREND_PCT, "abs"),
    "shift_pct": (st.MIN_SHIFT_PCT, "abs"),
    "top_share_vs_fair_share": (st.MIN_TOP_SHARE_RATIO, "log"),
    "rate_effect_relative_to_kpi_before": (MIN_RELATIVE_EFFECT, "abs"),
}
PREFIX_THRESHOLDS: tuple[tuple[str, tuple[float, str]], ...] = (
    ("permutation_importance_", (st.MIN_PERM_IMPORTANCE, "abs")),
)


def threshold_for(effect_label: str | None) -> tuple[float, str] | None:
    if not effect_label:
        return None
    if effect_label in EFFECT_THRESHOLDS:
        return EFFECT_THRESHOLDS[effect_label]
    return next((t for prefix, t in PREFIX_THRESHOLDS if effect_label.startswith(prefix)), None)


def margin_of(effect: float, threshold: float, scale: str) -> float | None:
    if scale == "log":
        if effect <= 0 or threshold <= 1:
            return None
        return abs(math.log(effect)) / math.log(threshold)  # a protective ratio (< 1) counts by its size too
    return abs(effect) / threshold if threshold > 0 else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def grade(stat: Mapping[str, Any], *, alpha: float) -> Strength | None:
    """The strength of a supported result (None when the result is not supported or cannot be graded)."""
    if not stat.get("supported"):
        return None
    effect, label = _num(stat.get("effect_size")), stat.get("effect_label")
    q = _num(stat.get("p_adjusted", stat.get("p_value")))
    t = threshold_for(label)
    margin = margin_of(effect, *t) if t is not None and effect is not None else None
    margin = round(margin, 6) if margin is not None else None  # 0.15 / 0.10 is 1.5, not 1.4999999999999998
    reasons: list[str] = []
    weak = strong_effect = False
    if margin is not None and t is not None:
        times = f"{margin:.1f}x" if margin < 10 else f"{margin:.0f}x"
        reasons.append(f"the effect ({effect:.3g}) is {times} the minimum that counts ({t[0]:g})")
        if margin < WEAK_EFFECT_MARGIN:
            weak = True
            reasons[-1] += f", under {WEAK_EFFECT_MARGIN:g}x: it only just passes"
        strong_effect = margin >= STRONG_EFFECT_MARGIN
    if q is not None:
        if q >= alpha / WEAK_Q_FACTOR:
            weak = True
            reasons.append(f"the adjusted p-value ({q:.2g}) is within {WEAK_Q_FACTOR:g}x of the {alpha:g} cut-off")
        else:
            reasons.append(f"the adjusted p-value ({q:.2g}) is well under the {alpha:g} cut-off")
    if margin is None and q is None:
        return None
    if weak:
        out = "weak"
    elif strong_effect and (q is None or q < alpha / STRONG_Q_FACTOR):
        out = "strong"
    else:
        out = "moderate"
    return Strength(label=out, effect=effect, effect_label=label, threshold=t[0] if t else None,
                    margin=margin, q=q, alpha=alpha, reasons=reasons)


WEAK_NOTE = "The effect only just clears the minimum size that counts, so this is weak evidence."
UNCONFIRMED_NOTE = "It is not yet confirmed on separate data: treat it as a lead worth checking, not an established fact."


# PILOT-FIX-06: which rows the sentence's figures describe, so an analyst's full-table check knows what to compare.
ALL_ROWS_NOTE = ("These figures cover every record in scope; the pattern was found on the discovery rows and confirmed "
                 "on the rows held back.")
DISCOVERY_NOTE = ("These figures come from the discovery rows only, not every record: the rest are held back to "
                  "confirm findings, so a check on the full table will differ slightly.")


def qualify(text: str, *, confirmed: bool, strength: str | None, scope: str | None = None) -> str:
    """The finding's sentence with what a reader must not skip: which rows its figures describe (`scope`:
    "all_rows" or "discovery"; None when the test read every row), and a weak or unconfirmed finding is not
    stated as an established fact. Plain words, no numbers (the numbers guard is unaffected); idempotent."""
    out = text.rstrip()
    for note, other in ((ALL_ROWS_NOTE, "discovery"), (DISCOVERY_NOTE, "all_rows")):
        if scope == other:  # a re-verified finding whose scope changed keeps one scope note
            out = out.replace(f" {note}", "").replace(note, "").rstrip()
    for note, applies in ((ALL_ROWS_NOTE, scope == "all_rows"), (DISCOVERY_NOTE, scope == "discovery"),
                          (WEAK_NOTE, strength == "weak"), (UNCONFIRMED_NOTE, not confirmed)):
        if applies and note not in out:
            out = f"{out} {note}"
    return out

"""Typed fact bindings (P4-03, target spec §4 "Claim"; design review P0 on `insight._guard`).

A finding may only quote numbers that the deterministic path computed, and each quoted number must be
used *as what it is*: the right group, the right unit, the right direction. `facts_from` turns a
method's `StatResult` into typed `Fact`s; `bind` checks a narrative (title, finding, action) against
them and returns a `Binding` that maps every number to a fact id or names the problem:

* **unbound number** — no fact has that value in that unit (an invented number, or a unit swap: a
  rate written as ``0.3x``, a ratio written as ``314%``, 12 hours written as ``12 days``, a fraction
  written as ``41.2`` without ``%``);
* **wrong group** — the number belongs to one group but the sentence attributes it to another (the
  nearest group label in its clause is neither the fact's subject nor its baseline);
* **wrong direction** — "increase" for a decrease, or "higher" for the group the facts say is lower;
* **direction not stated** — an unsigned change with no direction word.

The flat legacy guard (`agents.insight._guard`) accepted any number anywhere in the evidence, and
the numerals 1, 2 and 3 unconditionally; this binds each number to its meaning. Fail-closed: a
model narrative that does not bind is replaced by the method's deterministic template, and a
template that does not bind fails the REV ``fact_binding`` check.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from analystos.contracts.evidence import Binding, Fact, Mention

TIME_UNITS = {"seconds": 1 / 3600, "minutes": 1 / 60, "hours": 1.0, "days": 24.0}

# statistic key -> (kind, unit, subject key, baseline key). `outcome` resolves to the outcome's unit.
_LEVEL = "level"
_KEYS: dict[str, tuple[str, str, str | None, str | None]] = {
    "top_rate": (_LEVEL, "fraction", "top_segment", None),
    "baseline_rate": (_LEVEL, "fraction", "baseline_segment", None),
    "reference_rate": (_LEVEL, "fraction", "reference_segment", None),
    "overall_rate": (_LEVEL, "fraction", "=overall", None),
    "rate_before": (_LEVEL, "fraction", "period_before", None),
    "rate_after": (_LEVEL, "fraction", "period_after", None),
    "top_median": (_LEVEL, "outcome", "top_segment", None),
    "top_mean": (_LEVEL, "outcome", "top_segment", None),
    "top_value": (_LEVEL, "outcome", "top_segment", None),
    "baseline_median": (_LEVEL, "outcome", "baseline_segment", None),
    "baseline_mean": (_LEVEL, "outcome", "baseline_segment", None),
    "baseline_value": (_LEVEL, "outcome", "baseline_segment", None),
    "mean_before": (_LEVEL, "outcome", "period_before", None),
    "mean_after": (_LEVEL, "outcome", "period_after", None),
    "first_value": (_LEVEL, "outcome", "first_period", None),
    "last_value": (_LEVEL, "outcome", "last_period", None),
    "fitted_first": (_LEVEL, "outcome", "first_period", None),
    "fitted_last": (_LEVEL, "outcome", "last_period", None),
    "change_before_mean": (_LEVEL, "outcome", None, None),
    "change_after_mean": (_LEVEL, "outcome", None, None),
    "top_anomaly_value": (_LEVEL, "outcome", "top_anomaly", None),
    "rate_ratio": ("comparison", "ratio", "top_segment", "baseline_segment"),
    "ratio": ("comparison", "ratio", "top_segment", "baseline_segment"),
    "median_ratio": ("comparison", "ratio", "top_segment", "baseline_segment"),
    "mean_ratio": ("comparison", "ratio", "top_segment", "baseline_segment"),
    "odds_ratio": ("comparison", "ratio", "top_segment", "baseline_segment"),
    "rate_ratio_vs_reference": ("comparison", "ratio", "top_segment", "reference_segment"),
    "rate_ratio_vs_overall": ("comparison", "ratio", "top_segment", "=overall"),
    "top_share_vs_fair_share": ("comparison", "ratio", "top_segment", "=fair share"),
    "top_odds_ratio": ("comparison", "ratio", "top_feature", None),
    "strongest_odds_ratio": ("comparison", "ratio", "strongest_feature", None),
    "rate_difference": ("comparison", "fraction", "top_segment", "baseline_segment"),
    "median_difference": ("comparison", "outcome", "top_segment", "baseline_segment"),
    "top_share": ("share", "fraction", "top_segment", None),
    "top_k_share": ("share", "fraction", None, None),
    "top_20pct_share": ("share", "fraction", None, None),
    "top_20pct_share_ci_low": ("statistic", "fraction", None, None),
    "pct_change": ("change", "fraction", None, "first_period"),
    "pct_change_raw": ("change", "fraction", None, "first_period"),
    "within_segment_points": ("change", "points", None, "period_before"),
    "mix_points": ("change", "points", None, "period_before"),
    "within_segment_change": ("change", "outcome", None, "period_before"),
    "mix_change": ("change", "outcome", None, "period_before"),
    "volume_effect": ("change", "outcome", None, "period_before"),
    "top_segment_contribution": ("statistic", "value", "top_segment", None),
    "slope_per_period": ("statistic", "outcome", None, None),
}
_COUNTS = {"n", "top_n", "baseline_n", "n_groups", "n_periods", "top_k", "segments_for_80pct", "top_20pct_segments",
           "n_before", "n_after", "affected_records", "excess_events", "horizon", "n_cohorts", "change_points", "strata",
           "top_volume", "top_segment_n", "rows_scanned"}
_CHANGE_DIRECTION = {"within_segment_points": "within_segment_direction", "mix_points": "mix_direction",
                     "within_segment_change": "within_segment_direction", "mix_change": "mix_direction"}
_PRIMARY_ORDER = ("rate_ratio", "median_ratio", "ratio", "mean_ratio", "odds_ratio", "pct_change",
                  "within_segment_points", "within_segment_change", "top_share_vs_fair_share", "strongest_odds_ratio")
_LABEL_KEYS = ("top_segment", "baseline_segment", "reference_segment", "top_driver", "top_feature", "strongest_feature",
               "period_before", "period_after", "first_period", "last_period", "change_period", "top_anomaly")
_TOP_K_SHARE = re.compile(r"^top_(\d+)_share$")


def outcome_unit(spec: Mapping[str, Any]) -> str:
    """The unit of the outcome's values: a derived duration is in hours; a column named for a time unit
    (``shipping_days``, ``handle_minutes``) is in that unit; anything else is an unlabelled value."""
    out = spec.get("outcome") or {}
    if out.get("type") == "duration_hours":
        return "hours"
    name = f"{out.get('column') or ''} {out.get('label') or ''}".lower()
    for token, unit in (("hour", "hours"), ("_hrs", "hours"), ("day", "days"), ("minute", "minutes"),
                        ("_mins", "minutes"), ("second", "seconds"), ("_secs", "seconds")):
        if token in name:
            return unit
    return "value"


def _label(spec: Mapping[str, Any], part: str) -> str | None:
    d = spec.get(part) or {}
    return d.get("label") or d.get("column")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def facts_from(spec: Mapping[str, Any], stat: Mapping[str, Any], *, extra: Mapping[str, Any] | None = None,
               query_ids: Sequence[str] = (), result_hashes: Sequence[str] = ()) -> list[Fact]:
    """Typed facts for one result. Generic over the highlight vocabulary the methods share, so a method
    plugin only overrides `facts` when it introduces a statistic with a new meaning."""
    hl = {**(stat.get("highlights") or {}), **(extra or {})}
    ounit = outcome_unit(spec)
    metric = _label(spec, "outcome")
    dimension = _label(spec, "segment")
    method = stat.get("test") or spec.get("method")
    common = {"method": method, "query_ids": list(query_ids), "result_hashes": [h for h in result_hashes if h]}
    window = f"{hl['first_period']}..{hl['last_period']}" if hl.get("first_period") and hl.get("last_period") else \
        f"{hl['period_before']}..{hl['period_after']}" if hl.get("period_before") and hl.get("period_after") else None
    facts: list[Fact] = []

    def ref(key: str | None) -> str | None:
        if key is None:
            return None
        if key.startswith("="):
            return key[1:]
        v = hl.get(key)
        return None if v is None else str(v)

    def add(role: str, value: float, kind: str, unit: str, subject: str | None = None, baseline: str | None = None,
            direction: str | None = None, what: str | None = None) -> None:
        facts.append(Fact(id=f"F{len(facts) + 1}", role=role, kind=kind, metric=what or metric, value=value,
                          unit=unit, subject=subject, dimension=dimension if subject else None, baseline=baseline,
                          direction=direction, window=window, **common))  # type: ignore[arg-type]

    for key, raw in hl.items():
        top_k = _TOP_K_SHARE.match(key)
        spec_key = "top_k_share" if top_k else key
        if isinstance(raw, list | tuple) and len(raw) == 2 and key.endswith("_ci"):
            base = _KEYS.get(key[:-3])
            unit = base[1] if base else "coefficient"
            unit = ounit if unit == "outcome" else unit
            for bound, v in zip(("low", "high"), raw, strict=True):
                if (f := _num(v)) is not None:
                    add(f"{key}_{bound}", f, "statistic", unit, ref(base[2]) if base else None, ref(base[3]) if base else None)
            continue
        value = _num(raw)
        if value is None:
            continue
        if spec_key in _KEYS:
            kind, unit, subj, base = _KEYS[spec_key]
            unit = ounit if unit == "outcome" else unit
            direction = None
            if kind == "comparison":
                pivot = 1.0 if unit == "ratio" else 0.0
                direction = "higher" if value > pivot else "lower" if value < pivot else "equal"
            elif kind == "change":
                named = hl.get(_CHANGE_DIRECTION.get(key, ""))
                direction = named if named in ("increase", "decrease") else \
                    "increase" if value > 0 else "decrease" if value < 0 else "equal"
            add(key, value, kind, unit, ref(subj), ref(base), direction)
        elif key in _COUNTS:
            add(key, value, "count", "count", ref("top_segment") if key == "top_n" else ref("baseline_segment")
                if key == "baseline_n" else None, what="records" if key in ("n", "top_n", "baseline_n") else key)
        else:
            add(key, value, "statistic", "probability" if "p_value" in key else "coefficient", what=key)
    for key, unit, what in (("n", "count", "records"), ("p_adjusted", "probability", "q-value (BH-adjusted p)"),
                            ("p_value", "probability", "p-value")):
        v = _num(stat.get(key))
        if v is not None and not any(f.role == key for f in facts):
            add(key, v, "count" if unit == "count" else "statistic", unit, what=what)
    eff = _num(stat.get("effect_size"))
    label = str(stat.get("effect_label") or "effect_size")
    if eff is not None:
        unit = "ratio" if "ratio" in label else "fraction" if "pct_change" in label else "coefficient"
        add("effect_size", eff, "statistic", unit, what=label)
    for bound in ("ci_low", "ci_high"):
        v = _num(stat.get(bound))
        if v is not None:
            add(bound, v, "statistic", "coefficient", what=f"interval bound ({bound})")
    for g in stat.get("groups") or []:  # per-group sizes: sample sizes by group are evidence, and quotable
        n, seg = _num(g.get("n")), g.get("segment")
        if n is not None and seg is not None and not any(f.role in ("top_n", "baseline_n") and f.subject == str(seg)
                                                         for f in facts):
            add("group_n", n, "count", "count", str(seg), what="records")
    for role in _PRIMARY_ORDER:
        hit = next((f for f in facts if f.role == role), None)
        if hit is not None:
            hit.primary = True
            break
    return facts


def labels_from(spec: Mapping[str, Any], stat: Mapping[str, Any], facts: Sequence[Fact]) -> tuple[set[str], set[str]]:
    """(group labels, context labels) the narrative may name. Groups are what values belong to (segment
    values, cohorts, periods, drivers); context is the outcome / segment names and scope filters."""
    hl = stat.get("highlights") or {}
    groups = {str(hl[k]) for k in _LABEL_KEYS if hl.get(k) is not None}
    groups |= {str(g["segment"]) for g in stat.get("groups") or [] if g.get("segment") is not None}
    groups |= {f.subject for f in facts if f.subject} | {f.baseline for f in facts if f.baseline}
    context = {x for x in (_label(spec, "outcome"), _label(spec, "segment"), (spec.get("outcome") or {}).get("column"),
                           (spec.get("segment") or {}).get("column"), hl.get("outcome"), hl.get("segment_by"),
                           hl.get("metric")) if x}
    for f in spec.get("filters") or []:
        context.add(f"{f.get('column')} {f.get('op')} {f.get('value')}")
    for d in spec.get("drivers") or []:
        context |= {x for x in (d.get("label"), d.get("column")) if x}
    return {g for g in groups if g.strip()}, {str(c) for c in context if str(c).strip()}


# ------------------------------------------------------------------------------------ narrative binding
_UNIT_WORDS = (r"%|percent\b|per cent\b|percentage points?\b|pp\b|points?\b|pts?\b|x\b|×|times\b|hours?\b|hrs?\b|h\b|"
               r"days?\b|minutes?\b|mins?\b|seconds?\b|secs?\b")
_NUMBER = re.compile(r"(?<![\w.§])(?P<sign>[-+−])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                     rf"(?:\s*(?P<unit>{_UNIT_WORDS}))?", re.I)
_UNIT_OF = {"%": "percent", "percent": "percent", "per cent": "percent", "percentage point": "points",
            "percentage points": "points", "pp": "points", "point": "points", "points": "points", "pt": "points",
            "pts": "points", "x": "ratio", "×": "ratio", "times": "ratio", "hour": "hours", "hours": "hours",
            "hr": "hours", "hrs": "hours", "h": "hours", "day": "days", "days": "days", "minute": "minutes",
            "minutes": "minutes", "min": "minutes", "mins": "minutes", "second": "seconds", "seconds": "seconds",
            "sec": "seconds", "secs": "seconds"}
_CLAUSE = re.compile(r"\n|;|:(?!\d)|\bversus\b|\bvs\b\.?|\bcompared (?:with|to)\b|\bwhereas\b|\bwhile\b|\bbut\b|[.!?](?=\s|$)", re.I)
_UP = re.compile(r"\b(higher|more|greater|larger|above|exceed\w*|concentrat\w*|highest|most|increas\w*|rose|rises?|"
                 r"risen|rising|grew|grow\w*|up|jump\w*|climb\w*|surg\w*)\b", re.I)
_DOWN = re.compile(r"\b(lower|less|fewer|smaller|below|lowest|least|decreas\w*|fell|falls?|fallen|falling|declin\w*|"
                   r"drop\w*|down|shr[ai]nk\w*|reduc\w*)\b", re.I)
MASK = "§"


def _mask(text: str, groups: set[str], context: set[str]) -> tuple[str, list[tuple[int, int, str]]]:
    """Replace group and context labels by same-length runs of MASK (positions preserved, digits in
    labels hidden); return the masked text and the group spans. Numeric-only group labels ("0", "3") are
    only taken as labels in the `name = value` form, so an ordinary number is never swallowed."""
    spans: list[tuple[int, int, str]] = []
    chars = list(text)
    items = [(g, True) for g in groups] + [(c, False) for c in context]
    for label, is_group in sorted(items, key=lambda x: -len(x[0])):
        pre = r"(?<=[=:]\s)" if re.fullmatch(r"[\d.,]+", label) else (r"(?<!\w)" if label[:1].isalnum() else "")
        post = r"(?![\w%])" if label[-1:].isalnum() else ""
        for m in re.finditer(pre + re.escape(label) + post, "".join(chars), re.I if not is_group else 0):
            if MASK in m.group(0):
                continue
            chars[m.start():m.end()] = MASK * (m.end() - m.start())
            if is_group:
                spans.append((m.start(), m.end(), label))
    return "".join(chars), sorted(spans)


def _clauses(masked: str) -> list[tuple[int, int]]:
    out, start = [], 0
    for m in _CLAUSE.finditer(masked):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(masked)))
    return [(a, b) for a, b in out if b > a]


def _in_scale(f: Fact, unit: str | None) -> float | None:
    """The fact's value in the unit the narrative wrote, or None when that unit cannot express it."""
    if unit == "percent":
        return f.value * 100 if f.unit == "fraction" else f.value if f.unit == "percent" else None
    if unit == "points":
        if f.unit == "points":
            return f.value
        return f.value * 100 if f.unit == "fraction" and f.kind in ("comparison", "change") else None
    if unit == "ratio":
        return f.value if f.unit == "ratio" else None
    if unit in TIME_UNITS:
        return f.value * TIME_UNITS[f.unit] / TIME_UNITS[unit] if f.unit in TIME_UNITS else None
    return f.value  # a bare number: the fact's own scale (a fraction stays 0.41, never 41)


def _matches(x: float, decimals: int, target: float) -> bool:
    return abs(x - target) <= 0.5 * 10 ** -decimals * 1.02 + 1e-9


def _polarity(text: str) -> str | None:
    up, down = bool(_UP.search(text)), bool(_DOWN.search(text))
    return "up" if up and not down else "down" if down and not up else None


def _nearest(spans: list[tuple[int, int, str]], a: int, b: int, lo: int, hi: int) -> str | None:
    inside = [s for s in spans if s[0] >= lo and s[1] <= hi]
    if not inside:
        return None
    return min(inside, key=lambda s: (max(s[0] - b, a - s[1], 0), s[0] < a))[2]


def _nearest_direction(masked: str, a: int, b: int, lo: int, hi: int) -> str | None:
    best: tuple[int, str] | None = None
    for rx, pol in ((_UP, "up"), (_DOWN, "down")):
        for m in rx.finditer(masked, lo, hi):
            d = max(m.start() - b, a - m.end(), 0)
            if best is None or d < best[0]:
                best = (d, pol)
    return best[1] if best else None


def _pair(facts: Sequence[Fact]) -> Fact | None:
    """The headline two-sided comparison (top vs baseline), which direction words are judged against."""
    both = [f for f in facts if f.kind == "comparison" and f.subject and f.baseline and f.direction in ("higher", "lower")]
    return next((f for f in both if f.primary), both[0] if both else None)


def bind(text: str, facts: Sequence[Fact], groups: set[str], context: set[str]) -> Binding:
    """Bind every number and every directional group claim in `text` to the facts (see module doc)."""
    masked, spans = _mask(text, groups, context)
    clauses = _clauses(masked)
    mentions: list[Mention] = []
    problems: list[str] = []
    pair = _pair(facts)
    primary_change = next((f for f in facts if f.primary and f.kind == "change" and f.direction in ("increase", "decrease")),
                          None)
    for m in _NUMBER.finditer(masked):
        raw_unit = (m.group("unit") or "").lower()
        if not raw_unit and m.end() < len(masked) and masked[m.end()].isalpha():
            continue  # an identifier or ordinal ("3rd", "Q3x"), not a quantity
        num = m.group("num").replace(",", "")
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = float(num)
        signed = m.group("sign") in ("-", "−")
        has_sign = m.group("sign") is not None
        unit = _UNIT_OF.get(raw_unit)
        lo, hi = next(((a, b) for a, b in clauses if a <= m.start() < b), (0, len(masked)))
        near_group = _nearest(spans, m.start(), m.end(), lo, hi)
        near_dir = _nearest_direction(masked, m.start(), m.end(), lo, hi)
        mention = Mention(text=m.group(0).strip(), value=-value if signed else value, unit=unit)
        candidates = []
        for f in facts:
            scaled = _in_scale(f, unit)
            if scaled is None:
                continue
            if f.kind == "change" and not has_sign:
                ok = _matches(value, decimals, abs(scaled))
            else:
                ok = _matches(-value if signed else value, decimals, scaled)
            if ok:
                candidates.append(f)
        if not candidates:
            mention.problem = f"{mention.text}: no computed fact has this value in {unit or 'this'} unit"
        else:
            grouped = [f for f in candidates if not f.subject or near_group is None
                       or near_group in (f.subject, f.baseline)]
            if not grouped:
                owners = sorted({f.subject for f in candidates if f.subject})
                mention.problem = f"{mention.text} belongs to {', '.join(owners)}, not {near_group}"
            else:
                directed = []
                for f in grouped:
                    if f.kind == "change" and f.direction in ("increase", "decrease"):
                        want = "up" if f.direction == "increase" else "down"
                        if has_sign:
                            directed.append(f)
                        elif near_dir is None:
                            continue
                        elif near_dir == want:
                            directed.append(f)
                    else:
                        directed.append(f)
                if not directed:
                    change = grouped[0]
                    mention.problem = (f"{mention.text}: the change is a {change.direction}, but the text says otherwise"
                                       if near_dir else f"{mention.text}: an unsigned change needs its direction stated")
                else:
                    mention.fact_id = directed[0].id
        if mention.problem:
            problems.append(mention.problem)
        mentions.append(mention)
    for lo, hi in clauses:  # directional group claims, with or without a number ("concentrates in X")
        seg = masked[lo:hi]
        pol = _polarity(seg)
        if pol is None:
            continue
        first = next((s[2] for s in spans if s[0] >= lo and s[1] <= hi), None)
        if first is not None and pair is not None:
            higher, lower = (pair.subject, pair.baseline) if pair.direction == "higher" else (pair.baseline, pair.subject)
            if (pol == "up" and first == lower) or (pol == "down" and first == higher):
                problems.append(f"'{text[lo:hi].strip()}' says {first} is {'higher' if pol == 'up' else 'lower'}; "
                                f"the facts say {higher} is higher than {lower}")
            elif pol == "up" and first not in (higher, lower) and first in groups and any(
                    f.subject == higher and f.kind == "level" for f in facts):
                problems.append(f"'{text[lo:hi].strip()}' names {first} as highest; the facts say {higher}")
        elif first is None and primary_change is not None and not _NUMBER.search(seg):
            want = "up" if primary_change.direction == "increase" else "down"
            if pol != want:
                problems.append(f"'{text[lo:hi].strip()}' states the wrong direction; the change is a "
                                f"{primary_change.direction}")
    return Binding(ok=not problems, mentions=mentions, problems=problems)


def bind_finding(spec: Mapping[str, Any], stat: Mapping[str, Any], facts: Sequence[Fact], *texts: str,
                 claim_label: str | None = None) -> Binding:
    """The numbers guard of a finding: every number bound to its fact, and (S07-T3) no causal wording the
    claim's label and method do not license (`evidence.claims`), read with group labels masked."""
    from analystos.evidence.claims import causal_problems

    groups, context = labels_from(spec, stat, facts)
    text = "\n".join(t for t in texts if t)
    binding = bind(text, facts, groups, context)
    causal = causal_problems(_mask(text, groups, context)[0], label=claim_label, method=spec.get("method"))
    if causal:
        return Binding(ok=False, mentions=binding.mentions, problems=[*binding.problems, *causal])
    return binding

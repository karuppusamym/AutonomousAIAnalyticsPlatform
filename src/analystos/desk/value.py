"""Value metrics (FR-053, spec S13 §3.6): how much analyst work the desk took off people's hands.

Pure math over request rows so the numbers are unit-tested against a fixture with known baselines:

* auto-resolution rate = closed requests resolved ``auto`` (accepted from auto_answered or auto_with_review)
  ÷ all closed requests (accepted + declined; linked duplicates are not work and are excluded);
* accepted rate = accepted ÷ closed;
* time to answer = first answer − created, p50 / p95 (linear interpolation between ranks);
* human minutes = Σ analyst minutes recorded on answers;
* estimated minutes saved = Σ over accepted requests whose class has a baseline of (baseline − human minutes).
  Requests of a class without a baseline are counted in ``unbaselined`` and contribute nothing (no guess).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):  # SQLite drops tzinfo; compare naive UTC
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    return max(0.0, (end - start).total_seconds())


def _summary(rows: list[Mapping[str, Any]], baselines: Mapping[str, float]) -> dict[str, Any]:
    work = [r for r in rows if r.get("resolution") != "duplicate"]
    closed = [r for r in work if r.get("state") in ("accepted", "declined")]
    accepted = [r for r in closed if r.get("state") == "accepted"]
    auto = [r for r in closed if r.get("resolution") == "auto"]
    tta = [s for s in (_seconds(r.get("created_at"), r.get("first_answer_at")) for r in work) if s is not None]
    saved, unbaselined = 0.0, 0
    for r in accepted:
        base = baselines.get(str(r.get("class")))
        if base is None:
            unbaselined += 1
            continue
        saved += float(base) - float(r.get("human_minutes") or 0)
    p50, p95 = percentile(tta, 0.5), percentile(tta, 0.95)
    return {
        "n": len(work), "duplicates": len(rows) - len(work), "closed": len(closed), "accepted": len(accepted),
        "auto_resolved": len(auto),
        "auto_resolution_rate": round(len(auto) / len(closed), 4) if closed else None,
        "accepted_rate": round(len(accepted) / len(closed), 4) if closed else None,
        "tta_p50_seconds": round(p50, 1) if p50 is not None else None,
        "tta_p95_seconds": round(p95, 1) if p95 is not None else None,
        "human_minutes": int(sum(int(r.get("human_minutes") or 0) for r in work)),
        "est_minutes_saved": round(saved, 1), "unbaselined": unbaselined,
        "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in work), 4),
    }


def compute(rows: Iterable[Mapping[str, Any]], baselines: Mapping[str, float]) -> dict[str, Any]:
    rows = list(rows)
    by_class: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_class.setdefault(str(r.get("class") or "untriaged"), []).append(r)
    return {**_summary(rows, baselines),
            "by_class": {k: {**_summary(v, baselines), "baseline_minutes": baselines.get(k)} for k, v in sorted(by_class.items())},
            "baselines": dict(sorted(baselines.items()))}

"""Typed periods for guided investigations (PILOT-FIX-01).

The rehearsal (card C03) passed "Q3 2026" and "Q2 2026" as words in the question: the run analysed all
history and never compared the two quarters. Here the requested period and its baseline are date ranges, so
code, not a model, decides which rows every test reads:

* `baseline_for` derives the baseline from a comparison mode (previous period, same period last year) or
  takes a custom range; the two windows never overlap.
* `labels` names both windows in one sortable style ("2026 Q2" / "2026 Q3", "2026-07", ISO ranges), so a
  period segment and a two-period decomposition read in time order.
* `time_columns` picks each table's event timestamp (the template's hints, then the opening or creation
  timestamp the crawler recognised); a table without one cannot take the period, and its tests are refused.
* `constraint` is what the run stores as ``run.constraints["period"]``: part of the plan hash.
* `apply` puts the window on a spec: the requested period alone, or baseline through period for a
  comparison spec (one whose segment or time is the period derivation).
* `data_facts` reads two aggregates per table through the gateway and states, as data facts, an incomplete
  period (the data ends before the period does), months without rows, and rows dated in the future.
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.core.errors import InvalidInput

COMPARISON_MODES = ("previous_period", "same_period_last_year", "custom", "none")
# Names of the timestamp that dates a record's start, best first; end-of-life timestamps are never chosen.
_START_WORDS = ("opened", "created", "order", "ordered", "submitted", "reported", "logged", "received", "start",
                "started", "event", "booked", "placed", "sys_created")
_END_WORDS = ("closed", "resolved", "updated", "modified", "delivered", "due", "promised", "shipped", "ended",
              "end", "expires", "expiry", "deleted", "sla")


# ------------------------------------------------------------------------------------ ranges
def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def whole_months(start: date, end: date) -> int | None:
    """Months spanned when `start`..`end` (inclusive) is a run of whole calendar months, else None."""
    if start.day != 1 or end != _month_end(end):
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def baseline_for(start: date, end: date, mode: str, custom: tuple[date, date] | None = None) -> tuple[date, date] | None:
    """The baseline range (inclusive dates) for a requested period, or None for mode `none`."""
    if mode not in COMPARISON_MODES:
        raise InvalidInput(f"unknown comparison mode {mode!r}")
    if mode == "none":
        return None
    if mode == "custom":
        if custom is None:
            raise InvalidInput("a custom comparison needs the baseline dates")
        b0, b1 = custom
    else:
        months = whole_months(start, end)
        shift = 12 if mode == "same_period_last_year" else months
        if shift is not None:  # whole months shift by months; a last-year range shifts its dates
            b0 = _add_months(start, -shift)
            b1 = _month_end(_add_months(start, months - 1 - shift)) if months else _add_months(end, -shift)
        else:  # an arbitrary range: the same number of days just before it
            days = (end - start).days + 1
            b0, b1 = start - timedelta(days=days), start - timedelta(days=1)
    if b0 > b1:
        raise InvalidInput("the baseline must start before it ends")
    if not (b1 < start or b0 > end):
        raise InvalidInput("the baseline overlaps the period to investigate; choose dates that do not overlap")
    return b0, b1


def _quarter(start: date, end: date) -> str | None:
    if whole_months(start, end) == 3 and start.month in (1, 4, 7, 10):
        return f"{start.year} Q{(start.month - 1) // 3 + 1}"
    return None


def _months(start: date, end: date) -> str | None:
    n = whole_months(start, end)
    if n is None:
        return None
    return f"{start:%Y-%m}" if n == 1 else f"{start:%Y-%m}..{end:%Y-%m}"


def labels(*ranges: tuple[date, date]) -> list[str]:
    """One label per range in a single style, so labels sort in time order for non-overlapping ranges."""
    for style in (_quarter, _months):
        out = [style(a, b) for a, b in ranges]
        if all(out):
            return [str(x) for x in out]
    return [f"{a.isoformat()}..{b.isoformat()}" for a, b in ranges]


def describe(start: date, end: date) -> str:
    return f"{start.day} {start:%b %Y} to {end.day} {end:%b %Y}"


# ------------------------------------------------------------------------------------ time columns
def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def pick_time_column(columns: Iterable[tuple[str, str | None, str | None]], hints: Iterable[str] = ()) -> tuple[str | None, str]:
    """(column, why) for one table. `columns`: (name, semantic type, crawler role) in table order."""
    cols = [(n, t, r) for n, t, r in columns if t == "datetime" or r in ("timestamp", "date")]
    if not cols:
        return None, "the table has no date or time column"
    for h in hints:  # a domain pack's event-start words ("opened", "order_date"), exact name or name token
        for n, _, _ in cols:
            toks = _tokens(n)
            if (n.lower() == h.lower() or h.lower() in toks) and not set(toks) & set(_END_WORDS):
                return n, f"the record's {h} timestamp (domain pack)"
    for word in _START_WORDS:
        for n, _, _ in cols:
            toks = _tokens(n)
            if (word in toks or n.lower().startswith(word)) and not set(toks) & set(_END_WORDS):
                return n, f"the record's {word} timestamp"
    starts = [n for n, _, _ in cols if not set(_tokens(n)) & set(_END_WORDS)]
    if len(starts) == 1:
        return starts[0], "the table's only start-of-record date"
    if len(cols) == 1:
        return cols[0][0], "the table's only date column"
    return None, "several date columns and none is clearly the record's start: " + ", ".join(n for n, _, _ in cols[:5])


def time_columns(session: Any, scope: Any, hints: Iterable[str] = ()) -> dict[str, dict[str, str | None]]:
    """{asset: {column, reason}} for every table in scope; visible (not denied) columns only."""
    from sqlalchemy import select

    from analystos.db.models import SourceAsset, SourceColumn

    denied = set(getattr(scope, "denied_columns", []) or [])
    hints = list(hints)
    out: dict[str, dict[str, str | None]] = {}
    for fq in scope.assets:
        schema, _, name = fq.rpartition(".")
        asset = session.scalar(select(SourceAsset).where(SourceAsset.source_id == scope.asset_sources.get(fq),
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        cols = [] if asset is None else list(session.scalars(
            select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)))
        visible = set(scope.columns.get(fq) or [])
        rows = [(c.name, c.semantic_type or (c.profile or {}).get("semantic_type"), (c.semantics or {}).get("semantic_role"))
                for c in cols if c.name in visible and f"{fq}.{c.name}" not in denied and f"*.{c.name}" not in denied]
        column, reason = pick_time_column(rows, hints)
        out[fq] = {"column": column, "reason": reason}
    return out


# ------------------------------------------------------------------------------------ the constraint
def _window(label: str, start: date, end: date, requested: str | None) -> dict[str, Any]:
    return {"label": label, "start": start.isoformat(), "end": (end + timedelta(days=1)).isoformat(),
            "last_day": end.isoformat(), "requested": requested or describe(start, end)}


def constraint(start: date, end: date, *, mode: str, baseline: tuple[date, date] | None,
               columns: Mapping[str, Mapping[str, str | None]], period_label: str | None = None,
               baseline_label: str | None = None) -> dict[str, Any]:
    """`run.constraints["period"]`: windows are end-exclusive ISO dates; `columns` maps each table to its
    period column (None when the period cannot apply to it)."""
    names = labels((start, end), *([baseline] if baseline else []))
    out: dict[str, Any] = {"comparison": mode, "current": _window(names[0], start, end, period_label), "baseline": None,
                           "columns": {a: c.get("column") for a, c in columns.items()},
                           "column_reasons": {a: c.get("reason") for a, c in columns.items()}}
    if baseline:
        out["baseline"] = _window(names[1], baseline[0], baseline[1], baseline_label)
    return out


def windows(p: Mapping[str, Any]) -> list[dict[str, str]]:
    """The period derivation's windows, in time order."""
    ws = [p["current"], *([p["baseline"]] if p.get("baseline") else [])]
    return [{"label": w["label"], "start": w["start"], "end": w["end"]} for w in sorted(ws, key=lambda w: w["start"])]


def derivation(p: Mapping[str, Any], asset: str) -> Derivation | None:
    """Baseline vs requested period as one categorical derivation of `asset`'s period column."""
    column = (p.get("columns") or {}).get(asset)
    if not column or not p.get("baseline"):
        return None
    return Derivation(type="period", column=column, value=windows(p), label="period")


def is_comparison(spec: AnalysisSpec) -> bool:
    return any(d.type == "period" for d in spec.derivations())


def apply(spec: AnalysisSpec, p: Mapping[str, Any] | None) -> AnalysisSpec:
    """The spec restricted to the requested period (a comparison spec: baseline through period). Raises
    InvalidInput when the period cannot apply: the table has no period column, or a period derivation names
    other windows than the run's."""
    if not p:
        return spec
    column = (p.get("columns") or {}).get(spec.asset)
    if not column:
        why = (p.get("column_reasons") or {}).get(spec.asset) or "it has no date column"
        raise InvalidInput(f"the requested period cannot be applied to {spec.asset}: {why}")
    ws = windows(p)
    comparing = is_comparison(spec)
    for d in spec.derivations():
        if d.type == "period" and (d.value != ws or d.column != column or d.via):
            raise InvalidInput("a period comparison must use the run's requested period and baseline")
    lo, hi = (ws[0]["start"], ws[-1]["end"]) if comparing else (p["current"]["start"], p["current"]["end"])
    want = [Filter(column=column, op=">=", value=lo, origin="plan"), Filter(column=column, op="<", value=hi, origin="plan")]
    have = {(f.via, f.column, f.op, str(f.value)) for f in spec.filters}
    extra = [f for f in want if (None, f.column, f.op, str(f.value)) not in have]
    return spec.model_copy(update={"filters": [*spec.filters, *extra]}) if extra else spec


def comparison_proposals(p: Mapping[str, Any] | None, proposals: Iterable[Mapping[str, Any]], *, limit: int = 3,
                         segments: int = 2) -> list[dict[str, Any]]:
    """For the outcomes the playbook proposes (rates and measures by segment), the same outcome compared
    between baseline and requested period, and its change split by the first segments (within-group vs mix)."""
    from analystos import methods

    if not p or not p.get("baseline"):
        return []
    by_segment = {m.name for m in (methods.for_playbook("flag_by_segment"), methods.for_playbook("measure_by_segment")) if m}
    # the registered method that separates within-group change from mix (its manifest says so, S07-T5)
    decompose = next((n for n, m in sorted(methods.current().manifests.items()) if (m.spec or {}).get("mix_effects")), None)
    out: list[dict[str, Any]] = []
    seen: dict[tuple, dict[str, Any]] = {}  # (asset, outcome) -> {method, outcome, segments}
    for prop in proposals:
        spec = prop.get("spec") or {}
        outcome, segment = spec.get("outcome"), spec.get("segment")
        if not outcome or spec.get("method") not in by_segment or spec.get("joins"):
            continue
        entry = seen.setdefault((spec.get("asset"), str(outcome)), {"method": spec["method"], "outcome": dict(outcome),
                                                                     "segments": []})
        if segment and segment.get("type") == "column" and len(entry["segments"]) < segments:
            entry["segments"].append(dict(segment))
    cur, base = p["current"]["label"], p["baseline"]["label"]
    for (asset, _key), entry in list(seen.items())[:limit]:
        d = derivation(p, asset)
        if d is None:
            continue
        outcome = entry["outcome"]
        what = str(outcome.get("label") or outcome.get("column"))
        out.append({"question": f"Did {what} change between {base} and {cur}?",
                    "statement": f"{what[:1].upper()}{what[1:]} differs between {base} and {cur}.",
                    "rationale": "The investigation compares the requested period with its baseline.",
                    "priority": "high", "comparison": True,
                    "spec": {"method": entry["method"], "asset": asset, "outcome": outcome, "segment": d.model_dump()}})
        segs = entry["segments"]
        if decompose is None:
            continue
        for seg in segs:
            out.append({"question": f"Which {seg.get('label') or seg.get('column')} groups moved {what} between {base} and {cur}?",
                        "statement": f"{what[:1].upper()}{what[1:]} changed within "
                                     f"{seg.get('label') or seg.get('column')} groups between {base} and {cur}.",
                        "rationale": "Separates a change within groups from a change in the mix of groups.",
                        "priority": "high", "comparison": True,
                        "spec": {"method": decompose, "asset": asset, "outcome": outcome, "segment": dict(seg),
                                 "time": d.model_dump()}})
    return out


# ------------------------------------------------------------------------------------ data facts
def _asset_table(asset: str) -> exp.Table:
    from analystos.skills import sqlbuild as sb

    return sb.table(asset)


def fact_queries(asset: str, column: str, p: Mapping[str, Any], dialect: str, today: date) -> tuple[str, str]:
    """(monthly counts inside baseline..period, future-dated rows) for one table, as governed SQL text."""
    from analystos.skills import sqlbuild as sb

    ws = windows(p)
    ts = sb.ts_expr(column, dialect)
    lo, hi = sb.cast(sb.lit(ws[0]["start"]), "timestamp", dialect), sb.cast(sb.lit(ws[-1]["end"]), "timestamp", dialect)
    month = sb.trunc_expr(ts.copy(), "month", dialect)
    inside = exp.And(this=exp.GTE(this=ts.copy(), expression=lo), expression=exp.LT(this=ts.copy(), expression=hi))
    monthly = (exp.select(month.as_(sb.ident("month")), sb.count_star().as_(sb.ident("n")),
                          exp.Max(this=ts.copy()).as_(sb.ident("latest")))
               .from_(_asset_table(asset)).where(inside).group_by(month.copy()).order_by(month.copy()).limit(400))
    tomorrow = sb.cast(sb.lit((today + timedelta(days=1)).isoformat()), "timestamp", dialect)
    later = exp.GTE(this=ts.copy(), expression=tomorrow)
    future = (exp.select(sb.count_star().as_(sb.ident("n")), exp.Min(this=ts.copy()).as_(sb.ident("first")),
                         exp.Max(this=ts.copy()).as_(sb.ident("last")))
              .from_(_asset_table(asset)).where(later))
    return sb.to_sql(monthly, dialect), sb.to_sql(future, dialect)


def _day(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _months_in(start: date, end_exclusive: date) -> list[str]:
    out, d = [], start.replace(day=1)
    while d < end_exclusive:
        out.append(f"{d:%Y-%m}")
        d = _add_months(d, 1)
    return out


def summarize(asset: str, column: str, p: Mapping[str, Any], monthly: list[Mapping[str, Any]],
              future: Mapping[str, Any] | None, today: date) -> dict[str, Any]:
    """Counts per window and the data facts a reader must see, from the two aggregates."""
    table = asset.rsplit(".", 1)[-1]
    by_month = {}
    latest: dict[str, date] = {}
    for r in monthly:
        m = _day(r.get("month"))
        if m is None:
            continue
        by_month[f"{m:%Y-%m}"] = int(r.get("n") or 0)
        if (d := _day(r.get("latest"))) is not None:
            latest[f"{m:%Y-%m}"] = d
    facts: list[str] = []
    out: dict[str, Any] = {"asset": asset, "column": column, "windows": {}, "facts": facts}
    for key in ("baseline", "current"):
        w = p.get(key)
        if not w:
            continue
        start, end = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
        months = _months_in(start, end)
        n = sum(by_month.get(m, 0) for m in months)
        last = max((latest[m] for m in months if m in latest), default=None)
        empty = [m for m in months if not by_month.get(m)]
        out["windows"][key] = {"label": w["label"], "rows": n, "latest": last.isoformat() if last else None,
                               "empty_months": empty}
        name = "requested period" if key == "current" else "baseline"
        if n == 0:
            facts.append(f"{table}: the {name} {w['label']} has no rows ({column}).")
            continue
        last_day = end - timedelta(days=1)
        if end > today + timedelta(days=1):
            facts.append(f"{table}: the {name} {w['label']} has not ended yet (it runs to {last_day.isoformat()}); "
                         "its figures are partial.")
        trailing = []
        for m in reversed(months):
            if by_month.get(m):
                break
            trailing.append(m)
        if trailing and last is not None:
            facts.append(f"{table}: the {name} {w['label']} is incomplete: the data ends on {last.isoformat()}, before "
                         f"the period ends on {last_day.isoformat()} (no rows in {', '.join(reversed(trailing))}).")
        gaps = [m for m in empty if m not in trailing]
        if gaps:
            facts.append(f"{table}: the {name} {w['label']} has months without rows: {', '.join(gaps)}.")
    if future and int(future.get("n") or 0) > 0:
        first, last = _day(future.get("first")), _day(future.get("last"))
        n = int(future["n"])
        span = f" ({first.isoformat()} to {last.isoformat()})" if first and last else ""
        facts.append(f"{table}: {n} row{'s are' if n != 1 else ' is'} dated after today{span} by {column}; "
                     "they are outside every period and are not analysed.")
        out["future_rows"] = n
    return out


def _rows(result: Any) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, r, strict=False)) for r in result.rows]


def data_facts(p: Mapping[str, Any] | None, run_sql_for: Callable[[str], Any], *, today: date | None = None) -> list[dict[str, Any]]:
    """Per table with a period column: window counts and data facts. `run_sql_for(asset)` returns the governed
    runner for that table's source (it has a `dialect`)."""
    if not p:
        return []
    today = today or datetime.now(UTC).date()
    out = []
    for asset, column in sorted((p.get("columns") or {}).items()):
        if not column:
            out.append({"asset": asset, "column": None, "windows": {}, "facts": [
                f"{asset.rsplit('.', 1)[-1]}: the requested period cannot be applied "
                f"({(p.get('column_reasons') or {}).get(asset) or 'no date column'}); its tests are not run."]})
            continue
        run_sql = run_sql_for(asset)
        monthly_sql, future_sql = fact_queries(asset, column, p, run_sql.dialect, today)
        monthly = _rows(run_sql(monthly_sql, purpose="analysis.period_facts"))
        future = (_rows(run_sql(future_sql, purpose="analysis.period_facts")) or [None])[0]
        out.append(summarize(asset, column, p, monthly, future, today))
    return out


__all__ = ["COMPARISON_MODES", "apply", "baseline_for", "comparison_proposals", "constraint", "data_facts",
           "derivation", "is_comparison", "labels", "pick_time_column", "summarize", "time_columns", "windows"]

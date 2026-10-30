"""Periods an Ask question names, and whether the data covers them (PILOT-FIX-02; rehearsal C02, C03).

`find_period` reads a period from the wording, deterministically: a quarter ("Q3 2026", "third quarter of 2026",
"2026 Q3"), a half ("H1 2026"), a month ("September 2026"), a year ("in 2026") and "this|last month|quarter|year".
A period without a year takes the year given by the caller (the previous answer's period), else the latest year in
which that period has started.

`coverage_note` reads the date window the answer's SQL applied to the period (a column compared with date literals),
asks the gateway when the records inside it start and end, and says so when part of the period has no records: the
demo's Q3 2026 holds only July and August, and a partial quarter must not read as a whole one. The check is one
aggregate query through `QueryGateway.execute`; it never changes the answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_YEAR = r"((?:19|20)\d{2})"
_OF_YEAR = r"(?:\s*(?:of\s+|,\s*)?(?:fy\s*)?" + _YEAR + r")?"
_QUARTER = re.compile(r"\b(?:q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th) quarter)" + _OF_YEAR + r"\b")
_YEAR_QUARTER = re.compile(r"\b" + _YEAR + r"[\s-]?q([1-4])\b")
_HALF = re.compile(r"\b(?:h([12])|(first|second) half)" + _OF_YEAR + r"\b")
# "may" is a verb too: it names a month only with a year.
_MONTH = re.compile(r"\b(?:(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
                    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)" + _OF_YEAR + r"|(may)\s+" + _YEAR + r")\b")
_IN_YEAR = re.compile(r"\b(?:in|for|during|over|throughout|year|fy)\s+" + _YEAR + r"\b")
_RELATIVE = re.compile(r"\b(this|current|last|previous|past)\s+(month|quarter|year)\b")
_TOLERANCE = timedelta(days=2)  # a period whose data stops this close to its end is not called incomplete


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive
    label: str
    span: tuple[int, int] = (0, 0)  # where the wording named it, in the lower-cased text

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label}

    @classmethod
    def from_dict(cls, value: Any) -> Period | None:
        if not isinstance(value, dict):
            return None
        try:
            return cls(date.fromisoformat(str(value["start"])), date.fromisoformat(str(value["end"])), str(value["label"]))
        except (KeyError, ValueError):
            return None


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _quarter(year: int, q: int) -> tuple[date, date]:
    start = date(year, 3 * (q - 1) + 1, 1)
    return start, _add_months(start, 3)


def _latest_year(start_of: Any, today: date) -> int:
    """The latest year in which a period without a year has started (Q3 asked in October 2026 is Q3 2026)."""
    return today.year if start_of(today.year) <= today else today.year - 1


def find_period(text: str, *, today: date | None = None, default_year: int | None = None) -> Period | None:
    """The first period the wording names, or None."""
    t = (text or "").lower().replace("’", "'")
    today = today or date.today()
    found: list[Period] = []
    for m in _YEAR_QUARTER.finditer(t):
        start, end = _quarter(int(m.group(1)), int(m.group(2)))
        found.append(Period(start, end, f"Q{m.group(2)} {m.group(1)}", m.span()))
    for m in _QUARTER.finditer(t):
        q = int(m.group(1)) if m.group(1) else _ORDINAL[m.group(2)]
        year = int(m.group(3)) if m.group(3) else default_year or _latest_year(lambda y, q=q: _quarter(y, q)[0], today)
        start, end = _quarter(year, q)
        found.append(Period(start, end, f"Q{q} {year}", m.span()))
    for m in _HALF.finditer(t):
        h = int(m.group(1)) if m.group(1) else (1 if m.group(2) == "first" else 2)
        year = int(m.group(3)) if m.group(3) else default_year or _latest_year(lambda y, h=h: date(y, 6 * h - 5, 1), today)
        start = date(year, 6 * h - 5, 1)
        found.append(Period(start, _add_months(start, 6), f"H{h} {year}", m.span()))
    for m in _MONTH.finditer(t):
        word, year_text = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        month = _MONTHS[word[:3]]
        year = int(year_text) if year_text else default_year or _latest_year(lambda y, mo=month: date(y, mo, 1), today)
        start = date(year, month, 1)
        found.append(Period(start, _add_months(start, 1), f"{_MONTH_NAMES[month - 1]} {year}", m.span()))
    for m in _RELATIVE.finditer(t):
        which, unit = m.group(1), m.group(2)
        if unit == "month":
            start = date(today.year, today.month, 1)
            months = 1
        elif unit == "quarter":
            start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
            months = 3
        else:
            start, months = date(today.year, 1, 1), 12
        if which in ("last", "previous", "past"):
            start = _add_months(start, -months)
        end = _add_months(start, months)
        label = {1: f"{_MONTH_NAMES[start.month - 1]} {start.year}", 3: f"Q{(start.month - 1) // 3 + 1} {start.year}",
                 12: str(start.year)}[months]
        found.append(Period(start, end, label, m.span()))
    for m in _IN_YEAR.finditer(t):
        if not any(p.span[0] <= m.start(1) < p.span[1] for p in found):
            year = int(m.group(1))
            found.append(Period(date(year, 1, 1), date(year + 1, 1, 1), str(year), m.span()))
    return min(found, key=lambda p: p.span[0]) if found else None


# ------------------------------------------------------------------------------------ the SQL's date window
_LOWER = (exp.GTE, exp.GT)
_UPPER = (exp.LTE, exp.LT)
_FLIP = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT}


def _literal_date(node: exp.Expression) -> date | None:
    while isinstance(node, (exp.Cast, exp.TryCast)) or type(node).__name__ in ("TsOrDsToDate", "StrToDate", "Date"):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        try:
            return datetime.fromisoformat(node.this.strip().replace("Z", "")).date()
        except ValueError:
            return None
    return None


def _date_column(node: exp.Expression) -> exp.Column | None:
    """The one column a date-side expression reads (`created_on`, `CAST(created_on AS DATE)`), or None."""
    cols = list(node.find_all(exp.Column))
    if len(cols) != 1 or node.find(exp.Select) is not None:
        return None
    return cols[0]


def conjuncts(where: exp.Expression | None) -> list[exp.Expression]:
    if where is None:
        return []
    node = where.this if isinstance(where, exp.Where) else where
    out: list[exp.Expression] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, exp.Paren):
            stack.append(n.this)
        elif isinstance(n, exp.And):
            stack.extend([n.right, n.left])
        else:
            out.append(n)
    return out


def date_bounds(condition: exp.Expression) -> tuple[exp.Column, date | None, date | None] | None:
    """(column, lower, exclusive upper) of one comparison of a column with a date literal, or None."""
    if isinstance(condition, exp.Between):
        column, lo, hi = _date_column(condition.this), _literal_date(condition.args["low"]), _literal_date(condition.args["high"])
        if column is None or lo is None or hi is None:
            return None
        return column, lo, hi + timedelta(days=1)
    kind = type(condition)
    if kind not in _FLIP:
        return None
    left, right = condition.this, condition.expression
    value = _literal_date(right)
    side = left
    if value is None:
        value, side, kind = _literal_date(left), right, _FLIP[kind]
    column = _date_column(side) if value is not None else None
    if column is None or value is None:
        return None
    if kind is exp.GTE:
        return column, value, None
    if kind is exp.GT:
        return column, value + timedelta(days=1), None
    if kind is exp.LT:
        return column, None, value
    return column, None, value + timedelta(days=1)


def windows(select: exp.Select) -> dict[str, tuple[exp.Column, date | None, date | None]]:
    """Column SQL -> (column, lower, exclusive upper) of every date window in one SELECT's WHERE."""
    out: dict[str, tuple[exp.Column, date | None, date | None]] = {}
    for c in conjuncts(select.args.get("where")):
        b = date_bounds(c)
        if b is None:
            continue
        key = b[0].sql()
        _, lo, hi = out.get(key, (b[0], None, None))
        lo = max(x for x in (lo, b[1]) if x is not None) if (lo or b[1]) else None
        hi = min(x for x in (hi, b[2]) if x is not None) if (hi or b[2]) else None
        out[key] = (b[0], lo, hi)
    return out


def coverage_sql(sql: str, dialect: str, period: Period) -> str | None:
    """A MIN/MAX/COUNT of the date column over the rows the answer read for `period`, or None when its SQL applies no
    date window that overlaps the period."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    for select in tree.find_all(exp.Select):
        for column, lo, hi in windows(select).values():
            if (lo is None or lo < period.end) and (hi is None or hi > period.start) and (lo or hi):
                probe = select.copy()
                for arg in ("group", "order", "limit", "offset", "having", "distinct", "qualify"):
                    probe.set(arg, None)
                cte = tree.args.get("with_") or tree.args.get("with")
                if select is not tree and cte is not None:
                    probe.set("with_" if "with_" in tree.args else "with", cte.copy())
                probe.set("expressions", [exp.alias_(exp.Min(this=column.copy()), "first_at"),
                                          exp.alias_(exp.Max(this=column.copy()), "last_at"),
                                          exp.alias_(exp.Count(this=exp.Star()), "n")])
                return probe.sql(dialect=dialect)
    return None


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).date()
    except ValueError:
        return None


def _day(d: date) -> str:
    return f"{d.day} {_MONTH_NAMES[d.month - 1][:3]} {d.year}"


def note_for(period: Period, first: Any, last: Any, n: Any, today: date | None = None) -> str | None:
    """What a reader must know about the period's coverage, or None when the data covers it."""
    today = today or date.today()
    first_d, last_d = _as_date(first), _as_date(last)
    if not n or first_d is None or last_d is None:
        return f"No records fall in {period.label}."
    in_progress = period.end > today + timedelta(days=1)
    until = min(period.end - timedelta(days=1), today)
    late_start, early_end = first_d > period.start + _TOLERANCE, last_d < until - _TOLERANCE
    if not (late_start or early_end):
        return f"{period.label} is still in progress: these records run to {_day(last_d)}." if in_progress else None
    months, m = [], date(period.start.year, period.start.month, 1)
    while m <= until:
        nxt = _add_months(m, 1)
        if nxt <= first_d or m > last_d:
            months.append(f"{_MONTH_NAMES[m.month - 1]} {m.year}")
        m = nxt
    text = f"{period.label} is incomplete in this data: its records run from {_day(first_d)} to {_day(last_d)}"
    if months:
        text += f", with none in {', '.join(months[:-1]) + ' or ' + months[-1] if len(months) > 1 else months[0]}"
    return text + (". The period is still in progress." if in_progress else ". Read it as a partial period, not a whole one.")


def coverage_note(ctx: Any, sql: str | None, period: Period | None) -> str | None:
    """The coverage note for one answered SQL and the period its question named; never raises."""
    from analystos.agents.sql_agent import ASK_PURPOSE, ask_actor, ask_dialect
    from analystos.core.logging import get_logger

    if not sql or period is None:
        return None
    dialect = ask_dialect(ctx)
    probe = coverage_sql(sql, dialect, period)
    if probe is None:
        return (f"The question names {period.label}, but no date filter for it was found in the query: check which "
                f"period these numbers cover.")
    try:
        result = ctx.services.gateway.execute(ctx.scope, probe, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE,
                                              run_id=None, task_id=getattr(ctx, "turn_id", None), max_rows=1)
    except Exception:  # noqa: BLE001 - the answer stands without the note; the log says why
        get_logger(__name__).warning("ask period coverage check failed", exc_info=True)
        return None
    row = (result.rows or [[None, None, 0]])[0]
    return note_for(period, row[0], row[1], row[2])


def left_out_note(sql: str | None, dialect: str) -> str | None:
    """Records an answer's groups leave out: a GROUP BY column its WHERE requires to be present (`x IS NOT NULL`),
    so "incidents without a group" are said to be left out rather than silently missing (rehearsal C02)."""
    if not sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is None:
            continue
        grouped = {c.name.lower() for g in group.expressions for c in g.find_all(exp.Column)}
        aliases = {p.alias_or_name.lower(): p for p in select.expressions}
        for g in group.expressions:  # GROUP BY 1 or an alias names a projection
            target = aliases.get(g.name.lower()) if isinstance(g, exp.Column) else None
            if isinstance(g, exp.Literal) and g.is_int and 0 < int(g.this) <= len(select.expressions):
                target = select.expressions[int(g.this) - 1]
            if target is not None:
                grouped |= {c.name.lower() for c in target.find_all(exp.Column)}
        for cond in conjuncts(select.args.get("where")):
            is_ = cond.this if isinstance(cond, exp.Not) else cond  # `NOT x IS NULL`, or `x IS NOT NULL` (negate)
            negated = isinstance(cond, exp.Not) or bool(is_.args.get("negate"))
            if negated and isinstance(is_, exp.Is) and isinstance(is_.this, exp.Column) \
                    and isinstance(is_.expression, exp.Null) and is_.this.name.lower() in grouped:
                return f"Records with no {is_.this.name.replace('_', ' ')} are left out of these groups."
    return None

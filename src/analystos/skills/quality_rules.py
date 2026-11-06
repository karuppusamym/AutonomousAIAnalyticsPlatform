"""Declared quality rules (P1-7) as SQL: one single-row, read-only statement per rule, built with the
`skills/sqlbuild` helpers and run through the gateway (`RunSQL`), and the deterministic verdict.

Row rules return `evaluated` and `passed` (pass rate = passed / evaluated, compared with the rule's threshold);
`unique` returns `evaluated` rows and `groups` (pass rate = distinct key groups / non-null rows). Aggregate rules
return one `value` compared with [min, max]: they pass (1.0) or fail (0.0) whole. Owner SQL (sql_row,
sql_aggregate) is parsed in the source dialect and must be an expression over the asset's own columns:
`check_expression` refuses subqueries, table references, unknown (anonymous) functions, windows, parameters
and stars before anything is sent, and the gateway validator checks scope again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.contracts.quality import QualityRule
from analystos.core.errors import InvalidInput
from analystos.skills.base import RunSQL
from analystos.skills.sqlbuild import (
    _check_dialect,
    and_all,
    as_double,
    case,
    cast,
    col,
    count_star,
    ident,
    is_null,
    lit,
    not_null,
    num,
    or_all,
    percentile_cont,
    table,
    to_sql,
)

NO_REGEX = frozenset({"tsql"})  # SQL Server has no regular-expression predicate before 2025
FORBIDDEN_NODES = (exp.Select, exp.Subquery, exp.Table, exp.Anonymous, exp.Window, exp.Placeholder, exp.Parameter,
                   exp.Star, exp.Command, exp.Into, exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Union,
                   )


@dataclass(frozen=True)
class CompiledRule:
    sql: str
    shape: str  # rows | groups | value | null_rate


@dataclass
class RuleOutcome:
    status: str  # passed | failed | error
    pass_rate: float | None
    evaluated: int | None = None
    passed_rows: int | None = None
    value: float | None = None
    query_id: str | None = None
    sql: str = ""
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "pass_rate": self.pass_rate, "evaluated": self.evaluated,
                "passed_rows": self.passed_rows, "value": self.value, "query_id": self.query_id, "error": self.error,
                **({"detail": self.detail} if self.detail else {})}


def check_expression(sql: str, dialect: str, columns: list[str] | None, *, aggregate: bool) -> exp.Expression:
    """Parse owner SQL as one expression over the asset's columns, or `InvalidInput` with why not."""
    text = (sql or "").strip().rstrip(";")
    if not text or ";" in text:
        raise InvalidInput("rule sql must be one expression")
    try:
        parsed = sqlglot.parse_one(text, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise InvalidInput(f"rule sql does not parse as {dialect}: {str(exc).splitlines()[0]}") from None
    for node in parsed.walk():
        if isinstance(node, exp.Star) and isinstance(node.parent, exp.Count):
            continue  # COUNT(*)
        if isinstance(node, FORBIDDEN_NODES):
            raise InvalidInput(f"rule sql may not contain {type(node).__name__.lower()}: "
                               "only an expression over this asset's columns and known functions")
    known = {c.lower() for c in columns} if columns is not None else None
    for c in parsed.find_all(exp.Column):
        if c.table:
            raise InvalidInput("rule sql may not qualify columns with a table: it runs over the rule's asset only")
        if known is not None and c.name.lower() not in known:
            raise InvalidInput(f"rule sql names unknown column {c.name!r}")
    has_agg = parsed.find(exp.AggFunc) is not None
    if aggregate and not has_agg:
        raise InvalidInput("a sql_aggregate rule needs an aggregate (SUM, COUNT, AVG, MIN, MAX, ...)")
    if not aggregate and has_agg:
        raise InvalidInput("a sql_row rule is a per-row condition: no aggregates")
    for c in parsed.find_all(exp.Column):  # quote every identifier: the owner's spelling, never a keyword
        c.set("this", exp.to_identifier(c.name, quoted=True))
    return parsed


def _value_in(c: exp.Expression, values: list[Any], dialect: str) -> exp.Expression:
    return exp.In(this=c, expressions=[lit(v, dialect) for v in values])


def _null_ok(rule: QualityRule, c: exp.Expression, cond: exp.Expression) -> exp.Expression:
    return or_all([is_null(c.copy()), exp.Paren(this=cond)]) if rule.allow_null else and_all([not_null(c.copy()), cond])


def _pass_condition(rule: QualityRule, dialect: str) -> exp.Expression:
    if rule.type == "not_null":
        return not_null(col(rule.column or ""))
    c = col(rule.column or "") if rule.column else None
    if rule.type == "accepted_values":
        return _null_ok(rule, c, _value_in(c.copy(), list(rule.values), dialect))
    if rule.type == "range":
        bounds = [exp.GTE(this=as_double(c.copy(), dialect), expression=num(rule.min))] if rule.min is not None else []
        bounds += [exp.LTE(this=as_double(c.copy(), dialect), expression=num(rule.max))] if rule.max is not None else []
        return _null_ok(rule, c, and_all(bounds))
    if rule.type == "regex":
        if dialect in NO_REGEX:
            raise InvalidInput(f"regex rules are not supported on {dialect}; use accepted_values or sql_row")
        match = exp.RegexpLike(this=cast(c.copy(), "text", dialect), expression=exp.Literal.string(rule.pattern or ""))
        return _null_ok(rule, c, match)
    if rule.type == "sql_row":
        return exp.Paren(this=check_expression(rule.sql or "", dialect, None, aggregate=False))
    raise InvalidInput(f"{rule.type} is not a row condition")


def _statistic(rule: QualityRule, dialect: str) -> tuple[exp.Expression, str]:
    c = col(rule.column or "")
    s = rule.statistic
    if s == "null_rate":
        return c, "null_rate"
    if s == "count":
        return exp.Count(this=c), "value"
    if s == "distinct_count":
        return exp.Count(this=exp.Distinct(expressions=[c])), "value"
    if s == "median":
        if dialect == "tsql":
            raise InvalidInput("a median statistic is not available on tsql (PERCENTILE_CONT is window-only there)")
        return percentile_cont(as_double(c, dialect), 0.5, dialect), "value"
    agg = {"mean": exp.Avg, "min": exp.Min, "max": exp.Max, "sum": exp.Sum, "stddev": exp.Stddev}[s or "mean"]
    return agg(this=as_double(c, dialect)), "value"


def compile_rule(rule: QualityRule, dialect: str, *, columns: list[str] | None = None) -> CompiledRule:
    """The rule's one statement in `dialect`. `columns` (the asset's catalog columns) lets owner SQL be checked
    for unknown names before it is sent; the gateway checks scope either way."""
    dialect = _check_dialect(dialect)
    known = {c.lower() for c in columns} if columns is not None else None
    if known is not None:
        missing = [t for t in rule.targets() if t.lower() not in known]
        if missing:
            raise InvalidInput(f"{rule.asset} has no column {', '.join(missing)}")
    src = table(rule.asset)
    if rule.type == "unique":
        keys = [col(k) for k in rule.targets()]
        inner = (exp.select(*[k.copy() for k in keys], count_star().as_(ident("cnt"))).from_(src)
                 .where(and_all([not_null(k.copy()) for k in keys])).group_by(*[k.copy() for k in keys]))
        q = exp.select(exp.Sum(this=exp.column("cnt", quoted=True)).as_(ident("evaluated")),
                       count_star().as_(ident("groups"))).from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=ident("g"))))
        return CompiledRule(to_sql(q, dialect), "groups")
    if rule.row_level:
        if rule.type == "sql_row" and columns is not None:
            check_expression(rule.sql or "", dialect, columns, aggregate=False)
        cond = _pass_condition(rule, dialect)
        q = exp.select(count_star().as_(ident("evaluated")),
                       exp.Sum(this=case([(cond, num(1))], num(0))).as_(ident("passed"))).from_(src)
        return CompiledRule(to_sql(q, dialect), "rows")
    if rule.type == "sql_aggregate":
        e = check_expression(rule.sql or "", dialect, columns, aggregate=True)
        return CompiledRule(to_sql(exp.select(as_double(exp.Paren(this=e), dialect).as_(ident("value"))).from_(src), dialect),
                            "value")
    e, shape = _statistic(rule, dialect)
    if shape == "null_rate":
        q = exp.select(count_star().as_(ident("evaluated")), exp.Count(this=e).as_(ident("non_null"))).from_(src)
        return CompiledRule(to_sql(q, dialect), "null_rate")
    return CompiledRule(to_sql(exp.select(e.as_(ident("value"))).from_(src), dialect), "value")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def judge(rule: QualityRule, compiled: CompiledRule, row: dict[str, Any]) -> RuleOutcome:
    """The verdict from the statement's single row (keys lower-cased)."""
    if compiled.shape in ("rows", "groups"):
        n = int(row.get("evaluated") or 0)
        ok = int(row.get("passed") if compiled.shape == "rows" else row.get("groups") or 0)
        rate = (ok / n) if n else 1.0  # an empty table breaks no row rule; freshness and volume say it is empty
        status = "passed" if rate >= rule.threshold - 1e-12 else "failed"
        return RuleOutcome(status=status, pass_rate=round(rate, 6), evaluated=n, passed_rows=ok,
                           detail={"threshold": rule.threshold, "empty": n == 0})
    if compiled.shape == "null_rate":
        n = int(row.get("evaluated") or 0)
        value = (1 - int(row.get("non_null") or 0) / n) if n else None
    else:
        value = _num(row.get("value"))
    inside = value is not None and (rule.min is None or value >= rule.min) and (rule.max is None or value <= rule.max)
    return RuleOutcome(status="passed" if inside else "failed", pass_rate=1.0 if inside else 0.0,
                       value=round(value, 9) if value is not None else None,
                       detail={"min": rule.min, "max": rule.max, **({"no_value": True} if value is None else {})})


def run_rule(run_sql: RunSQL, rule: QualityRule, *, columns: list[str] | None = None) -> RuleOutcome:
    """Compile, run through the gateway, judge. A refused or failed statement is an `error` outcome, never a pass."""
    from analystos.core.errors import AnalystOSError

    dialect = getattr(run_sql, "dialect", "duckdb")
    try:
        compiled = compile_rule(rule, dialect, columns=columns)
    except InvalidInput as exc:
        return RuleOutcome(status="error", pass_rate=None, error=exc.message)
    try:
        res = run_sql(compiled.sql, purpose=f"quality.rule.{rule.type}", max_rows=1)
    except AnalystOSError as exc:
        return RuleOutcome(status="error", pass_rate=None, sql=compiled.sql, error=exc.message)
    records = [{k.lower(): v for k, v in r.items()} for r in res.records()]
    out = judge(rule, compiled, records[0] if records else {})
    out.sql, out.query_id = compiled.sql, getattr(res, "query_id", None)
    return out

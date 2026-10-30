"""Ask follow-ups that only narrow the previous answer (PILOT-FIX-02, rehearsal C02).

"Only priority 1 and 2 incidents", "just for Q3", "exclude cancelled", "without the marketplace channel": the person
keeps the previous question and adds one restriction. Before this rung such a message reached generation on its own,
and a model with no previous question asked what to measure.

The rules read the wording (`parse`), resolve the restriction against the table the previous answer read (column words
and glossary synonyms as `ask_rules` matches them; values against the column's complete profiled vocabulary, or its
profiled range for numbers; periods by `ask_period`) and add it to the previous query:

* a governed previous answer keeps its approved metric: its `SemanticQuery` gets one more filter (or its time window)
  and is compiled again by `semantic/compiler.py`;
* otherwise the previous SQL gets one more predicate in the SELECT that reads the table (an earlier restriction of the
  same column, or the date window of the same time column, is replaced, not stacked).

Nothing here is a model, and the SQL still runs through `QueryGateway.execute`. Two columns that hold the value equally
-> clarify, naming them. A restriction the rules cannot resolve is not guessed: the message goes on to generation, which
then receives the previous question and SQL as data (`refinement_context`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.agents import ask_rules
from analystos.agents.ask_period import Period, conjuncts, date_bounds, find_period
from analystos.core.logging import get_logger

log = get_logger(__name__)

_FILLER = re.compile(r"^(?:(?:and|but|ok|okay|now|then|also|so|please|great|thanks|thank you|right|cool|sure)[ ,]+)+")
_RELATIVE = re.compile(r"^(?:what about|how about|and for|now for|same for)\s+")
_SAME = re.compile(r"^(?:do |show |give me |run |ask )?(?:the |that )?same(?: thing| again| question| query| table| numbers| answer)?"
                   r"(?: but| for| with| again|,)*\s+")
# (pattern, strong): a strong form says "narrow the previous answer" even when the rules cannot resolve it
_INCLUDE = [
    (re.compile(r"^(?:only|just) (?:for |with |where |in |on |from |the |those |these )*(?P<body>.+?)(?: only| please)?$"), True),
    (re.compile(r"^(?:limit|limited|restrict|restricted|narrow|narrowed)(?: it| this| that| down| the results?)? to "
                r"(?P<body>.+?)(?: only)?$"), True),
    (re.compile(r"^filter(?:ed)?(?: it| this| that| the results?)? (?:to|on|for|by) (?P<body>.+?)(?: only)?$"), True),
    (re.compile(r"^(?:keep|include|show) only (?P<body>.+)$"), True),
    (re.compile(r"^(?P<body>.+?) only$"), True),
    (re.compile(r"^(?:for|in|during|within) (?P<body>.+)$"), False),
]
_EXCLUDE = [
    (re.compile(r"^(?:exclude|excluding|except|except for|without|leave out|leaving out|ignore|ignoring|not including|"
                r"other than|minus) (?:the |any |all )?(?P<body>.+)$"), True),
    (re.compile(r"^(?P<body>.+?) excluded$"), True),
]
_BODY_LEAD = re.compile(r"^(?:(?:the|those|these|with|where|that are|which are|that were|which were|in|on|from|of|for|"
                        r"during|within|records|rows) )+")
_GENERIC = frozenset({"one", "record", "row", "item", "case", "entry", "result", "data"})
_VALUE_SPLIT = re.compile(r"\s*(?:,|/|&|\+|\band\b|\bor\b|\bnor\b)\s*")
_RANGE = re.compile(r"^p?(\d{1,4})\s*(?:-|–|to|through|thru)\s*p?(\d{1,4})$")
_CONNECT = re.compile(r"^(?:is|are|=|==|of|in|equal to|equals|level|value|values|set to)\s+")
_CODE_PREFIX = re.compile(r"^(p|sev|s)(\d{1,3})$")  # "P1" is priority 1, "Sev2" severity 2
_CODE_COLUMNS = {"p": frozenset({"priority"}), "sev": frozenset({"severity", "sev"}), "s": frozenset({"severity", "sev"})}
MAX_VALUES = 50


@dataclass
class Narrowing:
    mode: str  # include | exclude
    body: str
    strong: bool = True  # worded as a restriction of the previous answer ("only", "exclude", "what about")


def parse(question: str) -> Narrowing | None:
    """The restriction a message adds, when the whole message is only a restriction; else None."""
    q = re.sub(r"\s+", " ", re.sub(r"[?!.;:]+", " ", (question or "").lower().replace("’", "'"))).strip()
    if not q or len(q) > 160:
        return None
    rest = _FILLER.sub("", q).strip(" ,")
    relative = _RELATIVE.match(rest)
    rest = rest[relative.end():] if relative else rest
    same = _SAME.match(rest)
    rest = rest[same.end():].strip(" ,") if same else rest
    for mode, patterns in (("exclude", _EXCLUDE), ("include", _INCLUDE)):
        for pattern, strong in patterns:
            m = pattern.match(rest)
            if m:
                return Narrowing(mode, m.group("body").strip(), strong or bool(relative or same))
    if (relative or same) and rest:  # "what about priority 1 and 2?", "same for Q2"
        return Narrowing("include", rest)
    return None


# ------------------------------------------------------------------------------------ values
def _number(text: str) -> int | float | None:
    try:
        n = float(text)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _stems(text: str) -> set[str]:
    t = re.sub(r"[^a-z0-9]+", "", text.lower())
    out = {t}
    for suffix in ("s", "es", "ed", "d"):
        if len(t) > len(suffix) + 2 and t.endswith(suffix):
            out.add(t[: -len(suffix)])
    if "ll" in t:
        out |= {x.replace("ll", "l") for x in list(out)}  # cancelled / canceled
    return out


def _vocabulary_value(column: ask_rules.Column, raw: str) -> Any:
    """The vocabulary's own spelling of one typed value, or None (exact, then letters only, then word stems, then a
    value that starts with the typed code: '1' for '1 - Critical'). Two equally good spellings: None."""
    vals = [v for v in column.values if v is not None]
    low = raw.lower().strip()
    tests = (lambda v: str(v).lower() == low,
             lambda v: re.sub(r"[^a-z0-9]+", "", str(v).lower()) == re.sub(r"[^a-z0-9]+", "", low),
             lambda v: bool(_stems(str(v)) & _stems(low)),
             lambda v: re.match(re.escape(low) + r"(?:\W|$)", str(v).lower()) is not None)
    for test in tests:
        hits = list(dict.fromkeys(v for v in vals if test(v)))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def _typed_value(column: ask_rules.Column, raw: str) -> Any:
    raw = raw.strip().strip("'\"")
    if not raw:
        return None
    if column.values:
        return _vocabulary_value(column, raw)
    if column.semantic == "boolean" or column.data_type == "boolean":
        return {"yes": True, "true": True, "no": False, "false": False}.get(raw.lower())
    if column.is_numeric:
        code = _CODE_PREFIX.match(raw.lower())
        if code and column.name_words & _CODE_COLUMNS[code.group(1)]:
            raw = code.group(2)
        n = _number(raw)
        if n is None:
            return None
        if column.bounds is not None:
            try:
                lo, hi = float(column.bounds[0]), float(column.bounds[1])
            except (TypeError, ValueError):
                return n
            if not lo <= n <= hi:
                return None
        return n
    return None


def values_for(column: ask_rules.Column, text: str) -> list[Any] | None:
    """Every value a phrase lists, typed for the column, or None when any of them is not the column's."""
    text = _CONNECT.sub("", text.strip())
    if not text:
        return None
    whole = _typed_value(column, text) if column.values else None  # a multi-word value: "network operations"
    if whole is not None:
        return [whole]
    out: list[Any] = []
    for part in [p for p in _VALUE_SPLIT.split(text) if p.strip()]:
        r = _RANGE.match(part.strip())
        if r and column.is_numeric and not column.values:
            lo, hi = int(r.group(1)), int(r.group(2))
            if hi < lo or hi - lo > MAX_VALUES:
                return None
            vals = [_typed_value(column, str(n)) for n in range(lo, hi + 1)]
        else:
            vals = [_typed_value(column, part)]
        if any(v is None for v in vals):
            return None
        out += [v for v in vals if v not in out]
    return out[:MAX_VALUES] or None


# ------------------------------------------------------------------------------------ resolution
@dataclass
class Restriction:
    """What the follow-up adds: a value filter on a column, or a period on a time column."""

    mode: str  # include | exclude
    column: ask_rules.Column | None = None
    values: list[Any] = field(default_factory=list)
    period: Period | None = None
    ambiguous: dict[str, list[str]] | None = None

    def describe(self) -> str:
        label = self.column.label.lower() if self.column else "date"
        if self.period is not None:
            return f"{self.period.label} (by {self.column.name.replace('_', ' ') if self.column else 'date'})"
        shown = " or ".join(str(v) for v in self.values) if len(self.values) <= 6 else f"{len(self.values)} values"
        return f"{label} {shown}" if self.mode == "include" else f"excluding {label} {shown}"

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode, "column": self.column.name if self.column else None}
        if self.period is not None:
            out["period"] = self.period.as_dict()
        else:
            out["values"] = list(self.values)
        return out


def _clean(body: str, table: ask_rules.Table, lex: Any) -> str:
    words = _BODY_LEAD.sub("", body.strip()).split()
    ignorable = set(table.entity_words) | _GENERIC

    def skip(word: str) -> bool:
        w = ask_rules._words(word, lex)
        return bool(w) and w <= ignorable

    while words and skip(words[-1]):
        words.pop()
    while words and skip(words[0]):
        words.pop(0)
    return " ".join(words).strip(" ,")


def resolve(narrowing: Narrowing, table: ask_rules.Table, lex: Any, *, time_column: ask_rules.Column | None,
            default_year: int | None) -> Restriction | None:
    """The one restriction a narrowing names on `table`, a clarify (`ambiguous`) or None."""
    text = _clean(narrowing.body, table, lex)
    if not text:
        return None
    period = find_period(text, default_year=default_year)
    if period is not None and narrowing.mode == "include":
        rest = _clean((text[:period.span[0]] + " " + text[period.span[1]:]).strip(), table, lex)
        if not rest:
            if time_column is not None:
                return Restriction("include", column=time_column, period=period)
            return Restriction("include", period=period, ambiguous={"date": [c.name for c in table.columns if c.is_time]})
    columns = [c for c in table.columns if not c.personal and not c.is_time and (c.is_dimension or c.values)]
    found: dict[str, tuple[ask_rules.Column, list[Any]]] = {}
    ambiguous: list[str] = []
    words = text.split()
    for i in range(1, len(words)):
        for col_words, val_words in ((words[:i], words[i:]), (words[i:], words[:i])):
            try:
                c = ask_rules._match(" ".join(col_words), columns, table, lex)
            except ask_rules._Ambiguous as amb:
                ambiguous += [x.name for x in amb.candidates]
                continue
            if c is None:
                continue
            vals = values_for(c, " ".join(val_words))
            if vals:
                found.setdefault(c.name, (c, vals))
    if not found:
        for c in columns:
            vals = values_for(c, text)
            if vals:
                found[c.name] = (c, vals)
    if len(found) == 1:
        (c, vals), = found.values()
        return Restriction(narrowing.mode, column=c, values=vals)
    options = list(found) or list(dict.fromkeys(ambiguous))
    return Restriction(narrowing.mode, ambiguous={text: options}) if len(options) > 1 else None


# ------------------------------------------------------------------------------------ the previous query
@dataclass
class Target:
    tree: exp.Expression
    select: exp.Select
    table: ask_rules.Table
    qualifier: str | None


def target(sql: str, dialect: str, tables: list[ask_rules.Table]) -> Target | None:
    """The one SELECT of the previous SQL that reads one of the caller's tables in its FROM (with its alias)."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    hits: list[Target] = []
    for select in tree.find_all(exp.Select):
        from_ = select.args.get("from_") or select.args.get("from")
        src = from_.this if from_ is not None else None
        if not isinstance(src, exp.Table):
            continue
        named = [t for t in tables if (t.fq.lower() == f"{src.db}.{src.name}".lower() if src.db
                                       else t.fq.split(".", 1)[1].lower() == src.name.lower())]
        if len(named) != 1:
            continue
        qualifier = src.alias or (src.name if select.args.get("joins") else None)
        hits.append(Target(tree, select, named[0], qualifier))
    return hits[0] if len(hits) == 1 else None


def _restricts(condition: exp.Expression, column: str) -> bool:
    """An earlier include restriction of the same column by literals (`priority IN (1, 2)`, `status = 'Open'`)."""
    if isinstance(condition, exp.In):
        return isinstance(condition.this, exp.Column) and condition.this.name.lower() == column \
            and all(isinstance(v, exp.Literal) for v in condition.expressions)
    if isinstance(condition, exp.EQ):
        sides = (condition.this, condition.expression)
        return any(isinstance(a, exp.Column) and a.name.lower() == column and isinstance(b, exp.Literal)
                   for a, b in (sides, sides[::-1]))
    return False


def time_columns(t: Target) -> list[str]:
    """Time columns of the table that the previous SQL already bounds by date literals."""
    names = {c.name.lower() for c in t.table.columns if c.is_time}
    out: list[str] = []
    for cond in conjuncts(t.select.args.get("where")):
        b = date_bounds(cond)
        if b is not None and b[0].name.lower() in names and b[0].name.lower() not in out:
            out.append(b[0].name.lower())
    return out


def refine_sql(t: Target, r: Restriction, dialect: str) -> str:
    """The previous SQL with the restriction in the SELECT that reads the table."""
    from analystos.skills.sqlbuild import lit

    assert r.column is not None
    column = exp.column(r.column.name, table=t.qualifier, quoted=True)
    conds = conjuncts(t.select.args.get("where"))
    name = r.column.name.lower()
    if r.period is not None:
        kept = [c for c in conds if not ((b := date_bounds(c)) is not None and b[0].name.lower() == name)]
        new: exp.Expression = exp.and_(exp.GTE(this=column.copy(), expression=lit(r.period.start.isoformat(), dialect)),
                                       exp.LT(this=column.copy(), expression=lit(r.period.end.isoformat(), dialect)))
    else:
        values = [lit(v, dialect) for v in r.values]
        if r.mode == "include":
            kept = [c for c in conds if not _restricts(c, name)]
            new = exp.EQ(this=column, expression=values[0]) if len(values) == 1 else exp.In(this=column, expressions=values)
        else:  # "exclude cancelled" keeps the records that have no status at all
            kept = conds
            new = exp.paren(exp.or_(exp.Not(this=exp.In(this=column.copy(), expressions=values)),
                                    exp.Is(this=column.copy(), expression=exp.Null())))
    t.select.set("where", exp.Where(this=exp.and_(*[c.copy() for c in kept], new) if kept else new))
    return t.tree.sql(dialect=dialect)


# ------------------------------------------------------------------------------------ the rung
def refinement_context(ctx: Any, question: str) -> dict[str, Any] | None:
    """For generation: a message worded as a narrowing, with the previous question and SQL as data."""
    previous = getattr(ctx, "previous_answer", None) or {}
    narrowing = parse(question)
    if not previous.get("sql") or not previous.get("question") or narrowing is None or not narrowing.strong:
        return None
    return {"previous_question": previous["question"], "previous_sql": previous["sql"],
            "note": "This message narrows the previous question: answer the previous question with this restriction added."}


def _base(previous: dict[str, Any]) -> str:
    """The question a chain of follow-ups started from ("...in Q3 2026?" after "only P1", then "just for Q2")."""
    return str(previous.get("base_question") or previous.get("question") or "")


def _followup(previous: dict[str, Any], r: Restriction) -> dict[str, Any]:
    """What this follow-up records: its restriction, the question the chain started from and every restriction the
    answer now carries (one of the same column and kind replaces the earlier one, as the SQL does)."""
    column = r.column.name if r.column else None
    kept = [c for c in previous.get("narrowed") or [] if isinstance(c, dict)
            and not (c.get("column") == column and c.get("mode") == r.mode)]
    return {**r.summary(), "base_question": _base(previous),
            "narrowed": [*kept, {"column": column, "mode": r.mode, "text": r.describe()}]}


def _narrowed_text(followup: dict[str, Any]) -> str:
    return "; ".join(c["text"] for c in followup["narrowed"])


def _default_year(previous: dict[str, Any]) -> int | None:
    p = Period.from_dict(previous.get("period")) or find_period(previous.get("question") or "")
    return p.start.year if p is not None else None


def answer(ctx: Any, question: str, parameters: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The previous answer narrowed by this message, a clarify, or None (not a narrowing the rules can apply)."""
    previous = getattr(ctx, "previous_answer", None) or {}
    if parameters or not previous.get("sql"):
        return None
    narrowing = parse(question)
    if narrowing is None or not getattr(getattr(ctx, "scope", None), "columns", None):
        return None
    from analystos.agents.sql_agent import ask_dialect
    from analystos.capabilities import packs

    try:
        with packs.only(packs.for_scope(ctx.scope, getattr(ctx, "policy", None))):
            tables, lex = ask_rules.load_tables(ctx)
    except Exception as exc:  # noqa: BLE001 - the rung is optional; the message takes the next one
        log.warning("ask follow-up: catalog unavailable: %s", exc)
        return None
    dialect = ask_dialect(ctx)
    t = target(previous["sql"], dialect, tables)
    if t is None:
        return None
    bounded = time_columns(t)
    time_column = next((c for c in t.table.columns if c.name.lower() == bounded[0]), None) if len(bounded) == 1 else None
    if time_column is None:
        try:
            time_column = ask_rules._time_column(t.table, ask_rules.Intent(shape="count"), previous.get("question") or "", lex)
        except (LookupError, ask_rules._Ambiguous):
            time_column = None
    r = resolve(narrowing, t.table, lex, time_column=time_column, default_year=_default_year(previous))
    if r is None:
        return None
    if r.ambiguous:
        return _clarify(ctx, previous, r)
    governed = _governed(ctx, previous, r)
    if governed is not None:
        return governed
    return _run(ctx, previous, r, refine_sql(t, r, dialect), t.table.fq)


def _clarify(ctx: Any, previous: dict[str, Any], r: Restriction) -> dict[str, Any]:
    from analystos.agents.sql_agent import _stage

    (phrase, options), = r.ambiguous.items()  # type: ignore[union-attr]
    _stage(ctx, "clarify", f"'{phrase}' matches more than one column: {', '.join(options)}")
    suggestions = [f"Only {o.replace('_', ' ')} {phrase}" for o in options[:4]] if phrase != "date" else []
    return {"status": "clarify", "answered_by": "rules", "route": "tool", "sql": None, "model": None, "attempts": [],
            "decisions": [], "result": None, "missing": [{"name": phrase, "values": options}], "suggestions": suggestions,
            "parent_turn_id": previous.get("id"),
            "explanation": f"This narrows the previous answer, but '{phrase}' could be {' or '.join(options)}. Say which "
                           "one; nothing was guessed."}


def _governed(ctx: Any, previous: dict[str, Any], r: Restriction) -> dict[str, Any] | None:
    """A governed previous answer narrowed through its own SemanticQuery, compiled again (it stays governed)."""
    from pydantic import ValidationError

    from analystos.contracts.semantic import SemanticFilter, SemanticQuery, SemanticTime
    from analystos.core.errors import InvalidInput

    semantic = previous.get("semantic") or {}
    catalog = getattr(ctx, "semantic_catalog", None)
    if not semantic.get("query") or not catalog or r.column is None:
        return None
    try:
        query = SemanticQuery.model_validate(semantic["query"])
        metric = (catalog.get("metrics") or {}).get(query.metrics[0]) or {}
        dataset = next((d for d in catalog.get("datasets") or []
                        if d["name"] == (metric.get("definition") or {}).get("dataset")), None)
        if r.column.name not in {f["name"] for f in (dataset or {}).get("fields") or []}:
            return None
        if r.period is not None:
            grain = query.time.grain if query.time is not None and query.time.dimension == r.column.name else None
            query = query.model_copy(update={"time": SemanticTime(dimension=r.column.name, grain=grain,
                                                                  start=r.period.start, end=r.period.end)})
        else:
            kept = [f for f in query.filters if not (r.mode == "include" and f.field == r.column.name)]
            op = ("=" if len(r.values) == 1 else "in") if r.mode == "include" else "not_in"
            value = r.values[0] if op == "=" else list(r.values)
            query = query.model_copy(update={"filters": [*kept, SemanticFilter(field=r.column.name, op=op, value=value)]})
        from analystos.agents.sql_agent import compile_governed

        out = compile_governed(ctx, query, chosen_by="rules")
    except (ValidationError, InvalidInput) as exc:
        log.info("ask follow-up: governed refinement not possible (%s); refining the SQL", exc)
        return None
    followup = _followup(previous, r)
    return {**out, "parent_turn_id": previous.get("id"), "followup": followup,
            **({"period": r.period.as_dict()} if r.period else {}),
            "explanation": f"{out['explanation']}, narrowed to {_narrowed_text(followup)} (your follow-up to the previous "
                           "question; no model call)."}


def _run(ctx: Any, previous: dict[str, Any], r: Restriction, sql: str, table: str) -> dict[str, Any]:
    from analystos.agents.sql_agent import ASK_PURPOSE, _record_skip, _result, _stage, ask_actor

    restriction = r.describe()
    _stage(ctx, "followup", f"Narrowing the previous answer to {restriction}")
    _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
    result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE, run_id=None,
                                          task_id=None)
    _record_skip(ctx, f"L2 rules: follow-up narrows the previous answer to {restriction}", 500, rung="rules")
    chart = previous.get("chart") if isinstance(previous.get("chart"), dict) else None
    followup = _followup(previous, r)
    return {"status": "answered", "answered_by": "rules", "route": "tool", "sql": sql, "model": None, "attempts": [],
            "decisions": [], "result": _result(result), "parent_turn_id": previous.get("id"),
            "chart": {k: v for k, v in chart.items() if k in ("type", "x", "y")} if chart else None,
            "followup": followup, **({"period": r.period.as_dict()} if r.period else {}),
            "rules": {"shape": "followup", "table": table, **r.summary()},
            "explanation": f"The question \"{_base(previous)[:200]}\", narrowed to {_narrowed_text(followup)}. "
                           "The Ask rules added this to the previous query; no model call."}

"""Verified-query registry for Ask (ladder rung L1, ADR-0012, P4-T05).

Tool-first: before any model is asked, Ask matches the question against the workspace's verified
queries deterministically (normalised tokens, glossary synonyms and crawler business names), extracts
typed parameters (enum values from the profiled vocabulary, dates, numbers, quoted strings) and runs
the rendered template through the gateway. When the best match needs a parameter the question does
not give, it declines and asks for it instead of guessing. Only a miss falls through to the model.

A template is SQL with `{{name}}` placeholders. Values are rendered as SQL literals by sqlglot after
type validation (an enum value must be in the vocabulary), and the rendered statement is validated
and executed by the gateway under the asking user's scope, like every other statement."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import (
    Hypothesis,
    Insight,
    QueryExecution,
    SourceAsset,
    SourceColumn,
    VerifiedQuery,
)

PARAM_TYPES = ("enum", "date", "number", "string")
MIN_COVERAGE = 0.8  # share of the pattern's tokens the question must contain
MIN_PRECISION = 0.6  # share of the question's tokens the pattern must explain
PROMOTABLE_PURPOSES = ("ask", "console")

_STOP_WORDS = """a an the of for in on at to with from and or is are was were be been do does did what which who whom
how show me give list tell please there their its it this that these those we our i you your as than then all any
each within during into about can could would should get find display"""
_STOP = frozenset(_STOP_WORDS.split())
_SYNONYMS = {"many": "count", "number": "count", "num": "count", "howmany": "count", "total": "sum", "avg": "average",
             "mean": "average", "biggest": "top", "largest": "top", "highest": "top", "most": "top", "lowest": "bottom",
             "smallest": "bottom", "least": "bottom", "breakdown": "by", "split": "by", "per": "by", "trend": "over_time",
             "monthly": "month", "weekly": "week", "daily": "day"}
_PLACEHOLDER = re.compile(r"\{\{?\s*(\w+)\s*\}?\}")
_WORD = re.compile(r"[a-z0-9]+")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR_MONTH = re.compile(r"\b(\d{4})-(\d{2})\b")
_MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"))}
_MONTH_YEAR = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")


# ------------------------------------------------------------------------------ normalisation
@dataclass
class Lexicon:
    """Workspace synonyms: glossary terms and crawler business names, as phrase -> canonical phrase."""

    phrases: dict[str, str] = field(default_factory=dict)
    loaded_at: float = 0.0


_LEXICON_TTL_SECONDS = 60.0
_lexicons: dict[str, Lexicon] = {}


def lexicon(session: Session, workspace_id: str) -> Lexicon:
    cached = _lexicons.get(workspace_id)
    if cached is not None and time.monotonic() - cached.loaded_at < _LEXICON_TTL_SECONDS:
        return cached
    phrases: dict[str, str] = {}
    from analystos.knowledge.entries import visible_entries

    for e in visible_entries(session, workspace_id, kinds=("term", "metric", "definition"), trusted_only=True):
        canonical = _plain_phrase(e.name)
        for syn in e.synonyms or []:
            if _plain_phrase(syn) and _plain_phrase(syn) != canonical:
                phrases[_plain_phrase(syn)] = canonical
    for name, business in session.execute(
            select(SourceColumn.name, SourceColumn.business_name).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
            .where(SourceAsset.workspace_id == workspace_id, SourceColumn.business_name.is_not(None))):
        if _plain_phrase(business) and _plain_phrase(business) != _plain_phrase(name):
            phrases[_plain_phrase(business)] = _plain_phrase(name)
    out = Lexicon(phrases=phrases, loaded_at=time.monotonic())
    _lexicons[workspace_id] = out
    return out


def forget_lexicon(workspace_id: str) -> None:
    """Drop the cached lexicon so the next match reads the session's knowledge (after an apply or a rollback)."""
    _lexicons.pop(workspace_id, None)


def _plain_phrase(text: str | None) -> str:
    return " ".join(_WORD.findall((text or "").lower().replace("_", " ")))


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str, lex: Lexicon | None = None) -> set[str]:
    """Content tokens of a question: placeholders, stop words and plural endings removed; glossary
    synonyms and business names mapped to their canonical term or column name."""
    plain = " " + _plain_phrase(_PLACEHOLDER.sub(" ", text)) + " "
    for phrase in sorted((lex.phrases if lex else {}), key=len, reverse=True):
        plain = plain.replace(f" {phrase} ", f" {lex.phrases[phrase]} ")
    out = set()
    for w in plain.split():
        if w in _STOP or w.isdigit():
            continue
        w = _SYNONYMS.get(w, w)
        out.add(_stem(w))
    return out


# ------------------------------------------------------------------------------ parameters
def _find_phrase(text: str, value: str) -> tuple[int, int] | None:
    m = re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def _dates(text: str) -> list[tuple[int, int, str]]:
    found = []
    for m in _ISO_DATE.finditer(text):
        found.append((m.start(), m.end(), m.group(0)))
    for m in _YEAR_MONTH.finditer(text):
        if not any(s <= m.start() < e for s, e, _ in found):
            found.append((m.start(), m.end(), f"{m.group(1)}-{m.group(2)}-01"))
    for m in _MONTH_YEAR.finditer(text):
        found.append((m.start(), m.end(), f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}-01"))
    return sorted(found)


def extract(question: str, params: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Typed values for `params` found in the question, and the question with those spans blanked."""
    text = question
    values: dict[str, Any] = {}

    def blank(start: int, end: int) -> None:
        nonlocal text
        text = text[:start] + " " * (end - start) + text[end:]

    for p in (p for p in params if p.get("type") == "enum"):
        best = None
        for v in sorted((str(x) for x in p.get("values") or []), key=len, reverse=True):
            span = _find_phrase(text, v)
            if span:
                best = (span, v)
                break
        if best:
            values[p["name"]] = best[1]
            blank(*best[0])
    date_params = [p for p in params if p.get("type") == "date"]
    for p, (_, _, value) in zip(date_params, _dates(text), strict=False):
        values[p["name"]] = value
    for start, end, _ in _dates(text):
        blank(start, end)
    string_params = [p for p in params if p.get("type") == "string"]
    for p, m in zip(string_params, list(_QUOTED.finditer(text)), strict=False):
        values[p["name"]] = m.group(1) or m.group(2)
        blank(m.start(), m.end())
    number_params = [p for p in params if p.get("type") == "number"]
    for p, m in zip(number_params, list(_NUMBER.finditer(text)), strict=False):
        values[p["name"]] = float(m.group(0)) if "." in m.group(0) else int(m.group(0))
        blank(m.start(), m.end())
    return values, text


def coerce(param: dict[str, Any], value: Any) -> Any:
    """Validate one parameter value by its type; enum values must be in the vocabulary."""
    kind, name = param.get("type"), param.get("name")
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"parameter {name} must be a number") from exc
        return int(number) if number.is_integer() else number
    if kind == "date":
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError as exc:
            raise InvalidInput(f"parameter {name} must be a date (YYYY-MM-DD)") from exc
    text = str(value)
    if kind == "enum" and param.get("values"):
        for v in param["values"]:
            if str(v).lower() == text.lower():
                return str(v)
        raise InvalidInput(f"parameter {name} must be one of {param['values'][:20]}")
    if len(text) > 200:
        raise InvalidInput(f"parameter {name} is too long")
    return text


_BIND = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, params: list[dict[str, Any]], values: dict[str, Any], dialect: str) -> str:
    """Bind typed literals in ONE pass over the template: a value is never scanned again, so a value that looks like
    a placeholder (`{{other}}`) cannot pull another literal inside its quotes (governance review 2026-11-06)."""
    literals = {}
    for p in params:
        value = coerce(p, values[p["name"]])
        literal = exp.Literal.number(value) if p.get("type") == "number" else exp.Literal.string(str(value))
        literals[p["name"]] = literal.sql(dialect=dialect)
    unbound: list[str] = []

    def bind(m: re.Match[str]) -> str:
        if m.group(1) not in literals:
            unbound.append(m.group(1))
            return m.group(0)
        return literals[m.group(1)]

    sql = _BIND.sub(bind, template)
    if unbound or "{{" in _BIND.sub("", template):
        raise InvalidInput("verified query template has an unbound parameter")
    return sql


# ------------------------------------------------------------------------------ compatibility
# Token overlap says a question is *about* the same thing as a verified query; it cannot see that the
# question asks for another aggregate, an extra group or filter, or another value than the one the SQL
# hard-codes (P4-V02 off tier: 9 confident wrong answers, all such near misses). These checks compare
# what the question asks for with what the entry's SQL computes (derived by sqlglot) and its phrasings.
_AGG_CUES = {"sum": r"\b(?:sum|sums|total|totals)\b", "avg": r"\b(?:average|averages|avg|mean)\b",
             "count": r"\b(?:count|counts|how many|number of)\b", "median": r"\bmedian\b",
             "max": r"\b(?:max|maximum)\b", "min": r"\b(?:min|minimum)\b",
             "ratio": r"\b(?:rate|rates|share|percent|percentage|fraction|proportion|ratio)\b"}
_AGG_SQL = {exp.Sum: "sum", exp.Avg: "avg", exp.Count: "count", exp.Max: "max", exp.Min: "min", exp.Median: "median",
            exp.PercentileCont: "median", exp.PercentileDisc: "median"}
_DIM_CUE = re.compile(r"\b(?:broken down by|grouped by|split by|for each|for every|by|per|each|across)\s+(.+?)"
                      r"(?=\s+(?:for|in|with|where|since|between|from|during|over|under|above|below|that|which|who|when|"
                      r"having|including|excluding|except|on|at|versus|vs)\b|[?.,;:!]|\s*$)", re.IGNORECASE)
_DIM_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bthen\b)\s*", re.IGNORECASE)
_TIME_UNITS = frozenset(("hour", "day", "week", "month", "quarter", "year"))
_GENERIC = frozenset(("name", "id", "type", "code", "date", "at", "by", "count", "sum", "average", "top", "bottom", "value"))
_CAPITALISED = re.compile(r"(?<![\w'])[A-Z][\w-]*")
_PARAM_SENTINEL = "__aos_param_{}__"


@dataclass(frozen=True)
class Semantics:
    """What a verified query's SQL computes: its aggregates, one token set per group-by expression,
    and the literals it hard-codes in comparisons (column, value). Derived from the template."""

    aggregates: frozenset[str]
    dimensions: tuple[frozenset[str], ...]
    literals: tuple[tuple[str, str], ...]
    tables: tuple[str, ...]


# A pure function of the template text (no tenant data, no authorization), so not tenant-prefixed (S01-T7);
# bounded (ADR-014): cleared when full, a refill is one parse per template.
_SEMANTICS_MAX = 4096
_semantics_cache: dict[tuple[str, str], Semantics | None] = {}


def _remember(key: tuple[str, str], value: Semantics | None) -> None:
    if len(_semantics_cache) >= _SEMANTICS_MAX:
        _semantics_cache.clear()
    _semantics_cache[key] = value


def _column_tokens(names: list[str]) -> frozenset[str]:
    return frozenset(tokens(" ".join(names)))


def semantics(template: str, dialect: str = "postgres") -> Semantics | None:
    """Declared semantics of a template, or None when sqlglot cannot parse it (no checks then)."""
    key = (template, dialect)
    if key in _semantics_cache:
        return _semantics_cache[key]
    sql = _PLACEHOLDER.sub(lambda m: "'" + _PARAM_SENTINEL.format(m.group(1)) + "'", template)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        _remember(key, None)
        return None
    aggs: set[str] = set()
    for node in tree.walk():
        for cls, name in _AGG_SQL.items():
            if isinstance(node, cls):
                aggs.add(name)
        if isinstance(node, exp.Div) and any(True for _ in node.find_all(exp.AggFunc)):
            aggs.add("ratio")
    if "avg" in aggs:
        aggs.add("ratio")  # a rate is an average of a 0/1 flag
    dims: list[frozenset[str]] = []
    for group in tree.find_all(exp.Group):
        select_ = group.parent if isinstance(group.parent, exp.Select) else None
        projections = list(select_.expressions) if select_ is not None else []
        by_alias = {p.alias.lower(): p for p in projections if isinstance(p, exp.Alias)}
        for g in group.expressions:
            if isinstance(g, exp.Literal) and not g.is_string and str(g.this).isdigit() and 0 < int(g.this) <= len(projections):
                g = projections[int(g.this) - 1]
            elif isinstance(g, exp.Column) and not g.table and g.name.lower() in by_alias:
                g = by_alias[g.name.lower()]
            words = [c.name for c in g.find_all(exp.Column)]
            if isinstance(g, exp.Alias):
                words.append(g.alias)
            words += [str(v.this).lower() for v in g.find_all(exp.Var, exp.Literal) if str(v.this).lower() in _TIME_UNITS]
            dims.append(_column_tokens(words))
    literals: list[tuple[str, str]] = []
    for lit in tree.find_all(exp.Literal):
        value = str(lit.this)
        if value.startswith("__aos_param_"):
            continue
        parent = lit.parent
        column = None
        if isinstance(parent, exp.In):
            column = parent.this
        elif isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)):
            column = parent.left if parent.right is lit else parent.right
        if isinstance(column, exp.Column):
            literals.append((column.name, value))
    tables = tuple(".".join(p for p in (t.db, t.name) if p) for t in tree.find_all(exp.Table))
    out = Semantics(frozenset(aggs), tuple(dims), tuple(literals), tables)
    _remember(key, out)
    return out


def aggregates_asked(text: str, dimension_words: frozenset[str] = frozenset()) -> set[str]:
    """Aggregate words of a question. "number of X" where X is a grouped column names a dimension
    ("breach rate by the number of reassignments"), not a count."""
    low = text.lower()
    out = {name for name, cue in _AGG_CUES.items() if re.search(cue, low)}
    if "count" in out and not re.search(r"\b(?:count|counts|how many)\b", low):
        nouns = [_stem(m.group(1)) for m in re.finditer(r"\bnumber of\s+(?:the\s+)?(\w+)", low)]
        if nouns and all(n in dimension_words for n in nouns):
            out.discard("count")
    return out


def dimensions_asked(text: str, lex: Lexicon | None = None) -> list[set[str]]:
    """Group-by phrases of a question ("by X", "per X", "for each X", "X and Y"), as token sets."""
    out = []
    for m in _DIM_CUE.finditer(text):
        for part in _DIM_SPLIT.split(m.group(1)):
            t = tokens(part, lex)
            if t:
                out.append(t)
    return out


def _significant(t: set[str] | frozenset[str]) -> set[str]:
    return set(t) - _GENERIC or set(t)


def _covers(asked: set[str], grouped: set[str] | frozenset[str]) -> bool:
    """One names the other: "region" ~ sales_region, "sales channel" ~ channel; a shared word alone
    ("sales channel" vs sales_region) is another dimension."""
    a, g = _significant(asked), _significant(grouped)
    return bool(a) and bool(g) and (a <= g or g <= a)


def _mentions(text: str, value: str) -> bool:
    return _find_phrase(text, value) is not None


def _unexplained_values(question: str, residual: str, entry: VerifiedQuery, sem: Semantics, found: dict[str, Any],
                        vocabulary: dict[str, list[str]], lex: Lexicon | None) -> list[str]:
    """Values the question names that neither a parameter, a hard-coded literal nor a phrasing of the
    entry accounts for: each is a filter (or another value) the entry does not apply."""
    patterns = " \n ".join(entry.patterns or [])
    pattern_tokens = set().union(*(tokens(p, lex) for p in entry.patterns or [])) if entry.patterns else set()
    literal_values = {v.lower() for _, v in sem.literals}
    date_values = {str(found[p["name"]]) for p in entry.parameters or [] if p.get("type") == "date" and p["name"] in found}
    out: list[str] = []

    def explained(value: str) -> bool:
        return value.lower() in literal_values or _mentions(patterns, value)

    for start, end, iso in _dates(question):
        if iso not in date_values and not explained(iso) and not explained(question[start:end]):
            out.append(question[start:end])
    text = residual
    for m in _QUOTED.finditer(text):
        value = m.group(1) or m.group(2)
        if not explained(value):
            out.append(value)
        text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    for column, values in vocabulary.items():
        for v in sorted(values, key=len, reverse=True):
            span = _find_phrase(text, v)
            if span is None:
                continue
            if not explained(v):
                out.append(f"{column} = {v}")
            text = text[:span[0]] + " " * (span[1] - span[0]) + text[span[1]:]
    for m in _NUMBER.finditer(text):
        if not explained(m.group(0)):
            out.append(m.group(0))
    first = re.search(r"[A-Za-z]", question)
    for m in _CAPITALISED.finditer(text):
        if first is not None and m.start() == first.start():
            continue
        word = m.group(0)
        t = tokens(word, lex)
        if t and not t <= pattern_tokens and not explained(word):
            out.append(word)
    return out


def _vocabularies(session: Session | None, workspace_id: str, entries: list[VerifiedQuery]) -> dict[str, dict[str, list[str]]]:
    """Known values per table: profiled top values of non-sensitive columns, plus the enum
    vocabularies recorded on the workspace's verified-query parameters."""
    by_table: dict[str, dict[str, list[str]]] = {}
    for e in entries:
        for p in e.parameters or []:
            if p.get("type") == "enum" and p.get("values"):
                ref = str(p.get("column") or p["name"])
                table, _, column = ref.rpartition(".")
                by_table.setdefault(table.lower(), {})[column] = [str(v) for v in p["values"]]
    if session is not None:
        names = {t.rpartition(".")[2] for e in entries if (s := semantics(e.sql_template, e.dialect)) for t in s.tables}
        if names:
            rows = session.execute(select(SourceColumn.name, SourceColumn.profile, SourceColumn.tags, SourceAsset.schema_name,
                                          SourceAsset.name).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
                                   .where(SourceAsset.workspace_id == workspace_id, SourceAsset.name.in_(names)))
            for name, profile, tags, schema, asset in rows:
                if {"pii", "restricted"} & set(tags or []):
                    continue
                values = [str(t.get("value")) for t in (profile or {}).get("top_values") or [] if t.get("value") is not None]
                if values:
                    by_table.setdefault(f"{schema}.{asset}".lower(), {}).setdefault(name, values)
    return by_table


def _usable_value(v: str) -> bool:
    low = v.strip().lower()
    return len(low) >= 2 and low not in ("true", "false", "null", "none", "yes", "no") and not _NUMBER.fullmatch(low)


def incompatibilities(question: str, residual: str, entry: VerifiedQuery, found: dict[str, Any], pattern: str,
                      vocabulary: dict[str, dict[str, list[str]]] | None = None, lex: Lexicon | None = None) -> list[str]:
    """Why the entry does not answer the question although its words match (empty: it does). Checks
    the aggregate, the group-by, filters/values the entry does not apply, and a value the entry
    hard-codes that the question does not ask for."""
    sem = semantics(entry.sql_template, entry.dialect or "postgres")
    if sem is None:
        return []
    reasons: list[str] = []
    dim_words = frozenset().union(*sem.dimensions) if sem.dimensions else frozenset()
    patterns = list(entry.patterns or [])
    declared = set(sem.aggregates)
    for p in patterns:
        declared |= aggregates_asked(p, dim_words)
    asked = aggregates_asked(residual, dim_words)
    if asked and declared and not asked & declared:
        reasons.append(f"aggregate: the question asks for {'/'.join(sorted(asked))}, the verified query computes "
                       f"{'/'.join(sorted(declared - {'ratio'}) or sorted(declared))}")
    q_dims = dimensions_asked(residual, lex)
    options = [set(d) for d in sem.dimensions] + [d for p in patterns for d in dimensions_asked(_PLACEHOLDER.sub(" ", p), lex)]
    uncovered = [d for d in q_dims if not any(_covers(d, o) for o in options)]
    if uncovered:
        reasons.append("group-by: the verified query does not group by " + ", ".join(" ".join(sorted(d)) for d in uncovered))
    elif len(q_dims) > len(sem.dimensions):
        reasons.append(f"group-by: the question groups by {len(q_dims)} dimension(s), the verified query by {len(sem.dimensions)}")
    vocab: dict[str, list[str]] = {}
    for table in sem.tables:
        for column, values in (vocabulary or {}).get(table.lower(), {}).items():
            usable = [v for v in values if _usable_value(v)]
            if usable:
                vocab.setdefault(column, usable)
    extra = _unexplained_values(question, residual, entry, sem, found, vocab, lex)
    hard = [(c, v) for c, v in sem.literals if _mentions(_PLACEHOLDER.sub(" ", pattern), v) and not _mentions(question, v)]
    for column, value in hard:
        if extra:
            reasons.append(f"literal: the verified query hard-codes {column} = {value}; the question asks about "
                           f"{', '.join(extra)}")
        else:
            reasons.append(f"literal: the verified query hard-codes {column} = {value}, which the question does not ask for")
    if extra and not hard:
        reasons.append("filter: the verified query does not filter on " + ", ".join(extra))
    return reasons


# ------------------------------------------------------------------------------ matching
@dataclass
class Match:
    entry: VerifiedQuery
    values: dict[str, Any]
    missing: list[dict[str, Any]]
    score: float
    pattern: str


@dataclass
class Lookup:
    """The registry's answer to a question: the best compatible match (or None), and the entries whose
    words matched better but whose SQL does not answer the question, with the reasons."""

    hit: Match | None
    rejected: list[dict[str, Any]] = field(default_factory=list)


def find(session: Session, workspace_id: str, question: str, explicit: dict[str, Any] | None = None) -> Lookup:
    """Best active verified query that is compatible with the question. A token match whose SQL would
    answer a different question (another aggregate, a missing group-by or filter, a contradicted
    hard-coded value) is rejected and reported, never served."""
    entries = list(session.scalars(select(VerifiedQuery).where(VerifiedQuery.workspace_id == workspace_id,
                                                               VerifiedQuery.status == "active")))
    if not entries:
        return Lookup(None)
    return choose(entries, question, explicit, lexicon(session, workspace_id),
                  lambda: _vocabularies(session, workspace_id, entries))


def choose(entries: list[VerifiedQuery], question: str, explicit: dict[str, Any] | None = None, lex: Lexicon | None = None,
           vocabulary: Any = None) -> Lookup:
    """`find` without the database: rank the entries by token match, then serve the best compatible one.
    `vocabulary` is {table: {column: values}} or a callable returning it (loaded only when needed)."""
    candidates: list[tuple[tuple, Match, str]] = []
    for entry in entries:
        params = list(entry.parameters or [])
        found, residual = extract(question, params)
        q = tokens(residual, lex)
        if not q:
            continue
        best: tuple[tuple, str, float] | None = None
        for pattern in entry.patterns or []:
            p = tokens(pattern, lex)
            if not p:
                continue
            common = len(p & q)
            coverage, precision = common / len(p), common / len(q)
            if coverage < MIN_COVERAGE or precision < MIN_PRECISION:
                continue
            key = (round(coverage + precision, 6), entry.hits, entry.id)
            if best is None or key > best[0]:
                best = (key, pattern, coverage + precision)
        if best is None:
            continue
        values = {**found, **{k: v for k, v in (explicit or {}).items() if any(x["name"] == k for x in params)}}
        for x in params:
            if x["name"] not in values and x.get("default") is not None:
                values[x["name"]] = x["default"]
        missing = [x for x in params if x["name"] not in values]
        candidates.append((best[0], Match(entry, values, missing, best[2], best[1]), residual))
    if not candidates:
        return Lookup(None)
    candidates.sort(key=lambda c: c[0], reverse=True)
    if callable(vocabulary):
        vocabulary = vocabulary()
    rejected: list[dict[str, Any]] = []
    for _, m, residual in candidates:
        found = {k: v for k, v in m.values.items() if k not in (explicit or {})}
        reasons = incompatibilities(question, residual, m.entry, found, m.pattern, vocabulary, lex)
        if not reasons:
            return Lookup(m, rejected)
        rejected.append({"id": m.entry.id, "name": m.entry.name, "pattern": m.pattern, "score": round(m.score, 3),
                         "reasons": reasons})
    return Lookup(None, rejected)


def match(session: Session, workspace_id: str, question: str, explicit: dict[str, Any] | None = None) -> Match | None:
    """Best active compatible verified query for the question, or None (a miss: the model path may answer)."""
    return find(session, workspace_id, question, explicit).hit


def record_hit(session: Session, entry_id: str) -> None:
    from analystos.core.ids import utcnow

    row = session.get(VerifiedQuery, entry_id)
    if row is not None:
        row.hits = (row.hits or 0) + 1
        row.last_hit_at = utcnow()


# ------------------------------------------------------------------------------ promotion
def _vocabulary(session: Session, workspace_id: str, tables: list[str], column: str) -> tuple[str | None, list[str] | None]:
    """(asset.column, profiled top values) of the first referenced table with that column; PII columns get no vocabulary."""
    for table in tables:
        schema, _, name = table.rpartition(".")
        stmt = select(SourceColumn, SourceAsset).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id).where(
            SourceAsset.workspace_id == workspace_id, SourceAsset.name == name, SourceColumn.name == column)
        if schema:
            stmt = stmt.where(SourceAsset.schema_name == schema)
        row = session.execute(stmt.limit(1)).first()
        if row is None:
            continue
        col, asset = row
        ref = f"{asset.schema_name}.{asset.name}.{col.name}"
        if "pii" in (col.tags or []) or "restricted" in (col.tags or []):
            return ref, None
        return ref, [str(t.get("value")) for t in (col.profile or {}).get("top_values") or [] if t.get("value") is not None]
    return None, None


def _mentioned(question: str, value: str, kind: str) -> tuple[int, int] | None:
    if kind == "number":
        m = re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w.])", question)
        return (m.start(), m.end()) if m else None
    return _find_phrase(question, value)


def parameterise(session: Session, workspace_id: str, sql: str, dialect: str, question: str) -> tuple[str, list[dict], str]:
    """Template, typed parameters and question pattern from a successful statement.

    A literal becomes a parameter only when the question names it (the part the asker varies); every
    other literal stays fixed. Required: a later question must name a value too, or Ask declines."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise InvalidInput(f"cannot parse the query to promote: {exc}") from exc
    tables = [".".join(p for p in (t.db, t.name) if p) for t in tree.find_all(exp.Table)]
    params: list[dict[str, Any]] = []
    spans: list[tuple[int, int, str]] = []
    for lit in list(tree.find_all(exp.Literal)):
        parent = lit.parent
        if isinstance(parent, exp.In):
            column = parent.this
        elif isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            column = parent.left if parent.right is lit else parent.right
        else:
            continue
        if not isinstance(column, exp.Column):
            continue
        value = str(lit.this)
        kind = "number" if not lit.is_string else "date" if _ISO_DATE.fullmatch(value) else "string"
        span = _mentioned(question, value, kind)
        if span is None:
            continue
        ref, vocab = (None, None) if kind != "string" else _vocabulary(session, workspace_id, tables, column.name)
        if kind == "string" and vocab and any(v.lower() == value.lower() for v in vocab):
            kind = "enum"
        base = re.sub(r"[^a-z0-9_]+", "_", column.name.lower()).strip("_") or "value"
        name = base if all(p["name"] != base for p in params) else f"{base}_{len(params) + 1}"
        param: dict[str, Any] = {"name": name, "type": kind, "required": True, "column": ref or column.name}
        if kind == "enum":
            param["values"] = vocab
        params.append(param)
        spans.append((*span, name))
        lit.replace(exp.Literal.string(f"__aos_param_{name}__"))
    order = {name: start for start, _, name in spans}
    params.sort(key=lambda p: order[p["name"]])  # in the order the question names them
    template = tree.sql(dialect=dialect)
    for p in params:
        template = template.replace(exp.Literal.string(f"__aos_param_{p['name']}__").sql(dialect=dialect), "{{" + p["name"] + "}}")
    pattern = question
    for start, end, name in sorted(spans, reverse=True):
        pattern = pattern[:start] + "{" + name + "}" + pattern[end:]
    return template, params, pattern


def source_of(session: Session, workspace_id: str, *, query_id: str | None = None,
              insight_id: str | None = None) -> dict[str, Any]:
    """The successful statement a verified query is promoted from: an Ask/console answer or a verified finding."""
    if bool(query_id) == bool(insight_id):
        raise InvalidInput("promote exactly one of query_id (an Ask answer) or insight_id (a verified finding)")
    if query_id:
        q = session.get(QueryExecution, query_id)
        if q is None or q.workspace_id != workspace_id:
            raise NotFound("query not found in this workspace")
        if q.status != "ok" or q.purpose not in PROMOTABLE_PURPOSES:
            raise InvalidInput("only a successful Ask or console statement can be promoted")
        return {"sql": q.sql, "source_id": q.source_id, "origin": {"type": "ask", "query_id": q.id}, "question": None, "spec": None}
    ins = session.get(Insight, insight_id)
    if ins is None or ins.workspace_id != workspace_id:
        raise NotFound("finding not found in this workspace")
    if ins.status != "verified":
        raise InvalidInput("only a verified finding can be promoted")
    qids = [e["id"] for e in ins.evidence or [] if e.get("type") == "query"]
    q = session.get(QueryExecution, qids[0]) if qids else None
    if q is None or q.status != "ok":
        raise InvalidInput("the finding has no successful evidence query to promote")
    hyp = session.get(Hypothesis, ins.hypothesis_id) if ins.hypothesis_id else None
    return {"sql": q.sql, "source_id": q.source_id, "question": (hyp.question if hyp and hyp.question else ins.title),
            "origin": {"type": "finding", "insight_id": ins.id, "insight_code": ins.code, "run_id": ins.run_id, "query_id": q.id},
            "spec": dict(hyp.spec) if hyp else None, "patterns": [ins.title]}


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:60] or "verified_query"


def promote(session: Session, user: Any, workspace_id: str, *, question: str | None = None, name: str | None = None,
            query_id: str | None = None, insight_id: str | None = None, patterns: list[str] | None = None,
            description: str = "") -> VerifiedQuery:
    """Register a verified query from a successful Ask answer or a verified finding. The rendered
    template must pass the gateway validator for the promoting user before it is stored."""
    from analystos.artifacts.registry import link
    from analystos.core.ids import new_id
    from analystos.events.bus import emit
    from analystos.gateway.validator import validate_sql
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role, resolve_scope

    require_role(session, user, workspace_id, "analyst")
    src = source_of(session, workspace_id, query_id=query_id, insight_id=insight_id)
    question = (question or src["question"] or "").strip()
    if not question:
        raise InvalidInput("question is required: the phrasing this verified query answers")
    scope = resolve_scope(session, user, workspace_id)
    dialect = scope.source_dialects.get(src["source_id"] or "", next(iter(scope.source_dialects.values()), "postgres"))
    template, params, pattern = parameterise(session, workspace_id, src["sql"], dialect, question)
    original, _ = extract(question, params)
    validate_sql(scope, render(template, params, original, dialect), max_rows=scope.max_rows)
    extra = [p.strip() for p in [*(patterns or []), *src.get("patterns", [])] if isinstance(p, str) and p.strip()]
    all_patterns = list(dict.fromkeys([pattern, *extra]))
    if not any(tokens(p) for p in all_patterns):
        raise InvalidInput("the question has no words to match on once its parameter values are removed")
    base = _slug(name or question)
    taken = set(session.scalars(select(VerifiedQuery.name).where(VerifiedQuery.workspace_id == workspace_id,
                                                                 VerifiedQuery.name.like(f"{base}%"))))
    final = base if base not in taken else next(f"{base}_{i}" for i in range(2, 10_000) if f"{base}_{i}" not in taken)
    vq = VerifiedQuery(id=new_id("vq"), workspace_id=workspace_id, name=final, description=description or question,
                       patterns=all_patterns, sql_template=template, parameters=params, source_id=src["source_id"],
                       dialect=dialect, origin={**src["origin"], "question": question}, spec=src["spec"], status="active",
                       hits=0, created_by=user.id)
    session.add(vq)
    session.flush()
    origin = src["origin"]
    link(session, workspace_id, ("verified_query", vq.id), "promoted_from",
         ("insight", origin["insight_id"]) if origin["type"] == "finding" else ("query", origin["query_id"]))
    emit(workspace_id, "verified_query.promoted", {"id": vq.id, "name": vq.name, "origin": origin["type"],
                                                   "parameters": [p["name"] for p in params]}, session=session)
    audit(f"user:{user.id}", "verified_query.promoted", workspace_id=workspace_id, target=vq.id,
          details={"origin": origin, "parameters": [p["name"] for p in params]}, session=session)
    _lexicons.pop(workspace_id, None)
    return vq


def update(session: Session, user: Any, entry_id: str, patch: dict[str, Any]) -> VerifiedQuery:
    """Rename, rephrase, retire/reactivate, or relax a parameter (required, default). The template
    itself is immutable: a different statement is a new promotion."""
    from analystos.events.bus import emit
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role

    vq = session.get(VerifiedQuery, entry_id)
    if vq is None:
        raise NotFound("verified query not found")
    require_role(session, user, vq.workspace_id, "analyst")
    if patch.get("status") is not None:
        if patch["status"] not in ("active", "retired"):
            raise InvalidInput("status must be active or retired")
        vq.status = patch["status"]
    if patch.get("patterns") is not None:
        pats = [p.strip() for p in patch["patterns"] if isinstance(p, str) and p.strip()]
        if not pats or not any(tokens(p) for p in pats):
            raise InvalidInput("patterns must contain at least one phrasing with words to match on")
        vq.patterns = list(dict.fromkeys(pats))
    if patch.get("name") is not None:
        vq.name = str(patch["name"])[:200]
    if patch.get("description") is not None:
        vq.description = str(patch["description"])
    if patch.get("parameters") is not None:
        by_name = {p["name"]: dict(p) for p in vq.parameters or []}
        for change in patch["parameters"]:
            p = by_name.get(change.get("name"))
            if p is None:
                raise InvalidInput(f"unknown parameter {change.get('name')}")
            if "required" in change:
                p["required"] = bool(change["required"])
            if "default" in change:
                p["default"] = None if change["default"] is None else coerce(p, change["default"])
            if not p.get("required", True) and p.get("default") is None:
                raise InvalidInput(f"parameter {p['name']} needs a default to be optional")
        vq.parameters = list(by_name.values())
    emit(vq.workspace_id, "verified_query.updated", {"id": vq.id, "status": vq.status}, session=session)
    audit(f"user:{user.id}", "verified_query.updated", workspace_id=vq.workspace_id, target=vq.id, details=patch, session=session)
    return vq

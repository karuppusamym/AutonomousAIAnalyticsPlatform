"""Relationship (join key) discovery across a given set of assets, validated with SQL.

Candidate sources, in order of prior confidence:
  declared        a column's `references` ("table", "schema.table" or "table.column"; a bare table
                  resolves to the installed domain packs' key column, then the declared key, then `id`)        prior 0.9
  name_heuristic  `x_id` -> table `x` / `xs` / `xes` / `x`->`ies`, column `id` (or `x_id`)  prior 0.6
  same_name_key   a column named like another asset's declared key column                   prior 0.5

Every candidate is validated with one containment query (share of non-null FK values found among the
target's values) plus one uniqueness query per target column (cached). Cardinality, read from the
from-side to the to-side: many_to_one when the target column is unique, one_to_one when both sides
are unique, one_to_many when only the from-side is (the direction is reversed), else many_to_many. confidence = (0.35 * prior + 0.65 * containment) x (1.0 if target unique else 0.8).
Heuristic candidates with containment < 0.5 are dropped; declared ones are always returned (with
their measured containment) because a broken declared reference is itself a finding.
Only assets from the given list are considered as targets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from pydantic import BaseModel, Field
from sqlglot import exp

from analystos.skills.base import RunSQL
from analystos.skills.profiling import type_family
from analystos.skills.sqlbuild import (
    _check_dialect,
    and_all,
    case,
    cast,
    col,
    count_star,
    ident,
    is_null,
    not_null,
    num,
    table,
    to_sql,
)

PRIORS = {"declared": 0.9, "name_heuristic": 0.6, "same_name_key": 0.5, "composite_key": 0.6}
MIN_CONTAINMENT = 0.5


class RelationshipCandidate(BaseModel):
    from_asset: str
    from_column: str  # the first column of a composite key
    to_asset: str
    to_column: str
    cardinality: str  # many_to_one | one_to_one | one_to_many | many_to_many (measured, never from a model)
    confidence: float
    evidence: dict[str, Any] = Field(default_factory=dict)
    from_columns: list[str] = Field(default_factory=list)  # every column, in key order (composite keys)
    to_columns: list[str] = Field(default_factory=list)
    assessment: dict[str, Any] = Field(default_factory=dict)  # assess_relationship (Atlas rules) on measured facts

    def model_post_init(self, _context: Any) -> None:
        if not self.from_columns:
            self.from_columns = [self.from_column]
        if not self.to_columns:
            self.to_columns = [self.to_column]


def _cardinality(from_unique: bool, to_unique: bool) -> str:
    if to_unique:
        return "one_to_one" if from_unique else "many_to_one"
    return "one_to_many" if from_unique else "many_to_many"


def _short(asset: str) -> str:
    return asset.split(".")[-1].lower()


def _plural_forms(stem: str) -> list[str]:
    s = stem.lower()
    forms = [s, s + "s", s + "es"]
    if s.endswith("y"):
        forms.append(s[:-1] + "ies")
    return forms


BASE_PREFIXES = ("dim_", "fact_", "fct_", "stg_", "raw_", "src_")


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        return w[:-1]
    return w


def table_base(name: str) -> str:
    """The entity a table holds: modelling prefixes (dim_, fact_, stg_, raw_, src_) stripped, the last word
    singularised: `dim_customers` -> `customer`, `stg_order_categories` -> `order_category`."""
    s = name.split(".")[-1].lower()
    changed = True
    while changed:
        changed = False
        for p in BASE_PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                s, changed = s[len(p):], True
    head, _, last = s.rpartition("_")
    return f"{head}_{_singular(last)}" if head else _singular(last)


_REFERENCE_NAME = re.compile(r"^(?P<stem>[a-z0-9]+(?:_[a-z0-9]+)*?)(?:_id|(?<=[a-z]{3})id|_no|_num|_nbr|_number|_key)$")
_ID_WORDS = frozenset({"valid", "invalid", "rapid", "liquid", "fluid", "solid", "humid", "acid", "avoid", "void", "hybrid",
                       "android", "pyramid", "vivid", "lipid", "squid", "candid", "stupid", "timid", "splendid", "grid"})
_KEY_LIKE = re.compile(r"(?:_id|(?<=[a-z]{3})id|_no|_num|_nbr|_number|_key|_code)$")


def _candidates(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from analystos.capabilities.packs import hints

    key_columns = [k.lower() for k in hints().key_columns]
    by_full = {a["asset"].lower(): a for a in assets}
    by_short: dict[str, list[dict[str, Any]]] = {}
    by_base: dict[str, list[dict[str, Any]]] = {}
    for a in assets:
        by_short.setdefault(_short(a["asset"]), []).append(a)
        by_base.setdefault(table_base(a["asset"]), []).append(a)

    def cols(a: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {c["name"].lower(): c for c in a.get("columns", [])}

    def resolve(ref: str) -> tuple[dict[str, Any] | None, str | None]:
        r = ref.strip().lower()
        if r in by_full:
            return by_full[r], None
        parts = r.split(".")
        if len(parts) >= 2:
            head, last = ".".join(parts[:-1]), parts[-1]
            if head in by_full and last in cols(by_full[head]):
                return by_full[head], last
            if head in by_short and len(by_short[head]) == 1 and last in cols(by_short[head][0]):
                return by_short[head][0], last
            # "origin_schema.table.column": a staged copy lives in another schema; the table name still decides
            short = parts[-2]
            if short in by_short and len(by_short[short]) == 1 and last in cols(by_short[short][0]):
                return by_short[short][0], last
        if parts[-1] in by_short and len(by_short[parts[-1]]) == 1:
            return by_short[parts[-1]][0], None
        return None, None

    def target_key(t: dict[str, Any]) -> str | None:
        tc = cols(t)
        for key in key_columns:  # a domain's surrogate key (installed packs' hints) wins over declared keys
            if key in tc:
                return tc[key]["name"]
        keys = [c["name"] for c in t.get("columns", []) if c.get("is_key")]
        if keys:
            return keys[0]
        if "id" in tc:
            return tc["id"]["name"]
        return None

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(a: dict[str, Any], c: dict[str, Any], t: dict[str, Any], tcol: str, source: str) -> None:
        key = (a["asset"], c["name"], t["asset"], tcol)
        if key in seen or (a["asset"] == t["asset"] and c["name"] == tcol):
            return
        seen.add(key)
        tc = cols(t)[tcol.lower()]
        out.append({"from": a, "col": c, "to": t, "to_col": tc, "source": source})

    for a in assets:
        for c in a.get("columns", []):
            ref = c.get("references")
            if ref:
                t, tcol = resolve(str(ref))
                if t is not None:
                    tcol = tcol or target_key(t)
                    if tcol:
                        add(a, c, t, cols(t)[tcol.lower()]["name"], "declared")
                continue
            if c.get("is_key"):
                continue
            name = c["name"].lower()
            ref = _REFERENCE_NAME.match(name) if name not in _ID_WORDS else None
            if ref and table_base(ref.group("stem")) == table_base(a["asset"]):
                continue  # customers.customerid is that table's own key, not a reference to another table
            if ref:  # customer_id, customerid (a header "CustomerID"), customer_no, customer_number, customer_key
                stem = ref.group("stem")
                targets = [t for form in _plural_forms(stem) for t in by_short.get(form, [])]
                # table-base match: customer_id -> dim_customers, stg_customer (prefix stripped, singular)
                targets += [t for t in by_base.get(table_base(stem), []) if t not in targets]
                for t in targets:
                    tc = cols(t)
                    tcol = "id" if "id" in tc else name if name in tc else target_key(t)
                    if tcol:
                        add(a, c, t, tc[tcol.lower()]["name"] if tcol.lower() in tc else tcol, "name_heuristic")
            for t in assets:
                if t is a:
                    continue
                tc = cols(t)
                # the same key-like name on both sides, on the table whose name owns it (orders.cust_no ->
                # customers.cust_no): files declare no keys, so the name proposes and the measurement decides
                owner = bool(ref) and table_base(t["asset"]).startswith(ref.group("stem"))  # customers owns cust_no
                if name in tc and (tc[name].get("is_key") or (owner and _KEY_LIKE.search(name) and name not in _ID_WORDS)):
                    add(a, c, t, tc[name]["name"], "same_name_key")
    return out


def measure_candidate(run_sql: RunSQL, dialect: str, a: dict[str, Any], c: dict[str, Any], t: dict[str, Any],
                      tc: dict[str, Any], source: str, uniq_cache: dict[tuple[str, str], dict[str, Any]]
                      ) -> RelationshipCandidate | None:
    """Containment of ``a.c`` in ``t.tc`` plus target uniqueness, measured through ``run_sql`` (the
    gateway). None when the from-column has no non-null value. Used for rule candidates here and for
    model/user join-key proposals in ``skills/federation.py`` (models propose, this decides)."""
    fam_f, fam_t = type_family(c.get("data_type", "")), type_family(tc.get("data_type", ""))
    mismatch = fam_f != fam_t

    def key_expr(e: exp.Expression, _m: bool = mismatch) -> exp.Expression:
        return cast(e, "text", dialect) if _m else e

    ukey = (t["asset"], tc["name"])
    if ukey not in uniq_cache:
        uq = exp.select(exp.Count(this=col(tc["name"])).as_(ident("n")),
                        exp.Count(this=exp.Distinct(expressions=[col(tc["name"])])).as_(ident("nd"))).from_(table(t["asset"]))
        sql_u = to_sql(uq, dialect)
        r = run_sql(sql_u, purpose="relationships.target_uniqueness", max_rows=1).records()
        row = {k.lower(): v for k, v in r[0].items()} if r else {}
        uniq_cache[ukey] = {"rows": int(row.get("n") or 0), "distinct": int(row.get("nd") or 0), "sql": sql_u}
    u = uniq_cache[ukey]
    parent = (exp.select(key_expr(col(tc["name"])).as_(ident("k"))).distinct().from_(table(t["asset"]))
              .where(not_null(col(tc["name"]))))
    f = exp.column(c["name"], table="f", quoted=True)
    pk = exp.column("k", table="p", quoted=True)
    q = (exp.select(count_star().as_(ident("fk_rows")),
                    exp.Count(this=exp.Distinct(expressions=[f.copy()])).as_(ident("fk_distinct")),
                    exp.Sum(this=case([(is_null(pk.copy()), num(0))], num(1))).as_(ident("matched")))
         .from_(table(a["asset"], alias="f"))
         .join(exp.Subquery(this=parent, alias=exp.TableAlias(this=ident("p"))),
               on=exp.EQ(this=key_expr(f.copy()), expression=pk.copy()), join_type="left")
         .where(not_null(f.copy())))
    sql = to_sql(q, dialect)
    rr = run_sql(sql, purpose="relationships.containment", max_rows=1).records()
    row = {k.lower(): v for k, v in rr[0].items()} if rr else {}
    fk_rows, fk_distinct, matched = int(row.get("fk_rows") or 0), int(row.get("fk_distinct") or 0), int(row.get("matched") or 0)
    if fk_rows == 0:
        return None
    containment = matched / fk_rows
    pk_unique = u["rows"] > 0 and u["rows"] == u["distinct"]
    fk_unique = fk_distinct == fk_rows
    cardinality = _cardinality(fk_unique, pk_unique)
    prior = PRIORS.get(source, 0.5)
    conf = (0.35 * prior + 0.65 * containment) * (1.0 if pk_unique else 0.8)
    return RelationshipCandidate(
        from_asset=a["asset"], from_column=c["name"], to_asset=t["asset"], to_column=tc["name"], cardinality=cardinality,
        confidence=round(conf, 3),
        evidence={"source": source, "containment": round(containment, 6), "fk_rows": fk_rows,
                  "fk_distinct": fk_distinct, "matched_rows": matched, "orphan_rows": fk_rows - matched,
                  "target_unique": pk_unique, "target_rows": u["rows"], "target_distinct": u["distinct"],
                  "type_cast": mismatch, "sql": [sql, u["sql"]]})


def discover_relationships(run_sql: RunSQL, assets: list[dict[str, Any]], *,
                           observed: dict[JoinKey, int] | None = None) -> list[RelationshipCandidate]:
    """assets: [{"asset": "schema.table", "columns": [{name, data_type, is_key, references}], "row_count"}];
    `observed`: join counts from governed query history (corroborating evidence for the assessment)."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    uniq_cache: dict[tuple[str, str], dict[str, Any]] = {}
    results: list[RelationshipCandidate] = []
    for cand in _candidates(assets):
        found = measure_candidate(run_sql, dialect, cand["from"], cand["col"], cand["to"], cand["to_col"],
                                  cand["source"], uniq_cache)
        if found is None:
            continue
        if cand["source"] != "declared" and found.evidence["containment"] < MIN_CONTAINMENT:
            continue
        results.append(with_assessment(found, assets, observed=observed))
    results.sort(key=lambda r: (-r.confidence, r.from_asset, r.from_column))
    return results


MAX_POLYMORPHIC_COLUMNS = 8
MAX_POLYMORPHIC_TARGETS = 10
MIN_POLYMORPHIC_SHARE = 0.02  # a table must hold at least this share of the column's values to count as a target
MIN_POLYMORPHIC_COVERAGE = 0.95


class PolymorphicReference(BaseModel):
    """One id column whose values live in several tables' keys (a document reference that is an order, a return or an
    invoice): no single join is right, so it is reported as evidence, never queued as a relationship."""
    from_asset: str
    from_column: str
    targets: list[dict[str, Any]]  # [{asset, column, rows, share}] largest first
    coverage: float  # share of the column's non-null rows found in one of the targets
    rows: int
    sql: list[str] = Field(default_factory=list)


def discover_polymorphic_references(run_sql: RunSQL, assets: list[dict[str, Any]], *,
                                    exclude: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset()
                                    ) -> list[PolymorphicReference]:
    """Id-named text columns that no single table explains (`exclude`: the columns a normal candidate already covers)
    but that the keys of two or more tables together do. Measured through `run_sql` (the gateway): containment of the
    column in each other table's surrogate key, bounded per column and overall. Text keys only (numeric ids overlap
    between tables by chance) and the targets must be unique and disjoint: their matched rows may not add up to more
    than the column's rows, which would mean the same value sits in several tables."""
    from analystos.capabilities.packs import hints

    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    key_names = [k.lower() for k in hints().key_columns]

    def key_of(a: dict[str, Any]) -> dict[str, Any] | None:
        cols = a.get("columns", [])
        by = {c["name"].lower(): c for c in cols}
        pick = next((by[k] for k in key_names if k in by), None) or next((c for c in cols if c.get("is_key")), None)
        return pick if pick is not None and type_family(pick.get("data_type", "")) == "text" else None

    targets = [(a, k) for a in assets if (k := key_of(a)) is not None][:MAX_POLYMORPHIC_TARGETS]
    if len(targets) < 2:
        return []
    uniq_cache: dict[tuple[str, str], dict[str, Any]] = {}
    out: list[PolymorphicReference] = []
    columns = [(a, c) for a in assets for c in a.get("columns", [])
               if (a["asset"], c["name"]) not in exclude and not c.get("is_key") and not c.get("references")
               and c["name"].lower().endswith(("_id", "_key")) and type_family(c.get("data_type", "")) == "text"]
    for a, c in columns[:MAX_POLYMORPHIC_COLUMNS]:
        measured = []
        for t, tk in targets:
            if t["asset"] == a["asset"]:
                continue
            m = measure_candidate(run_sql, dialect, a, c, t, tk, "polymorphic", uniq_cache)
            if m is None:
                break
            measured.append(m)
        if not measured:
            continue
        rows = measured[0].evidence["fk_rows"]
        hits = [m for m in measured if m.evidence["matched_rows"] / rows >= MIN_POLYMORPHIC_SHARE and m.evidence["target_unique"]]
        matched = sum(m.evidence["matched_rows"] for m in hits)
        if len(hits) < 2 or matched > rows or matched / rows < MIN_POLYMORPHIC_COVERAGE:
            continue
        hits.sort(key=lambda m: -m.evidence["matched_rows"])
        out.append(PolymorphicReference(
            from_asset=a["asset"], from_column=c["name"], rows=rows, coverage=round(matched / rows, 4),
            targets=[{"asset": m.to_asset, "column": m.to_column, "rows": m.evidence["matched_rows"],
                      "share": round(m.evidence["matched_rows"] / rows, 4)} for m in hits],
            sql=[q for m in hits[:1] for q in m.evidence.get("sql", [])[:1]]))
    return out


# =====================================================================================================
# P7-09: composite keys and composite foreign keys, measured through the gateway.
#
# Search bounds ported from AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:
# src/aida/composite_key_inference.py (Atlas PR-1). Atlas *guesses* joint uniqueness from independent
# single-column statistics because it cannot query its sources; here each candidate is measured: the
# distinct (a, b[, c]) tuples of non-null rows against the table's row count, the portable spelling of
# `COUNT(DISTINCT (a, b))` (`SELECT COUNT(*) FROM (SELECT DISTINCT a, b ...) k`). Atlas's member floor
# (`MIN_MEMBER_DISTINCT_RATIO`) is replaced by an exact bound that measurement makes available: a
# combination whose members' distinct counts multiply to fewer than the row count cannot be unique and
# is never queried. Only minimal keys are returned (a superset of a found key is not a new key).
# =====================================================================================================
MAX_KEY_SIZE = 3
MAX_CANDIDATE_MEMBERS = 8
MAX_COMBINATIONS_EVALUATED = 200
MAX_CANDIDATES_RETURNED = 20
MAX_MEMBER_NULL_RATE = 0.01
PER_EXTRA_MEMBER_DISCOUNT = 0.97
MAX_KEY_QUERIES = 40  # AnalystOS: statements one table's key search may send through the gateway
KEY_RULE = "composite_key_measured_distinct_v1"


class ColumnKeyStats(BaseModel):
    name: str
    rows: int
    non_null: int
    distinct: int

    @property
    def null_rate(self) -> float:
        return 1.0 if self.rows <= 0 else (self.rows - self.non_null) / self.rows

    @property
    def distinct_ratio(self) -> float:
        return 0.0 if self.rows <= 0 else min(self.distinct / self.rows, 1.0)


class KeyCandidate(BaseModel):
    asset: str
    columns: list[str]
    rows: int
    distinct_tuples: int
    confidence: float  # measured unique; discounted per extra member (parsimony, as Atlas ranks)
    detection_rule: str = KEY_RULE
    evidence: dict[str, Any] = Field(default_factory=dict)


def key_member_stats(run_sql: RunSQL, dialect: str, asset: str, columns: list[str]) -> list[ColumnKeyStats]:
    """One statement: the row count and, per column, its non-null and distinct counts."""
    select = [count_star().as_(ident("n"))]
    for i, c in enumerate(columns):
        select += [exp.Count(this=col(c)).as_(ident(f"nn{i}")),
                   exp.Count(this=exp.Distinct(expressions=[col(c)])).as_(ident(f"nd{i}"))]
    rows = run_sql(to_sql(exp.select(*select).from_(table(asset)), dialect), purpose="relationships.key_stats",
                   max_rows=1).records()
    row = {k.lower(): v for k, v in rows[0].items()} if rows else {}
    n = int(row.get("n") or 0)
    return [ColumnKeyStats(name=c, rows=n, non_null=int(row.get(f"nn{i}") or 0), distinct=int(row.get(f"nd{i}") or 0))
            for i, c in enumerate(columns)]


def key_member_pool(stats: list[ColumnKeyStats], declared: set[str] | frozenset[str] = frozenset()) -> list[ColumnKeyStats]:
    """Atlas's eligibility and ranking: declared key columns out, members with more than 1% nulls out,
    the rest ranked by distinct ratio, the top MAX_CANDIDATE_MEMBERS kept."""
    lowered = {d.lower() for d in declared}
    eligible = [s for s in stats if s.name.lower() not in lowered and s.rows > 0 and s.null_rate <= MAX_MEMBER_NULL_RATE]
    eligible.sort(key=lambda s: (-s.distinct_ratio, s.name))
    return eligible[:MAX_CANDIDATE_MEMBERS]


def key_combinations(pool: list[ColumnKeyStats], rows: int) -> list[tuple[ColumnKeyStats, ...]]:
    """Combinations worth measuring, narrow first, at most MAX_COMBINATIONS_EVALUATED considered: one whose
    members' distinct counts multiply to fewer than `rows` cannot be unique and is skipped."""
    out, considered = [], 0
    for size in range(1, min(MAX_KEY_SIZE, len(pool)) + 1):
        for combo in combinations(pool, size):
            if considered >= MAX_COMBINATIONS_EVALUATED:
                return out
            considered += 1
            product = 1
            for member in combo:
                product *= max(member.distinct, 0)
            if product >= rows:
                out.append(combo)
    return out


def measure_distinct_tuples(run_sql: RunSQL, dialect: str, asset: str, columns: list[str]) -> tuple[int, str]:
    """Distinct tuples of `columns` over rows where none is null: COUNT(DISTINCT (a, b)), portably."""
    inner = exp.select(*[col(c) for c in columns]).distinct().from_(table(asset)).where(
        and_all([not_null(col(c)) for c in columns]))
    sql = to_sql(exp.select(count_star().as_(ident("n"))).from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=ident("k")))),
                 dialect)
    rows = run_sql(sql, purpose="relationships.key_uniqueness", max_rows=1).records()
    return (int(next(iter(rows[0].values())) or 0) if rows else 0), sql


def discover_keys(run_sql: RunSQL, asset: dict[str, Any], *, max_queries: int = MAX_KEY_QUERIES) -> list[KeyCandidate]:
    """Minimal unique column sets of one asset (size 1..MAX_KEY_SIZE), measured. `asset` is the discovery
    shape ({"asset", "columns": [{name, is_key}]}); declared key columns are not re-proposed (Atlas rule)."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    names = [c["name"] for c in asset.get("columns", [])]
    if not names:
        return []
    stats = key_member_stats(run_sql, dialect, asset["asset"], names)
    rows = stats[0].rows if stats else 0
    if rows <= 0:
        return []
    declared = {c["name"] for c in asset.get("columns", []) if c.get("is_key")}
    found: list[KeyCandidate] = []
    queries = 1
    for combo in key_combinations(key_member_pool(stats, declared), rows):
        if len(found) >= MAX_CANDIDATES_RETURNED:
            break
        members = [m.name for m in combo]
        if any(set(k.columns) <= set(members) for k in found):
            continue  # a superset of a key is not a new (minimal) key
        if len(combo) == 1:
            distinct, sql = (combo[0].distinct if combo[0].non_null == rows else -1), None  # measured by the stats statement
        else:
            if queries >= max_queries:
                break
            distinct, sql = measure_distinct_tuples(run_sql, dialect, asset["asset"], members)
            queries += 1
        if distinct != rows:
            continue
        found.append(KeyCandidate(asset=asset["asset"], columns=members, rows=rows, distinct_tuples=distinct,
                                  confidence=round(PER_EXTRA_MEMBER_DISCOUNT ** (len(members) - 1), 4),
                                  evidence={"algorithm": KEY_RULE, "rows": rows, "distinct_tuples": distinct, "sql": sql,
                                            "columns": [m.model_dump() | {"distinct_ratio": round(m.distinct_ratio, 6)}
                                                        for m in combo], "queries": queries}))
    found.sort(key=lambda k: (-k.confidence, k.columns))
    return found[:MAX_CANDIDATES_RETURNED]


_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-\s]+")


def canonical_name(name: str) -> str:
    """`CustomerID`, `customerId`, `CUSTOMER_ID` -> `customer_id` (Atlas relationship_naming rule)."""
    return "_".join(p.lower() for p in _WORD_BOUNDARY.split(name) if p)


def composite_fk_candidates(assets: list[dict[str, Any]], keys: dict[str, list[list[str]]]) -> list[dict[str, Any]]:
    """(from asset, from columns, to asset, key columns) for every multi-column key of a target whose
    members all have a same-named (canonical) column in another asset. `keys`: asset -> its keys
    (declared composite keys and measured `KeyCandidate`s)."""
    out = []
    for t in assets:
        for key in keys.get(t["asset"], []):
            if len(key) < 2:
                continue
            for a in assets:
                if a is t:
                    continue
                by_name = {canonical_name(c["name"]): c["name"] for c in a.get("columns", [])}
                cols = [by_name.get(canonical_name(k)) for k in key]
                if all(cols):
                    out.append({"from": a["asset"], "from_columns": cols, "to": t["asset"], "to_columns": list(key),
                                "source": "composite_key"})
    return out


def measure_columns(run_sql: RunSQL, dialect: str, from_asset: str, from_columns: list[str], to_asset: str,
                    to_columns: list[str], source: str) -> RelationshipCandidate | None:
    """Multi-column containment and both sides' uniqueness, measured: tuples of `from_columns` found among
    the DISTINCT tuples of `to_columns`, and each side's distinct tuples against its non-null rows."""
    keys = [f"k{i}" for i in range(len(to_columns))]
    parent = (exp.select(*[col(c).as_(ident(k)) for c, k in zip(to_columns, keys, strict=True)]).distinct()
              .from_(table(to_asset)).where(and_all([not_null(col(c)) for c in to_columns])))
    fcols = [exp.column(c, table="f", quoted=True) for c in from_columns]
    on = and_all([exp.EQ(this=f.copy(), expression=exp.column(k, table="p", quoted=True)) for f, k in zip(fcols, keys, strict=True)])
    q = (exp.select(count_star().as_(ident("fk_rows")),
                    exp.Sum(this=case([(is_null(exp.column(keys[0], table="p", quoted=True)), num(0))], num(1))).as_(ident("matched")))
         .from_(table(from_asset, alias="f"))
         .join(exp.Subquery(this=parent, alias=exp.TableAlias(this=ident("p"))), on=on, join_type="left")
         .where(and_all([not_null(f.copy()) for f in fcols])))
    sql = to_sql(q, dialect)
    rr = run_sql(sql, purpose="relationships.containment", max_rows=1).records()
    row = {k.lower(): v for k, v in rr[0].items()} if rr else {}
    fk_rows, matched = int(row.get("fk_rows") or 0), int(row.get("matched") or 0)
    if fk_rows == 0:
        return None
    fk_distinct, fk_sql = measure_distinct_tuples(run_sql, dialect, from_asset, from_columns)
    to_distinct, to_distinct_sql = measure_distinct_tuples(run_sql, dialect, to_asset, to_columns)
    rows_sql = to_sql(exp.select(count_star().as_(ident("n"))).from_(table(to_asset))
                      .where(and_all([not_null(col(c)) for c in to_columns])), dialect)
    tr = run_sql(rows_sql, purpose="relationships.target_rows", max_rows=1).records()
    to_rows = int(next(iter(tr[0].values())) or 0) if tr else 0
    pk_unique, fk_unique = to_rows > 0 and to_rows == to_distinct, fk_distinct == fk_rows
    containment = matched / fk_rows
    conf = (0.35 * PRIORS.get(source, 0.5) + 0.65 * containment) * (1.0 if pk_unique else 0.8)
    return RelationshipCandidate(
        from_asset=from_asset, from_column=from_columns[0], to_asset=to_asset, to_column=to_columns[0],
        from_columns=list(from_columns), to_columns=list(to_columns), cardinality=_cardinality(fk_unique, pk_unique),
        confidence=round(conf, 3),
        evidence={"source": source, "containment": round(containment, 6), "fk_rows": fk_rows, "fk_distinct": fk_distinct,
                  "matched_rows": matched, "orphan_rows": fk_rows - matched, "target_unique": pk_unique,
                  "source_unique": fk_unique, "target_rows": to_rows, "target_distinct": to_distinct,
                  "type_cast": False, "sql": [sql, fk_sql, to_distinct_sql, rows_sql]})


def discover_composite_relationships(run_sql: RunSQL, assets: list[dict[str, Any]], *,
                                     keys: dict[str, list[list[str]]] | None = None,
                                     observed: dict[JoinKey, int] | None = None) -> list[RelationshipCandidate]:
    """Composite foreign keys between the given assets: target keys are measured (`discover_keys`) unless
    given, candidates come from same-named columns, each is measured; low containment is dropped."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    if keys is None:
        keys = {a["asset"]: [k.columns for k in discover_keys(run_sql, a)] for a in assets}
    out = []
    for cand in composite_fk_candidates(assets, keys):
        found = measure_columns(run_sql, dialect, cand["from"], cand["from_columns"], cand["to"], cand["to_columns"],
                                cand["source"])
        if found is not None and found.evidence["containment"] >= MIN_CONTAINMENT:
            out.append(with_assessment(found, assets, observed=observed))
    out.sort(key=lambda r: (-r.confidence, r.from_asset, r.from_columns))
    return out


# =====================================================================================================
# P7-09: Atlas's relationship validation rules, on measured facts.
#
# Ported from AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/relationship_validation.py
# (`assess_relationship`, R11-FP06), its tests are the spec (tests/unit/test_relationship_assessment.py).
# Kept: what corroborates a join (a declared foreign key either way, joins observed in query history, a key
# on exactly one side for a name more specific than a bare identifier), what does not (name and type
# matches; two keys sharing a name; a key on a generic name such as id/key/code), the reversed direction
# and the fan-out warning. Changed: Atlas reads uniqueness from sampled profiles and can never check
# inclusion; here uniqueness and containment are measured through the gateway (`MEASURED_UNIQUE` is never
# sample-bounded, `MEASURED_CONTAINMENT` is new) and cardinality uses AnalystOS's vocabulary. Nothing here
# sets a relationship's cardinality from model output: the result is evidence for the review queue.
# =====================================================================================================
CORROBORATED = "corroborated"
NAME_MATCH_ONLY = "name_match_only"
GENERIC_COLUMN_NAMES = frozenset({"id", "key", "pk", "uuid", "guid", "code", "name", "value"})
MIN_CORROBORATING_CONTAINMENT = 0.99
_BASIS = {"DECLARED_KEY": "a declared primary or unique key", "UNIQUE_INDEX": "a unique index",
          "APPROVED_KEY": "an accepted key candidate", "MEASURED": "a measurement showing every non-null value distinct"}
_BASIS_CLASS = {"DECLARED_KEY": "DECLARED_KEY", "UNIQUE_INDEX": "UNIQUE_INDEX", "APPROVED_KEY": "APPROVED_KEY",
                "MEASURED": "MEASURED_UNIQUE"}


@dataclass(frozen=True, slots=True)
class ColumnFacts:
    name: str
    physical_type: str = ""
    nullable: bool = True
    null_count: int | None = None
    non_null_count: int | None = None
    distinct_count: int | None = None  # measured through the gateway (exact), not a profile estimate

    @property
    def measured_unique(self) -> bool:
        return bool(self.non_null_count) and self.distinct_count == self.non_null_count


@dataclass(frozen=True, slots=True)
class DeclaredForeignKey:
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TableFacts:
    table: str
    declared_keys: tuple[frozenset[str], ...] = ()
    unique_indexes: tuple[frozenset[str], ...] = ()
    approved_keys: tuple[frozenset[str], ...] = ()
    foreign_keys: tuple[DeclaredForeignKey, ...] = ()
    measured_rows: int | None = None  # non-null rows of the join columns, when there are several
    measured_distinct_tuples: int | None = None


@dataclass(frozen=True, slots=True)
class RelationshipFacts:
    source: TableFacts
    target: TableFacts
    pairs: tuple[tuple[ColumnFacts, ColumnFacts], ...]
    detection_rule: str = ""
    observed_join_count: int = 0
    containment: float | None = None  # measured share of source tuples found on the target side


@dataclass(frozen=True, slots=True)
class EvidenceClass:
    name: str
    corroborating: bool
    detail: str

    def as_evidence(self) -> dict[str, Any]:
        return {"name": self.name, "corroborating": self.corroborating, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SideUniqueness:
    unique: bool
    basis: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {"unique": self.unique, "basis": self.basis}


@dataclass(frozen=True, slots=True)
class RelationshipAssessment:
    outcome: str
    evidence_classes: tuple[EvidenceClass, ...]
    source_key_columns: tuple[str, ...]
    target_key_columns: tuple[str, ...]
    cardinality: str  # one_to_one | many_to_one | one_to_many | many_to_many | unknown
    direction: str  # either | source_references_target | target_references_source | undetermined
    source_uniqueness: SideUniqueness
    target_uniqueness: SideUniqueness
    referencing_side: str
    optionality: str
    warnings: tuple[str, ...]

    @property
    def approvable(self) -> bool:
        return self.outcome == CORROBORATED

    @property
    def join_condition(self) -> str:
        return " AND ".join(f"source.{s} = target.{t}"
                            for s, t in zip(self.source_key_columns, self.target_key_columns, strict=True))

    def as_evidence(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "approvable": self.approvable,
                "evidence_classes": [c.as_evidence() for c in self.evidence_classes],
                "join_condition": self.join_condition, "cardinality": self.cardinality, "direction": self.direction,
                "source_uniqueness": self.source_uniqueness.as_evidence(),
                "target_uniqueness": self.target_uniqueness.as_evidence(), "referencing_side": self.referencing_side,
                "optionality": self.optionality, "warnings": list(self.warnings)}


def _fk_matches(fk: DeclaredForeignKey, referencing: list[ColumnFacts], referenced_table: str,
                referenced: list[ColumnFacts]) -> bool:
    if fk.referenced_table.lower() != referenced_table.lower():
        return False
    if sorted(c.lower() for c in fk.columns) != sorted(c.name.lower() for c in referencing):
        return False
    if not fk.referenced_columns:
        return True  # the metadata named the referenced table but not its columns
    declared = {a.lower(): b.lower() for a, b in zip(fk.columns, fk.referenced_columns, strict=False)}
    return declared == {s.name.lower(): t.name.lower() for s, t in zip(referencing, referenced, strict=True)}


def _covers(keys: tuple[frozenset[str], ...], names: frozenset[str]) -> bool:
    """A key that is a subset of the join columns makes the whole column set unique."""
    return any(k and {x.lower() for x in k} <= names for k in keys)


def _side_uniqueness(facts: TableFacts, columns: list[ColumnFacts], *, referenced_by_fk: bool) -> SideUniqueness:
    names = frozenset(c.name.lower() for c in columns)
    if _measured(facts, columns) and not (
            (facts.measured_rows and facts.measured_distinct_tuples == facts.measured_rows) if len(columns) > 1
            else any(c.measured_unique for c in columns)):
        # AnalystOS: a measurement that finds duplicates outranks a declaration (a broken key is a finding).
        return SideUniqueness(False, "MEASURED_NOT_UNIQUE")
    if _covers(facts.declared_keys, names):
        return SideUniqueness(True, "DECLARED_KEY")
    if _covers(facts.unique_indexes, names):
        return SideUniqueness(True, "UNIQUE_INDEX")
    if referenced_by_fk:
        return SideUniqueness(True, "DECLARED_FOREIGN_KEY")
    if _covers(facts.approved_keys, names):
        return SideUniqueness(True, "APPROVED_KEY")
    if len(columns) > 1:
        if facts.measured_rows and facts.measured_distinct_tuples == facts.measured_rows:
            return SideUniqueness(True, "MEASURED")
    elif any(c.measured_unique for c in columns):
        return SideUniqueness(True, "MEASURED")
    return SideUniqueness(False)


def _measured(facts: TableFacts, columns: list[ColumnFacts]) -> bool:
    if len(columns) > 1:
        return facts.measured_rows is not None and facts.measured_distinct_tuples is not None
    return all(c.distinct_count is not None and c.non_null_count is not None for c in columns)


def _optionality(columns: list[ColumnFacts]) -> str:
    if any((c.null_count or 0) > 0 for c in columns):
        return "optional"
    if all(not c.nullable for c in columns):
        return "mandatory"
    if all(c.null_count is not None for c in columns if c.nullable):
        return "nullable_none_observed"
    return "unknown"


def assess_relationship(facts: RelationshipFacts) -> RelationshipAssessment:
    sources = [s for s, _ in facts.pairs]
    targets = [t for _, t in facts.pairs]
    classes: list[EvidenceClass] = []
    warnings: list[str] = []
    forward_fk = any(_fk_matches(fk, sources, facts.target.table, targets) for fk in facts.source.foreign_keys)
    reverse_fk = any(_fk_matches(fk, targets, facts.source.table, sources) for fk in facts.target.foreign_keys)
    if forward_fk:
        classes.append(EvidenceClass("DECLARED_FOREIGN_KEY", True,
                                     "The source declares a foreign key on these columns referencing the target."))
    if reverse_fk:
        classes.append(EvidenceClass("DECLARED_FOREIGN_KEY", True,
                                     "The target declares a foreign key on these columns referencing the source."))
    if facts.observed_join_count > 0:
        classes.append(EvidenceClass("OBSERVED_QUERY_JOIN", True,
                                     f"Query history joins these columns {facts.observed_join_count} time(s)."))
    source_unique = _side_uniqueness(facts.source, sources, referenced_by_fk=reverse_fk)
    target_unique = _side_uniqueness(facts.target, targets, referenced_by_fk=forward_fk)
    for side_facts, cols, uniq, by_fk in ((facts.source, sources, source_unique, reverse_fk),
                                          (facts.target, targets, target_unique, forward_fk)):
        names = frozenset(c.name.lower() for c in cols)
        if uniq.basis == "MEASURED_NOT_UNIQUE" and (by_fk or _covers(side_facts.declared_keys, names)
                                                     or _covers(side_facts.unique_indexes, names)):
            warnings.append("DECLARED_KEY_NOT_UNIQUE")
    generic = all(canonical_name(c.name) in GENERIC_COLUMN_NAMES for c in sources)
    exactly_one_key = source_unique.unique != target_unique.unique
    for side, uniqueness in (("target", target_unique), ("source", source_unique)):
        if uniqueness.basis not in _BASIS_CLASS:
            continue
        corroborating = exactly_one_key and not generic
        if corroborating:
            detail = f"The {side} columns are unique by {_BASIS[uniqueness.basis]}."
        elif not exactly_one_key:
            detail = (f"The {side} columns are unique by {_BASIS[uniqueness.basis]}, but so is the other side; "
                      "two keys sharing a name are not evidence of a reference.")
        else:
            detail = (f"The {side} columns are unique by {_BASIS[uniqueness.basis]}, but the matched name is a bare "
                      "identifier that any table can carry.")
        classes.append(EvidenceClass(_BASIS_CLASS[uniqueness.basis], corroborating, detail))
    if facts.containment is not None:
        full = facts.containment >= MIN_CORROBORATING_CONTAINMENT
        classes.append(EvidenceClass("MEASURED_CONTAINMENT", full and exactly_one_key and not generic,
                                     f"{facts.containment:.2%} of the referencing values exist on the key side."))
        if not full:
            warnings.append("ORPHAN_VALUES")
    if all(canonical_name(s.name) == canonical_name(t.name) for s, t in facts.pairs):
        literal = all(s.name.lower() == t.name.lower() for s, t in facts.pairs)
        classes.append(EvidenceClass("NAME_MATCH", False, "Column names match exactly." if literal
                                     else "Column names match after naming-convention normalization."))
    if all(type_family(s.physical_type) == type_family(t.physical_type) for s, t in facts.pairs):
        literal = all(s.physical_type.lower() == t.physical_type.lower() for s, t in facts.pairs)
        classes.append(EvidenceClass("TYPE_MATCH", False, "Physical types match exactly." if literal
                                     else "Physical types share a family."))
    else:
        warnings.append("TYPE_FAMILY_MISMATCH")
    if target_unique.unique and source_unique.unique:
        cardinality, direction = "one_to_one", "either"
    elif target_unique.unique:
        cardinality, direction = "many_to_one", "source_references_target"
    elif source_unique.unique:
        cardinality, direction = "one_to_many", "target_references_source"
        warnings.append("DIRECTION_REVERSED")
    else:
        measured = _measured(facts.source, sources) and _measured(facts.target, targets)
        cardinality, direction = ("many_to_many" if measured else "unknown"), "undetermined"
        warnings.append("FAN_OUT_POSSIBLE")
    if generic:
        warnings.append("GENERIC_COLUMN_NAME")
    referencing_side = "target" if direction == "target_references_source" else "source"
    referencing = targets if referencing_side == "target" else sources
    return RelationshipAssessment(
        outcome=CORROBORATED if any(c.corroborating for c in classes) else NAME_MATCH_ONLY,
        evidence_classes=tuple(classes), source_key_columns=tuple(c.name for c in sources),
        target_key_columns=tuple(c.name for c in targets), cardinality=cardinality, direction=direction,
        source_uniqueness=source_unique, target_uniqueness=target_unique, referencing_side=referencing_side,
        optionality=_optionality(referencing), warnings=tuple(warnings))


def _declared_fk(column: dict[str, Any], assets: dict[str, dict[str, Any]]) -> DeclaredForeignKey | None:
    """A column's `references` ("table", "schema.table", "table.column" or "schema.table.column")."""
    ref = str(column.get("references") or "").strip()
    if not ref:
        return None
    low = {a.lower(): a for a in assets}
    shorts: dict[str, list[str]] = {}
    for a in assets:
        shorts.setdefault(a.split(".")[-1].lower(), []).append(a)
    if ref.lower() in low:
        return DeclaredForeignKey((column["name"],), low[ref.lower()])
    head, _, last = ref.rpartition(".")
    if head.lower() in low:
        return DeclaredForeignKey((column["name"],), low[head.lower()], (last,))
    if len(shorts.get(ref.lower(), [])) == 1:
        return DeclaredForeignKey((column["name"],), shorts[ref.lower()][0])
    if head and len(shorts.get(head.split(".")[-1].lower(), [])) == 1:
        return DeclaredForeignKey((column["name"],), shorts[head.split(".")[-1].lower()][0], (last,))
    return None


def join_sql(from_asset: str, from_columns: list[str], to_asset: str, to_columns: list[str], dialect: str = "postgres") -> str:
    """The validated join as a runnable, value-free statement (`SELECT COUNT(*) ... JOIN ... ON ...`) built with
    `sqlbuild`, so a reader (the Catalog, `context.lookup`) sees exactly the predicate the measurement corroborated.
    It still runs through the gateway like any other statement."""
    if not from_columns or len(from_columns) != len(to_columns):
        raise ValueError("a join needs the same number of columns on each side")
    on = and_all([exp.EQ(this=exp.Column(this=ident(f), table=ident("f")), expression=exp.Column(this=ident(t), table=ident("t")))
                  for f, t in zip(from_columns, to_columns, strict=True)])
    q = (exp.select(count_star().as_(ident("joined_rows"))).from_(table(from_asset, "f"))
         .join(table(to_asset, "t"), on=on, join_type="inner"))
    return to_sql(q, dialect)


JoinKey = tuple[str, str, str, str]


def join_key(left_table: str, left_column: str, right_table: str, right_column: str) -> JoinKey:
    """An undirected join between two columns, as `skills/query_history` counts it (sorted pair, lower case)."""
    a, b = (left_table.lower(), left_column.lower()), (right_table.lower(), right_column.lower())
    first, second = sorted([a, b])
    return (*first, *second)  # type: ignore[return-value]


def observed_join_count(candidate: RelationshipCandidate, observed: dict[JoinKey, int] | None) -> int:
    """How often query history joined the candidate's columns; for a composite key, the least-joined pair."""
    if not observed:
        return 0
    counts = [observed.get(join_key(candidate.from_asset, f, candidate.to_asset, t), 0)
              for f, t in zip(candidate.from_columns, candidate.to_columns, strict=True)]
    return min(counts) if counts else 0


def facts_for(candidate: RelationshipCandidate, assets: list[dict[str, Any]], *,
              observed: dict[JoinKey, int] | None = None) -> RelationshipFacts:
    """Assessment facts for a measured candidate: declarations from the asset metadata (`is_key`,
    `references`, `nullable`), uniqueness and containment from the candidate's measurement, and how often
    governed query history joined these columns (`observed`, from `skills/query_history` join counts).
    Every `is_key` column of a table together is ONE declared key (a composite primary key's parts are not
    keys by themselves)."""
    by = {a["asset"]: a for a in assets}
    ev = candidate.evidence

    def side(asset: str, names: list[str], rows: int, distinct: int) -> tuple[TableFacts, list[ColumnFacts]]:
        meta = by.get(asset, {"columns": []})
        cols = {c["name"].lower(): c for c in meta.get("columns", [])}
        key = frozenset(c["name"] for c in meta.get("columns", []) if c.get("is_key"))
        keys = (key,) if key else ()
        fks = tuple(fk for c in meta.get("columns", []) if (fk := _declared_fk(c, by)) is not None)
        multi = len(names) > 1
        facts = TableFacts(asset, declared_keys=keys, foreign_keys=fks, measured_rows=rows if multi else None,
                           measured_distinct_tuples=distinct if multi else None)
        columns = [ColumnFacts(name=n, physical_type=str(cols.get(n.lower(), {}).get("data_type") or ""),
                               nullable=bool(cols.get(n.lower(), {}).get("nullable", True)),
                               non_null_count=None if multi else rows, distinct_count=None if multi else distinct)
                   for n in names]
        return facts, columns

    src, s_cols = side(candidate.from_asset, candidate.from_columns, int(ev.get("fk_rows") or 0), int(ev.get("fk_distinct") or 0))
    tgt, t_cols = side(candidate.to_asset, candidate.to_columns, int(ev.get("target_rows") or 0),
                       int(ev.get("target_distinct") or 0))
    return RelationshipFacts(source=src, target=tgt, pairs=tuple(zip(s_cols, t_cols, strict=True)),
                             detection_rule=str(ev.get("source") or ""), containment=ev.get("containment"),
                             observed_join_count=observed_join_count(candidate, observed))


def with_assessment(candidate: RelationshipCandidate, assets: list[dict[str, Any]], *,
                    observed: dict[JoinKey, int] | None = None) -> RelationshipCandidate:
    candidate.assessment = assess_relationship(facts_for(candidate, assets, observed=observed)).as_evidence()
    joins = observed_join_count(candidate, observed)
    if joins:
        candidate.evidence = {**candidate.evidence, "observed_joins": joins}
    return candidate

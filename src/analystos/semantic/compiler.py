"""Deterministic semantic compiler (ADR-0019, P7-02): a `SemanticQuery` plus a pinned, approved model
version becomes one SQL statement for the source dialect. No model writes SQL on this path.

What the compiler enforces, in order:

* **Names only.** Metrics must have an approved version; dimensions, filters and the time dimension
  are declared fields of the model. Expressions are parsed with sqlglot for the source dialect and
  may reference only their dataset's fields or source columns; no subqueries or windows.
* **Policy in the compiler.** Fields whose columns the caller may not read (restricted, PII without
  clearance, ABAC) are *masked*: absent from `planner_catalog` (what the rules rung and the
  `semantic_query` model see) and refused here. Row filters rendered for the caller
  (`DataScope.row_filters`) wrap every dataset table (`governance/row_filters.wrap_tables`; the
  gateway re-applies the same wrapper idempotently). A withheld dataset fails closed with its reason.
* **Join safety.** Dimensions and filters on another dataset join over the model's relationships
  (one shortest path, else refused as ambiguous). Every edge must carry a validated cardinality
  (P7-09 review queue). An additive measure (anything but MIN, MAX, COUNT DISTINCT) that would cross
  a `one_to_many` or `many_to_many` edge is refused, naming the edge and the fix, unless every such
  metric declares that relationship in `pre_aggregations`: the far dataset is then reduced to
  DISTINCT (join key, used fields) rows, with its filters applied inside, before the join.
* **Same question, same SQL.** Everything iterates in a fixed order, so one model version and scope
  compile a query to identical SQL (and `sql_hash`); the statement is validated by the gateway
  validator before it is returned and executes through `QueryGateway.execute`.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.contracts.policy import DataScope
from analystos.contracts.semantic import SemanticFilter, SemanticMetricDef, SemanticQuery
from analystos.core.errors import InvalidInput, PolicyDenied
from analystos.core.ids import stable_hash
from analystos.gateway.dialects import profile as dialect_profile
from analystos.gateway.validator import validate_sql

COMPILER_VERSION = "semantic.v2"
FANOUT = ("one_to_many", "many_to_many")
_REVERSE = {"one_to_one": "one_to_one", "many_to_one": "one_to_many", "one_to_many": "many_to_one",
            "many_to_many": "many_to_many"}
_GRAIN_WORDS = {"daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year",
                "annual": "year", "day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year"}


@dataclass(frozen=True)
class CompiledMetricQuery:
    sql: str
    provenance: dict[str, Any]


# ------------------------------------------------------------------------------------ catalog
def load_catalog(session, workspace_id: str) -> dict[str, Any] | None:
    """The approved model version and approved metrics, plus glossary synonyms for the rules rung."""
    from analystos.semantic.review import approved_model
    from analystos.semantic.service import approved_metrics

    # The newest approved structure: an agent's pending proposal does not switch governed answers off.
    model = approved_model(session, workspace_id)
    if model is None:
        return None
    metrics = {name: {"id": m.id, "version": m.version, "hash": m.content_hash, "definition": m.definition}
               for name, m in approved_metrics(session, workspace_id).items()}
    return {"id": model.id, "version": model.version, "hash": model.content_hash, "datasets": model.datasets,
            "relationships": list(model.relationships or []), "metrics": metrics,
            "synonyms": _glossary_synonyms(session, workspace_id, metrics)}


def _words(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", str(value)).casefold()).strip()


def _aliases(name: str, definition: dict[str, Any]) -> set[str]:
    out = {_words(name), _words(definition.get("display_name") or name)}
    ctx = definition.get("ai_context")
    if isinstance(ctx, dict):
        out |= {_words(s) for s in ctx.get("synonyms") or [] if isinstance(s, str) and s.strip()}
    return out


def _glossary_synonyms(session, workspace_id: str, metrics: dict[str, Any]) -> dict[str, str]:
    """phrase -> metric, from trusted glossary entries whose name is exactly a metric's name or display
    name. A phrase that would name two metrics is dropped: the rules rung never guesses between them."""
    from analystos.knowledge.entries import visible_entries

    by_alias: dict[str, set[str]] = {}
    for name, entry in metrics.items():
        for alias in _aliases(name, entry["definition"]):
            by_alias.setdefault(alias, set()).add(name)
    found: dict[str, set[str]] = {}
    for e in visible_entries(session, workspace_id, kinds=("term", "metric", "definition"), trusted_only=True):
        targets = by_alias.get(_words(e.name), set())
        for syn in e.synonyms or []:
            if _words(syn):
                found.setdefault(_words(syn), set()).update(targets)
    return {phrase: next(iter(names)) for phrase, names in sorted(found.items()) if len(names) == 1}


# ------------------------------------------------------------------------------------ datasets
@dataclass
class _Dataset:
    name: str
    source: exp.Expression  # a read query over physical tables
    fields: dict[str, dict[str, Any]]
    outputs: dict[str, set[str]]  # source output column (lower) -> physical "schema.table.column"
    assets: list[str]
    masked: set[str] = field(default_factory=set)  # field names the caller may not read
    unjoined: set[str] = field(default_factory=set)  # field names that read a lookup join left out (see _base_only)


def _parse(text: str, dialect: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(text, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise InvalidInput("The approved definition does not parse for this source dialect.") from exc
    if len(statements) != 1 or statements[0] is None:
        raise InvalidInput("A semantic expression must contain exactly one expression.")
    return statements[0]


def _denied(scope: DataScope, column_fq: str) -> bool:
    low = column_fq.lower()
    for d in scope.denied_columns:
        if d.startswith("*.") and low.rsplit(".", 1)[-1] == d[2:].lower():
            return True
        if d.lower() == low:
            return True
    return False


def _asset_for(scope: DataScope, table: exp.Table) -> str:
    name, db = table.name, table.db
    if db:
        hits = [a for a in scope.assets if a.lower() == f"{db}.{name}".lower()]
    else:
        hits = [a for a in scope.assets if a.split(".", 1)[1].lower() == name.lower()]
    if len(hits) == 1:
        return hits[0]
    wanted = f"{db}.{name}" if db else name
    for asset, reason in sorted(scope.withheld_assets.items()):
        if asset.lower() == wanted.lower() or asset.split(".", 1)[1].lower() == name.lower():
            raise PolicyDenied(f"The data behind this metric is withheld from you: {reason}.",
                               details={"asset": asset, "reason": reason})
    raise InvalidInput(f"The dataset reads {wanted}, which is not in your authorized scope.")


_UNCERTIFIED_JOIN = "The dataset contains joins whose cardinality has not been certified for governed metrics."


def _base_only(source: exp.Expression) -> tuple[exp.Expression, set[str]]:
    """A dataset whose only joins are LEFT JOINs from its base table (the lookups an investigation's dataset adds:
    product category, customer region) is read from the base table alone: the joins and every output that reads a
    joined table are left out, and those outputs are returned so the fields over them are unavailable. A LEFT JOIN never
    removes a base row, and with no join left no uncertified join reaches a governed query (ADR-0019). Any other join,
    a join in a nested query, or an unqualified column next to a join is still refused. PILOT-FIX-05: every dataset an
    investigation built carried such lookups, so no approved metric over it could answer governed."""
    joins = source.args.get("joins") if isinstance(source, exp.Select) else None
    if not joins:
        if any(source.find_all(exp.Join)):
            raise InvalidInput(_UNCERTIFIED_JOIN)
        return source, set()
    base = (source.args.get("from_") or source.args.get("from"))
    if base is None or not isinstance(base.this, exp.Table) or any(j.side != "LEFT" or j.kind not in ("", "OUTER")
                                                                    or not isinstance(j.this, exp.Table) for j in joins):
        raise InvalidInput(_UNCERTIFIED_JOIN)
    if sum(1 for _ in source.find_all(exp.Join)) != len(joins) or source.find(exp.Subquery) is not None:
        raise InvalidInput(_UNCERTIFIED_JOIN)
    joined = {j.this.alias_or_name for j in joins}
    if any(not c.table for c in source.find_all(exp.Column) if not isinstance(c.parent, exp.Join)):
        raise InvalidInput(_UNCERTIFIED_JOIN)  # an unqualified column could come from either side
    for clause in ("where", "group", "having", "qualify", "order", "distinct"):
        node = source.args.get(clause)
        if node is not None and any(c.table in joined for c in node.find_all(exp.Column)):
            raise InvalidInput(_UNCERTIFIED_JOIN)
    kept, dropped = [], set()
    for projection in source.expressions:
        if isinstance(projection, exp.Star) or any(c.table in joined for c in projection.find_all(exp.Column)):
            if isinstance(projection, exp.Star):
                raise InvalidInput(_UNCERTIFIED_JOIN)
            dropped.add(projection.alias_or_name.lower())
            continue
        kept.append(projection)
    if not kept:
        raise InvalidInput(_UNCERTIFIED_JOIN)
    pruned = source.copy()
    pruned.set("joins", None)
    pruned.set("expressions", [k.copy() for k in kept])
    return pruned, dropped


def _grain_holds(grain: str, ds: _Dataset) -> bool:
    """A declared grain the dataset itself guarantees: all rows, one row per record, or one row per the entity of the
    single table it reads with no join (`order` on `orders`). Joins to other datasets keep it: an additive measure that
    would cross a fan-out edge is refused separately."""
    if grain in {"all", "overall", "record", "records", "row", "rows"}:
        return True
    if len(ds.assets) != 1 or any(ds.source.find_all(exp.Join)):
        return False
    table = _words(ds.assets[0].rsplit(".", 1)[-1])
    return grain in {table, table[:-1] if table.endswith("s") else table, table[:-2] if table.endswith("es") else table}


def _unavailable(ds: _Dataset, name: str) -> Exception:
    if name in ds.unjoined:
        return InvalidInput(f"Field {ds.name}.{name} reads a joined table, and that join is not certified for governed "
                            "metrics.", details={"field": f"{ds.name}.{name}", "reason": "uncertified_join"})
    return PolicyDenied(f"Field {ds.name}.{name} is masked for you by workspace policy.", details={"field": f"{ds.name}.{name}"})


def _dataset(raw: dict[str, Any], scope: DataScope, dialect: str) -> _Dataset:
    source = _parse(raw["source"], dialect)
    if not isinstance(source, exp.Select | exp.SetOperation):
        if not isinstance(source, exp.Column | exp.Identifier | exp.Table):
            raise InvalidInput("A dataset source must be a table or a read query.")
        source = _parse("SELECT * FROM " + raw["source"], dialect)
    source, unjoined = _base_only(source)
    tables = [(t, _asset_for(scope, t)) for t in source.find_all(exp.Table)]
    if not tables:
        raise InvalidInput("A dataset source must read a table.")
    assets = sorted({a for _, a in tables})
    outputs: dict[str, set[str]] = {}
    if isinstance(source, exp.Select) and len(assets) == 1:
        # Stars become the columns, and columns the caller may not read are left out of the dataset
        # (the fields over them are masked below), so a mask hides a field instead of failing the query.
        asset, kept = assets[0], []
        for projection in source.expressions:
            if isinstance(projection, exp.Star):
                for c in scope.columns.get(asset, []):
                    outputs.setdefault(c.lower(), set()).add(f"{asset}.{c}")
                    if not _denied(scope, f"{asset}.{c}"):
                        kept.append(exp.column(exp.to_identifier(c, quoted=True)))
                continue
            reads = {f"{asset}.{c.name}" for c in projection.find_all(exp.Column)}
            outputs.setdefault(projection.alias_or_name.lower(), set()).update(reads)
            if not any(_denied(scope, r) for r in reads):
                kept.append(projection)
        if not kept:
            raise PolicyDenied(f"Every column of dataset {raw['name']} is masked for you by workspace policy.")
        source.set("expressions", kept)
    else:  # a set operation: every output may carry any column the statement reads (fail closed for masks)
        reads = {f"{a}.{c.name}" for a in assets for c in source.find_all(exp.Column)}
        for name in source.named_selects:
            outputs[name.lower()] = set(reads)
    fields = {f["name"]: f for f in raw.get("fields", [])}
    if len(fields) != len(raw.get("fields", [])):
        raise InvalidInput(f"Dataset {raw['name']} has duplicate field names.")
    ds = _Dataset(raw["name"], source, fields, outputs, assets)
    for fname, f in fields.items():
        expr = _parse(f["expressions"][0]["expression"], dialect)
        cols = {c.name.lower() for c in expr.find_all(exp.Column)}
        reads = set().union(*(outputs.get(c, set()) for c in cols)) if cols else set()
        if cols & unjoined:
            ds.masked.add(fname)
            ds.unjoined.add(fname)
        elif any(_denied(scope, r) for r in reads):
            ds.masked.add(fname)
    return ds


def _dialect(scope: DataScope) -> str:
    dialects = {scope.source_dialects.get(s, "postgres") for s in scope.source_ids}
    if len(dialects) != 1:
        raise InvalidInput("Select a single source dialect for a governed metric query.")
    return next(iter(dialects))


def _column_names(text: str, dialect: str) -> set[str]:
    return {c.name for c in _parse(text, dialect).find_all(exp.Column)}


def planner_catalog(catalog: dict[str, Any], scope: DataScope) -> dict[str, Any]:
    """The catalog the planner sees (rules rung, `semantic_query` model): masked fields, datasets
    outside the caller's scope and metrics that need either are absent, not only refused later."""
    try:
        dialect = _dialect(scope)
    except InvalidInput:
        return {**catalog, "datasets": [], "metrics": {}, "synonyms": {}}
    datasets, visible = [], {}
    for raw in catalog.get("datasets") or []:
        try:
            ds = _dataset(raw, scope, dialect)
        except (InvalidInput, PolicyDenied):
            continue
        visible[ds.name] = ds
        datasets.append({**raw, "fields": [f for f in raw.get("fields", []) if f["name"] not in ds.masked]})
    metrics = {}
    for name, entry in sorted(catalog.get("metrics", {}).items()):
        d = entry["definition"]
        ds = visible.get(d.get("dataset") or "")
        if ds is None:
            continue
        try:
            used = _column_names(d["expressions"][0]["expression"], dialect)
            for f in d.get("filters") or []:
                used |= _column_names(f, dialect)
        except InvalidInput:
            continue
        hidden = {n for n in used if n in ds.masked or any(_denied(scope, r) for r in ds.outputs.get(n.lower(), set()))}
        if hidden:
            continue
        dims = [x for x in d.get("dimensions") or [] if x.split(".")[-1] not in ds.masked or "." in x]
        metrics[name] = {**entry, "definition": {**d, "dimensions": dims}}
    synonyms = {p: m for p, m in (catalog.get("synonyms") or {}).items() if m in metrics}
    return {**catalog, "datasets": datasets, "metrics": metrics, "synonyms": synonyms}


# ------------------------------------------------------------------------------------ rules rung
def match_question(question: str, catalog: dict[str, Any]) -> SemanticQuery | None:
    """Rules rung of `semantic_query`: the whole question is a metric name, display name or glossary
    synonym, optionally `by <dimension>` or with a time grain (`monthly X`, `X per month`). Anything
    else (an unrecognised period, filter or word) is not dropped: no match."""
    text = _words(question.rstrip("?.! "))
    text = re.sub(r"^(?:show me |show |what is |what's |give me )(?:the )?", "", text)
    hits = []
    for name, entry in catalog["metrics"].items():
        defn = entry["definition"]
        aliases = _aliases(name, defn) | {p for p, m in (catalog.get("synonyms") or {}).items() if m == name}
        dims = defn.get("dimensions") or []
        times = [d for d in dims if _is_time(catalog, defn, d)]
        for alias in aliases:
            if text == alias:
                hits.append(SemanticQuery(metrics=[name]))
                continue
            grain = _grain_phrase(text, alias)
            if grain and len(times) == 1:
                hits.append(SemanticQuery(metrics=[name], time={"dimension": times[0], "grain": grain}))
            elif text.startswith(alias + " by "):
                wanted = text[len(alias) + 4:]
                matched = [d for d in dims if wanted in {_words(d.split(".")[-1]), _words(_label(catalog, defn, d))}]
                if len(matched) == 1:
                    hits.append(SemanticQuery(metrics=[name], dimensions=matched))
    unique = {h.model_dump_json(): h for h in hits}
    if len(unique) > 1:
        raise InvalidInput("More than one approved metric matches. Use the metric's unique name.")
    return next(iter(unique.values()), None)


def _grain_phrase(text: str, alias: str) -> str | None:
    for word, grain in _GRAIN_WORDS.items():
        if word in ("daily", "weekly", "monthly", "quarterly", "yearly", "annual") and text == f"{word} {alias}":
            return grain
        if grain == word and text in (f"{alias} per {word}", f"{alias} by {word}", f"{alias} each {word}"):
            return grain
    return None


def _field_def(catalog: dict[str, Any], defn: dict[str, Any], ref: str) -> dict[str, Any] | None:
    ds_name, _, fname = ref.rpartition(".")
    ds_name = ds_name or defn.get("dataset")
    for d in catalog.get("datasets") or []:
        if d["name"] == ds_name:
            return next((f for f in d.get("fields", []) if f["name"] == fname), None)
    return None


def _is_time(catalog: dict[str, Any], defn: dict[str, Any], ref: str) -> bool:
    f = _field_def(catalog, defn, ref)
    return bool(f and isinstance(f.get("dimension"), dict) and f["dimension"].get("is_time"))


def _label(catalog: dict[str, Any], defn: dict[str, Any], ref: str) -> str:
    f = _field_def(catalog, defn, ref)
    return (f or {}).get("label") or ref.split(".")[-1]


# ------------------------------------------------------------------------------------ compilation
def _additive(tree: exp.Expression) -> bool:
    """False only when every aggregate is fan-out safe (MIN, MAX, COUNT(DISTINCT ...), APPROX_DISTINCT)."""
    safe = (exp.Min, exp.Max, exp.ApproxDistinct)
    for agg in tree.find_all(exp.AggFunc):
        if isinstance(agg, safe):
            continue
        if isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct):
            continue
        return True
    return False


@dataclass
class _Join:
    relationship: dict[str, Any]
    from_ds: str
    to_ds: str
    cardinality: str  # in traversal direction
    forward: bool
    pre_aggregated: bool = False


def join_path(relationships: list[dict[str, Any]], base: str, target: str) -> list[_Join]:
    """The one shortest relationship path from `base` to `target`, every edge validated; ties are refused
    (not guessed). Offline: needs only the model's relationships (also used by reconciliation)."""
    rels = sorted(relationships, key=lambda r: r["name"])
    adj: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
    for r in rels:
        adj.setdefault(r["from"], []).append((r["to"], r, True))
        adj.setdefault(r["to"], []).append((r["from"], r, False))
    best: list[list[tuple[str, str, dict[str, Any], bool]]] = []
    queue = deque([(base, [])])
    seen_depth: dict[str, int] = {base: 0}
    while queue:
        node, trail = queue.popleft()
        if best and len(trail) >= len(best[0]):
            continue
        for nxt, r, forward in adj.get(node, []):
            if any(step[1] == nxt for step in trail) or nxt == base:
                continue
            step = [*trail, (node, nxt, r, forward)]
            if nxt == target:
                if not best or len(step) == len(best[0]):
                    best.append(step)
                continue
            if seen_depth.get(nxt, len(step)) < len(step):
                continue
            seen_depth[nxt] = len(step)
            queue.append((nxt, step))
    if not best:
        raise InvalidInput(f"No relationship in the approved model joins {base} to {target}.")
    if len(best) > 1:
        routes = ["; ".join(r["name"] for _, _, r, _ in p) for p in best]
        raise InvalidInput(f"More than one join path leads from {base} to {target} ({' | '.join(routes)}). "
                           "The compiler does not choose between them: remove or rename a relationship.",
                           details={"paths": routes})
    out = []
    for src, dst, r, forward in best[0]:
        card = r.get("cardinality")
        if not card or not r.get("validated_at") or not r.get("validated_by"):
            raise InvalidInput(
                f"Relationship {r['name']} ({r['from']} -> {r['to']}) has no validated cardinality. Governed "
                "metrics join only over relationships measured and accepted in the relationship review queue.",
                details={"edge": r["name"], "fix": "review the relationship candidate for these columns"})
        out.append(_Join(r, src, dst, card if forward else _REVERSE[card], forward))
    return out



class _Compilation:
    def __init__(self, query: SemanticQuery, catalog: dict[str, Any], scope: DataScope):
        self.query, self.catalog, self.scope = query, catalog, scope
        self.dialect = _dialect(scope)
        self.fold = dialect_profile(self.dialect).fold
        self.raw = {d["name"]: d for d in catalog.get("datasets") or []}
        self.datasets: dict[str, _Dataset] = {}
        self.aliases: dict[str, str] = {}
        self.joins: dict[str, list[_Join]] = {}  # dataset -> path from the base

    # -- datasets and fields
    def ds(self, name: str) -> _Dataset:
        if name not in self.datasets:
            matches = [d for d in self.catalog.get("datasets") or [] if d["name"] == name]
            if len(matches) != 1:
                raise InvalidInput(f"Dataset {name!r} is missing or ambiguous in the approved model.")
            self.datasets[name] = _dataset(matches[0], self.scope, self.dialect)
        return self.datasets[name]

    def resolve(self, ref: str, base: str) -> tuple[str, str]:
        """(dataset, field) for `field` or `dataset.field`; the base dataset wins an unqualified name."""
        if "." in ref:
            ds_name, fname = ref.split(".", 1)
            if fname not in self.ds(ds_name).fields:
                raise InvalidInput(f"{ref!r} is not a declared field of the approved model.")
            return ds_name, fname
        if ref in self.ds(base).fields:
            return base, ref
        owners = sorted(n for n, raw in self.raw.items() if any(f["name"] == ref for f in raw.get("fields", [])))
        if not owners:
            raise InvalidInput(f"{ref!r} is not a declared field of the approved model.")
        if len(owners) > 1:
            raise InvalidInput(f"{ref!r} is a field of {', '.join(owners)}. Name it as dataset.field.")
        return owners[0], ref

    def expression(self, ds: _Dataset, text: str, alias: str, *, fields_first: bool = True) -> exp.Expression:
        """Parse `text` for the dialect; columns are the dataset's fields (replaced by their expression) or
        source columns, qualified with the dataset's alias. Masked fields and columns are refused."""
        value = _parse(text, self.dialect)
        if isinstance(value, exp.Query) or any(value.find_all(exp.Subquery)):
            raise InvalidInput("Metric and dimension expressions cannot contain subqueries.")

        def swap(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            if node.table not in ("", "d", ds.name):
                raise InvalidInput(f"Undeclared dataset column: {node.sql()}.")
            name = node.name
            if name in ds.masked:
                raise _unavailable(ds, name)
            if fields_first and name in ds.fields:
                inner = _parse(ds.fields[name]["expressions"][0]["expression"], self.dialect)
                if not (isinstance(inner, exp.Column) and inner.name == name):
                    return self.expression(ds, inner.sql(dialect=self.dialect), alias, fields_first=False)
            if name.lower() not in ds.outputs and name not in ds.fields:
                raise InvalidInput(f"Undeclared dataset column: {node.sql()}.")
            if any(_denied(self.scope, r) for r in ds.outputs.get(name.lower(), set())):
                raise PolicyDenied(f"Field {ds.name}.{name} is masked for you by workspace policy.",
                                   details={"field": f"{ds.name}.{name}"})
            return exp.column(name, table=alias)

        return value.transform(swap)

    def field_expr(self, ds_name: str, fname: str) -> exp.Expression:
        ds = self.ds(ds_name)
        if fname in ds.masked:
            raise _unavailable(ds, fname)
        return self.expression(ds, ds.fields[fname]["expressions"][0]["expression"], self.alias_of(ds_name))

    def alias_of(self, ds_name: str) -> str:
        return self.aliases[ds_name]

    # -- joins
    def path(self, base: str, target: str) -> list[_Join]:
        return join_path(self.catalog.get("relationships") or [], base, target)

    # -- the statement
    def compile(self) -> CompiledMetricQuery:
        q, entries = self.query, self.catalog["metrics"]
        if len(set(q.metrics)) != len(q.metrics) or len(set(q.dimensions)) != len(q.dimensions):
            raise InvalidInput("Metric and dimension names must be unique.")
        if any(name not in entries for name in q.metrics):
            raise InvalidInput("Every requested metric must have an approved definition.")
        definitions = [SemanticMetricDef.model_validate(entries[n]["definition"]) for n in q.metrics]
        bases = {d.dataset for d in definitions}
        if None in bases:
            raise InvalidInput("A governed metric must declare the dataset it is defined on.")
        if len(bases) != 1:
            raise InvalidInput("These metrics are defined on different datasets. A multi-fact query (one branch per "
                               "fact on a shared dimension spine) is not supported yet: ask for them separately.")
        base = next(iter(bases))
        self.aliases[base] = "d"
        base_ds = self.ds(base)
        if len({tuple(d.filters) for d in definitions}) != 1:
            raise InvalidInput("These metrics have different filters. Ask for them separately.")
        for defn in definitions:
            if defn.grain and not _grain_holds(_words(defn.grain), base_ds):
                raise InvalidInput("This metric declares a grain that this compiler cannot yet enforce.")
            if defn.dialect.upper() not in {"ANSI_SQL", self.dialect.upper()}:
                raise InvalidInput("The metric expression dialect does not match the selected source.")

        # Every field the query names, resolved to (dataset, field) and checked against the approvals.
        refs: list[tuple[str, str, str]] = []  # (role, dataset, field)
        for dim in q.dimensions:
            refs.append(("dimension", *self.resolve(dim, base)))
        if q.time is not None:
            if q.time.dimension in q.dimensions:
                raise InvalidInput("Name the time dimension in `time` or in `dimensions`, not both.")
            refs.append(("time", *self.resolve(q.time.dimension, base)))
        for f in q.filters:
            refs.append(("filter", *self.resolve(f.field, base)))
        requested = {"dimension": q.dimensions, "time": [q.time.dimension] if q.time else [],
                     "filter": [f.field for f in q.filters]}
        names = [n for role in ("dimension", "time", "filter") for n in requested[role]]
        for (role, ds_name, fname), ref in zip(refs, names, strict=True):
            fdef = self.ds(ds_name).fields[fname]
            if fdef.get("dimension") is None:
                raise InvalidInput(f"Field {ref!r} is not declared as a dimension.")
            if role == "time" and not (isinstance(fdef["dimension"], dict) and fdef["dimension"].get("is_time")):
                raise InvalidInput(f"Field {ref!r} is not a time dimension.")
            approved = {fname if ds_name == base else None, f"{ds_name}.{fname}"}
            if any(not approved & set(d.dimensions) for d in definitions):
                raise InvalidInput(f"Dimension {ref!r} is not approved for every requested metric.")
            if fname in self.ds(ds_name).masked:
                raise _unavailable(self.ds(ds_name), fname)

        # Joins: one path per other dataset; fan-out refused unless pre-aggregated by every additive metric.
        additive = [n for n, d in zip(q.metrics, definitions, strict=True)
                    if _additive(_parse(d.expression, self.dialect))]
        others = sorted({ds for _, ds, _ in refs if ds != base})
        plan: list[_Join] = []
        for target in others:
            for j in self.path(base, target):
                if any(p.to_ds == j.to_ds for p in plan):
                    continue
                if j.cardinality in FANOUT and additive:
                    missing = [n for n in additive
                               if j.relationship["name"] not in SemanticMetricDef.model_validate(entries[n]["definition"]).pre_aggregations]
                    if missing:
                        r = j.relationship
                        raise InvalidInput(
                            f"Refused (fan-out): {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} additive and "
                            f"relationship {r['name']} ({j.from_ds} -> {j.to_ds}) is {j.cardinality} in this direction, "
                            f"so the join would count each {j.from_ds} row once per matching {j.to_ds} row. Fix: declare "
                            f"a pre-aggregation for {r['name']} on the metric (pre_aggregations: [{r['name']}]), or define "
                            f"the metric on a dataset at the {j.to_ds} grain (a declared distinct measure).",
                            details={"edge": r["name"], "from": j.from_ds, "to": j.to_ds, "cardinality": j.cardinality,
                                     "metrics": missing, "fix": ["pre_aggregate", "distinct_measure"]})
                    j.pre_aggregated = True
                if any(p.pre_aggregated and p.to_ds == j.from_ds for p in plan):
                    raise InvalidInput(f"A pre-aggregated dataset ({j.from_ds}) cannot be joined further; "
                                       "use a dimension of that dataset directly.")
                plan.append(j)
        for i, j in enumerate(plan, start=1):
            self.aliases[j.to_ds] = f"j{i}"

        # Sources, with row filters applied to every table they read (same wrapper the gateway applies).
        from analystos.governance.row_filters import wrap_tables

        filtered: list[str] = []
        for name in [base, *[j.to_ds for j in plan]]:
            ds = self.ds(name)
            validate_sql(self.scope, ds.source.sql(dialect=self.dialect), max_rows=q.limit)
            wrap_tables([(t, _asset_for(self.scope, t)) for t in list(ds.source.find_all(exp.Table))],
                        self.scope, self.dialect, self.fold)
            filtered += [a for a in ds.assets if self.scope.row_filters.get(a)]

        selected, groups, where = [], [], []
        pre_fields: dict[str, list[tuple[str, str]]] = {}  # pre-aggregated dataset -> (field, output alias)
        pre_filters: dict[str, list[exp.Expression]] = {}
        pre_ds = {j.to_ds for j in plan if j.pre_aggregated}

        def field_sql(ds_name: str, fname: str) -> exp.Expression:
            if ds_name in pre_ds:
                out = f"{fname}"
                if (fname, out) not in pre_fields.setdefault(ds_name, []):
                    pre_fields[ds_name].append((fname, out))
                return exp.column(out, table=self.alias_of(ds_name))
            return self.field_expr(ds_name, fname)

        def output(ds_name: str, fname: str, alias: str, grain: str | None = None) -> None:
            value = field_sql(ds_name, fname)
            if any(value.find_all(exp.AggFunc)):
                raise InvalidInput("Dimensions cannot contain aggregates.")
            if grain:
                value = exp.TimestampTrunc(this=value, unit=exp.var(grain.upper()))
            groups.append(value)
            selected.append(value.copy().as_(alias, quoted=True))

        for (role, ds_name, fname), ref in zip(refs, names, strict=True):
            if role == "dimension":
                output(ds_name, fname, ref)
        if q.time is not None:
            _, ds_name, fname = next(r for r in refs if r[0] == "time")
            if q.time.grain:
                output(ds_name, fname, q.time.dimension, q.time.grain)
            for bound, op in ((q.time.start, exp.GTE), (q.time.end, exp.LT)):
                if bound is None:
                    continue
                cond = op(this=self._raw_field(ds_name, fname, pre_ds, pre_fields),
                          expression=exp.cast(exp.Literal.string(bound.isoformat()), "DATE"))
                (pre_filters.setdefault(ds_name, []) if ds_name in pre_ds else where).append(cond)
        for f, (_, ds_name, fname) in zip(q.filters, [r for r in refs if r[0] == "filter"], strict=True):
            target = self._raw_field(ds_name, fname, pre_ds, pre_fields)
            (pre_filters.setdefault(ds_name, []) if ds_name in pre_ds else where).append(_predicate(target, f))

        for name, defn in zip(q.metrics, definitions, strict=True):
            value = self.expression(base_ds, defn.expression, "d")
            if not any(value.find_all(exp.AggFunc)) or any(value.find_all(exp.Window)):
                raise InvalidInput("Governed metric expressions must aggregate their dataset without windows.")
            selected.append(value.as_(name, quoted=True))
        for predicate in definitions[0].filters:
            condition = self.expression(base_ds, predicate, "d")
            if any(condition.find_all(exp.AggFunc)):
                raise InvalidInput("Metric filters must apply before aggregation.")
            where.insert(0, condition)

        statement = exp.select(*selected).from_(base_ds.source.subquery("d"))
        for j in plan:
            statement = self._join(statement, j, pre_fields.get(j.to_ds, []), pre_filters.get(j.to_ds, []))
        for condition in where:
            statement = statement.where(condition)
        if groups:
            statement = statement.group_by(*groups)
        outputs = {*q.metrics, *q.dimensions, *([q.time.dimension] if q.time and q.time.grain else [])}
        if q.order:
            for o in q.order:
                if o.field not in outputs:
                    raise InvalidInput(f"Order by {o.field!r}: order only by a requested metric or dimension.")
                statement = statement.order_by(exp.Ordered(this=exp.column(exp.to_identifier(o.field, quoted=True)),
                                                           desc=o.direction == "desc"))
        elif groups:
            statement = statement.order_by(*(g.copy() for g in groups))
        statement = statement.limit(q.limit)
        validated = validate_sql(self.scope, statement.sql(dialect=self.dialect), max_rows=q.limit)
        entries_used = [{"name": n, **{k: entries[n][k] for k in ("id", "version", "hash")}} for n in q.metrics]
        provenance = {"model_id": self.catalog["id"], "model_version": self.catalog["version"],
                      "semantic_model_version": self.catalog["version"], "model_hash": self.catalog["hash"],
                      "compiler_version": COMPILER_VERSION, "query": q.model_dump(mode="json"), "metrics": entries_used,
                      "joins": [{"relationship": j.relationship["name"], "from": j.from_ds, "to": j.to_ds,
                                 "cardinality": j.cardinality, "pre_aggregated": j.pre_aggregated} for j in plan],
                      "row_filtered_assets": sorted(set(filtered)),
                      "policy_version": self.scope.policy_version, "scope_hash": self.scope.scope_hash(),
                      "sql_hash": stable_hash(validated.executable_sql)}
        return CompiledMetricQuery(validated.executable_sql, provenance)

    def _raw_field(self, ds_name: str, fname: str, pre_ds: set[str], pre_fields: dict) -> exp.Expression:
        if ds_name in pre_ds:  # evaluated inside the pre-aggregation, against its own source
            return self.expression(self.ds(ds_name), self.ds(ds_name).fields[fname]["expressions"][0]["expression"], "p")
        return self.field_expr(ds_name, fname)

    def _join(self, statement: exp.Select, j: _Join, used: list[tuple[str, str]], filters: list[exp.Expression]) -> exp.Select:
        r = j.relationship
        near_cols, far_cols = (r["from_columns"], r["to_columns"]) if j.forward else (r["to_columns"], r["from_columns"])
        near_alias, far_alias = self.alias_of(j.from_ds), self.alias_of(j.to_ds)
        near, far = self.ds(j.from_ds), self.ds(j.to_ds)
        near_keys = [self.expression(near, c, near_alias) for c in near_cols]
        if j.pre_aggregated:
            keys = [self.expression(far, c, "p").as_(f"k{i}", quoted=True) for i, c in enumerate(far_cols)]
            values = [self.expression(far, far.fields[f]["expressions"][0]["expression"], "p").as_(out, quoted=True)
                      for f, out in used]
            inner = exp.select(*keys, *values).distinct().from_(far.source.subquery("p"))
            for cond in filters:
                inner = inner.where(cond)
            far_keys = [exp.column(f"k{i}", table=far_alias, quoted=True) for i in range(len(far_cols))]
            source = inner.subquery(far_alias)
            join_type = "inner" if filters else "left"
        else:
            far_keys = [self.expression(far, c, far_alias) for c in far_cols]
            source = far.source.subquery(far_alias)
            join_type = "left"
        on = exp.and_(*[exp.EQ(this=a, expression=b) for a, b in zip(near_keys, far_keys, strict=True)])
        return statement.join(source, on=on, join_type=join_type)


def _predicate(target: exp.Expression, f: SemanticFilter) -> exp.Expression:
    def lit(v: Any) -> exp.Expression:
        if isinstance(v, bool):
            return exp.Boolean(this=v)
        if isinstance(v, int | float):
            return exp.Literal.number(v)
        return exp.Literal.string(str(v))

    ops = {"=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}
    if f.op in ops:
        return ops[f.op](this=target, expression=lit(f.value))
    if f.op in ("in", "not_in"):
        cond = exp.In(this=target, expressions=[lit(v) for v in f.value])
        return exp.Not(this=cond) if f.op == "not_in" else cond
    null = exp.Is(this=target, expression=exp.Null())
    return null if f.op == "is_null" else exp.Not(this=null)


def compile_query(query: SemanticQuery, catalog: dict[str, Any], scope: DataScope) -> CompiledMetricQuery:
    return _Compilation(query, catalog, scope).compile()

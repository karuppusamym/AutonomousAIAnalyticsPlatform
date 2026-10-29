"""SQL validation for the governed query gateway (fail closed).

Every statement a user or a model sends is parsed with sqlglot in the source dialect, checked
against the authorized DataScope, fully qualified against the known column metadata and then
re-generated. The database only ever sees the *generated* SQL, never the caller's text.

Rules (each failure raises SQLRejected with a message a model can act on):
  * exactly one statement, and it must be a read query (SELECT / set operation / WITH ... SELECT);
  * no write, DDL, DCL, transaction, session or utility node anywhere in the tree (this also
    covers data-modifying CTEs, SELECT INTO and FOR UPDATE/SHARE locks);
  * no 3-part (catalog) names, table-valued functions, LATERAL, VALUES or NATURAL joins;
  * every table reference that is not a CTE resolves to exactly one asset in ``scope.assets``;
    unqualified names resolve when unambiguous. All assets must belong to one source;
  * no denied function (file access, sleeps, dblink, session settings, advisory locks ...), and
    no schema-qualified function calls;
  * stars are expanded with ``scope.columns``; unknown columns and any reference to a column in
    ``scope.denied_columns`` (``schema.table.column`` or ``*.column``) are rejected wherever they
    appear, including through star expansion, aliases, CTEs and subqueries.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from analystos.contracts.policy import DataScope
from analystos.core.errors import SQLRejected
from analystos.gateway.dialects import BASE_DENYLIST, SUPPORTED_DIALECTS, DialectProfile, profile
from analystos.gateway.types import ValidatedSQL

DEFAULT_DIALECT = "postgres"
MAX_SQL_LENGTH = 100_000
FEDERATION_DIALECT = "duckdb"  # cross-source statements run on the local DuckDB federation engine...
IN_PLACE_FEDERATION = frozenset({"trino"})  # ...unless every source speaks one of these: then they run on that engine
FEDERATED_SOURCE = "federated"  # ValidatedSQL.source_id of a cross-source statement

# Statement/node types that must not appear anywhere in the tree.
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable", "Command",
    "Copy", "Set", "SetItem", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Use", "Pragma",
    "Describe", "Kill", "LoadData", "Lock", "Into", "Analyze", "Cache", "Uncache", "Refresh",
    "Attach", "Detach", "Summarize", "Show", "Execute", "Declare", "Prepare", "Export", "Returning",
)
_FORBIDDEN_NODES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if isinstance(getattr(exp, name, None), type)
)
_UNSUPPORTED_SOURCES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in ("Lateral", "Unnest", "Values", "TableFromRows", "Pivot", "Unpivot")
    if isinstance(getattr(exp, name, None), type)
)

# Exact names and prefixes (a trailing "*") of functions that are never allowed in any dialect;
# each dialect adds its own (gateway/dialects.py).
FUNCTION_DENYLIST = BASE_DENYLIST
_DENY_EXACT = {f for f in FUNCTION_DENYLIST if not f.endswith("*")}
_DENY_PREFIX = tuple(f[:-1] for f in FUNCTION_DENYLIST if f.endswith("*"))

_log = logging.getLogger(__name__)


def _reject(message: str, **details: object) -> SQLRejected:
    return SQLRejected(message, details={k: v for k, v in details.items() if v is not None})


def _function_denied(name: str, prof: DialectProfile | None = None) -> bool:
    lowered = name.strip().strip('"[]`').lower()
    if lowered in _DENY_EXACT or lowered.startswith(_DENY_PREFIX):
        return True
    if prof is None or not prof.denylist and not prof.strict:
        return False
    for pattern in prof.denied():
        if lowered == pattern or (pattern.endswith("*") and lowered.startswith(pattern[:-1])):
            return True
    return False


def _function_names(node: exp.Func) -> set[str]:
    names: set[str] = set()
    if isinstance(node, exp.Anonymous):
        names.add(node.name)
    else:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            names.add(node.sql_name())
        names.add(type(node).__name__)
        for key in getattr(node, "_sql_names", ()) or ():
            names.add(key)
    return {n.lower() for n in names if n}


# ---------------------------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------------------------


@dataclass
class _AssetIndex:
    """Case-aware lookup of the assets in scope."""

    dialect: str
    assets: list[str]
    by_key: dict[str, str] = field(default_factory=dict)  # exact "schema.table" -> canonical
    by_lower: dict[str, list[str]] = field(default_factory=dict)
    by_table: dict[str, list[str]] = field(default_factory=dict)  # table (lower) -> canonical assets

    @classmethod
    def build(cls, assets: Iterable[str], dialect: str) -> _AssetIndex:
        idx = cls(dialect=dialect, assets=[])
        for asset in assets:
            if "." not in asset:
                continue
            idx.assets.append(asset)
            idx.by_key[asset] = asset
            idx.by_lower.setdefault(asset.lower(), []).append(asset)
            idx.by_table.setdefault(asset.split(".", 1)[1].lower(), []).append(asset)
        return idx

    def _norm(self, ident: exp.Identifier | None) -> str:
        if ident is None:
            return ""
        name = ident.name
        if self.dialect == "tsql":
            return name.lower()
        return name if ident.quoted else name.lower()

    def resolve(self, table: exp.Table) -> str:
        name_ident = table.this if isinstance(table.this, exp.Identifier) else None
        db_ident = table.args.get("db")
        name = self._norm(name_ident)
        raw = table.sql(dialect=self.dialect)
        if db_ident is not None:
            key = f"{self._norm(db_ident)}.{name}"
            if key in self.by_key:
                return key
            matches = self.by_lower.get(key.lower(), [])
            if len(matches) == 1:
                return matches[0]
            raise _reject(
                f"Table {raw} is not in the authorized scope. Use one of: {', '.join(sorted(self.assets)) or '(none)'}.",
                table=raw,
            )
        matches = self.by_table.get(name.lower(), [])
        exact = [m for m in matches if m.split(".", 1)[1] == name]
        if len(exact) == 1:
            return exact[0]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _reject(
                f"Table name {raw} is ambiguous; qualify it with its schema: {', '.join(sorted(matches))}.",
                table=raw,
                candidates=sorted(matches),
            )
        raise _reject(
            f"Unknown table {raw}: it is not in the authorized scope. Use one of: "
            f"{', '.join(sorted(self.assets)) or '(none)'}.",
            table=raw,
        )


def _withheld(scope: DataScope, table: exp.Table) -> str | None:
    if not scope.withheld_assets:
        return None
    name, db = table.name.lower(), (table.db or "").lower()
    for asset, reason in scope.withheld_assets.items():
        schema_name, table_name = asset.lower().split(".", 1)
        if table_name == name and db in ("", schema_name):
            return reason
    return None


def _is_denied(column_key: str, denied: set[str], denied_any: set[str]) -> bool:
    lowered = column_key.lower()
    return lowered in denied or lowered.rsplit(".", 1)[-1] in denied_any


def _is_order_alias_ref(column: exp.Column) -> bool:
    """True when ``column`` stands alone as an ORDER BY item naming an output column of the
    enclosing SELECT / set operation. Inside an expression, Postgres resolves such a name to an
    *input* column instead, so those are not treated as alias references."""
    ordered = column.parent
    if not isinstance(ordered, exp.Ordered) or not isinstance(ordered.parent, exp.Order):
        return False
    owner = ordered.parent.parent
    if isinstance(owner, exp.Select):
        names = {e.alias for e in owner.expressions if isinstance(e, exp.Alias)}
    elif isinstance(owner, exp.SetOperation):
        names = set(owner.named_selects)
    else:
        return False
    return column.name in names


def _lookup_source(scope: Scope, name: str) -> object | None:
    current: Scope | None = scope
    while current is not None:
        if name in current.sources:
            return current.sources[name]
        current = current.parent
    return None


# ---------------------------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------------------------


def _parse(sql: str, dialect: str) -> exp.Expression:
    sqlglot_logger = logging.getLogger("sqlglot")
    previous = sqlglot_logger.level
    sqlglot_logger.setLevel(logging.ERROR)  # "falling back to Command" warnings are rejections here
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except ParseError as exc:
        first = exc.errors[0] if exc.errors else {}
        where = f" near line {first.get('line')}, column {first.get('col')}" if first.get("line") else ""
        desc = first.get("description") or str(exc).splitlines()[0]
        raise _reject(f"Could not parse the statement as {dialect} SQL{where}: {desc}.") from None
    except SqlglotError as exc:
        raise _reject(f"Could not parse the statement as {dialect} SQL: {str(exc).splitlines()[0]}.") from None
    finally:
        sqlglot_logger.setLevel(previous)
    statements = [s for s in statements if s is not None]
    if not statements:
        raise _reject("The statement is empty. Send a single SELECT query.")
    if len(statements) > 1:
        raise _reject(
            f"Exactly one statement is allowed, found {len(statements)}. Send a single SELECT query "
            "without extra ';'-separated statements."
        )
    root = statements[0]
    while isinstance(root, exp.Subquery | exp.Paren) and not root.alias:
        root = root.this
    return root


def _check_forbidden(root: exp.Expression) -> None:
    for node in root.walk():
        if _FORBIDDEN_NODES and isinstance(node, _FORBIDDEN_NODES):
            kind = type(node).__name__
            if isinstance(node, exp.Into):
                raise _reject("SELECT ... INTO is not allowed: the gateway is read-only. Remove the INTO clause.")
            if isinstance(node, exp.Lock):
                raise _reject("Row locking clauses (FOR UPDATE / FOR SHARE) are not allowed. Remove the locking clause.")
            if node is not root and isinstance(node, exp.Insert | exp.Update | exp.Delete | exp.Merge | exp.Returning):
                raise _reject(
                    f"Data-modifying statements ({kind.upper()}) are not allowed anywhere, including inside "
                    "CTEs. Only read-only SELECT queries are accepted."
                )
            if isinstance(node, exp.Command):
                word = str(node.this).upper()
                raise _reject(f"{word} statements are not allowed. Only read-only SELECT queries are accepted.")
            raise _reject(f"{kind.upper()} statements are not allowed. Only read-only SELECT queries are accepted.")
    if not isinstance(root, exp.Select | exp.SetOperation):
        raise _reject(
            f"Only read-only queries (SELECT, WITH ... SELECT, UNION/INTERSECT/EXCEPT) are allowed; got "
            f"{type(root).__name__.upper()}."
        )


def _check_functions(root: exp.Expression, prof: DialectProfile | None = None) -> None:
    for node in root.find_all(exp.Func):
        names = _function_names(node)
        denied = sorted(n for n in names if _function_denied(n, prof))
        if denied:
            raise _reject(f"Function {denied[0]}() is not allowed in governed queries.", function=denied[0])
    for dot in root.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            fname = dot.expression.name or type(dot.expression).__name__
            raise _reject(
                f"Schema-qualified function calls ({dot.sql()}) are not allowed; call built-in functions unqualified.",
                function=fname,
            )
    for node in root.walk():
        if isinstance(node, exp.Dot):
            raise _reject(
                f"Field access expression {node.sql()} is not supported. Reference columns as table.column."
            )


_NAMESPACED_CALL = re.compile(r"^([A-Za-z_][\w$]*\.[A-Za-z_][\w$.]*)\(")
_PATH_CHARS = set("/\\:@~$*")
_BIND_NODES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in ("Parameter", "Placeholder", "SessionParameter")
    if isinstance(getattr(exp, name, None), type)
)
_TIME_TRAVEL_NODES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in ("Version", "HistoricalData", "Changes") if isinstance(getattr(exp, name, None), type)
)


def _check_strict(root: exp.Expression, prof: DialectProfile) -> None:
    """Fail closed on calls and syntax the gateway cannot certify."""
    for node in root.walk():
        if _BIND_NODES and isinstance(node, _BIND_NODES):
            raise _reject(
                f"Parameters and session variables ({node.sql(dialect=prof.name)}) are not allowed in governed "
                "queries; write literal values."
            )
        if _TIME_TRAVEL_NODES and isinstance(node, _TIME_TRAVEL_NODES):
            raise _reject("Time travel (AS OF / AT / BEFORE / CHANGES) is not allowed; query the current table.")
        if isinstance(node, exp.Func):
            names = _function_names(node)
            if isinstance(node, exp.Anonymous):
                name = (node.name or "").lower()
                if "." in name or name not in prof.allowed_anonymous:
                    raise _reject(
                        f"Function {node.name}() is not a known built-in of {prof.name}; user-defined, external and "
                        "namespaced functions are not allowed in governed queries.",
                        function=node.name,
                    )
            else:
                namespaced = _NAMESPACED_CALL.match(node.sql(dialect=prof.name))
                if any("." in n for n in names) or namespaced:
                    head = namespaced.group(1) if namespaced else next(n for n in names if "." in n)
                    raise _reject(f"Namespaced function {head}() is not allowed in governed queries.", function=head)
        if type(node).__name__ == "MatchRecognize":
            raise _reject("MATCH_RECOGNIZE is not supported in governed queries.")
        if isinstance(node, exp.Table):
            for key in ("version", "when", "changes", "format", "pattern"):
                if node.args.get(key) is not None:
                    raise _reject(
                        f"Table {node.sql(dialect=prof.name)} uses {key.upper()} syntax (time travel, stages or file "
                        "formats), which is not allowed. Read the authorized table itself."
                    )
            name = node.name or ""
            db = node.args.get("db")
            db_name = db.name.lower() if isinstance(db, exp.Identifier) else ""
            if (any(ch in _PATH_CHARS for ch in name) or db_name in prof.path_schemas
                    or (prof.name in ("duckdb", "databricks") and "." in name)):
                raise _reject(
                    f"{node.sql(dialect=prof.name)} looks like a file path, stage, wildcard or metadata table, not an "
                    "authorized table. Read authorized tables only.",
                    table=node.sql(dialect=prof.name),
                )


def _check_sources(root: exp.Expression) -> None:
    for node in root.walk():
        if _UNSUPPORTED_SOURCES and isinstance(node, _UNSUPPORTED_SOURCES):
            kind = type(node).__name__.upper()
            raise _reject(
                f"{kind} is not supported in governed queries. Read from authorized tables with plain FROM/JOIN."
            )
        if isinstance(node, exp.Join) and (node.args.get("method") or "").upper() == "NATURAL":
            raise _reject("NATURAL JOIN is not supported. Use an explicit JOIN ... ON condition.")
        if isinstance(node, exp.Join):
            predicate = node.args.get("on")
            if _single_row_cross_join(node):
                continue
            if (node.args.get("kind") or "").upper() == "CROSS" or (
                predicate is None and node.args.get("using") is None
            ) or (predicate is not None and not any(predicate.find_all(exp.Column))):
                raise _reject("Unconditioned joins are not allowed. Add a join key with ON or USING.")
        if isinstance(node, exp.Table):
            if node.args.get("hints"):
                raise _reject("Table hints are not allowed in governed queries.")
            if not isinstance(node.this, exp.Identifier):
                raise _reject(
                    f"Table-valued functions and dotted table expressions ({node.sql()}) are not allowed. "
                    "Read from authorized tables only."
                )
            if node.args.get("catalog") is not None:
                raise _reject(
                    f"Three-part names ({node.sql()}) are not allowed. Reference tables as schema.table.",
                    table=node.sql(),
                )


def _single_row_cross_join(join: exp.Join) -> bool:
    """An explicit CROSS JOIN to a derived table that provably returns one row (aggregates only and no
    GROUP BY, or LIMIT 1): the percent-of-total shape. It cannot multiply rows, so it is allowed; comma
    joins, ON TRUE and CROSS JOIN to a table stay refused (owner decision 2026-09-26, P7-15)."""
    if (join.args.get("kind") or "").upper() != "CROSS" or join.args.get("on") is not None or join.args.get("using"):
        return False
    sub = join.this
    if not isinstance(sub, exp.Subquery) or not isinstance(sub.this, exp.Select):
        return False
    select = sub.this
    limit = select.args.get("limit")
    if limit is not None and _literal_int(limit.expression) == 1:
        return True
    if select.args.get("group") is not None or any(select.find_all(exp.Window)):
        return False
    return bool(select.expressions) and all(any(e.find_all(exp.AggFunc)) for e in select.expressions)


_JOIN_COMPARISONS: tuple[type, ...] = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ)


def _join_keyed(predicate: exp.Expression, joined: str) -> bool:
    """True when ``predicate`` cannot hold for every row pair: some conjunct compares a column of the
    joined source with a column of another source (every disjunct must be keyed on its own). This
    refuses ``ON TRUE OR a.k = b.k`` and ``ON b.k = b.k`` as well as ``ON TRUE`` (P7-15)."""
    if isinstance(predicate, exp.Paren):
        return _join_keyed(predicate.this, joined)
    if isinstance(predicate, exp.And):
        return _join_keyed(predicate.this, joined) or _join_keyed(predicate.expression, joined)
    if isinstance(predicate, exp.Or):
        return _join_keyed(predicate.this, joined) and _join_keyed(predicate.expression, joined)
    if isinstance(predicate, _JOIN_COMPARISONS):
        left = {c.table for c in predicate.this.find_all(exp.Column)}
        right = {c.table for c in predicate.expression.find_all(exp.Column)}
        if not left or not right or "" in left | right:
            return False
        return (joined in left and bool(right - {joined})) or (joined in right and bool(left - {joined}))
    return False


def _check_join_keys(qualified: exp.Expression) -> None:
    """After qualification (USING is expanded to ON and every column names its source), each join
    must be keyed across its two sides."""
    for join in qualified.find_all(exp.Join):
        if _single_row_cross_join(join):
            continue
        predicate = join.args.get("on")
        joined = join.this.alias_or_name if isinstance(join.this, exp.Expression) else ""
        if predicate is None or not joined or not _join_keyed(predicate, joined):
            raise _reject(
                "Unconditioned joins are not allowed. Add a join key with ON or USING that compares a column "
                "of the joined table with a column of another table."
            )


def _classify_tables(root: exp.Expression) -> list[exp.Table]:
    """Return the Table nodes that reference physical tables (not CTEs). Fail closed on anything
    the scope analysis cannot account for."""
    try:
        scopes = traverse_scope(root)
    except SqlglotError as exc:
        raise _reject(f"Could not analyse the query structure: {str(exc).splitlines()[0]}.") from None
    real: dict[int, exp.Table] = {}
    ctes: set[int] = set()
    for scope in scopes:
        for table in scope.tables:
            source = scope.sources.get(table.alias_or_name)
            if source is table:
                real[id(table)] = table
            elif isinstance(source, Scope) and not table.args.get("db"):
                ctes.add(id(table))
            else:
                raise _reject(f"Could not resolve table reference {table.sql()}.")
    for table in root.find_all(exp.Table):
        if id(table) not in real and id(table) not in ctes:
            raise _reject(f"Table reference {table.sql()} is in an unsupported position.")
    return list(real.values())


# ---------------------------------------------------------------------------------------------
# Row cap
# ---------------------------------------------------------------------------------------------


def _literal_int(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except ValueError:
            return None
    return None


def _apply_row_cap(root: exp.Expression, cap: int, dialect: str) -> exp.Expression:
    capped = root.copy()
    limit = capped.args.get("limit")
    has_fetch = capped.args.get("fetch") is not None or isinstance(limit, exp.Fetch)
    if dialect == "tsql" and (not isinstance(capped, exp.Select) or capped.args.get("offset") is not None or has_fetch):
        return exp.select("*").from_(capped.subquery("_q")).limit(cap)
    if has_fetch:
        return exp.select("*").from_(capped.subquery("_q")).limit(cap)
    if limit is None:
        return capped.limit(cap)
    current = _literal_int(limit.expression if isinstance(limit, exp.Limit) else None)
    if current is None:
        return exp.select("*").from_(capped.subquery("_q")).limit(cap)
    limit.set("expression", exp.Literal.number(min(current, cap)))
    return capped


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


class _DialectMismatch(Exception):
    pass


def _fold(name: str, prof: DialectProfile) -> str:
    if prof.fold == "insensitive":
        return name.lower()
    if prof.fold == "upper":
        return name.upper()
    return name


def validate_sql(scope: DataScope, sql: str, *, max_rows: int) -> ValidatedSQL:
    """Validate ``sql`` against ``scope`` and return executable, row-capped SQL.

    The executable SQL requests ``max_rows + 1`` rows so the caller can detect truncation.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise _reject("The statement is empty. Send a single SELECT query.")
    if len(sql) > MAX_SQL_LENGTH:
        raise _reject(f"The statement is too long ({len(sql)} characters, limit {MAX_SQL_LENGTH}).")
    if "\x00" in sql:
        raise _reject("The statement contains a NUL byte.")
    if max_rows < 1:
        raise _reject("max_rows must be at least 1.")
    sources = sorted(set(scope.asset_sources.values()) | set(scope.source_ids))
    dialects: list[str] = []
    for source_id in sources:
        d = scope.source_dialects.get(source_id, DEFAULT_DIALECT)
        if d not in dialects:
            dialects.append(d)
    if not dialects:
        dialects = [DEFAULT_DIALECT]
    dialects.sort(key=lambda d: d != DEFAULT_DIALECT)
    first_error: SQLRejected | None = None
    for dialect in dialects:
        try:
            return _validate_in_dialect(scope, sql, dialect, max_rows, strict_dialect=len(dialects) > 1)
        except _DialectMismatch:
            continue
        except SQLRejected as exc:
            if len(dialects) == 1:
                raise
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
    raise _reject("The query could not be matched to a single authorized source dialect.")


def federation_dialect(scope: DataScope) -> str:
    """The dialect a cross-source statement over ``scope`` is written in: trino when every source of the scope is
    a trino source (the statement then runs on Trino across catalogs, TRN-004), otherwise duckdb (legs pulled
    through the gateway and joined locally)."""
    sources = sorted(set(scope.source_ids) | set(scope.asset_sources.values()))
    dialects = {scope.source_dialects.get(s, DEFAULT_DIALECT) for s in sources}
    if len(dialects) == 1 and next(iter(dialects)) in IN_PLACE_FEDERATION:
        return next(iter(dialects))
    return FEDERATION_DIALECT


def validate_federated_sql(scope: DataScope, sql: str, *, max_rows: int) -> ValidatedSQL:
    """Validate a cross-source statement (P4-E03). It is written in the federation dialect
    (``federation_dialect``: duckdb, or trino when every source is a Trino catalog)
    and may join assets of several sources of the scope; every other rule is the single-source one,
    applied per asset: each table must be an authorized asset of its own source, each column must be
    known for that asset, and a column denied in any source is rejected wherever it appears. The
    result names every source and asset so the gateway can extract each leg under its own scope."""
    if not isinstance(sql, str) or not sql.strip():
        raise _reject("The statement is empty. Send a single SELECT query.")
    if len(sql) > MAX_SQL_LENGTH:
        raise _reject(f"The statement is too long ({len(sql)} characters, limit {MAX_SQL_LENGTH}).")
    if "\x00" in sql:
        raise _reject("The statement contains a NUL byte.")
    if max_rows < 1:
        raise _reject("max_rows must be at least 1.")
    return _validate_in_dialect(scope, sql, federation_dialect(scope), max_rows, strict_dialect=False, federated=True)


def _validate_in_dialect(
    scope: DataScope, sql: str, dialect: str, max_rows: int, *, strict_dialect: bool, federated: bool = False
) -> ValidatedSQL:
    if dialect not in SUPPORTED_DIALECTS:
        raise _reject(f"Source dialect {dialect!r} is not supported by the gateway.")
    prof = profile(dialect)
    root = _parse(sql, dialect)
    _check_forbidden(root)
    _check_functions(root, prof)
    if prof.strict:
        _check_strict(root, prof)
    _check_sources(root)
    tables = _classify_tables(root)
    if not tables:
        raise _reject("The query must read from at least one authorized table.")

    index = _AssetIndex.build(scope.assets, dialect)
    default_source = scope.source_ids[0] if len(set(scope.source_ids)) == 1 else None
    referenced: list[str] = []
    resolved: list[tuple[exp.Table, str]] = []
    for table in tables:
        try:
            asset = index.resolve(table)
        except SQLRejected:
            withheld = _withheld(scope, table)
            if withheld:
                raise _reject(f"Table {table.sql(dialect=dialect)} is withheld from you by a row filter: {withheld}.",
                              table=table.sql()) from None
            raise
        schema_name, table_name = asset.split(".", 1)
        if prof.fold == "upper":
            schema_name, table_name = schema_name.upper(), table_name.upper()
        table.set("db", exp.to_identifier(schema_name, quoted=True))
        table.set("this", exp.to_identifier(table_name, quoted=True))
        resolved.append((table, asset))
        if asset not in referenced:
            referenced.append(asset)
    if scope.row_filters:
        # Row-level security (P7-02): every read of a filtered asset goes through its filter, whichever
        # path wrote the statement; compiled SQL that already carries the exact wrapper passes unchanged.
        from analystos.governance.row_filters import wrap_tables

        wrap_tables(resolved, scope, dialect, prof.fold)

    asset_sources = {}
    for asset in referenced:
        source_id = scope.asset_sources.get(asset) or default_source
        if source_id is None:
            raise _reject(f"Asset {asset} is not bound to an authorized source.", asset=asset)
        asset_sources[asset] = source_id
    distinct_sources = sorted(set(asset_sources.values()))
    if federated:
        duplicated = sorted(a for a in referenced if scope.assets.count(a) > 1)
        if duplicated:
            raise _reject(
                f"{', '.join(duplicated)} exists in more than one source of the scope, so a federated query cannot "
                "tell them apart. Narrow the run to one of those sources.",
                assets=duplicated,
            )
        source_id = FEDERATED_SOURCE
    else:
        if len(distinct_sources) > 1:
            by_source = {s: sorted(a for a, v in asset_sources.items() if v == s) for s in distinct_sources}
            raise _reject(
                "Cross-source queries are not supported: all tables in one query must come from the same "
                f"source. This query mixes {by_source}. Query each source separately, or use the federated "
                "runner of a cross-source run.",
                sources=distinct_sources,
            )
        source_id = distinct_sources[0]
        source_dialect = scope.source_dialects.get(source_id, DEFAULT_DIALECT)
        if source_dialect != dialect:
            if strict_dialect:
                raise _DialectMismatch()
            raise _reject(f"Source {source_id} uses the {source_dialect} dialect; write the query in {source_dialect}.")

    # Build the schema for qualification from the column metadata of the referenced assets.
    mapping: dict[str, dict[str, dict[str, str]]] = {}
    known_columns: dict[str, dict[str, str]] = {}
    for asset in referenced:
        cols = scope.columns.get(asset)
        if not cols:
            raise _reject(
                f"No column metadata is available for {asset}; the asset must be discovered before it can be queried.",
                asset=asset,
            )
        schema_name, table_name = asset.split(".", 1)
        if prof.fold == "insensitive":
            schema_name, table_name = schema_name.lower(), table_name.lower()
        elif prof.fold == "upper":
            schema_name, table_name = schema_name.upper(), table_name.upper()
        col_map = {_fold(c, prof): "text" for c in cols}
        mapping.setdefault(schema_name, {})[table_name] = col_map
        known_columns[f"{schema_name}.{table_name}"] = {c.lower(): c for c in cols}
        known_columns.setdefault(asset.lower(), {c.lower(): c for c in cols})
    had_star = any(True for _ in root.find_all(exp.Star))
    try:
        schema = MappingSchema(mapping, dialect=dialect, normalize=False)
        qualified = qualify(
            root,
            dialect=dialect,
            schema=schema,
            infer_schema=False,
            validate_qualify_columns=True,
            expand_stars=True,
            identify=True,
        )
    except OptimizeError as exc:
        msg = str(exc).splitlines()[0]
        hint = "; ".join(f"{a}: {', '.join(scope.columns.get(a, []))}" for a in referenced)
        raise _reject(f"{msg}. Known columns — {hint}.") from None
    except SqlglotError as exc:
        raise _reject(f"Could not qualify the query: {str(exc).splitlines()[0]}.") from None

    _check_join_keys(qualified)

    denied = {d.lower() for d in scope.denied_columns if not d.startswith("*.")}
    denied_any = {d[2:].lower() for d in scope.denied_columns if d.startswith("*.")}
    referenced_columns: list[str] = []
    try:
        post_scopes = traverse_scope(qualified)
    except SqlglotError as exc:
        raise _reject(f"Could not analyse the query structure: {str(exc).splitlines()[0]}.") from None

    for node in qualified.walk():
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            raise _reject(
                "Star expressions are only allowed as SELECT * / t.* in the select list or COUNT(*). "
                "List the columns you need explicitly."
            )
        if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
            raise _reject("Qualified stars (t.*) are only allowed in the select list. List the columns explicitly.")

    checked: set[int] = set()
    for sc in post_scopes:
        for column in sc.columns:
            checked.add(id(column))
            if isinstance(column.this, exp.Star):
                continue
            table_alias = column.table
            if not table_alias:
                # After qualification the only legitimate unqualified column is an ORDER BY
                # reference to a select-list alias. Anything else (notably a whole-row reference
                # to a table alias, e.g. row_to_json(t)) is rejected.
                if _is_order_alias_ref(column) and _lookup_source(sc, column.name) is None:
                    continue
                if _lookup_source(sc, column.name) is not None:
                    raise _reject(
                        f"Whole-row reference to {column.name} is not allowed. Reference individual columns.",
                        column=column.name,
                    )
                raise _reject(f"Could not resolve column {column.sql(dialect=dialect)}.", column=column.name)
            source = _lookup_source(sc, table_alias)
            if source is None:
                raise _reject(f"Could not resolve column {column.sql(dialect=dialect)}.", column=column.name)
            if isinstance(source, Scope):
                continue  # derived table / CTE output; its own scope is checked separately
            if not isinstance(source, exp.Table):
                raise _reject(f"Could not resolve column {column.sql(dialect=dialect)}.", column=column.name)
            table_key = f"{source.db}.{source.name}"
            cols = known_columns.get(table_key) or known_columns.get(table_key.lower())
            if cols is None:
                raise _reject(f"Column {column.sql(dialect=dialect)} references an unknown table.")
            canonical = cols.get(column.name.lower())
            if canonical is None:
                raise _reject(
                    f"Unknown column {column.name} on {table_key}. Known columns: {', '.join(cols.values())}.",
                    column=column.name,
                )
            asset_key = next((a for a in referenced if a.lower() == table_key.lower()), table_key)
            column_key = f"{asset_key}.{canonical}"
            if _is_denied(column_key, denied, denied_any):
                via = " (SELECT * / t.* expands to it; list the permitted columns explicitly)" if had_star else ""
                raise _reject(
                    f"Column {column_key} is restricted by policy and cannot be referenced{via}. "
                    "Remove it from the query.",
                    column=column_key,
                )
            if column_key not in referenced_columns:
                referenced_columns.append(column_key)

    # Fail closed on any column node the scope analysis did not account for (sqlglot omits, for
    # example, bare table aliases used as whole-row values such as row_to_json(t)).
    source_names = {t.alias_or_name for t in qualified.find_all(exp.Table)}
    source_names |= {cte.alias for cte in qualified.find_all(exp.CTE)}
    source_names |= {sq.alias for sq in qualified.find_all(exp.Subquery) if sq.alias}
    table_column = getattr(exp, "TableColumn", None)
    if table_column is not None:
        for node in qualified.find_all(table_column):
            raise _reject(
                f"Whole-row reference to {node.name} is not allowed. Reference individual columns.",
                column=node.name,
            )
    for column in qualified.find_all(exp.Column):
        if id(column) in checked:
            continue
        if not column.table and _is_order_alias_ref(column):
            continue
        if not column.table and column.name in source_names:
            raise _reject(
                f"Whole-row reference to {column.name} is not allowed. Reference individual columns.",
                column=column.name,
            )
        raise _reject(
            f"Could not resolve column {column.sql(dialect=dialect)}. Qualify columns used inside ORDER BY "
            "expressions with their table (t.col) or order by a select-list alias on its own.",
            column=column.name,
        )

    normalized = qualified.sql(dialect=dialect, comments=False)
    fingerprint = hashlib.sha256(f"{dialect}\n{normalized}".encode()).hexdigest()
    executable = _apply_row_cap(qualified, max_rows + 1, dialect).sql(dialect=dialect, comments=False)
    return ValidatedSQL(
        original_sql=sql,
        executable_sql=executable,
        dialect=dialect,
        source_id=source_id,
        referenced_assets=sorted(referenced),
        referenced_columns=sorted(referenced_columns),
        fingerprint=fingerprint,
        asset_sources={a: asset_sources[a] for a in sorted(referenced)},
    )


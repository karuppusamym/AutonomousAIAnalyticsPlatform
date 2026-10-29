"""Pre-execution cost estimates per dialect (S02-T4).

The gateway asks the engine that will run a validated statement what it expects to scan, before running it,
and refuses a statement whose estimate exceeds the budget (`budget.max_scan_bytes`):

    postgres   EXPLAIN (FORMAT JSON, VERBOSE): relations read whole count their on-disk size, other scan nodes
               rows x width (analytics DB and PG pushdown)
    mysql      EXPLAIN FORMAT=JSON: rows_examined_per_scan x row width (data_read_per_join when given)
    bigquery   dry run: totalBytesProcessed
    snowflake  EXPLAIN USING JSON: GlobalStats.bytesAssigned
    trino      EXPLAIN (FORMAT JSON): every scan node's (TableScan, ScanFilter, ScanProject...) estimated output
               size, summed; rows from the root. A scan without statistics (NaN) makes the plan unreadable, so a
               budget fails closed rather than passing an unknown scan
    otherwise  row cap only (basis `row_cap`, bytes unknown: the row cap is then the only guard)

The parsers are pure functions over the engine's plan output, so they are unit-tested on recorded plans;
only PostgreSQL and Trino run live here (MySQL, BigQuery and Snowflake are staged kinds: their queries run on the
analytics PostgreSQL, so the postgres estimator applies to them until they push down).
"""
from __future__ import annotations

import json
from typing import Any

from analystos.gateway.types import CostEstimate

SCAN_NODES = {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Tid Scan", "Function Scan",
              "Values Scan", "CTE Scan", "Subquery Scan", "Foreign Scan", "Sample Scan", "Tid Range Scan"}


def _doc(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


SEQ_SCANS = {"Seq Scan", "Bitmap Heap Scan", "Sample Scan", "Tid Range Scan"}


def _top(raw_plan: Any) -> dict[str, Any]:
    doc = _doc(raw_plan)
    return ((doc[0] if isinstance(doc, list) and doc else doc) or {}).get("Plan") or {}


def _walk(node: dict[str, Any]):  # noqa: ANN202
    yield node
    for child in node.get("Plans") or []:
        yield from _walk(child)


def _relation(node: dict[str, Any]) -> str:
    return f"{node.get('Schema') or ''}.{node.get('Relation Name') or ''}".strip(".")


def seq_scanned_relations(raw_plan: Any) -> list[str]:
    """Relations a plan reads whole (sequential / bitmap heap scans), as "schema.table" (EXPLAIN VERBOSE)."""
    return sorted({_relation(n) for n in _walk(_top(raw_plan)) if n.get("Node Type") in SEQ_SCANS
                   and n.get("Relation Name")})


def postgres_estimate(raw_plan: Any, relation_bytes: dict[str, int] | None = None) -> CostEstimate:
    """`EXPLAIN (FORMAT JSON, VERBOSE)` output -> estimated output rows and scanned bytes.

    A scan node's Plan Rows counts rows *after* its filter, so for a relation read whole (sequential or
    bitmap heap scan) the scanned bytes are the relation's size when the gateway looked it up
    (``relation_bytes``); other scans count rows x width."""
    top = _top(raw_plan)
    sizes = relation_bytes or {}
    scanned = 0
    for node in _walk(top):
        if node.get("Node Type") in SCAN_NODES and node.get("Relation Name"):
            whole = sizes.get(_relation(node)) if node.get("Node Type") in SEQ_SCANS else None
            scanned += whole if whole is not None else int(node.get("Plan Rows") or 0) * int(node.get("Plan Width") or 0)
    rows = int(top.get("Plan Rows") or 0)
    if not scanned:  # no relation scan (VALUES, functions): the output itself
        scanned = rows * int(top.get("Plan Width") or 0)
    return CostEstimate(rows=rows, bytes=scanned, basis="explain")


def relation_size_sql(relations: list[str]) -> str:
    """One catalog statement for the on-disk size of each relation (names passed as quoted literals)."""
    def lit(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    pairs = ", ".join(f"({lit(r.split('.', 1)[0])}, {lit(r.split('.', 1)[1])})" for r in relations if "." in r)
    return ("SELECT n.nspname || '.' || c.relname, pg_relation_size(c.oid) FROM pg_class c "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE (n.nspname, c.relname) IN ({pairs})")


def mysql_estimate(raw_plan: Any) -> CostEstimate:
    """`EXPLAIN FORMAT=JSON` (MySQL 8): sum over tables of data_read_per_join, else rows x 100 B."""
    doc = _doc(raw_plan)
    total_bytes = 0
    rows = 0

    def walk(node: Any) -> None:
        nonlocal total_bytes, rows
        if isinstance(node, dict):
            if "table_name" in node and ("rows_examined_per_scan" in node or "cost_info" in node):
                examined = int(node.get("rows_examined_per_scan") or 0)
                rows += examined
                read = (node.get("cost_info") or {}).get("data_read_per_join")
                total_bytes += _mysql_size(read) if read else examined * 100
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return CostEstimate(rows=rows, bytes=total_bytes, basis="explain")


def _mysql_size(text: Any) -> int:
    """MySQL prints sizes as `1K`, `2M`, `3G` or a plain number."""
    s = str(text).strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}.get(s[-1:], 1)
    try:
        return int(float(s[:-1] if mult != 1 else s) * mult)
    except ValueError:
        return 0


def bigquery_estimate(dry_run: Any) -> CostEstimate:
    """A dry-run job (`statistics.totalBytesProcessed` or the client's `total_bytes_processed`)."""
    doc = _doc(dry_run) if not hasattr(dry_run, "total_bytes_processed") else None
    if doc is None:
        value = getattr(dry_run, "total_bytes_processed", None)
    else:
        value = ((doc.get("statistics") or {}).get("totalBytesProcessed") or doc.get("totalBytesProcessed"))
    return CostEstimate(bytes=int(value) if value is not None else None, basis="dry_run")


def snowflake_estimate(raw_plan: Any) -> CostEstimate:
    """`EXPLAIN USING JSON`: GlobalStats.bytesAssigned (partitions scanned as context)."""
    doc = _doc(raw_plan)
    stats = (doc or {}).get("GlobalStats") or {}
    value = stats.get("bytesAssigned")
    return CostEstimate(bytes=int(value) if value is not None else None, basis="explain")


def trino_estimate(raw_plan: Any) -> CostEstimate:
    """`EXPLAIN (FORMAT JSON)` (fragments of nodes with `estimates` and `children`) -> rows and scanned bytes."""
    import math

    doc = _doc(raw_plan)
    if not isinstance(doc, dict) or not doc:
        return CostEstimate(basis="explain_unreadable")

    def walk(node: dict[str, Any]):  # noqa: ANN202
        yield node
        for child in node.get("children") or []:
            yield from walk(child)

    def first(node: dict[str, Any], key: str) -> float | None:
        estimates = node.get("estimates") or []
        value = estimates[0].get(key) if estimates and isinstance(estimates[0], dict) else None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    scanned: list[float | None] = []
    for fragment in doc.values():
        if isinstance(fragment, dict):
            scanned += [first(n, "outputSizeInBytes") for n in walk(fragment)
                        if str(n.get("name") or "").startswith(("TableScan", "Scan"))]
    root = doc.get("0") if isinstance(doc.get("0"), dict) else None
    rows = first(root, "outputRowCount") if root else None
    if not scanned or any(v is None for v in scanned):
        return CostEstimate(rows=int(rows) if rows is not None else None, basis="explain_unreadable")
    return CostEstimate(rows=int(rows) if rows is not None else None, bytes=int(sum(scanned)),  # type: ignore[arg-type]
                        basis="explain")


def row_cap_estimate(max_rows: int) -> CostEstimate:
    return CostEstimate(rows=max_rows, bytes=None, basis="row_cap")


# dialect -> (statement wrapper, parser). The gateway runs the wrapper through the same read-only path.
EXPLAINERS: dict[str, tuple[str, Any]] = {
    "postgres": ("EXPLAIN (FORMAT JSON, VERBOSE) {sql}", postgres_estimate),
    "mysql": ("EXPLAIN FORMAT=JSON {sql}", mysql_estimate),
    "snowflake": ("EXPLAIN USING JSON {sql}", snowflake_estimate),
}


def over_budget(estimate: CostEstimate, max_scan_bytes: int | None) -> bool:
    return bool(max_scan_bytes) and estimate.bytes is not None and estimate.bytes > int(max_scan_bytes or 0)

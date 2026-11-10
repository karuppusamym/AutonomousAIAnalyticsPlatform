"""Sensitive tags along column lineage (review 2026-11-05 P1-5). Pure: no database, no model.

Column edges come from every lineage we already compute: parsed SQL (`sql.py`), dbt manifests (`dbt.py`), recipe IR
lineage (`recipes/lineage.py`) and the OpenLineage column facets build jobs store (`build/lineage.py`). They are
normalised to `schema.table` names so the catalog can be joined on them.

`propagate` answers one question: which column receives values from a column tagged pii / restricted / sensitive but
does not carry that tag itself? Only the adding direction exists: a gap is a tag to add, never one to remove. Row
filters and `*` expansion do not move values into a column, so FILTERED / TABLE_STAR edges are not followed.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

PROPAGATED_TAGS = frozenset({"pii", "restricted", "sensitive"})  # the same set recipes copy onto outputs
MAX_EDGES = 50_000
MAX_GAPS = 500
_SKIPPED = {"FILTERED", "TABLE_STAR", "FILTER", "JOIN", "GROUP_BY", "SORT", "WINDOW"}
# How sure we are that values (not just a fact about them) reached the target: a copy, a function of the value, an
# aggregate of it. The weakest hop on the path decides.
_STRENGTH = {"DIRECT": 0.95, "IDENTITY": 0.95, "DERIVED": 0.8, "TRANSFORMATION": 0.8, "AGGREGATED": 0.6, "AGGREGATION": 0.6}

Col = tuple[str, str]  # (schema.table, column), lower case


def norm_asset(name: str | None) -> str:
    """`db.schema.table`, `"Schema"."Table"` or `schema.table` -> `schema.table` (lower case, unquoted)."""
    parts = [p.strip().strip('"`[]').lower() for p in str(name or "").split(".") if p.strip()]
    return ".".join(parts[-2:])


def _col(asset: str | None, column: str | None) -> Col | None:
    a, c = norm_asset(asset), str(column or "").strip().strip('"`[]').lower()
    return (a, c) if a and c else None


@dataclass(frozen=True, slots=True)
class ColumnEdge:
    source: Col
    target: Col
    transformation: str
    origin: str  # recipe_run:<id> | build_job:<id> | dbt:<unique_id> | sql:<hash>

    def as_dict(self) -> dict[str, Any]:
        return {"from": f"{self.source[0]}.{self.source[1]}", "to": f"{self.target[0]}.{self.target[1]}",
                "transformation": self.transformation, "origin": self.origin}


def _edge(src: Col | None, dst: Col | None, transformation: str, origin: str) -> ColumnEdge | None:
    t = str(transformation or "DIRECT").upper()
    if src is None or dst is None or src == dst or t in _SKIPPED:
        return None
    return ColumnEdge(src, dst, t, origin)


# ------------------------------------------------------------------------------------ adapters
def edges_from_sql(sql: str, dialect: str = "postgres", *, catalog: dict[str, Any] | None = None,
                   target: str | None = None) -> list[ColumnEdge]:
    """A CREATE/INSERT ... SELECT (or a SELECT with `target`): the parser follows CTEs and subqueries."""
    from analystos.evidence.lineage.sql import parse_lineage

    result = parse_lineage(sql, dialect, catalog=catalog)
    origin = f"sql:{result.sql_hash[:16]}"
    out = []
    for e in result.edges:
        if not e.source_resolved:
            continue
        edge = _edge(_col(e.source_table, e.source_column), _col(target or e.target_table, e.target_column),
                     e.transformation_type, origin)
        if edge:
            out.append(edge)
    return out


def edges_from_dbt_manifest(manifest: dict[str, Any], dialect: str = "postgres", *,
                            catalog: dict[str, Any] | None = None) -> list[ColumnEdge]:
    """Column edges between dbt resources, named by the relation each one builds (`schema.alias`)."""
    from analystos.evidence.lineage.dbt import manifest_lineage, resources_from_manifest

    resources = resources_from_manifest(manifest)

    def relation(uid: str) -> str:
        r = resources.get(uid)
        if r is None:
            return ""
        return norm_asset(r.relation_name) or norm_asset(".".join(p for p in (r.schema_name, r.name) if p))

    out = []
    for e in manifest_lineage(manifest, dialect, catalog=catalog)["column_edges"]:
        edge = _edge(_col(relation(e["source_unique_id"]), e["source_column"]),
                     _col(relation(e["target_unique_id"]), e["target_column"]), e["transformation_type"],
                     f"dbt:{e['target_unique_id']}")
        if edge:
            out.append(edge)
    return out


def edges_from_recipe(lineage: list[dict[str, Any]], outputs: Mapping[str, Any], origin: str) -> list[ColumnEdge]:
    """A recipe run's IR lineage (`recipe_run.lineage`) onto the tables its outputs were written to."""
    out = []
    for entry in lineage or []:
        table = (outputs.get(entry.get("output")) or {}).get("table") if isinstance(outputs, Mapping) else None
        if not table:
            continue
        for column, refs in (entry.get("columns") or {}).items():
            for r in refs or []:
                edge = _edge(_col(r.get("asset"), r.get("column")), _col(table, column), r.get("subtype") or "IDENTITY",
                             origin)
                if edge:
                    out.append(edge)
    return out


def edges_from_openlineage(events: list[dict[str, Any]], origin: str) -> list[ColumnEdge]:
    """ColumnLineageDatasetFacet fields of completed OpenLineage events (build jobs, recipe runs)."""
    out = []
    for ev in events or []:
        if str(ev.get("eventType") or "").upper() != "COMPLETE":
            continue
        for ds in ev.get("outputs") or []:
            fields = (((ds.get("facets") or {}).get("columnLineage") or {}).get("fields")) or {}
            for column, spec in fields.items():
                for f in (spec or {}).get("inputFields") or []:
                    sub = next((t.get("subtype") for t in f.get("transformations") or [] if t.get("type") == "DIRECT"),
                               None) if f.get("transformations") else "IDENTITY"
                    if sub is None:  # INDIRECT only: decides rows, carries no values
                        continue
                    edge = _edge(_col(f.get("name"), f.get("field")), _col(ds.get("name"), column), sub, origin)
                    if edge:
                        out.append(edge)
    return out


# ------------------------------------------------------------------------------------ propagation
@dataclass
class Gap:
    """`target` receives values of a tagged column but lacks `missing`. `path` is the shortest chain of edges."""

    target: Col
    missing: list[str]
    present: list[str]
    carried_from: Col
    path: list[ColumnEdge] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return round(min((_STRENGTH.get(e.transformation, 0.7) for e in self.path), default=0.7), 2)

    def as_dict(self) -> dict[str, Any]:
        return {"asset": self.target[0], "column": self.target[1], "missing": self.missing, "present": self.present,
                "carried_from": {"asset": self.carried_from[0], "column": self.carried_from[1]},
                "hops": len(self.path), "confidence": self.confidence, "path": [e.as_dict() for e in self.path]}


def _graph(edges: Iterable[ColumnEdge]) -> dict[Col, list[ColumnEdge]]:
    adj: dict[Col, list[ColumnEdge]] = {}
    seen: set[tuple[Col, Col]] = set()
    for i, e in enumerate(edges):
        if i >= MAX_EDGES:
            break
        if (e.source, e.target) not in seen:
            seen.add((e.source, e.target))
            adj.setdefault(e.source, []).append(e)
    for v in adj.values():
        v.sort(key=lambda e: (e.target, e.origin))
    return adj


def propagate(edges: Iterable[ColumnEdge], tags: Mapping[Col, Iterable[str]]) -> list[Gap]:
    """Multi-source breadth-first search per tag, so each gap carries its shortest witness path. Cycles are fine
    (a column is reached once per tag). Deterministic: inputs are sorted before the walk."""
    adj = _graph(edges)
    own = {c: set(t) & PROPAGATED_TAGS for c, t in tags.items()}
    # (target, tag) -> (seed, path)
    reached: dict[tuple[Col, str], tuple[Col, list[ColumnEdge]]] = {}
    for tag in sorted(PROPAGATED_TAGS):
        seeds = sorted(c for c, t in own.items() if tag in t)
        prev: dict[Col, ColumnEdge | None] = {s: None for s in seeds}
        seed_of = {s: s for s in seeds}
        queue = deque(seeds)
        while queue:
            node = queue.popleft()
            for e in adj.get(node, ()):
                if e.target in prev:
                    continue
                prev[e.target], seed_of[e.target] = e, seed_of[node]
                queue.append(e.target)
                if tag not in own.get(e.target, set()):
                    path, cur = [], e
                    while cur is not None:
                        path.append(cur)
                        cur = prev[cur.source]
                    reached[(e.target, tag)] = (seed_of[node], path[::-1])
    gaps: dict[Col, Gap] = {}
    for (target, tag), (seed, path) in sorted(reached.items()):
        g = gaps.get(target)
        if g is None:
            gaps[target] = Gap(target, [tag], sorted(own.get(target, set())), seed, path)
        else:
            g.missing.append(tag)
            if len(path) < len(g.path):
                g.carried_from, g.path = seed, path
    return sorted(gaps.values(), key=lambda g: g.target)[:MAX_GAPS]


def downstream(edges: Iterable[ColumnEdge], start: Iterable[Col], limit: int = 2_000) -> dict[Col, list[ColumnEdge]]:
    """Every column that receives values from `start`, with its shortest path (the impact report's lineage part)."""
    adj = _graph(edges)
    prev: dict[Col, ColumnEdge | None] = {c: None for c in sorted(set(start))}
    queue = deque(prev)
    out: dict[Col, list[ColumnEdge]] = {}
    while queue and len(out) < limit:
        node = queue.popleft()
        for e in adj.get(node, ()):
            if e.target in prev:
                continue
            prev[e.target] = e
            queue.append(e.target)
            path, cur = [], e
            while cur is not None:
                path.append(cur)
                cur = prev[cur.source]
            out[e.target] = path[::-1]
    return out

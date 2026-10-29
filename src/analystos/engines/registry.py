"""Which engine runs a source's SQL, and the engine catalog (P4-E01/E02).

One engine per SQL source kind, derived from the source-kind catalog so a new kind is still a YAML
entry. Features are derived, never declared: ``pushdown`` only when the kind may be queried in place
(``SourceKind.pushdown_allowed``: validator + analysis compiler + a read-only session), ``federate``
only for DuckDB (local, over pulled legs) and Trino (in place, across catalogs of one cluster, TRN-004).
The status is ``certified`` only when the kind has live connector evidence (``connectors/certification.py``);
every other engine is ``draft``.
Snowflake, BigQuery and Databricks therefore exist as draft engines with validated dialects but no
pushdown: none of them can make a session read-only, and none has a live certification (P4-E02).
"""
from __future__ import annotations

from typing import Any

from analystos.connectors import kinds as catalog
from analystos.engines.base import Engine
from analystos.engines.duckdb import DuckDBEngine
from analystos.engines.sql import PostgresEngine, SessionSQLEngine
from analystos.engines.trino import TrinoEngine
from analystos.gateway.dialects import SUPPORTED_DIALECTS

FEDERATION_ENGINE = "duckdb"


def engine_for_kind(kind: str, dialect: str | None = None) -> Engine:
    """The engine that queries a pushdown source of ``kind`` in ``dialect`` (its native dialect)."""
    spec = catalog.get_kind(kind)
    dialect = dialect or spec.sqlglot_dialect
    if dialect == "postgres":
        return PostgresEngine()
    if dialect == "duckdb":
        return DuckDBEngine()
    if dialect == "trino" and spec.pushdown_allowed:
        return TrinoEngine()
    return SessionSQLEngine(spec.kind, dialect, features=frozenset({"pushdown"}) if spec.pushdown_allowed else frozenset())


def federation_engine(name: str = FEDERATION_ENGINE) -> Any:
    if name == "duckdb":
        return DuckDBEngine()
    if name == "trino":
        return TrinoEngine()
    raise ValueError(f"unknown federation engine {name!r}")


def catalog_entries() -> list[dict[str, Any]]:
    """Every SQL kind's engine: dialect, validated?, derived features, derived status."""
    from analystos.connectors.certification import statuses

    certs = statuses()
    out: list[dict[str, Any]] = []
    for spec in catalog.list_kinds():
        if not spec.is_sql:
            continue
        features = set()
        if spec.pushdown_allowed:
            features.add("pushdown")
        if spec.kind in ("duckdb", "trino"):
            features.add("federate")
        live = certs.get(spec.kind, {}).get("status") == "certified"
        out.append({
            "id": f"engine.{spec.kind}", "kind": spec.kind, "dialect": spec.sqlglot_dialect,
            "dialect_validated": spec.sqlglot_dialect in SUPPORTED_DIALECTS, "features": sorted(features),
            "readonly_enforcement": spec.readonly_enforcement, "status": "certified" if live else "draft",
            "evidence": certs.get(spec.kind, {}).get("evidence") if live else None,
        })
    return out


def engine_manifests() -> list[dict[str, Any]]:
    """`kind: Engine` capability manifests (spec v3 §3.1), generated from the catalog above."""
    out: list[dict[str, Any]] = []
    for e in catalog_entries():
        out.append({
            "kind": "Engine", "id": e["id"], "version": "1.0.0",
            "summary": f"{e['kind']} engine ({e['dialect']}; {', '.join(e['features']) or 'staged only'})",
            "entry": f"builtin:engine:{e['kind']}", "determinism": "deterministic", "side_effect": "read_source",
            "cost_class": "query", "permissions": ["role:analyst"],
            "certification": {"status": "certified" if e["status"] == "certified" else "draft",
                              "evidence": e["evidence"]},
            "tags": ["engine", *e["features"]],
            "spec": {k: e[k] for k in ("kind", "dialect", "dialect_validated", "features", "readonly_enforcement")},
        })
    return out

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ValidatedSQL(BaseModel):
    original_sql: str
    executable_sql: str  # normalized, row-limited, in the target dialect
    dialect: str
    source_id: str
    referenced_assets: list[str]  # "schema.table"
    referenced_columns: list[str] = Field(default_factory=list)  # "schema.table.column" where resolvable
    fingerprint: str  # hash of normalized SQL (no limit), used for cache key with scope hash
    asset_sources: dict[str, str] = Field(default_factory=dict)  # "schema.table" -> source_id (several when federated)


class CostEstimate(BaseModel):
    """Pre-execution estimate of a statement (S02-T4). ``bytes`` is the estimated scan; ``basis`` says how
    it was obtained (`explain`, `dry_run`, `row_cap`, ...) so a row-cap-only kind is never mistaken for a plan."""

    rows: int | None = None
    bytes: int | None = None
    credits: float | None = None
    basis: str = "unknown"


class ResultHandle(BaseModel):
    """A result written as Parquet to the result store (ADR-010). The uri is relative to the store and only
    resolves through ``gateway.handles`` (tenant visibility, expiry and sha256 checked on every open)."""

    model_config = ConfigDict(populate_by_name=True)

    query_execution_id: str
    workspace_id: str
    uri: str
    result_schema: list[dict[str, Any]] = Field(default_factory=list, alias="schema")
    row_count: int
    bytes: int
    sha256: str
    expires_at: datetime
    classification_max: str = "internal"


class QueryResult(BaseModel):
    query_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    cache_hit: bool = False
    fingerprint: str
    result_hash: str
    duration_ms: int = 0
    referenced_assets: list[str] = Field(default_factory=list)
    sql: str = ""
    # S02: a result above the inline thresholds comes back as a handle: `rows` is then empty and
    # `row_count` counts the rows in the file. `estimate` is set when a scan budget applied.
    handle: ResultHandle | None = None
    estimate: CostEstimate | None = None

    @property
    def inline(self) -> list[list[Any]] | None:
        return None if self.handle is not None else self.rows

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]

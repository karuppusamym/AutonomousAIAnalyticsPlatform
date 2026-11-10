"""Declared data-quality rules on catalog assets (review item P1-7, spec S09).

A `QualityRule` is what an owner states about one asset or column: the row gates of recipe outputs
(`contracts/recipe.py` `ROW_GATES`: not_null, unique, accepted_values, range) plus regex, a statistic range and
owner-written SQL (a row condition or an aggregate expression over the one asset). Each rule has a pass
threshold (the share of evaluated rows that must pass; aggregate rules pass or fail whole) and a quality
dimension. The rule is data: `skills/quality_rules.py` renders it to one read-only statement and the gateway
runs it (CLAUDE.md rules 3 and 4). Owner SQL is parsed and must stay an expression over the asset's own
columns: no subquery, no other table, no unknown function.

`DataContract` is the `contract` block of a data-product manifest (P2-9): a freshness SLA and refs to
approved rules, checked by a `data_contract` monitor.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from analystos.contracts.recipe import ROW_GATES
from analystos.core.ids import stable_hash

RuleType = Literal["not_null", "unique", "accepted_values", "range", "regex", "statistic_range", "sql_row", "sql_aggregate"]
Dimension = Literal["completeness", "uniqueness", "validity", "accuracy", "consistency", "timeliness"]
Statistic = Literal["mean", "min", "max", "sum", "stddev", "median", "count", "distinct_count", "null_rate"]
RuleSeverity = Literal["info", "warning", "critical"]
GATE_TYPES = frozenset(ROW_GATES)  # the recipe gate types, same meaning on a catalog asset
ROW_TYPES = frozenset({"not_null", "unique", "accepted_values", "range", "regex", "sql_row"})
AGGREGATE_TYPES = frozenset({"statistic_range", "sql_aggregate"})
DEFAULT_DIMENSION: dict[str, str] = {"not_null": "completeness", "unique": "uniqueness", "accepted_values": "validity",
                                     "range": "validity", "regex": "validity", "statistic_range": "accuracy",
                                     "sql_row": "validity", "sql_aggregate": "consistency"}
ASSET = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")
MAX_VALUES = 200
MAX_SQL_CHARS = 2000
MAX_PATTERN_CHARS = 500


class QualityRule(BaseModel):
    """One declared rule. `asset` is `schema.table` as the gateway sees it; `column` is set for every column
    rule (all but sql_row / sql_aggregate and a composite unique, which uses `columns`)."""

    model_config = ConfigDict(extra="forbid")
    type: RuleType
    asset: str = Field(max_length=330)
    column: str | None = Field(default=None, max_length=200)
    columns: list[str] = Field(default_factory=list, max_length=8)  # unique over several columns
    values: list[str | int | float | bool] = Field(default_factory=list, max_length=MAX_VALUES)  # accepted_values
    min: float | None = None  # range, statistic_range, sql_aggregate
    max: float | None = None
    allow_null: bool = True  # range / accepted_values / regex: a NULL passes unless False
    pattern: str | None = Field(default=None, max_length=MAX_PATTERN_CHARS)  # regex (full match not implied)
    statistic: Statistic | None = None  # statistic_range
    sql: str | None = Field(default=None, max_length=MAX_SQL_CHARS)  # sql_row: a condition; sql_aggregate: an expression
    threshold: float = Field(default=1.0, ge=0.0, le=1.0)  # share of evaluated rows that must pass
    dimension: Dimension | None = None
    severity: RuleSeverity = "warning"
    name: str | None = Field(default=None, max_length=120)
    description: str = Field(default="", max_length=1000)

    @field_validator("asset")
    @classmethod
    def _asset(cls, v: str) -> str:
        if not ASSET.match(v):
            raise ValueError(f"asset {v!r} must be schema.table")
        return v

    @model_validator(mode="after")
    def _shape(self) -> QualityRule:
        t = self.type
        if self.dimension is None:
            self.dimension = DEFAULT_DIMENSION[t]  # type: ignore[assignment]
        if t in ("sql_row", "sql_aggregate"):
            if not (self.sql or "").strip():
                raise ValueError(f"a {t} rule needs sql")
            if self.column or self.columns:
                raise ValueError(f"a {t} rule names its columns inside sql")
        elif t == "unique":
            if not (self.column or self.columns) or (self.column and self.columns):
                raise ValueError("a unique rule needs column or columns (not both)")
        elif not self.column:
            raise ValueError(f"a {t} rule needs column")
        if t not in ("sql_row", "sql_aggregate") and self.sql:
            raise ValueError("sql is only for sql_row and sql_aggregate rules")
        if t == "accepted_values" and not self.values:
            raise ValueError("accepted_values needs values")
        if t in ("range", "statistic_range", "sql_aggregate") and self.min is None and self.max is None:
            raise ValueError(f"a {t} rule needs min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        if t == "statistic_range" and self.statistic is None:
            raise ValueError("statistic_range needs statistic")
        if t != "statistic_range" and self.statistic is not None:
            raise ValueError("statistic is only for statistic_range")
        if t == "regex":
            if not self.pattern:
                raise ValueError("a regex rule needs pattern")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"pattern is not a valid regular expression: {exc}") from None
        elif self.pattern:
            raise ValueError("pattern is only for regex rules")
        if t in AGGREGATE_TYPES and self.threshold != 1.0:
            raise ValueError("an aggregate rule passes or fails whole: leave threshold at 1.0")
        return self

    @property
    def row_level(self) -> bool:
        return self.type in ROW_TYPES

    def targets(self) -> list[str]:
        return list(self.columns) if self.columns else ([self.column] if self.column else [])

    def key(self) -> str:
        if self.name:
            return self.name
        cols = ",".join(self.targets())
        return f"{self.type}({self.asset}{'.' + cols if cols else ''})"

    def definition(self) -> dict[str, Any]:
        """What the rule checks, without its presentation (name, description): its identity."""
        return self.model_dump(mode="json", exclude={"name", "description"})

    def content_hash(self) -> str:
        return stable_hash(self.definition())


class FreshnessSla(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_hours: float = Field(gt=0, le=24 * 366)
    assets: list[str] = Field(min_length=1, max_length=20)  # schema.table, judged by their newest load

    @field_validator("assets")
    @classmethod
    def _assets(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if not ASSET.match(a)]
        if bad:
            raise ValueError(f"assets {bad[:3]} must be schema.table")
        return sorted(set(v))


class DataContract(BaseModel):
    """A data product's promise to its consumers (P2-9). Rule refs are `quality_rule` ids that must be active
    when the product is published; the monitor reads their latest results and the freshness of `assets`."""

    model_config = ConfigDict(extra="forbid")
    freshness: FreshnessSla | None = None
    quality_rules: list[str] = Field(default_factory=list, max_length=50)
    min_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)  # None: each rule's own threshold

    @field_validator("quality_rules")
    @classmethod
    def _rules(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @model_validator(mode="after")
    def _not_empty(self) -> DataContract:
        if self.freshness is None and not self.quality_rules:
            raise ValueError("a contract needs a freshness SLA or at least one quality rule")
        return self

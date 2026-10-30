"""Executable analysis contracts: hypotheses compile to SQL from a closed vocabulary, not free text.

Why a closed vocabulary: the LLM proposes *what* to test; deterministic code decides *how* the
number is computed. That keeps every finding reproducible and keeps the model away from raw SQL
on the critical path (the SQL agent's free-form SQL still exists for ad-hoc questions and goes
through the same gateway validation).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator


def _method_schema(schema: dict[str, Any]) -> None:
    from analystos import methods

    methods.schema_extra(schema)


def _omit(data: dict[str, Any], **defaults: Any) -> dict[str, Any]:
    """Drop fields added after specs were first hashed while they hold their default: a spec written
    before them serializes (and hashes) exactly as it did, so registries and pins keep matching."""
    for key, default in defaults.items():
        if key in data and data[key] == default:
            data.pop(key)
    return data


class Derivation(BaseModel):
    """A column or a safe derived expression over one column (or two for durations).

    type:
      column          -> the raw column
      duration_hours  -> (end - start) in hours; `column` is start, `end_column` is end
      later_than      -> TRUE when `end_column` is later than `column` by more than `tolerance_hours`
                         (delivered after promised, resolved after due); NULL when either is NULL
      after_hours     -> TRUE when hour(column) outside [start_hour, end_hour) or weekend
      bucket          -> numeric column bucketed by `edges` (labels like "0", "1", "2", "3+")
      equals          -> TRUE when column = value (boolean outcome from a categorical)
      is_true         -> column interpreted as boolean
      date_trunc      -> date_trunc(grain, column)
      hour_of_day     -> extract(hour from column)
      day_of_week     -> extract(dow from column)
      period          -> the `label` of the window in `value` that holds `column` (a list of 2 to 4
                         {label, start, end} windows, ISO dates, `end` exclusive, in time order and not
                         overlapping); NULL outside every window. A guided investigation compares its
                         baseline with its requested period through it (PILOT-FIX-01).

    via: the column(s) live in a related table, reached through the spec's join whose `from_column`
    is `via` (customer region through `customer_id`); None reads the spec's own asset.
    """

    type: Literal["column", "duration_hours", "later_than", "after_hours", "bucket", "equals", "is_true",
                  "date_trunc", "hour_of_day", "day_of_week", "period"] = "column"
    column: str
    end_column: str | None = None
    value: Any = None
    edges: list[float] | None = None
    grain: Literal["day", "week", "month", "quarter"] | None = None
    start_hour: int = 8
    end_hour: int = 18
    tolerance_hours: float = Field(0.0, ge=0)
    via: str | None = None
    label: str | None = None

    def columns(self) -> list[str]:
        return [c for c in (self.column, self.end_column) if c]

    @model_validator(mode="after")
    def _period_windows(self) -> Derivation:
        if self.type == "period":
            self.value = period_windows(self.value)
        return self

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), tolerance_hours=0.0, via=None)


MAX_PERIOD_WINDOWS = 4


def period_windows(value: Any) -> list[dict[str, str]]:
    """The checked windows of a `period` derivation: labelled, ISO-dated, end-exclusive, in time order."""
    import datetime as _dt

    if not isinstance(value, list) or not 2 <= len(value) <= MAX_PERIOD_WINDOWS:
        raise ValueError(f"a period derivation needs 2 to {MAX_PERIOD_WINDOWS} windows in `value`")
    out: list[dict[str, str]] = []
    for w in value:
        if not isinstance(w, dict) or set(w) != {"label", "start", "end"}:
            raise ValueError("each period window is {label, start, end}")
        try:
            start, end = _dt.date.fromisoformat(str(w["start"])), _dt.date.fromisoformat(str(w["end"]))
        except ValueError as exc:
            raise ValueError("period window dates must be ISO dates (YYYY-MM-DD)") from exc
        label = str(w["label"]).strip()
        if not label or len(label) > 60 or start >= end:
            raise ValueError("a period window needs a short label and start before end")
        if out and start < _dt.date.fromisoformat(out[-1]["end"]):
            raise ValueError("period windows must be in time order and must not overlap")
        if label in {x["label"] for x in out}:
            raise ValueError("period window labels must differ")
        out.append({"label": label, "start": start.isoformat(), "end": end.isoformat()})
    return out


class Filter(BaseModel):
    column: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "is null", "is not null"]
    value: Any = None
    origin: Literal["plan", "user_redirect", "policy"] = "plan"
    via: str | None = None  # as Derivation.via: a related table's column

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), via=None)


class Join(BaseModel):
    """A many-to-one lookup from the spec's asset: `from_column` references the unique `to_column` of
    `asset`. Compiled as a LEFT JOIN, so every base row is kept exactly once; validation accepts only a
    validated (or user-declared) relationship between in-scope tables of one source. Derivations and
    filters name it by `via` = `from_column`."""

    from_column: str
    asset: str  # "schema.table"
    to_column: str


class AnalysisSpec(BaseModel):
    """One executable hypothesis test. `method` must be a registered analysis method: the vocabulary,
    its JSON Schema enum and the per-method requirements come from the method registry
    (`analystos.methods`, spec v3 §3.5), never from a list kept here."""

    method: str = Field(json_schema_extra=_method_schema)
    asset: str  # "schema.table"
    outcome: Derivation | None = None
    segment: Derivation | None = None
    drivers: list[Derivation] = Field(default_factory=list)
    time: Derivation | None = None
    filters: list[Filter] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    min_group_size: int = 30
    top_k: int = 12

    @field_validator("method")
    @classmethod
    def _registered(cls, v: str) -> str:
        from analystos import methods

        if v not in methods.names():
            raise ValueError(f"unknown analysis method {v!r}; registered methods: {', '.join(methods.names())}")
        return v

    @model_validator(mode="after")
    def _vias_declared(self) -> AnalysisSpec:
        keys = [j.from_column for j in self.joins]
        if len(keys) != len(set(keys)):
            raise ValueError("each join needs its own from_column")
        for via in self.vias():
            if via not in keys:
                raise ValueError(f"via {via!r} names no join of the spec (declare it in `joins`)")
        return self

    def derivations(self) -> list[Derivation]:
        return [d for d in (self.outcome, self.segment, self.time, *self.drivers) if d is not None]

    def vias(self) -> list[str]:
        return list(dict.fromkeys(x.via for x in (*self.derivations(), *self.filters) if x.via))

    def join(self, via: str | None) -> Join | None:
        return next((j for j in self.joins if j.from_column == via), None) if via else None

    def table_of(self, item: Derivation | Filter) -> str:
        """The table a derivation's or a filter's columns are read from."""
        j = self.join(item.via)
        return j.asset if j is not None else self.asset

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), joins=[])


class HypothesisProposal(BaseModel):
    question: str
    statement: str
    rationale: str = ""
    spec: AnalysisSpec
    priority: Literal["high", "medium", "low"] = "medium"


class StatResult(BaseModel):
    """Output of a statistical skill. Every number an insight quotes must come from here."""

    method: str
    test: str
    n: int
    statistic: float | None = None
    p_value: float | None = None
    effect_size: float | None = None
    effect_label: str | None = None  # e.g. "cramers_v", "rate_ratio", "spearman_rho"
    ci_low: float | None = None
    ci_high: float | None = None
    groups: list[dict[str, Any]] = Field(default_factory=list)  # per-segment rows
    highlights: dict[str, Any] = Field(default_factory=dict)  # best/worst segment, ratios, shares
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    supported: bool | None = None  # method-level verdict before REV
    details: dict[str, Any] = Field(default_factory=dict)


class Partition(BaseModel):
    """A deterministic split of one table's rows into a discovery part and a held-out part (P8-15).

    A row's bucket is the first 16 bits of md5(salt | key columns as text), 0..65535; rows whose bucket is
    below ``fraction * 65536`` are held out. The same rows and key give the same split on every run.
    `side` is the part a query reads. The held-out side compiles only with `claim_locked_at` set: the claim
    it confirms was locked first (`evidence.holdout`). Kept out of `AnalysisSpec` on purpose, so no
    proposal (model or rule) can ask to read the held-out rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    side: Literal["discovery", "holdout"] = "discovery"
    key: list[str] = Field(min_length=1)  # the columns hashed: the table's key, or every readable column
    basis: str = "full_row"  # approved_key | declared_key | measured_unique_key | profile_unique_key | full_row
    fraction: float = Field(gt=0, lt=1)  # share of rows held out
    salt: str = "analystos.holdout.v1"
    claim_locked_at: str | None = None

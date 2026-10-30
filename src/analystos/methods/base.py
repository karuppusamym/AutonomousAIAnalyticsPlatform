"""The `Method` protocol (spec v3 §3.5) and the helpers every analysis method shares.

A method is one module plus one `kind: Method` manifest. It owns everything that used to be spread
over the agents as `if method == ...` branches: the vocabulary line the model sees, spec validation,
the pushdown SQL (built from `skills/sqlbuild` primitives), the primary statistic, the independent
second method, the claim key, the numbers-guard-safe template and the chart intent. The rest of the
platform asks the registry (`analystos.methods`) and never names a method.

Data flow per call: `skills/analysis` compiles each of `purposes` through `sqlbuild.compile_spec`
(which dispatches to `compile`), runs the SQL through the governed `run_sql`, and hands the rows
(`Rows`, keyed by purpose) to `test` / `verify`. A method never opens a connection.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel, Field

from analystos.contracts.analysis import AnalysisSpec, Derivation, StatResult
from analystos.skills.sqlbuild import BOOLEAN_DERIVATIONS, CompiledQuery

OTHER = "(other)"
TABLE_MAX_ROWS = 2000

# What a derivation measures, as the method vocabulary sees it (spec v3 §3.5 `applicable`).
ColumnType = Literal["boolean", "numeric", "categorical", "datetime", "identifier", "text"]
Rows = Mapping[str, list[dict[str, Any]]]  # purpose -> result rows (lower-case keys, plain Python values)
Semantic = Callable[[Derivation], str | None]  # the crawler's semantic type of a derivation's column
ClaimKey = tuple[Any, ...]  # stable across runs; the last element is the group that came out on top


class AnalysisOutcome(BaseModel):
    method: str
    stat: StatResult
    query_ids: list[str] = Field(default_factory=list)
    table: dict[str, Any] = Field(default_factory=lambda: {"columns": [], "rows": []})
    sql: list[str] = Field(default_factory=list)
    agrees: bool | None = None  # set by verification


@dataclass(frozen=True)
class ChartIntent:
    """How a finding is best shown. `intent` is a `skills/viz` chart intent; `dimension` names the spec
    part on the x axis (segment | time | x | drivers | cohort); `measure` is `outcome` (the outcome's
    metric) or `volume` (record count)."""

    intent: str
    dimension: str
    measure: Literal["outcome", "volume"] = "outcome"


@runtime_checkable
class Method(Protocol):
    """What the platform needs from an analysis method. `AnalysisMethod` implements the defaults."""

    name: str
    vocabulary: str  # one line of the prompt vocabulary block: what the spec must contain
    purposes: tuple[str, ...]  # queries it runs, in execution order
    playbook: str | None  # role in the domain-neutral hypothesis playbook, if any
    drill_down: bool  # supported findings are drilled into by filtering to the top segment
    segment_matrix: bool  # tested outcomes are continued across the dimensions not yet used

    def applicable(self, outcome: ColumnType | None, segment: ColumnType | None) -> bool: ...
    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]: ...
    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary",
                sample_rows: int = 50000) -> CompiledQuery: ...
    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome: ...
    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome: ...
    def claim_key(self, spec: Mapping[str, Any], highlights: Mapping[str, Any] | None) -> ClaimKey: ...
    def identity_keys(self, spec: AnalysisSpec) -> list[Any]: ...
    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None: ...
    def facts(self, spec: Mapping[str, Any], stat: Mapping[str, Any], *, extra: Mapping[str, Any] | None = None,
              query_ids: tuple[str, ...] = (), result_hashes: tuple[str, ...] = ()) -> list[Any]: ...
    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent | None: ...


class AnalysisMethod:
    """Defaults for a method; subclasses set `name`/`vocabulary` and implement compile/test/verify."""

    name: ClassVar[str] = ""
    vocabulary: ClassVar[str] = ""
    purposes: ClassVar[tuple[str, ...]] = ("primary",)
    playbook: ClassVar[str | None] = None
    drill_down: ClassVar[bool] = False
    segment_matrix: ClassVar[bool] = False
    outcome_types: ClassVar[frozenset[str] | None] = None  # ColumnTypes accepted as outcome (None: no outcome)
    segment_types: ClassVar[frozenset[str] | None] = None  # ColumnTypes accepted as segment (None: no segment)
    # Evidence dimensions this method cannot supply and does not need (P4-03 `evidence.bundle`), e.g. "uncertainty".
    evidence_optional: ClassVar[frozenset[str]] = frozenset()

    def applicable(self, outcome: ColumnType | None, segment: ColumnType | None) -> bool:
        ok_out = (outcome is None) if self.outcome_types is None else (outcome in self.outcome_types)
        ok_seg = (segment is None) if self.segment_types is None else (segment in self.segment_types)
        return ok_out and ok_seg

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        return []

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> CompiledQuery:
        raise NotImplementedError

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        raise NotImplementedError

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        raise NotImplementedError

    def claim_subject(self, spec: Mapping[str, Any]) -> Any:
        """What the outcome is broken down by; part of the claim identity."""
        return ref_name(spec.get("segment") or {}) or None

    def claim_key(self, spec: Mapping[str, Any], highlights: Mapping[str, Any] | None) -> ClaimKey:
        """Identity of a claim: what was tested (method, outcome, subject, population filters) and which
        group came out on top. Wording is not part of it."""
        filters = ";".join(sorted(f"{ref_name(f)}{f.get('op')}{f.get('value')}" for f in spec.get("filters") or []))
        hl = highlights or {}
        top = hl.get("top_segment", hl.get("top_driver"))
        return (self.name, (spec.get("outcome") or {}).get("column"), self.claim_subject(spec), filters, str(top))

    def identity_keys(self, spec: AnalysisSpec) -> list[Any]:
        """Extra identities beyond the full spec hash: two specs sharing one are the same test."""
        return []

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        return None

    def facts(self, spec: Mapping[str, Any], stat: Mapping[str, Any], *, extra: Mapping[str, Any] | None = None,
              query_ids: tuple[str, ...] = (), result_hashes: tuple[str, ...] = ()) -> list[Any]:
        """Typed facts a finding may quote (P4-03). The shared highlight vocabulary covers the built-in
        methods; override only to give a new statistic its unit, group and direction."""
        from analystos.evidence.facts import facts_from

        return facts_from(spec, stat, extra=extra, query_ids=query_ids, result_hashes=result_hashes)

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent | None:
        return None


# ------------------------------------------------------------------------------------ value plumbing
def py(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


def json_value(v: Any) -> Any:
    v = py(v)
    if isinstance(v, _dt.datetime | _dt.date):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def is_number(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def seg_label(v: Any, d: Derivation | None) -> str:
    if v is None:
        return "(null)"
    if d is not None and d.type in BOOLEAN_DERIVATIONS:
        return "true" if float(v) == 1 else "false"
    if isinstance(v, _dt.datetime | _dt.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def seg_sort_key(d: Derivation | None, order: Any, label: str) -> tuple:
    if order is not None:
        return (0, float(order), label)
    if d is not None and d.type in ("hour_of_day", "day_of_week"):
        try:
            return (0, float(label), label)
        except ValueError:
            pass
    return (1, 0.0, label)


def first(rows: list[dict[str, Any]], key: str, default: Any = 0) -> Any:
    return rows[0].get(key, default) if rows else default


def no_data(spec: AnalysisSpec, why: str, test: str = "none") -> AnalysisOutcome:
    stat = StatResult(method=spec.method, test=test, n=0, supported=False, warnings=[why])
    return AnalysisOutcome(method=spec.method, stat=stat)


def select_segments(groups: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[dict[str, Any]], list[str], dict]:
    """Apply min_group_size and top_k (largest n first). Returns (kept, warnings, exclusion counts)."""
    warns: list[str] = []
    small = [g for g in groups if g["n"] < spec.min_group_size]
    big = sorted([g for g in groups if g["n"] >= spec.min_group_size], key=lambda g: (-g["n"], g["segment"]))
    kept, rest = big[: spec.top_k], big[spec.top_k:]
    info = {"small_segments": len(small), "small_segment_rows": sum(g["n"] for g in small),
            "beyond_top_k_segments": len(rest), "beyond_top_k_rows": sum(g["n"] for g in rest)}
    if small:
        warns.append(f"{len(small)} segment(s) with n < {spec.min_group_size} excluded from the test "
                     f"({info['small_segment_rows']} rows)")
    if rest:
        warns.append(f"only the {spec.top_k} largest segments tested; {len(rest)} more segment(s) "
                     f"({info['beyond_top_k_rows']} rows) excluded")
    return kept, warns, info


def cap_sample(values: list[float], k: int, seed: int = 0) -> np.ndarray:
    a = np.asarray(values, dtype=float)
    if a.size <= k:
        return a
    return np.random.default_rng(seed).choice(a, size=k, replace=False)


def verdict(primary: StatResult, supported: bool, direction_agrees: bool | None) -> bool:
    """The second method agrees when it reaches the same verdict and, if supported, the same direction."""
    if primary.supported:
        return bool(supported and direction_agrees)
    return not supported


def finish(spec: AnalysisSpec, primary: StatResult, v: StatResult, direction: bool | None,
           table: dict[str, Any]) -> AnalysisOutcome:
    agrees = verdict(primary, bool(v.supported), direction)
    v.details.update({"agrees": agrees, "direction_agrees": direction, "primary_supported": primary.supported})
    return AnalysisOutcome(method=spec.method, stat=v, table=table, agrees=agrees)


def nothing_to_verify(spec: AnalysisSpec, primary: StatResult, test: str, why: str, n: int = 0) -> AnalysisOutcome:
    v = StatResult(method=spec.method, test=test, n=n, supported=False, warnings=[why])
    return finish(spec, primary, v, None, {"columns": [], "rows": []})


# ------------------------------------------------------------------------------------ template text
def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def cap(text: str) -> str:
    """Capitalise the first letter only ('missed SLA' -> 'Missed SLA', not 'Missed sla')."""
    return text[:1].upper() + text[1:]


def ref_name(item: Mapping[str, Any]) -> str:
    """A derivation's or filter's column as stable text: `via.column` when it is read from a related table,
    so a joined `region` and the base table's own `region` are different subjects."""
    column = str(item.get("column") or "")
    return f"{item['via']}.{column}" if item.get("via") and column else column


def text_parts(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    """(segment label, outcome label, population scope suffix) for template sentences."""
    seg = (spec.get("segment") or {}).get("label") or ref_name(spec.get("segment") or {}) or "segment"
    out = (spec.get("outcome") or {}).get("label") or (spec.get("outcome") or {}).get("column") or "volume"
    scope = f" (where {', '.join(ref_name(f) + ' ' + f['op'] + ' ' + str(f.get('value')) for f in spec.get('filters') or [])})" \
        if spec.get("filters") else ""
    return seg, out, scope


def column_type(d: Derivation | None, semantic: str | None = None) -> ColumnType | None:
    """The vocabulary type of a derivation (for `applicable`), from its derivation type and the
    crawler's semantic type of its column."""
    if d is None:
        return None
    if d.type in BOOLEAN_DERIVATIONS:
        return "boolean"
    if d.type == "duration_hours":
        return "numeric"
    if d.type in ("bucket", "hour_of_day", "day_of_week", "period"):
        return "categorical"
    if d.type == "date_trunc":
        return "datetime"
    return {"numeric": "numeric", "boolean": "boolean", "categorical": "categorical", "datetime": "datetime",
            "id": "identifier", "text": "text"}.get(semantic or "", "categorical")  # type: ignore[return-value]

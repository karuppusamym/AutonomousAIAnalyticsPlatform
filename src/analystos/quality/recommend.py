"""Quality rules recommended from the stored profile (P1-7): deterministic, no query, no model.

From each column's stored profile (`skills/profiling.py`, as the crawler kept it):

* observed null rate below 20% -> `not_null` with a threshold just under the observed completeness
  (all present -> 1.0), so today's data passes and a drop in completeness fails;
* p01..p99 of a numeric, non-key column -> `range` [p01, p99] with threshold 0.98 (by construction about 98% of
  today's rows fall inside);
* a complete low-cardinality vocabulary (`values_complete`, at most 12 values) -> `accepted_values`;
* a key column whose distinct count equals its non-null count -> `unique`.

Value-bearing recommendations (range, accepted_values) are never made for a column tagged pii, sensitive or
restricted (the review's "what not to copy": a literal only when it is profiled vocabulary on a column with no
PII tag). Each recommendation is a review draft (`knowledge_suggestion`, kind `quality_rule`); nothing runs until
an owner approves it, which declares the rule (`quality/service.py`).
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.quality import QualityRule
from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.db.models import KnowledgeSuggestion, SourceAsset, SourceColumn, User
from analystos.governance.policy import require_role
from analystos.quality import service

KIND = "quality_rule"
ORIGIN = "quality.recommend"
PROPOSER = "process:quality.recommend"
NULL_RATE_MAX = 0.2
NULL_MARGIN = 0.01
RANGE_THRESHOLD = 0.98
ENUM_MAX = 12
SENSITIVE = frozenset({"pii", "sensitive", "restricted"})


def _floor(x: float, places: int = 2) -> float:
    f = 10 ** places
    return math.floor(x * f) / f


def _sensitive(c: SourceColumn) -> bool:
    sem = c.semantics or {}
    return bool(SENSITIVE & {str(t).lower() for t in c.tags or []}) or bool(sem.get("pii"))


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def recommend_column(asset: str, c: SourceColumn) -> list[tuple[QualityRule, str, dict[str, Any], float]]:
    """(rule, statement, evidence, confidence) for one column, from its stored profile alone."""
    p = c.profile or {}
    out: list[tuple[QualityRule, str, dict[str, Any], float]] = []
    non_null, nulls = int(p.get("non_null") or 0), int(p.get("null_count") or 0)
    rows = non_null + nulls
    if not rows or not non_null:
        return out
    rate = float(p.get("null_rate") if p.get("null_rate") is not None else nulls / rows)
    if rate < NULL_RATE_MAX:
        threshold = 1.0 if nulls == 0 else max(0.0, _floor(1.0 - rate - NULL_MARGIN))
        out.append((QualityRule(type="not_null", asset=asset, column=c.name, threshold=threshold),
                    f"{c.name} is present in at least {threshold:.0%} of rows (observed {1 - rate:.2%} of {rows}).",
                    {"null_rate": round(rate, 6), "rows": rows}, 0.9 if nulls == 0 else 0.8))
    semantic = str(p.get("semantic_type") or c.semantic_type or "")
    distinct = int(p.get("distinct") or 0)
    if (c.is_key or p.get("is_key") or semantic == "id") and distinct and distinct == non_null:
        out.append((QualityRule(type="unique", asset=asset, column=c.name, severity="critical"),
                    f"{c.name} identifies a row: every non-null value is distinct ({distinct} of {non_null}).",
                    {"distinct": distinct, "non_null": non_null}, 0.85))
    if _sensitive(c):
        return out
    pct = p.get("percentiles") or {}
    lo, hi = _num(pct.get("p01")), _num(pct.get("p99"))
    if p.get("type_family") == "numeric" and semantic not in ("id",) and lo is not None and hi is not None and lo < hi:
        out.append((QualityRule(type="range", asset=asset, column=c.name, min=lo, max=hi, threshold=RANGE_THRESHOLD),
                    f"{c.name} stays within its observed p01..p99 range [{lo:g}, {hi:g}] for at least "
                    f"{RANGE_THRESHOLD:.0%} of rows.", {"p01": lo, "p99": hi}, 0.7))
    values = p.get("values") or []
    if (p.get("values_complete") and 1 < len(values) <= ENUM_MAX and semantic in ("categorical", "boolean")
            and all(isinstance(v, str | int | float | bool) for v in values)):
        out.append((QualityRule(type="accepted_values", asset=asset, column=c.name, values=list(values),
                                allow_null=nulls > 0),
                    f"{c.name} takes one of its {len(values)} profiled values.", {"values": len(values),
                                                                                "values_complete": True}, 0.75))
    return out


def recommend(session: Session, user: User, workspace_id: str, asset_id: str) -> dict[str, Any]:
    """Queue recommendations for one catalog asset as review drafts. Needs editor. A rule already declared (same
    type on the same column) is not proposed again; the review queue skips content already decided or rejected."""
    from analystos.knowledge.suggestions import field, propose

    require_role(session, user, workspace_id, "editor")
    a = session.get(SourceAsset, asset_id)
    if a is None or a.workspace_id != workspace_id:
        raise InvalidInput("asset not found in this workspace")
    if not a.stats and not any((c.profile or {}).get("non_null") is not None for c in
                               session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))):
        raise InvalidInput(f"{a.schema_name}.{a.name} has no stored profile yet: crawl it with profiling first")
    fq = f"{a.schema_name}.{a.name}"
    declared = {(r.type, r.column) for r in service.list_rules(session, workspace_id, asset=fq, status="active")}
    queued, skipped = [], 0
    for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal)):
        for rule, statement, evidence, confidence in recommend_column(fq, c):
            if (rule.type, rule.column) in declared:
                skipped += 1
                continue
            prov = {"source": "profile", "asset_id": a.id, "column": c.name, "rule": "quality.recommend.v1"}
            row = propose(session, workspace_id, kind=KIND, subject=f"quality:{a.id}:{c.name}:{rule.type}",
                          title=f"Quality rule: {rule.key()}",
                          fields={"statement": field(statement, confidence, **prov),
                                  "rule": field(rule.model_dump(mode="json"), confidence, **prov),
                                  "evidence": field(evidence, 1.0, source="profile")},
                          origin=ORIGIN, proposed_by=PROPOSER, batch=f"asset:{a.id}")
            if row is None:
                skipped += 1
            else:
                queued.append({"id": row.id, "type": rule.type, "column": c.name, "statement": statement})
    return {"asset": fq, "queued": queued, "skipped": skipped}


def rule_error(fields: dict[str, Any]) -> str | None:
    rule = (fields.get("rule") or {}).get("value")
    if not isinstance(rule, dict):
        return "rule_missing"
    try:
        QualityRule.model_validate(rule)
    except Exception:  # noqa: BLE001 - any invalid edit is a review error, reported per draft
        return "invalid_rule"
    return None


def apply_suggestion(session: Session, row: KnowledgeSuggestion, fields: dict[str, Any], user: Any) -> str:
    """An approved recommendation becomes an active rule, declared by the approving owner. Owner checks and
    compilation happen in `service.declare`; a refusal surfaces as the review error."""
    try:
        rule_row, created = service.declare(session, user, row.workspace_id, fields["rule"]["value"],
                                            origin="import:okf" if row.origin == "import:okf" else "recommended",
                                            suggestion_id=row.id)
    except AnalystOSError as exc:
        raise ReviewRefused(exc.message) from None
    return f"quality rule {'declared' if created else 'already declared'}: {rule_row.id}"


class ReviewRefused(Exception):
    """A quality-rule draft the reviewer may not apply (not the owner, unknown column, unsafe SQL)."""

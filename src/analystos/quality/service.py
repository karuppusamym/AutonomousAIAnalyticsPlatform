"""Declared quality rules (P1-7): declare, list, disable, run, and the trust signal they give an asset.

Models propose, code decides. A rule comes from an owner (declared here) or from a profile recommendation that
an owner approved in the review queue (`quality/recommend.py`); either way it is a validated `QualityRule` on an
asset of this workspace's catalog, compiled once at declaration so a broken rule is refused up front. Running a
rule is one statement through `QueryGateway.execute` (the runner of the caller's own scope), recorded as a
`quality_rule_result` with counts only. The trust signal is computed from the latest results, never from a model.

Owner: when the asset's source names owners with a `user_id` (`Source.owners`, P4-09) only they, a workspace owner
or an admin may declare or approve a rule on it; otherwise any editor.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.quality import QualityRule
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import member_role, require_role
from analystos.quality.models import QualityRuleResult, QualityRuleRow
from analystos.skills.quality_rules import RuleOutcome, compile_rule, run_rule

STATUSES = ("active", "disabled")
ORIGINS = ("owner", "recommended", "import:okf")
HISTORY_LIMIT = 50


def parse_rule(data: dict[str, Any] | QualityRule) -> QualityRule:
    if isinstance(data, QualityRule):
        return data
    try:
        return QualityRule.model_validate(data)
    except ValidationError as exc:
        raise InvalidInput("not a valid quality rule", details={"errors": exc.errors(include_url=False)}) from None


def catalog_asset(session: Session, workspace_id: str, fq: str) -> tuple[SourceAsset, Source, list[str]]:
    schema, _, name = fq.partition(".")
    a = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                                 SourceAsset.name == name))
    if a is None:
        raise NotFound(f"asset {fq} is not in this workspace's catalog")
    src = session.get(Source, a.source_id)
    cols = [c.name for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id)
                                            .order_by(SourceColumn.ordinal))]
    return a, src, cols


def source_dialect_of(src: Source | None) -> str:
    from analystos.connectors.kinds import dialect_for

    if src is None:
        return "duckdb"
    try:
        return dialect_for(src.kind, src.execution_mode)
    except Exception:  # noqa: BLE001 - an unknown kind compiles as duckdb; the gateway decides at run time
        return "duckdb"


def owner_ids(src: Source | None) -> set[str]:
    owners = (src.owners or {}) if src is not None else {}
    return {str(v["user_id"]) for v in owners.values() if isinstance(v, dict) and v.get("user_id")}


def require_rule_owner(session: Session, user: User, workspace_id: str, src: Source | None) -> str:
    role = require_role(session, user, workspace_id, "editor")
    named = owner_ids(src)
    if named and user.id not in named and role != "owner" and not user.is_admin:
        raise Forbidden("only the asset's named owners (or a workspace owner) may declare or approve its quality rules")
    return role


def declare(session: Session, user: User, workspace_id: str, data: dict[str, Any] | QualityRule, *,
            origin: str = "owner", suggestion_id: str | None = None) -> tuple[QualityRuleRow, bool]:
    """(row, created). The same rule declared twice returns the first (re-activated if it was disabled)."""
    rule = parse_rule(data)
    if origin not in ORIGINS:
        raise InvalidInput(f"origin must be one of {ORIGINS}")
    asset, src, cols = catalog_asset(session, workspace_id, rule.asset)
    require_rule_owner(session, user, workspace_id, src)
    compile_rule(rule, source_dialect_of(src), columns=cols)  # refuses unknown columns and unsafe owner SQL now
    digest = rule.content_hash()
    existing = session.scalar(select(QualityRuleRow).where(QualityRuleRow.workspace_id == workspace_id,
                                                           QualityRuleRow.content_hash == digest))
    now = utcnow()
    if existing is not None:
        if existing.status != "active":
            existing.status, existing.revision = "active", existing.revision + 1
            existing.approved_by, existing.approved_at = user.id, now
        return existing, False
    row = QualityRuleRow(id=new_id("qrule"), workspace_id=workspace_id, asset_id=asset.id, asset=rule.asset,
                         column=rule.column or (",".join(rule.columns) or None), name=rule.key()[:200], type=rule.type,
                         dimension=str(rule.dimension), severity=rule.severity, threshold=rule.threshold,
                         definition=rule.model_dump(mode="json"), content_hash=digest, status="active", origin=origin,
                         suggestion_id=suggestion_id, created_by=user.id, approved_by=user.id, approved_at=now, revision=1)
    session.add(row)
    session.flush()
    from analystos.artifacts.registry import link

    link(session, workspace_id, ("quality_rule", row.id), "derived_from", ("source_asset", asset.id))
    audit(f"user:{user.id}", "quality_rule.declared", workspace_id=workspace_id, target=row.id, decision="allow",
          details={"asset": rule.asset, "type": rule.type, "origin": origin, "content_hash": digest}, session=session)
    emit(workspace_id, "quality_rule.declared", {"id": row.id, "asset": rule.asset, "type": rule.type, "origin": origin},
         actor=f"user:{user.id}", session=session)
    return row, True


def set_status(session: Session, user: User, rule_id: str, status: str) -> QualityRuleRow:
    if status not in STATUSES:
        raise InvalidInput(f"status must be one of {STATUSES}")
    row = get_rule(session, user, rule_id)
    require_rule_owner(session, user, row.workspace_id, rule_source(session, row))
    if row.status != status:
        row.status, row.revision = status, row.revision + 1
        audit(f"user:{user.id}", f"quality_rule.{status}", workspace_id=row.workspace_id, target=row.id, session=session)
    return row


def rule_source(session: Session, row: QualityRuleRow) -> Source | None:
    a = session.get(SourceAsset, row.asset_id) if row.asset_id else None
    return session.get(Source, a.source_id) if a is not None else None


def get_rule(session: Session, user: User, rule_id: str) -> QualityRuleRow:
    row = session.get(QualityRuleRow, rule_id)
    if row is None or member_role(session, user, row.workspace_id) is None:
        raise NotFound("quality rule not found")
    return row


def rule_view(row: QualityRuleRow, history: list[QualityRuleResult] | None = None) -> dict[str, Any]:
    out = {"id": row.id, "workspace_id": row.workspace_id, "asset": row.asset, "column": row.column, "name": row.name,
           "type": row.type, "dimension": row.dimension, "severity": row.severity, "threshold": row.threshold,
           "definition": row.definition, "content_hash": row.content_hash, "status": row.status, "origin": row.origin,
           "suggestion_id": row.suggestion_id, "created_by": row.created_by, "approved_by": row.approved_by,
           "approved_at": row.approved_at, "last_status": row.last_status, "last_pass_rate": row.last_pass_rate,
           "last_run_at": row.last_run_at, "revision": row.revision}
    if history is not None:
        out["history"] = [result_view(r) for r in history]
    return out


def result_view(r: QualityRuleResult) -> dict[str, Any]:
    return {"status": r.status, "pass_rate": r.pass_rate, "evaluated": r.evaluated, "passed_rows": r.passed_rows,
            "value": r.value, "query_id": r.query_id, "error": r.error, "trigger": r.trigger, "run_at": r.run_at}


def list_rules(session: Session, workspace_id: str, *, asset: str | None = None, status: str | None = None) -> list[QualityRuleRow]:
    q = select(QualityRuleRow).where(QualityRuleRow.workspace_id == workspace_id)
    if asset:
        q = q.where(QualityRuleRow.asset == asset)
    if status:
        q = q.where(QualityRuleRow.status == status)
    return list(session.scalars(q.order_by(QualityRuleRow.asset, QualityRuleRow.name, QualityRuleRow.id)))


def history(session: Session, rule_id: str, limit: int = HISTORY_LIMIT) -> list[QualityRuleResult]:
    return list(session.scalars(select(QualityRuleResult).where(QualityRuleResult.rule_id == rule_id)
                                .order_by(QualityRuleResult.run_at.desc(), QualityRuleResult.id.desc())
                                .limit(max(1, min(limit, 500)))))


# ------------------------------------------------------------------------------------ running
def _with_dialect(run_sql: Any, dialect: str) -> Any:
    """The gateway runner carries its source dialect; a plain callable (a test double) is given the scope's."""
    if getattr(run_sql, "dialect", None):
        return run_sql

    def bound(sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> Any:
        return run_sql(sql, purpose=purpose, max_rows=max_rows)

    bound.dialect = dialect  # type: ignore[attr-defined]
    return bound


def run_rules(session: Session, runner_user: User, workspace_id: str, *, rule_ids: list[str] | None = None,
              assets: list[str] | None = None, trigger: str, gateway: Any = None) -> list[dict[str, Any]]:
    """Run active rules through the gateway as `runner_user` (their scope, row filters and column denials apply).
    A rule whose asset is outside that scope is an `error` result, never a silent pass."""
    from analystos.governance.policy import resolve_scope

    if gateway is None:
        from analystos.runtime.context import default_gateway

        gateway = default_gateway()
    rows = [r for r in list_rules(session, workspace_id, status="active")
            if (rule_ids is None or r.id in rule_ids) and (assets is None or r.asset in assets)]
    if not rows:
        return []
    scope = resolve_scope(session, runner_user, workspace_id, minimum_role="analyst")
    runners: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    now = utcnow()
    for row in rows:
        rule = QualityRule.model_validate(row.definition)
        if row.asset not in scope.assets:
            outcome = RuleOutcome(status="error", pass_rate=None, error=f"{row.asset} is not in the runner's data scope")
        else:
            sid = scope.asset_sources[row.asset]
            run_sql = runners.get(sid)
            if run_sql is None:
                run_sql = runners[sid] = _with_dialect(gateway.run_sql_for(scope, actor=trigger, source_id=sid),
                                                       scope.source_dialects.get(sid, "postgres"))
            outcome = run_rule(run_sql, rule, columns=scope.columns.get(row.asset))
        res = QualityRuleResult(workspace_id=workspace_id, rule_id=row.id, rule_hash=row.content_hash, status=outcome.status,
                                pass_rate=outcome.pass_rate, evaluated=outcome.evaluated, passed_rows=outcome.passed_rows,
                                value=outcome.value, query_id=outcome.query_id, error=(outcome.error or None),
                                trigger=trigger[:80], run_at=now)
        session.add(res)
        row.last_status, row.last_pass_rate, row.last_run_at = outcome.status, outcome.pass_rate, now
        out.append({"rule_id": row.id, "name": row.name, "asset": row.asset, "column": row.column, "type": row.type,
                    "dimension": row.dimension, "severity": row.severity, "threshold": row.threshold, **outcome.as_dict()})
    session.flush()
    emit(workspace_id, "quality_rule.evaluated", {"rules": len(out), "failed": sum(1 for o in out if o["status"] == "failed"),
                                                  "errors": sum(1 for o in out if o["status"] == "error"), "trigger": trigger},
         session=session)
    return out


# ------------------------------------------------------------------------------------ trust signal
def trust_level(passing: int, failing_warn: int, failing_critical: int, errors: int, unrun: int) -> str:
    """`failing` when a critical rule fails, `at_risk` when another fails or errors, `trusted` when every rule
    passed, `unverified` when no rule has a result yet."""
    total = passing + failing_warn + failing_critical + errors + unrun
    if total == 0:
        return "none"
    if failing_critical:
        return "failing"
    if failing_warn or errors:
        return "at_risk"
    if passing and not unrun:
        return "trusted"
    return "unverified" if not passing else "partial"


def asset_trust(session: Session, workspace_id: str, assets: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """`schema.table` -> its quality trust signal from active rules' latest results (one query)."""
    q = select(QualityRuleRow).where(QualityRuleRow.workspace_id == workspace_id, QualityRuleRow.status == "active")
    if assets is not None:
        if not assets:
            return {}
        q = q.where(QualityRuleRow.asset.in_(assets))
    grouped: dict[str, list[QualityRuleRow]] = defaultdict(list)
    for r in session.scalars(q):
        grouped[r.asset].append(r)
    out: dict[str, dict[str, Any]] = {}
    for fq, rules in grouped.items():
        ran = [r for r in rules if r.last_status is not None]
        passing = sum(1 for r in ran if r.last_status == "passed")
        fail_c = sum(1 for r in ran if r.last_status == "failed" and r.severity == "critical")
        fail_w = sum(1 for r in ran if r.last_status == "failed" and r.severity != "critical")
        errors = sum(1 for r in ran if r.last_status == "error")
        rates = [r.last_pass_rate for r in ran if r.last_pass_rate is not None]
        dims: dict[str, dict[str, int]] = defaultdict(lambda: {"rules": 0, "passing": 0})
        for r in rules:
            dims[r.dimension]["rules"] += 1
            dims[r.dimension]["passing"] += int(r.last_status == "passed")
        last = max((r.last_run_at for r in ran if r.last_run_at), default=None)
        out[fq] = {"level": trust_level(passing, fail_w, fail_c, errors, len(rules) - len(ran)), "rules": len(rules),
                   "passing": passing, "failing": fail_w + fail_c, "errors": errors, "unrun": len(rules) - len(ran),
                   "score": round(passing / len(rules), 4), "mean_pass_rate": round(sum(rates) / len(rates), 6) if rates else None,
                   "by_dimension": dict(sorted(dims.items())), "last_run_at": last,
                   "failing_rules": [{"id": r.id, "name": r.name, "severity": r.severity, "pass_rate": r.last_pass_rate}
                                     for r in ran if r.last_status in ("failed", "error")][:10]}
    return out

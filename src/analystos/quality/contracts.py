"""A data product's contract, judged (P2-9): freshness of the declared assets and the latest results of the
referenced quality rules. Deterministic and query-free: catalog `freshness_at` (set when a source is staged or
discovered) and `quality_rule.last_*` (written by the rule runs), so showing a product's state never touches the
source. The `data_contract` monitor (`quality/monitoring.py`) alerts on the same breaches.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.quality import DataContract
from analystos.contracts.tool_manifest import ToolManifestV2
from analystos.core.errors import InvalidInput
from analystos.core.ids import utcnow
from analystos.db.models import RegisteredTool, SourceAsset
from analystos.quality.models import QualityRuleRow

STALE_CRITICAL_FACTOR = 2.0  # older than twice the SLA: critical


def validate_contract(session: Session, workspace_id: str, contract: DataContract) -> None:
    """Every referenced rule exists and is active here; every freshness asset is in this workspace's catalog."""
    for rid in contract.quality_rules:
        r = session.get(QualityRuleRow, rid)
        if r is None or r.workspace_id != workspace_id:
            raise InvalidInput(f"contract names quality rule {rid}, which is not in this workspace")
        if r.status != "active":
            raise InvalidInput(f"contract names quality rule {rid}, which is {r.status}")
    for fq in contract.freshness.assets if contract.freshness else []:
        if _asset(session, workspace_id, fq) is None:
            raise InvalidInput(f"contract freshness names {fq}, which is not in this workspace's catalog")


def _asset(session: Session, workspace_id: str, fq: str) -> SourceAsset | None:
    schema, _, name = fq.partition(".")
    return session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                    SourceAsset.schema_name == schema, SourceAsset.name == name))


def product_contract(session: Session, workspace_id: str, tool_id: str) -> tuple[RegisteredTool, DataContract | None]:
    """The version a call would run (latest certified, else the ref's pin) and its contract."""
    from analystos.toolreg import store

    row = store.resolve(session, workspace_id, tool_id)
    m = ToolManifestV2.model_validate(row.manifest)
    return row, m.contract


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def judge(session: Session, workspace_id: str, contract: DataContract, *, now: datetime | None = None) -> dict[str, Any]:
    """{checks, breaches, ok}. A rule that has not run yet is a check in state `unknown`, not a breach."""
    now = _aware(now or utcnow())
    checks: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []

    def record(check: dict[str, Any]) -> None:
        checks.append(check)
        if check["state"] == "breach":
            breaches.append(check)

    if contract.freshness is not None:
        sla = contract.freshness.max_age_hours
        for fq in contract.freshness.assets:
            a = _asset(session, workspace_id, fq)
            loaded = a.freshness_at if a is not None else None
            if loaded is None:
                record({"key": f"freshness:{fq}", "kind": "freshness", "asset": fq, "state": "breach", "severity": "critical",
                        "max_age_hours": sla, "message": f"{fq} has no recorded load time (SLA {sla:g} h)"})
                continue
            age = (now - _aware(loaded)).total_seconds() / 3600
            ok = age <= sla
            record({"key": f"freshness:{fq}", "kind": "freshness", "asset": fq, "state": "ok" if ok else "breach",
                    "severity": "critical" if age > STALE_CRITICAL_FACTOR * sla else "warning",
                    "age_hours": round(age, 3), "max_age_hours": sla, "loaded_at": _aware(loaded).isoformat(),
                    "message": f"{fq} loaded {age:.1f} h ago" + ("" if ok else f", over its {sla:g} h SLA")})
    for rid in contract.quality_rules:
        r = session.get(QualityRuleRow, rid)
        key = f"rule:{rid}"
        if r is None or r.workspace_id != workspace_id or r.status != "active":
            record({"key": key, "kind": "quality_rule", "rule_id": rid, "state": "breach", "severity": "critical",
                    "message": f"quality rule {rid} is no longer active"})
            continue
        base = {"key": key, "kind": "quality_rule", "rule_id": rid, "name": r.name, "asset": r.asset,
                "dimension": r.dimension, "pass_rate": r.last_pass_rate, "threshold": r.threshold,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None}
        if r.last_status is None:
            record({**base, "state": "unknown", "severity": "info", "message": f"{r.name} has not run yet"})
            continue
        below_min = (contract.min_pass_rate is not None and r.last_pass_rate is not None
                     and r.last_pass_rate < contract.min_pass_rate)
        if r.last_status in ("failed", "error") or below_min:
            what = "could not run" if r.last_status == "error" else (
                f"pass rate {r.last_pass_rate:.2%}" if r.last_pass_rate is not None else "failed")
            record({**base, "state": "breach", "severity": "critical" if r.severity == "critical" else "warning",
                    "message": f"{r.name} {what}"})
        else:
            record({**base, "state": "ok", "severity": "info", "message": f"{r.name} passes"})
    return {"checks": checks, "breaches": breaches, "ok": not breaches,
            "unknown": sum(1 for c in checks if c["state"] == "unknown"), "checked_at": now.isoformat()}


def contract_status(session: Session, workspace_id: str, tool_id: str) -> dict[str, Any]:
    row, contract = product_contract(session, workspace_id, tool_id)
    out: dict[str, Any] = {"tool_id": row.tool_id, "version": row.version, "has_contract": contract is not None}
    if contract is None:
        return {**out, "contract": None, "checks": [], "breaches": [], "ok": True, "unknown": 0}
    return {**out, "contract": contract.model_dump(mode="json"), **judge(session, workspace_id, contract)}

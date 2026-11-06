"""Monitors over declared quality rules and data-product contracts (P1-7, P2-9), evaluated by
`services/monitors.evaluate_monitor` on its schedule like every other kind.

* `quality_rules` — config `{rules?: [rule ids], assets?: [schema.table]}` (both empty: every active rule of the
  workspace). Runs the rules through the gateway as the monitor's owner; alerts when a warning or critical rule
  fails or cannot run. The alert's identity is the set of failing rules, so the same failures do not alert twice.
* `data_contract` — config `{tool_id}`. Reads the product's `contract` block and judges it from stored state only
  (catalog freshness, the rules' latest results): no query. Alerts on any breach.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput

KINDS = frozenset({"quality_rules", "data_contract"})
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def validate(session: Session, workspace_id: str, kind: str, config: dict) -> None:
    from analystos.quality.models import QualityRuleRow

    if kind == "quality_rules":
        ids = config.get("rules") or []
        assets = config.get("assets") or []
        if not isinstance(ids, list) or not isinstance(assets, list):
            raise InvalidInput("quality_rules monitors take config.rules (rule ids) and config.assets (schema.table) lists")
        for rid in ids:
            r = session.get(QualityRuleRow, str(rid))
            if r is None or r.workspace_id != workspace_id:
                raise InvalidInput(f"quality rule {rid} not found in this workspace")
        return
    if kind == "data_contract":
        from analystos.quality.contracts import product_contract

        if not isinstance(config.get("tool_id"), str):
            raise InvalidInput("a data_contract monitor needs config.tool_id (a published data product)")
        row, contract = product_contract(session, workspace_id, config["tool_id"])
        if contract is None:
            raise InvalidInput(f"{row.tool_id}@{row.version} has no contract block to monitor")
        return
    raise InvalidInput(f"not a quality monitor kind: {kind}")


def evaluate(session: Session, owner: Any, monitor: Any) -> dict[str, Any]:
    if monitor.kind == "quality_rules":
        return _evaluate_rules(session, owner, monitor)
    return _evaluate_contract(session, monitor)


def _evaluate_rules(session: Session, owner: Any, monitor: Any) -> dict[str, Any]:
    from analystos.quality.service import run_rules

    cfg = monitor.config or {}
    results = run_rules(session, owner, monitor.workspace_id, rule_ids=cfg.get("rules") or None,
                        assets=cfg.get("assets") or None, trigger=f"monitor:{monitor.id}")
    bad = [r for r in results if r["status"] in ("failed", "error") and r["severity"] != "info"]
    severity = max((r["severity"] for r in bad), key=SEVERITY_RANK.__getitem__, default="warning")
    summary = {"rules": len(results), "passed": sum(1 for r in results if r["status"] == "passed"),
               "failed": sum(1 for r in results if r["status"] == "failed"),
               "errors": sum(1 for r in results if r["status"] == "error")}
    msgs = [f"{r['name']}: {'error ' + str(r['error']) if r['status'] == 'error' else _rate(r)}" for r in bad[:5]]
    return {"alert": bool(bad), "severity": severity, "results": results, "summary": summary,
            "new": sorted(r["rule_id"] for r in bad),
            "message": ("; ".join(msgs) if bad else f"All {len(results)} quality rule(s) pass.") if results
            else "No active quality rules to run."}


def _rate(r: dict[str, Any]) -> str:
    if r.get("value") is not None:
        return f"value {r['value']:g} outside [{r.get('detail', {}).get('min')}, {r.get('detail', {}).get('max')}]"
    rate = r.get("pass_rate")
    return f"pass rate {rate:.2%} < {r['threshold']:.2%}" if isinstance(rate, float) else "failed"


def _evaluate_contract(session: Session, monitor: Any) -> dict[str, Any]:
    from analystos.quality.contracts import contract_status

    status = contract_status(session, monitor.workspace_id, (monitor.config or {})["tool_id"])
    breaches = status["breaches"]
    severity = "critical" if any(b.get("severity") == "critical" for b in breaches) else "warning"
    return {"alert": bool(breaches), "severity": severity, "contract": status,
            "new": sorted(b["key"] for b in breaches),
            "message": "; ".join(b["message"] for b in breaches[:5]) if breaches
            else f"{status['tool_id']}@{status['version']} meets its contract."}

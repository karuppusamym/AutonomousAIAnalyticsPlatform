from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.inspection import inspect


def row(obj: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    if obj is None:
        return None  # type: ignore[return-value]
    out = {}
    for attr in inspect(obj).mapper.column_attrs:
        key = attr.key
        if exclude and key in exclude:
            continue
        value = getattr(obj, key)
        if key == "capabilities" and isinstance(value, dict) and "manifests" in value:
            value = {k: v for k, v in value.items() if k != "manifests"}  # a run's bound manifests: refs are enough here
        if key == "origin" and isinstance(value, dict) and isinstance(value.get("reviewed_context"), dict):
            pinned = value["reviewed_context"]
            # The snapshot is internal context, not a second route for revealing business fact values.
            value = {**value, "reviewed_context": {k: v for k, v in pinned.items() if k != "facts"}}
            value["reviewed_context"]["fact_count"] = len(pinned.get("facts") or [])
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def rows(objs, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    return [row(o, exclude) for o in objs]


def standing(bundle: Any) -> dict[str, Any]:
    """P8-15: a finding's strength (weak | moderate | strong) and its held-out confirmation record, lifted
    out of the evidence bundle so the UI need not read the bundle; None for findings recorded before."""
    validation = (bundle.get("validation") or {}) if isinstance(bundle, dict) else {}
    return {"strength": validation.get("strength"), "holdout": validation.get("holdout")}


def with_verification(session: Any, insights: Any) -> list[dict[str, Any]]:
    """Insight rows with their verification record's state (P7-01): a VOID one carries its cause."""
    from analystos.evidence.verification import insight_states

    found = list(insights)
    states = insight_states(session, [i.id for i in found])
    return [{**row(i), "verification_state": states[i.id], **standing(i.evidence_bundle)} for i in found]

"""Publication reconcile (S08-T5): compare what Superset holds with the latest publication.

Drift kinds per object: `missing` (deleted in the BI tool), `changed` (its provenance stamp no longer
names the approved bundle hash: someone edited it there), `unpublished` (a dashboard set back to draft).
The job always records and flags drift (`publication.drift`, event `publication.drift_detected`).
With `repair=True` it re-applies exactly the approved bundle through the idempotent publisher, after
`verify_for_reapply` re-checked the executed approval's payload hash, plan, policy version and both
people's current rights. Anything that would publish different content needs a new approval.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.bi import PublishBundle
from analystos.core.errors import AnalystOSError
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Artifact, Publication, User
from analystos.publishing.base import bundle_hash
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
INTERVAL = timedelta(hours=1)


def _bundle_of(approval: Approval | None) -> tuple[PublishBundle | None, dict[str, Any] | None]:
    """(bundle, the approved payload) for a bundle-bound publication; Ask chart publications have none."""
    if approval is None:
        return None, None
    payload = dict(approval.payload or {})
    raw = payload.get("bundle", payload)
    try:
        return PublishBundle.model_validate(raw), payload
    except ValueError:
        return None, payload


def inspect(publisher: Any, external_ids: dict[str, Any], expected_hash: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for kind, resource in (("dashboards", "dashboard"), ("charts", "chart"), ("datasets", "dataset")):
        for key, oid in sorted((external_ids.get(kind) or {}).items()):
            state = publisher.object_state(resource, oid)
            if state is None:
                items.append({"kind": resource, "key": key, "id": oid, "drift": "missing"})
                continue
            if resource == "dashboard":
                if f"bundle={expected_hash}" not in str(state.get("css") or ""):
                    items.append({"kind": resource, "key": key, "id": oid, "drift": "changed"})
                elif state.get("published") is False:
                    items.append({"kind": resource, "key": key, "id": oid, "drift": "unpublished"})
            elif resource == "chart":
                try:
                    params = json.loads(state.get("params") or "{}")
                except (TypeError, ValueError):
                    params = {}
                if params.get("bundle_hash") not in (None, expected_hash):
                    items.append({"kind": resource, "key": key, "id": oid, "drift": "changed"})
    return items


def _reapply_payload(session: Session, pub: Publication, approval: Approval, payload: dict[str, Any]) -> tuple[dict, str | None]:
    """The payload the approval must still bind and its plan hash. A builder publication re-derives it
    from the stored artifact version (an edit since forked a draft, so it is unchanged unless tampered)."""
    if pub.artifact_id:
        from analystos.dashboards.service import publish_payload

        art = session.get(Artifact, pub.artifact_id)
        requester = session.get(User, approval.requested_by)
        if art is None or requester is None or art.version != pub.artifact_version:
            raise AnalystOSError("the published dashboard version is no longer stored; publish it again")
        return publish_payload(session, requester, art, pub.destination, list(payload.get("audience") or [])), None
    run = session.get(AnalysisRun, pub.run_id) if pub.run_id else None
    return payload, run.plan_hash if run else None


def reconcile_publication(session: Session, pub: Publication, *, publisher: Any, repair: bool = False) -> dict[str, Any]:
    from analystos.events.bus import emit
    from analystos.governance.approvals import verify_for_reapply
    from analystos.governance.audit import audit

    approval = session.get(Approval, pub.approval_id) if pub.approval_id else None
    bundle, payload = _bundle_of(approval)
    if bundle is None or approval is None or payload is None:
        return {"publication_id": pub.id, "status": "skipped", "reason": "not bundle-bound"}
    expected = bundle_hash(bundle)
    items = inspect(publisher, pub.external_ids or {}, expected)
    status = "in_sync" if not items else "drifted"
    repaired: dict[str, Any] | None = None
    if items and repair:
        try:
            bound, plan_hash = _reapply_payload(session, pub, approval, payload)
            verify_for_reapply(session, approval.id, payload=bound, plan_hash=plan_hash)
            result = publisher.publish(bundle, idempotency_key=pub.idempotency_key, previous=pub.external_ids)
            if result.status == "succeeded":
                pub.external_ids = result.external_ids
                items = inspect(publisher, result.external_ids, expected)
                status = "repaired" if not items else "drifted"
            repaired = {"status": result.status, "errors": result.errors[:3]}
        except AnalystOSError as exc:
            repaired = {"status": "refused", "error": exc.message[:300]}
    pub.drift = {"status": status, "items": items, "bundle_hash": expected, **({"repair": repaired} if repaired else {})}
    pub.reconciled_at = utcnow()
    if status != "in_sync":
        emit(pub.workspace_id, "publication.drift_detected", {"publication_id": pub.id, "status": status,
                                                              "items": items[:20]}, session=session)
        audit("system:reconcile", "publication.drift", workspace_id=pub.workspace_id, target=pub.id,
              decision="allow" if status == "repaired" else "flag", details={"items": items[:20], "repair": repaired},
              session=session)
    return {"publication_id": pub.id, "status": status, "items": items, **({"repair": repaired} if repaired else {})}


def latest_publications(session: Session, workspace_id: str) -> list[Publication]:
    """The newest succeeded Superset publication per artifact (builder), per run and dashboard selection
    (a collection published on its own), or per run."""
    from analystos.dashboards.collections import selection_of

    rows = session.scalars(select(Publication).where(Publication.workspace_id == workspace_id,
                                                     Publication.destination == "superset",
                                                     Publication.status.in_(["succeeded", "partial"]))
                           .order_by(Publication.created_at.desc()).limit(500))
    seen, out = set(), []
    for pub in rows:
        key = pub.artifact_id or selection_of(pub) or pub.run_id or pub.id
        if key not in seen:
            seen.add(key)
            out.append(pub)
    return out


@owned_by("workspace", "workspace_id")
def reconcile_workspace(workspace_id: str, *, repair: bool = False, publisher: Any = None) -> list[dict[str, Any]]:
    from analystos.core.config import get_settings
    from analystos.publishing.base import get_publisher

    with session_scope() as s:
        pubs = latest_publications(s, workspace_id)
        if not pubs:
            return []
        publisher = publisher or get_publisher("superset", get_settings())
        return [reconcile_publication(s, p, publisher=publisher, repair=repair) for p in pubs]


_last_run: dict[str, Any] = {"at": None}


@platform_loop
def reconcile_all_quietly(*, repair: bool = False) -> dict[str, Any]:
    """Scheduler housekeeping: at most hourly, flag drift for every workspace with Superset publications.
    Never fatal to the loop; repair is off here (an operator repairs through the API)."""
    from analystos.core.config import get_settings

    now = utcnow()
    if not get_settings().superset_url or (_last_run["at"] and now - _last_run["at"] < INTERVAL):
        return {"skipped": True}
    _last_run["at"] = now
    with session_scope() as s:
        workspaces = sorted({w for (w,) in s.execute(select(Publication.workspace_id).where(
            Publication.destination == "superset", Publication.status.in_(["succeeded", "partial"])).distinct())})
    out: dict[str, Any] = {"workspaces": 0, "drifted": 0}
    for ws in workspaces:
        try:
            results = reconcile_workspace(ws, repair=repair)
        except Exception:  # noqa: BLE001 - one workspace's BI outage never stops the others
            log.exception("publication reconcile failed for %s", ws)
            continue
        out["workspaces"] += 1
        out["drifted"] += sum(1 for r in results if r["status"] == "drifted")
    return out

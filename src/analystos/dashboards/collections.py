"""Publish a chosen topic collection on its own; group existing BI dashboards under the workspace name (BI-VIEWS-02).

An investigation's design step writes several dashboards (executive, operational and topic views). The run's own
publication approval covers all of them. Here a person picks some of them and publishes only those, under an
approval bound to exactly that selection: the payload names the run, its plan hash, the sorted keys and the bundle
built from them, so another selection, a changed chart or KPI, or a replan is a different hash. The payload is
re-derived from the stored artifacts and checked with `verify_for_execution` immediately before the BI call.

Grouping: generated dashboards are titled ``Workspace / Topic``; dashboards published before that convention keep
older titles in Superset. A grouping update renames them only after an approval of the exact list of
{id, slug, current title, new title}. The current titles are re-read from Superset right before acting, so a title
someone changed there since the proposal invalidates the approval instead of being overwritten.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle
from analystos.core.errors import AnalystOSError, Conflict, InvalidInput, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import new_id
from analystos.db.models import AnalysisRun, Approval, Artifact, Publication, User
from analystos.governance.policy import get_workspace, load_in_workspace, load_policy, require_role, scoped_loader

COLLECTION_ACTION = "dashboard.collection_publish"
GROUPING_ACTION = "dashboard.grouping_update"
SEPARATOR = " / "
KEY_PREFIX = "collection:"
_MAX_TITLE = 500  # Superset's dashboard_title column


# ------------------------------------------------------------------------------------------- collections
@scoped_loader
def load_run(session: Session, user: User, run_id: str, *, minimum: str = "viewer") -> AnalysisRun:
    return load_in_workspace(session, AnalysisRun, run_id, user=user, minimum=minimum, label="investigation")


def _run_artifacts(session: Session, run: AnalysisRun) -> dict[str, Any]:
    """The run's dataset and its current plan's metrics, charts and dashboards (as `load_bundle_parts`, in this session)."""
    from analystos.artifacts.registry import current_plan_filter

    arts = list(session.scalars(select(Artifact).where(Artifact.run_id == run.id,
                                                       Artifact.type.in_(["metric", "chart", "dashboard"]),
                                                       current_plan_filter(run))
                                .order_by(Artifact.created_at, Artifact.name)))
    ds = session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "dataset")
                        .order_by(Artifact.created_at.desc()))
    return {"dataset": ds,
            "metrics": [MetricDef.model_validate(a.content) for a in arts if a.type == "metric"],
            "charts": {a.name: ChartSpec.model_validate(a.content) for a in arts if a.type == "chart"},
            "dashboards": {a.name: (a, DashboardSpec.model_validate(a.content)) for a in arts if a.type == "dashboard"}}


def kind_of(key: str) -> str:
    return "topic" if key.startswith("topic_") else key if key in ("executive", "operational") else "other"


def collection_bundle(session: Session, run: AnalysisRun, keys: list[str], destination: str) -> PublishBundle:
    """Only the chosen dashboards, the charts they show (previews stripped), the KPIs those charts use and the run's
    dataset. A chart presenting a VOID finding is refused; KPIs get their approved definitions stamped (as for a run)."""
    from analystos.evidence.verification import gate_publish
    from analystos.publishing.preview import chart_metric_names
    from analystos.semantic.service import gate_bundle

    chosen = sorted(set(keys))
    if not chosen:
        raise InvalidInput("choose at least one dashboard to publish")
    parts = _run_artifacts(session, run)
    unknown = [k for k in chosen if k not in parts["dashboards"]]
    if unknown:
        raise InvalidInput(f"this investigation has no current dashboard {', '.join(unknown)}",
                           details={"available": sorted(parts["dashboards"])})
    if parts["dataset"] is None:
        raise InvalidInput("this investigation has no dataset to publish")
    content = dict(parts["dataset"].content)
    dataset = DatasetDef.model_validate({k: content[k] for k in DatasetDef.model_fields if k in content})
    dashboards = [parts["dashboards"][k][1] for k in chosen]
    chart_keys = list(dict.fromkeys(c for d in dashboards for c in d.charts))
    missing = [k for k in chart_keys if k not in parts["charts"]]
    if missing:
        raise InvalidInput(f"the chosen dashboards show charts this investigation no longer has: {', '.join(missing)}")
    charts = [ChartSpec.model_validate({**parts["charts"][k].model_dump(), "preview": {}}) for k in chart_keys]
    used = {n for c in charts for n in chart_metric_names(c)}
    metrics = [m for m in parts["metrics"] if m.name in used]
    bundle = PublishBundle(workspace_id=run.workspace_id, destination=destination, datasets=[dataset], metrics=metrics,
                           charts=charts, dashboards=dashboards)
    gate_publish(session, run.id, [code for c in charts for code in c.insight_codes])
    ws = get_workspace(session, run.workspace_id)
    # Unapproved KPIs stay in the bundle as proposed; the approver's one decision approves them (as for a run).
    policy = load_policy(session, ws).model_copy(update={"require_approved_metrics": False})
    return gate_bundle(session, run.workspace_id, policy, bundle)


def collection_payload(session: Session, run: AnalysisRun, keys: list[str], destination: str) -> dict[str, Any]:
    """Exactly what a collection approval binds to, derived from the stored artifacts."""
    bundle = collection_bundle(session, run, keys, destination)
    return {"run_id": run.id, "plan_hash": run.plan_hash, "collection": sorted(set(keys)), "destination": destination,
            "bundle": bundle.model_dump(mode="json")}


def _pending_kpis(session: Session, workspace_id: str, bundle: PublishBundle) -> list[str]:
    ws = get_workspace(session, workspace_id)
    if not load_policy(session, ws).require_approved_metrics:
        return []
    return [m.name for m in bundle.metrics if m.status != "approved"]


def _review(session: Session, requester: User, bundle: PublishBundle, allowed: list[str]) -> list[str]:
    """The governance review a run publication gets: structure, every dataset's SQL against the requester's current
    scope (a restricted column cannot leak into a published dataset), and the destination policy."""
    from analystos.gateway.validator import validate_sql
    from analystos.governance.policy import resolve_scope
    from analystos.publishing.preview import validate_bundle

    problems = list(validate_bundle(bundle))
    scope = resolve_scope(session, requester, bundle.workspace_id, minimum_role="editor")
    for ds in bundle.datasets:
        try:
            validate_sql(scope, ds.sql, max_rows=1)
        except AnalystOSError as exc:
            problems.append(f"dataset {ds.name}: {exc.message}")
    if bundle.destination != "preview" and bundle.destination not in allowed:
        problems.append(f"destination {bundle.destination} is not allowed by the workspace policy")
    return problems


def _collection_requests(session: Session, run: AnalysisRun) -> list[Approval]:
    rows = session.scalars(select(Approval).where(Approval.workspace_id == run.workspace_id,
                                                  Approval.action == COLLECTION_ACTION)
                           .order_by(Approval.created_at.desc()).limit(200))
    return [a for a in rows if (a.payload or {}).get("run_id") == run.id]


def _publication_of(session: Session, approval_id: str) -> Publication | None:
    return session.scalar(select(Publication).where(Publication.approval_id == approval_id)
                          .order_by(Publication.created_at.desc()))


def overview(session: Session, user: User, run_id: str) -> dict[str, Any]:
    """The run's dashboards a person can publish on their own, and the collection requests made so far."""
    from analystos.services.ask_outputs import usable_destination

    run = load_run(session, user, run_id)
    parts = _run_artifacts(session, run)
    ws = get_workspace(session, run.workspace_id)
    try:
        destination, note = usable_destination(load_policy(session, ws).publish_destinations, None)
    except AnalystOSError as exc:
        destination, note = None, exc.message
    requests = []
    for a in _collection_requests(session, run):
        pub = _publication_of(session, a.id)
        requests.append({"approval_id": a.id, "status": a.status, "collection": list(a.payload.get("collection") or []),
                         "destination": a.destination, "payload_hash": a.payload_hash, "risk_tier": a.risk_tier,
                         "created_at": a.created_at.isoformat() if a.created_at else None,
                         "expires_at": a.expires_at.isoformat() if a.expires_at else None, "reason": a.reason,
                         "publication": {"id": pub.id, "status": pub.status, "error": pub.error} if pub else None})
    return {"run_id": run.id, "workspace_id": run.workspace_id, "destination": destination, "destination_note": note,
            "dashboards": [{"key": key, "title": spec.title, "kind": kind_of(key), "audience": spec.audience,
                            "charts": len(spec.charts), "artifact_id": art.id, "status": art.status,
                            "external_url": art.external_url}
                           for key, (art, spec) in sorted(parts["dashboards"].items(),
                                                           key=lambda kv: (kind_of(kv[0]) == "topic", kv[1][1].title))],
            "requests": requests}


def request_collection(session: Session, user: User, run_id: str, keys: list[str], *,
                       destination: str | None = None) -> Approval:
    """Ask for an approval to publish the chosen dashboards on their own. Never publishes."""
    from analystos.events.bus import emit
    from analystos.governance.approvals import request_approval
    from analystos.services.ask_outputs import usable_destination

    run = load_run(session, user, run_id, minimum="editor")
    ws = get_workspace(session, run.workspace_id)
    policy = load_policy(session, ws)
    destination, _ = usable_destination(policy.publish_destinations, destination)
    payload = collection_payload(session, run, keys, destination)
    bundle = PublishBundle.model_validate(payload["bundle"])
    problems = _review(session, user, bundle, policy.publish_destinations)
    if problems:
        raise PolicyDenied("governance review failed: " + "; ".join(problems[:3]), details={"problems": problems})
    pending = _pending_kpis(session, run.workspace_id, bundle)
    parts = _run_artifacts(session, run)
    approval = request_approval(
        session, workspace_id=run.workspace_id, run_id=None, action=COLLECTION_ACTION, payload=payload,
        plan_hash=run.plan_hash, policy_version=ws.policy_version, requested_by=user.id, risk_tier="high",
        destination=destination, affected_assets=[parts["dashboards"][k][0].id for k in payload["collection"]],
        evidence={"governance_review": {"ok": True, "problems": []}, "run_id": run.id,
                  "dashboards": [{"key": d.key, "title": d.title} for d in bundle.dashboards],
                  "charts": len(bundle.charts), "metrics": len(bundle.metrics),
                  "risk_tier_basis": "deterministic: publication is always high risk and approval-gated",
                  **({"kpis_to_approve": [{"name": m.name, "expression": m.sql_expression, "definition": m.definition}
                                          for m in bundle.metrics if m.name in pending]} if pending else {})})
    emit(run.workspace_id, "dashboard.publish_requested",
         {"approval_id": approval.id, "destination": destination, "collection": payload["collection"]},
         run_id=run.id, actor=f"user:{user.id}", session=session)
    return approval


def idempotency_key(run_id: str, keys: list[str], payload_hash: str) -> str:
    """Per run and selection, so reconcile keeps the newest publication of each selection apart."""
    selection = hashlib.sha256("\x1f".join(sorted(set(keys))).encode()).hexdigest()[:12]
    return f"{KEY_PREFIX}{run_id}:{selection}:{payload_hash[:32]}"


def selection_of(pub: Publication) -> str | None:
    """`collection:<run>:<selection>` for a collection publication, else None."""
    key = pub.idempotency_key or ""
    return key.rsplit(":", 1)[0] if key.startswith(KEY_PREFIX) else None


def _approve_kpis(session: Session, approval: Approval, names: list[str]) -> None:
    """The approver reviewed the KPI definitions listed in the request: each still-proposed one is approved in that
    person's name through the ordinary metric decision (role and separation of duties apply)."""
    from analystos.semantic.service import decide_metric

    approver = session.get(User, approval.decided_by) if approval.decided_by else None
    if approver is None:
        raise PolicyDenied("the publication approval names no approver to approve its KPIs")
    for name in names:
        try:
            decide_metric(session, approval.workspace_id, name, approver, approve=True,
                          reason=f"approved with publication approval {approval.id}")
        except AnalystOSError as exc:
            raise PolicyDenied(f"KPI {name} could not be approved with the publication: {exc.message}",
                               details={"metric": name}) from exc


def execute_collection(session: Session, user: User, approval_id: str, *, publisher: Any = None) -> dict[str, Any]:
    """Re-derive the payload from the stored artifacts, verify the approval against it immediately before the BI call,
    then publish only the chosen dashboards. Single use; a failed BI call restores the approval so a retry reconciles."""
    from analystos.artifacts.registry import link
    from analystos.core.config import get_settings
    from analystos.events.bus import emit
    from analystos.governance.approvals import claim, consume, verify_for_execution
    from analystos.governance.audit import audit
    from analystos.publishing.base import get_publisher
    from analystos.services.platform_settings import get as platform

    approval = load_in_workspace(session, Approval, approval_id, user=user, minimum="editor", label="approval")
    if approval.action != COLLECTION_ACTION:
        raise InvalidInput("this approval is not a request to publish a dashboard collection")
    payload = dict(approval.payload or {})
    run = session.get(AnalysisRun, payload.get("run_id"))
    if run is None or run.workspace_id != approval.workspace_id:
        raise InvalidInput("the investigation this collection came from no longer exists")
    destination = str(payload.get("destination"))
    current = collection_payload(session, run, list(payload.get("collection") or []), destination)
    verify_for_execution(session, approval.id, payload=current, plan_hash=run.plan_hash)
    if destination == "superset" and not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing was turned off by the administrator after this approval was granted")
    bundle = PublishBundle.model_validate(current["bundle"])
    _approve_kpis(session, approval, _pending_kpis(session, run.workspace_id, bundle))
    consume(session, approval)
    key = idempotency_key(run.id, current["collection"], approval.payload_hash)
    pub = session.scalar(select(Publication).where(Publication.idempotency_key == key))
    previous = dict(pub.external_ids or {}) if pub else None
    if pub is None:
        pub = Publication(id=new_id("pub"), workspace_id=run.workspace_id, run_id=run.id, approval_id=approval.id,
                          destination=destination, idempotency_key=key, status="started")
        session.add(pub)
    session.flush()
    publisher = publisher or get_publisher(destination, get_settings())
    try:
        result = publisher.publish(bundle, idempotency_key=key, previous=previous)
    except AnalystOSError as exc:
        result, error = None, exc.message
    else:
        error = "; ".join(result.errors)
    if result is None or result.status != "succeeded":
        pub.status = "failed" if result is None else result.status
        pub.error = error[:1000] or None
        if result is not None:
            pub.external_ids = result.external_ids
        claim(session, approval, expect="executed", to="approved")
        audit(f"user:{user.id}", "publication.failed", workspace_id=run.workspace_id, run_id=run.id, target=pub.id,
              decision="error", details={"destination": destination, "approval_id": approval.id,
                                         "collection": current["collection"], "error": error[:300]}, session=session)
        session.flush()
        raise UpstreamUnavailable(f"publishing to {destination} failed: {error[:300]}. The approval stays usable: retry.",
                                  details={"publication_id": pub.id})
    pub.status, pub.external_ids, pub.error = "succeeded", result.external_ids, None
    names = {("dashboard", k) for k in (result.external_ids.get("dashboards") or {})} | \
        {("chart", k) for k in (result.external_ids.get("charts") or {})} | \
        {("dataset", k) for k in (result.external_ids.get("datasets") or {})}
    for art in session.scalars(select(Artifact).where(Artifact.run_id == run.id,
                                                      Artifact.type.in_(["dashboard", "chart", "dataset"]))):
        if (art.type, art.name) not in names:
            continue
        ext = (result.external_ids.get(f"{art.type}s") or {}).get(art.name)
        art.platform, art.external_id, art.status = destination, str(ext), "published"
        if art.type == "dashboard":
            art.external_url = result.urls.get(art.name)
        link(session, run.workspace_id, (art.type, art.id), "published_as", ("publication", pub.id), run_id=run.id)
    emit(run.workspace_id, "dashboard.published", {"destination": destination, "status": "succeeded", "urls": result.urls,
                                                   "collection": current["collection"], "publication_id": pub.id},
         run_id=run.id, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "publication.executed", workspace_id=run.workspace_id, run_id=run.id, target=pub.id,
          decision="allow", details={"destination": destination, "approval_id": approval.id,
                                     "collection": current["collection"]}, session=session)
    session.flush()
    return {"status": "published", "publication_id": pub.id, "collection": current["collection"],
            "destination": destination, "urls": result.urls}


def shared_objects(session: Session, pub: Publication) -> dict[str, set[str]]:
    """Charts and datasets of `pub` that another live publication in the workspace also placed: rolling a collection
    back must not delete a chart the run's executive dashboard (or another collection) still shows."""
    others = session.scalars(select(Publication).where(Publication.workspace_id == pub.workspace_id,
                                                       Publication.destination == pub.destination,
                                                       Publication.id != pub.id,
                                                       Publication.status.in_(["succeeded", "partial"])))
    shared: dict[str, set[str]] = {"charts": set(), "datasets": set()}
    for other in others:
        for kind in shared:
            shared[kind] |= {str(v) for v in ((other.external_ids or {}).get(kind) or {}).values()}
    return shared


def rollback_ids(session: Session, pub: Publication) -> dict[str, Any]:
    """What a collection publication's rollback removes: its dashboards, and its charts and datasets no other live
    publication uses. The database connection is never removed here."""
    ids = dict(pub.external_ids or {})
    shared = shared_objects(session, pub)
    out = {"dashboards": dict(ids.get("dashboards") or {})}
    for kind in ("charts", "datasets"):
        out[kind] = {k: v for k, v in (ids.get(kind) or {}).items() if str(v) not in shared[kind]}
    return out


# --------------------------------------------------------------------------------------------- grouping
def grouped_title(group: str, title: str) -> str:
    """``Workspace / title``; a title already in the group is kept as is."""
    title = title.strip()
    prefix = f"{group}{SEPARATOR}"
    return title if title.startswith(prefix) else f"{prefix}{title}"[:_MAX_TITLE]


def _marker_workspace(css: str) -> str | None:
    m = re.search(r"aos workspace=([a-z0-9_]+)", css or "")
    return m.group(1) if m else None


def _superset(publisher: Any = None) -> Any:
    from analystos.core.config import get_settings
    from analystos.publishing.base import get_dashboard_source
    from analystos.services.platform_settings import get as platform

    if publisher is not None:
        return publisher
    if not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing is turned off by the administrator")
    return get_dashboard_source("superset", get_settings())


def _own_dashboard(publisher: Any, workspace_id: str, dashboard_id: Any) -> dict[str, Any] | None:
    """The dashboard as Superset holds it now, when AnalystOS published it for this workspace: its slug carries the
    workspace prefix and its provenance stamp (when present) names this workspace."""
    from analystos.publishing.base import workspace_slug
    from analystos.publishing.superset import SupersetPublisher

    state = publisher.object_state("dashboard", dashboard_id)
    if not state:
        return None
    slug = str(state.get("slug") or "").lower()
    marker = _marker_workspace(str(state.get("css") or ""))
    if not slug.startswith(SupersetPublisher.dashboard_slug(workspace_id, "")) or \
            (marker is not None and marker != workspace_slug(workspace_id)):
        return None
    return state


def _change(state: dict[str, Any], group: str) -> dict[str, Any]:
    title = str(state.get("dashboard_title") or "")
    return {"id": int(state["id"]), "slug": state.get("slug"), "from": title, "to": grouped_title(group, title)}


def grouping_plan(session: Session, user: User, workspace_id: str, *, publisher: Any = None) -> dict[str, Any]:
    """Every dashboard AnalystOS published to Superset for this workspace, with the title the grouping would give it.
    Read only."""
    require_role(session, user, workspace_id, "viewer")
    ws = get_workspace(session, workspace_id)
    base = {"workspace_id": workspace_id, "group": ws.name, "separator": SEPARATOR}
    items = []
    try:
        publisher = _superset(publisher)
        for d in publisher.list_dashboards(workspace_id):
            state = _own_dashboard(publisher, workspace_id, d.get("id"))
            if state is None:
                continue
            change = _change(state, ws.name)
            items.append({**change, "url": d.get("url"), "published": bool(state.get("published", d.get("published"))),
                          "grouped": change["from"] == change["to"]})
    except AnalystOSError as exc:  # no BI tool here, or it is unreachable: say so instead of failing the page
        return {**base, "available": False, "reason": exc.message, "dashboards": [], "to_change": 0}
    items.sort(key=lambda i: (i["grouped"], i["from"].lower()))
    return {**base, "available": True, "reason": None, "dashboards": items,
            "to_change": sum(1 for i in items if not i["grouped"])}


def grouping_payload(session: Session, workspace_id: str, dashboard_ids: list[int], *, publisher: Any) -> dict[str, Any]:
    """Exactly what a grouping approval binds to: the chosen dashboards' current and new titles, read now."""
    ws = get_workspace(session, workspace_id)
    changes = []
    for did in sorted({int(i) for i in dashboard_ids}):
        state = _own_dashboard(publisher, workspace_id, did)
        if state is None:
            raise InvalidInput(f"dashboard {did} is not one this workspace published to Superset (or no longer exists)")
        change = _change(state, ws.name)
        if change["from"] != change["to"]:
            changes.append(change)
    if not changes:
        raise InvalidInput("the chosen dashboards are already grouped under this workspace's name")
    return {"workspace_id": workspace_id, "destination": "superset", "group": ws.name, "changes": changes}


def request_grouping(session: Session, user: User, workspace_id: str, dashboard_ids: list[int], *,
                     publisher: Any = None) -> Approval:
    """Ask for an approval to rename the chosen dashboards into the workspace's group. Never renames."""
    from analystos.events.bus import emit
    from analystos.governance.approvals import request_approval

    require_role(session, user, workspace_id, "editor")
    ws = get_workspace(session, workspace_id)
    if "superset" not in load_policy(session, ws).publish_destinations:
        raise PolicyDenied("workspace policy does not allow publishing to superset")
    publisher = _superset(publisher)
    payload = grouping_payload(session, workspace_id, dashboard_ids, publisher=publisher)
    approval = request_approval(
        session, workspace_id=workspace_id, run_id=None, action=GROUPING_ACTION, payload=payload, plan_hash=None,
        policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium", destination="superset",
        affected_assets=[f"superset:dashboard:{c['id']}" for c in payload["changes"]],
        evidence={"changes": len(payload["changes"]),
                  "note": "Titles only: charts, data, filters and access are unchanged."})
    emit(workspace_id, "dashboard.grouping_requested", {"approval_id": approval.id, "changes": len(payload["changes"])},
         actor=f"user:{user.id}", session=session)
    return approval


def apply_grouping(session: Session, user: User, workspace_id: str, approval_id: str, *,
                   publisher: Any = None) -> dict[str, Any]:
    """Re-read the approved dashboards' titles from Superset, verify the approval against them immediately before the
    write, then rename. Single use; a failed write restores the approval and says which titles changed."""
    from analystos.events.bus import emit
    from analystos.governance.approvals import claim, consume, verify_for_execution
    from analystos.governance.audit import audit

    require_role(session, user, workspace_id, "editor")
    approval = load_in_workspace(session, Approval, approval_id, workspace_id, user=user, minimum="editor",
                                 label="approval")
    if approval.action != GROUPING_ACTION:
        raise InvalidInput("this approval is not a dashboard grouping update")
    if approval.status == "executed":
        raise Conflict("this grouping update was already applied")
    publisher = _superset(publisher)
    ids = [c["id"] for c in (approval.payload or {}).get("changes") or []]
    try:
        current = grouping_payload(session, workspace_id, ids, publisher=publisher)
    except InvalidInput:
        current = {"changed": "a dashboard of this update was removed, moved or already renamed"}
    verify_for_execution(session, approval.id, payload=current, plan_hash=None)
    consume(session, approval)
    done: list[dict[str, Any]] = []
    try:
        for change in current["changes"]:
            publisher.rename_dashboard(change["id"], change["to"])
            done.append(change)
    except AnalystOSError as exc:
        if not done:  # nothing changed in Superset: the same approval can be retried
            claim(session, approval, expect="executed", to="approved")
        audit(f"user:{user.id}", "dashboard.grouping_failed", workspace_id=workspace_id, target=approval.id,
              decision="error", details={"renamed": done, "error": exc.message[:300]}, session=session)
        session.flush()
        then = "Retry with the same approval." if not done else "Ask for a new approval for the rest."
        raise UpstreamUnavailable(f"renaming in Superset failed after {len(done)} of {len(current['changes'])} "
                                  f"dashboard(s): {exc.message[:300]}. {then}",
                                  details={"renamed": [c["id"] for c in done]}) from None
    emit(workspace_id, "dashboard.grouping_applied", {"approval_id": approval.id, "changes": done},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "dashboard.grouping_applied", workspace_id=workspace_id, target=approval.id,
          decision="allow", details={"changes": done}, session=session)
    session.flush()
    return {"status": "applied", "approval_id": approval.id, "renamed": done}


__all__ = ["COLLECTION_ACTION", "GROUPING_ACTION", "apply_grouping", "collection_payload", "execute_collection",
           "grouped_title", "grouping_plan", "overview", "request_collection", "request_grouping", "rollback_ids"]

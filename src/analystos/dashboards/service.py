"""Builder dashboards: versioned persistence, the publish lifecycle and publication (S08 §4–§5).

A builder dashboard is an `artifact` of type `dashboard` whose content is a `DashboardDoc`
(`schema_version: dashboard.v2`). Each applied op batch is `ArtifactVersion + 1` with the ops as its
`diff`; undo is a revert to version N (a new version whose diff says so). Editing a published dashboard
forks a draft, so what viewers see only changes through a new approval.

Lifecycle (artifact.status): draft → previewed → validated → approval_required → approved → publishing →
published | failed | rolled_back. Publishing is a side effect bound to an approval of the exact payload
(artifact id, version, content hash, destination, audience and the publish bundle);
`verify_for_execution` re-derives that payload from the stored artifact immediately before the call.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.dashboard_dsl import SCHEMA_VERSION, SENSITIVITY_ORDER, DashboardDoc, derive_sensitivity
from analystos.core.errors import AnalystOSError, Conflict, InvalidInput, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import new_id, ordered_utcnow, stable_hash, utcnow
from analystos.dashboards.ops import OpResult, apply_ops, parse_ops
from analystos.dashboards.validate import validate_dashboard
from analystos.db.models import Approval, Artifact, ArtifactVersion, Publication, User
from analystos.governance.policy import load_in_workspace, scoped_loader

APPROVAL_ACTION = "dashboard.publish"
STATUSES = ("draft", "previewed", "validated", "approval_required", "approved", "publishing", "published", "failed",
            "rolled_back")
LIVE = frozenset({"publishing", "published"})  # an edit forks a draft
PENDING = frozenset({"approval_required", "approved"})  # an edit returns to draft (the approval no longer matches)


def is_builder(art: Artifact) -> bool:
    return art.type == "dashboard" and (art.content or {}).get("schema_version") == SCHEMA_VERSION


def document(art: Artifact) -> DashboardDoc:
    return DashboardDoc.model_validate(art.content)


def _check(raw: dict[str, Any]) -> DashboardDoc:
    doc, issues = validate_dashboard(raw)
    if doc is not None:
        doc = derive_sensitivity(doc)
        from analystos.dashboards.validate import problems

        issues = problems(doc)
    if doc is None or issues:
        raise InvalidInput("the dashboard document is not valid", details={"problems": [p.as_dict() for p in issues]})
    return doc


@scoped_loader
def load(session: Session, user: User, artifact_id: str, *, minimum: str = "viewer", for_update: bool = False) -> Artifact:

    art = load_in_workspace(session, Artifact, artifact_id, user=user, minimum=minimum, label="dashboard",
                            for_update=for_update)
    if not is_builder(art):
        raise InvalidInput(f"artifact {artifact_id} is not a builder dashboard (schema {SCHEMA_VERSION})")
    return art


def _event(session: Session, art: Artifact, kind: str, payload: dict[str, Any], user: User) -> None:
    from analystos.events.bus import emit

    emit(art.workspace_id, kind, {"artifact_id": art.id, "version": art.version, **payload}, actor=f"user:{user.id}",
         session=session)


def view(art: Artifact) -> dict[str, Any]:
    return {"id": art.id, "workspace_id": art.workspace_id, "name": art.name, "version": art.version,
            "status": art.status, "platform": art.platform, "external_id": art.external_id,
            "external_url": art.external_url, "content_hash": art.content_hash, "document": art.content,
            "updated_at": art.updated_at.isoformat() if art.updated_at else None}


def create(session: Session, user: User, workspace_id: str, raw: dict[str, Any], *,
           diff: list[dict[str, Any]] | None = None) -> Artifact:
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role

    require_role(session, user, workspace_id, "editor")
    doc = _check(raw)
    name = f"dashboard:{doc.id}"
    clash = session.scalar(select(Artifact.id).where(Artifact.workspace_id == workspace_id, Artifact.run_id.is_(None),
                                                     Artifact.type == "dashboard", Artifact.name == name))
    if clash:
        raise Conflict(f"a builder dashboard {doc.id!r} already exists in this workspace ({clash})")
    content = doc.model_dump(mode="json")
    art = Artifact(id=new_id("art"), workspace_id=workspace_id, run_id=None, type="dashboard", name=name, version=1,
                   status="draft", creator_user=user.id, content=content, content_hash=stable_hash(content),
                   created_at=ordered_utcnow())
    session.add(art)
    session.flush()
    session.add(ArtifactVersion(artifact_id=art.id, version=1, content=content, content_hash=art.content_hash,
                                created_by=f"user:{user.id}", diff=diff or [{"op": "create"}]))
    origin = ((diff or [{}])[0].get("origin") or {}).get("kind")
    _event(session, art, "dashboard.created", {"builder": True, **({"origin": origin} if origin else {})}, user)
    audit(f"user:{user.id}", "dashboard.created", workspace_id=workspace_id, target=art.id, session=session)
    return art


def _new_version(session: Session, art: Artifact, doc: DashboardDoc, diff: list[dict[str, Any]], user: User) -> None:
    content = doc.model_dump(mode="json")
    art.version += 1
    art.content, art.content_hash = content, stable_hash(content)
    session.add(ArtifactVersion(artifact_id=art.id, version=art.version, content=content, content_hash=art.content_hash,
                                created_by=f"user:{user.id}", diff=diff))


def _invalidate_pending(session: Session, art: Artifact, reason: str) -> None:
    for a in session.scalars(select(Approval).where(Approval.workspace_id == art.workspace_id,
                                                    Approval.action == APPROVAL_ACTION,
                                                    Approval.status.in_(["pending", "approved"]))):
        if art.id in (a.affected_assets or []):
            a.status, a.reason = "invalidated", reason


def fork(session: Session, user: User, art: Artifact) -> Artifact:
    """A draft copy of a published dashboard; the published one is untouched."""
    from analystos.artifacts.registry import link

    n = 1 + len(session.scalars(select(Artifact.id).where(Artifact.workspace_id == art.workspace_id,
                                                          Artifact.name.like(f"{art.name}~draft%"))).all())
    draft = Artifact(id=new_id("art"), workspace_id=art.workspace_id, run_id=None, type="dashboard",
                     name=f"{art.name}~draft{n}", version=1, status="draft", creator_user=user.id, content=art.content,
                     content_hash=art.content_hash, created_at=ordered_utcnow())
    session.add(draft)
    session.flush()
    session.add(ArtifactVersion(artifact_id=draft.id, version=1, content=art.content, content_hash=art.content_hash,
                                created_by=f"user:{user.id}",
                                diff=[{"op": "fork", "from": {"artifact_id": art.id, "version": art.version}}]))
    link(session, art.workspace_id, ("dashboard", draft.id), "forked_from", ("dashboard", art.id))
    _event(session, draft, "dashboard.forked", {"from": art.id, "from_version": art.version}, user)
    return draft


def _editable(session: Session, user: User, art: Artifact, base_version: int | None) -> Artifact:
    if base_version is not None and base_version != art.version:
        raise Conflict(f"dashboard is at version {art.version}, not {base_version}; reload and retry",
                       details={"version": art.version})
    if art.status in LIVE:
        return fork(session, user, art)
    if art.status in PENDING:
        _invalidate_pending(session, art, "dashboard edited after the publish request")
    return art


def apply(session: Session, user: User, artifact_id: str, raw_ops: list[dict[str, Any]], *, base_version: int | None = None,
          origin: dict[str, Any] | None = None, ask_fn: Any = None) -> dict[str, Any]:
    """Validate and apply an op batch; returns {status: applied|refused|unchanged, dashboard, refused, deferred}."""
    from analystos.dashboards.env import op_env
    from analystos.governance.audit import audit

    try:
        ops = parse_ops(raw_ops)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from None
    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    if base_version is not None and base_version != art.version:
        raise Conflict(f"dashboard is at version {art.version}, not {base_version}; reload and retry",
                       details={"version": art.version})
    doc = document(art)
    result: OpResult = apply_ops(doc, ops, op_env(session, user, art.workspace_id, doc, ask_fn=ask_fn))
    out: dict[str, Any] = {"refused": [r.as_dict() for r in result.refused],
                           "deferred": [d.model_dump(mode="json") for d in result.deferred]}
    if not result.ok:
        return {**out, "status": "refused", "dashboard": view(art)}
    target = art
    if result.changed:
        target = _editable(session, user, art, base_version)
        diff = [{**o, **({"origin": origin} if origin and i == 0 else {})} for i, o in enumerate(result.applied)]
        _new_version(session, target, result.doc, diff, user)
        target.status = "draft"
        _event(session, target, "dashboard.version_created", {"ops": [o["op"] for o in result.applied]}, user)
        audit(f"user:{user.id}", "dashboard.ops_applied", workspace_id=target.workspace_id, target=target.id,
              details={"version": target.version, "ops": [o["op"] for o in result.applied],
                       **({"forked_from": art.id} if target is not art else {})}, session=session)
    session.flush()
    out.update(status="applied" if result.changed else "unchanged", dashboard=view(target), applied=result.applied,
               **({"forked_from": art.id} if target is not art else {}))
    if result.deferred:
        req = result.deferred[0]
        approval = request_publish(session, user, target.id, destination=req.destination, audience=req.audience)
        out["approval"] = {"id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier}
        out["dashboard"] = view(target)
    return out


def revert(session: Session, user: User, artifact_id: str, to_version: int, *, base_version: int | None = None) -> dict[str, Any]:
    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    old = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id,
                                                       ArtifactVersion.version == to_version))
    if old is None:
        raise InvalidInput(f"dashboard has no version {to_version}")
    if old.content_hash == art.content_hash:
        return {"status": "unchanged", "dashboard": view(art)}
    target = _editable(session, user, art, base_version)
    _new_version(session, target, _check(old.content), [{"op": "revert", "to_version": to_version,
                                                         "from_artifact": art.id}], user)
    target.status = "draft"
    _event(session, target, "dashboard.version_created", {"ops": ["revert"], "to_version": to_version}, user)
    session.flush()
    return {"status": "applied", "dashboard": view(target), **({"forked_from": art.id} if target is not art else {})}


def history(session: Session, user: User, artifact_id: str) -> list[dict[str, Any]]:
    art = load(session, user, artifact_id)
    rows = session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id)
                           .order_by(ArtifactVersion.version.desc()).limit(200))
    return [{"version": v.version, "diff": v.diff or [], "content_hash": v.content_hash, "created_by": v.created_by,
             "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]


def version(session: Session, user: User, artifact_id: str, n: int) -> dict[str, Any]:
    art = load(session, user, artifact_id)
    v = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id, ArtifactVersion.version == n))
    if v is None:
        raise InvalidInput(f"dashboard has no version {n}")
    return {"version": v.version, "document": v.content, "diff": v.diff or [], "content_hash": v.content_hash}


# ------------------------------------------------------------------------------------------- publishing
def idempotency_key(artifact_id: str, version: int, destination: str) -> str:
    return hashlib.sha256(f"{artifact_id}\x1f{version}\x1f{destination}".encode()).hexdigest()


def _bundle(session: Session, requester: User, art: Artifact, destination: str) -> Any:
    from analystos.dashboards.bundle import Resolver, to_publish_bundle

    return to_publish_bundle(document(art), Resolver(session, requester, art.workspace_id), destination, art.workspace_id)


def publish_payload(session: Session, requester: User, art: Artifact, destination: str, audience: list[str]) -> dict[str, Any]:
    """Exactly what a publish approval binds to, derived from the stored artifact."""
    bundle = _bundle(session, requester, art, destination)
    return {"artifact_id": art.id, "version": art.version, "content_hash": art.content_hash, "destination": destination,
            "audience": sorted(set(audience)), "sensitivity": document(art).sensitivity,
            "bundle": bundle.model_dump(mode="json")}


def preview(session: Session, user: User, artifact_id: str) -> dict[str, Any]:
    """DRAFT → PREVIEWED: what would be published (the bundle as the preview destination sees it)."""
    from analystos.publishing.preview import validate_bundle

    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    bundle = _bundle(session, user, art, "preview")
    errors = validate_bundle(bundle)
    if art.status in ("draft", "failed", "rolled_back"):
        art.status = "previewed"
    return {"dashboard": view(art), "bundle": bundle.model_dump(mode="json"), "errors": errors}


def validate(session: Session, user: User, artifact_id: str, destination: str = "superset") -> dict[str, Any]:
    """PREVIEWED → VALIDATED: the document, the governed dataset SQL and the publish bundle all check out."""
    from analystos.publishing.preview import validate_bundle

    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    _check(art.content)
    try:
        errors = validate_bundle(_bundle(session, user, art, destination))
    except AnalystOSError as exc:
        errors = [exc.message]
    if not errors and art.status in ("draft", "previewed", "failed", "rolled_back"):
        art.status = "validated"
    return {"dashboard": view(art), "errors": errors, "valid": not errors}


def _risk(sensitivity: str) -> str:
    return "high" if SENSITIVITY_ORDER.get(sensitivity, 1) >= SENSITIVITY_ORDER["confidential"] else "medium"


def request_publish(session: Session, user: User, artifact_id: str, *, destination: str = "superset",
                    audience: list[str] | None = None) -> Approval:
    """VALIDATED → APPROVAL_REQUIRED. `publish.request` never publishes: it asks for an approval bound to
    the payload hash."""
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.publishing.preview import validate_bundle

    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    if art.status in LIVE:
        raise Conflict("this dashboard is already published; edit it (which forks a draft) to publish a change")
    ws = get_workspace(session, art.workspace_id)
    policy = load_policy(session, ws)
    if destination != "preview" and destination not in policy.publish_destinations:
        raise PolicyDenied(f"workspace policy does not allow publishing to {destination}")
    audience = audience or ["role:viewer"]
    bad = [a for a in audience if not (a in ("role:viewer", "role:editor") or a.startswith("group:"))]
    if bad:
        raise InvalidInput(f"unknown audience {bad}; use role:viewer, role:editor or group:<name>")
    payload = publish_payload(session, user, art, destination, audience)
    from analystos.contracts.bi import PublishBundle

    errors = validate_bundle(PublishBundle.model_validate(payload["bundle"]))
    if errors:
        raise InvalidInput("the dashboard cannot be published yet", details={"errors": errors})
    approval = request_approval(session, workspace_id=art.workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=payload, plan_hash=None, policy_version=ws.policy_version, requested_by=user.id,
                                risk_tier=_risk(payload["sensitivity"]), destination=destination,
                                affected_assets=[art.id], evidence={"version": art.version, "audience": audience})
    art.status = "approval_required"
    _event(session, art, "dashboard.publish_requested", {"approval_id": approval.id, "destination": destination}, user)
    return approval


def execute_publish(session: Session, user: User, artifact_id: str, approval_id: str, *, publisher: Any = None) -> dict[str, Any]:
    """APPROVED → PUBLISHING → PUBLISHED | FAILED. Re-verifies the approval against the payload re-derived
    from the stored artifact immediately before the external call; single use."""
    from analystos.core.config import get_settings
    from analystos.governance.approvals import claim, consume, verify_for_execution
    from analystos.governance.audit import audit
    from analystos.publishing.base import get_publisher
    from analystos.services.platform_settings import get as platform

    art = load(session, user, artifact_id, minimum="editor", for_update=True)
    approval = session.get(Approval, approval_id)
    if approval is None or approval.action != APPROVAL_ACTION or art.id not in (approval.affected_assets or []) \
            or approval.workspace_id != art.workspace_id:
        raise InvalidInput("this approval is not a publish approval for this dashboard")
    requester = session.get(User, approval.requested_by)
    if requester is None:
        raise PolicyDenied("the publish requester no longer exists")
    destination = str(approval.payload.get("destination"))
    payload = publish_payload(session, requester, art, destination, list(approval.payload.get("audience") or []))
    verify_for_execution(session, approval_id, payload=payload, plan_hash=None)
    if destination == "superset" and not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing was turned off by the administrator after this approval was granted")
    from analystos.contracts.bi import PublishBundle

    bundle = PublishBundle.model_validate(payload["bundle"])
    consume(session, approval)  # single use; restored below if the BI tool fails, so a retry can reconcile
    key = idempotency_key(art.id, art.version, destination)
    pub = session.scalar(select(Publication).where(Publication.idempotency_key == key))
    previous = dict(pub.external_ids or {}) if pub else None
    if pub is None:
        pub = Publication(id=new_id("pub"), workspace_id=art.workspace_id, run_id=None, approval_id=approval_id,
                          destination=destination, idempotency_key=key, status="started", artifact_id=art.id,
                          artifact_version=art.version)
        session.add(pub)
    art.status = "publishing"
    session.flush()
    publisher = publisher or get_publisher(destination, get_settings())
    try:
        result = publisher.publish(bundle, idempotency_key=key, previous=previous)
    except AnalystOSError as exc:
        result = None
        error = exc.message
    else:
        error = "; ".join(result.errors)
    if result is None or result.status != "succeeded":
        pub.status = "failed" if result is None else result.status
        pub.error = error[:1000] or None
        if result is not None:
            pub.external_ids = result.external_ids
        art.status = "failed"
        claim(session, approval, expect="executed", to="approved")
        audit(f"user:{user.id}", "publication.failed", workspace_id=art.workspace_id, target=pub.id, decision="error",
              details={"destination": destination, "approval_id": approval_id, "error": error[:300]}, session=session)
        session.flush()
        raise UpstreamUnavailable(f"publishing to {destination} failed: {error[:300]}. The approval stays usable: retry.",
                                  details={"publication_id": pub.id})
    access: dict[str, Any] = {}
    if destination == "superset" and hasattr(publisher, "ensure_workspace_roles"):
        from analystos.dashboards.access import apply_access

        access = apply_access(session, art.workspace_id, publisher, bundle, result.external_ids,
                              list(payload["audience"]))
    pub.status, pub.external_ids, pub.error = "succeeded", result.external_ids, None
    pub.drift, pub.reconciled_at = {"status": "in_sync", "items": []}, utcnow()
    dash_key = bundle.dashboards[0].key
    art.status, art.platform = "published", destination
    art.external_id = str((result.external_ids.get("dashboards") or {}).get(dash_key) or "") or None
    art.external_url = result.urls.get(dash_key)
    from analystos.artifacts.registry import link

    link(session, art.workspace_id, ("dashboard", art.id), "published_as", ("publication", pub.id))
    _event(session, art, "dashboard.published", {"destination": destination, "status": "succeeded",
                                                 "urls": result.urls, "publication_id": pub.id}, user)
    audit(f"user:{user.id}", "publication.executed", workspace_id=art.workspace_id, target=pub.id, decision="allow",
          details={"destination": destination, "approval_id": approval_id, "artifact_id": art.id, "version": art.version,
                   "access": access}, session=session)
    session.flush()
    return {"status": "published", "publication_id": pub.id, "url": art.external_url, "dashboard": view(art),
            "access": access}


# ------------------------------------------------------------------------------------------- conversation
def _call_ctx(session: Session, workspace_id: str, artifact_id: str, prompt_version: str = "dashboard_design.v1") -> Any:
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.llm.router import CallContext

    workspace = get_workspace(session, workspace_id)
    policy = load_policy(session, workspace)
    return CallContext.for_policy(policy, workspace_id=workspace_id, task_id=artifact_id, agent_id="dashboard_builder",
                                  prompt_version=prompt_version, tenant_id=workspace.tenant_id,
                                  policy_version=workspace.policy_version)


def _router() -> Any:
    from analystos.runtime import context as runtime_context

    return runtime_context.default_services().router


def converse(session: Session, user: User, artifact_id: str, utterance: str, *, focus: str | None = None,
             base_version: int | None = None, apply_now: bool = True, router: Any = None, ask_fn: Any = None) -> dict[str, Any]:
    """One builder chat turn: the `dashboard_design` ladder proposes ops, the engine decides."""
    from analystos.dashboards.design import design
    from analystos.dashboards.env import latest_metrics

    if not utterance.strip():
        raise InvalidInput("say what to change")
    art = load(session, user, artifact_id, minimum="editor")
    doc = document(art)
    router = router if router is not None else _router()
    proposal = design(doc, utterance[:4000], router=router, call_ctx=_call_ctx(session, art.workspace_id, art.id),
                      focus=focus, metrics=latest_metrics(session, art.workspace_id))
    out: dict[str, Any] = {"proposal": proposal.as_dict()}
    if not proposal.ops or not apply_now:
        return {**out, "status": "proposed" if proposal.ops else "no_ops", "dashboard": view(art)}
    applied = apply(session, user, artifact_id, proposal.as_dict()["ops"], base_version=base_version,
                    origin={"utterance": utterance[:500], "answered_by": proposal.answered_by}, ask_fn=ask_fn)
    clarify = next((r["suggestion"] for r in applied.get("refused") or [] if (r.get("suggestion") or {}).get("clarify")), None)
    return {**out, **applied, **({"clarify": clarify} if clarify else {})}


# ------------------------------------------------------------------------------------------- drafts (CONV-02/03)
def _free_doc_id(session: Session, workspace_id: str, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50] or "dashboard"
    base = base if base[0].isalpha() else f"d_{base}"
    taken = set(session.scalars(select(Artifact.name).where(Artifact.workspace_id == workspace_id, Artifact.type == "dashboard",
                                                            Artifact.name.like(f"dashboard:{base}%"))))
    out, n = base, 2
    while f"dashboard:{out}" in taken:
        out, n = f"{base}_{n}", n + 1
    return out


def create_draft(session: Session, user: User, workspace_id: str, prompt: str, *, title: str | None = None,
                 router: Any = None, ask_fn: Any = None) -> dict[str, Any]:
    """CONV-02: a draft dashboard (version 1) from one prompt. Every tile is a governed question; failed tiles
    are listed with their reason. Nothing grounded -> a clarifying reply and no dashboard."""
    from analystos.dashboards import drafts
    from analystos.dashboards.env import op_env
    from analystos.governance.policy import require_role

    require_role(session, user, workspace_id, "editor")
    prompt = (prompt or "").strip()
    if not prompt:
        raise InvalidInput("describe the dashboard you want")
    ctx = drafts.gather(session, user, workspace_id)
    plan = drafts.plan_draft(prompt, ctx)
    if plan.clarify:
        return {"status": "clarify", "clarify": plan.clarify, "examples": plan.examples, "plan": plan.as_dict(),
                "built": [], "failed": []}
    router = router if router is not None else _router()
    order = drafts.rank(prompt, plan, router=router,
                        call_ctx=_call_ctx(session, workspace_id, f"draft:{workspace_id}", drafts.PROMPT_VERSION))
    if order:
        plan = drafts.plan_draft(prompt, ctx, order=order)
        plan.ranked_by = "llm_small"
    doc = DashboardDoc(id=_free_doc_id(session, workspace_id, plan.title), title=(title or plan.title)[:300],
                       description=f"Draft built from the request: {prompt}"[:2000], audience=plan.audience)  # type: ignore[arg-type]
    doc, built, failed = drafts.build(doc, plan, op_env(session, user, workspace_id, doc, ask_fn=ask_fn))
    if not built:
        first = (failed[0].get("clarify") or failed[0]["reason"]) if failed else ""
        return {"status": "clarify", "plan": plan.as_dict(), "built": [], "failed": failed,
                "clarify": "None of the planned charts could be answered from governed data, so no dashboard was "
                           f"created. {first}".strip()}
    origin = {"kind": "prompt", "prompt": prompt[:500], "grounded_on": plan.grounded_on, "ranked_by": plan.ranked_by,
              "built": [b["tile"] for b in built], "failed": [{"tile": f["tile"], "reason": f["reason"][:200]} for f in failed]}
    art = create(session, user, workspace_id, doc.model_dump(mode="json"), diff=[{"op": "create", "origin": origin}])
    session.flush()
    return {"status": "created", "dashboard": view(art), "plan": plan.as_dict(), "built": built, "failed": failed}


def answer_of_turn(turn: Any) -> dict[str, Any]:
    """An Ask turn's stored answer in the shape `dashboards.questions` reads (no re-query)."""
    prov = turn.provenance or {}
    return {"status": "answered", "answered_by": turn.answered_by, "result": turn.result, "chart": turn.chart,
            "semantic": prov.get("semantic"), "verified_query": turn.verified_query, "explanation": turn.explanation,
            "governance": prov.get("governance")}


def add_answer(session: Session, user: User, turn: Any, *, dashboard_id: str | None = None,
               title: str | None = None) -> dict[str, Any]:
    """CONV-03: an answered Ask turn as a recommended chart in a draft builder dashboard (a new one, or an existing
    one by id; a published one forks a draft). Built from the stored answer and bound to its gateway execution."""
    from analystos.dashboards.env import op_env
    from analystos.dashboards.ops import NotGrounded, unique_id
    from analystos.dashboards.questions import element_from_answer, sensitivity_lookup
    from analystos.governance.policy import require_role

    require_role(session, user, turn.workspace_id, "editor")
    answer = answer_of_turn(turn)
    assets = list((turn.result or {}).get("referenced_assets") or [])
    try:
        built = element_from_answer(answer, question=turn.question, actor=f"user:{user.id}", turn_id=turn.id,
                                    sensitivity_of=sensitivity_lookup(session, turn.workspace_id, assets), kpi_doc=False)
    except NotGrounded as exc:
        raise InvalidInput(f"this answer cannot become a chart: {exc.reason}") from None
    chart = built.element
    origin = {"kind": "ask_turn", "turn_id": turn.id, "question": turn.question[:300], **built.summary}
    if dashboard_id:
        art = load(session, user, dashboard_id, minimum="editor")
        if art.workspace_id != turn.workspace_id:
            raise InvalidInput("the dashboard is in another workspace")
        chart = chart.model_copy(update={"id": unique_id(document(art), chart.id)})
        out = apply(session, user, dashboard_id, [{"op": "chart.add", "chart": chart.model_dump(mode="json")}], origin=origin)
        if out["status"] == "refused":
            raise InvalidInput("the chart could not be added: " + "; ".join(r["reason"] for r in out["refused"]))
        target, status = out["dashboard"], "added"
    else:
        name = (title or turn.question).strip()[:200] or "Ask answers"
        doc = DashboardDoc(id=_free_doc_id(session, turn.workspace_id, name), title=name,
                           description=f"Started from the Ask answer: {turn.question}"[:2000])
        result = apply_ops(doc, parse_ops([{"op": "chart.add", "chart": chart.model_dump(mode="json")}]),
                           op_env(session, user, turn.workspace_id, doc))
        if not result.ok:
            raise InvalidInput("the chart could not be added: " + "; ".join(r.reason for r in result.refused))
        art = create(session, user, turn.workspace_id, result.doc.model_dump(mode="json"),
                     diff=[{"op": "create", "origin": origin}])
        session.flush()
        target, status = view(art), "created"
    return {"target": "draft", "id": target["id"], "status": status, "dashboard": (target.get("document") or {}).get("title"),
            "version": target["version"], "chart_id": chart.id, "chart_type": chart.type}

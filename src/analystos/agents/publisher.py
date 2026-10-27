"""BI Publisher Agent + Governance review (§13.15, §13.17, §32, §39).

publish_request: build the immutable bundle, governance-review it, ask for approval bound to its
hash and to the current plan hash. publish: re-verify approval, payload, plan and authorization
immediately before the external side effect; publish idempotently (reconcile by idempotency key)."""
from __future__ import annotations

from sqlalchemy import select

from analystos.agents.sql_agent import dataset_def
from analystos.agents.visualization import load_bundle_parts
from analystos.artifacts.registry import link
from analystos.contracts.bi import ChartSpec, PublishBundle
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, ApprovalRequired, PolicyDenied
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Artifact, Publication, RunTask, User
from analystos.events.bus import emit
from analystos.governance.approvals import request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import denied, evaluate
from analystos.runtime.context import RunContext
from analystos.tools.registry import ToolRuntime, get_agent_spec


def choose_destination(ctx: RunContext) -> str:
    from analystos.publishing.base import get_publisher
    from analystos.services.platform_settings import get as platform

    if not platform().features.superset_publishing:
        ctx.say("Superset publishing is turned off by the administrator; using the local preview destination.", kind="decision")
        return "preview"
    if "superset" in ctx.policy.publish_destinations and not get_settings().superset_url:
        ctx.say("Superset is not part of this installation (no `bi` profile); publication targets the local preview "
                "destination.", kind="decision")
        return "preview"
    if "superset" in ctx.policy.publish_destinations:
        try:
            status = get_publisher("superset", get_settings()).test_connection()
            if (status or {}).get("ok", True):
                return "superset"
            reason = str((status or {}).get("error") or "connection test failed")
        except Exception as exc:  # noqa: BLE001 - visible degradation, not silent
            reason = str(exc)
        # test_connection reports most outages by returning ok=False rather than raising: say so either way.
        ctx.say(f"Superset not reachable ({reason[:120]}); publication will target the local preview destination.",
                kind="decision")
    return "preview"


def build_bundle(ctx: RunContext, destination: str, *, pending_ok: bool = False) -> PublishBundle:
    ds, _ = dataset_def(ctx.run.id)
    parts = load_bundle_parts(ctx.run.id)
    charts = [ChartSpec.model_validate({**c.model_dump(), "preview": {}}) for c in parts["charts"]]  # previews are not published
    bundle = PublishBundle(workspace_id=ctx.workspace.id, destination=destination, datasets=[ds], metrics=parts["metrics"],
                           charts=charts, dashboards=parts["dashboards"])
    # P4-K03: KPIs are published as their approved semantic-layer definitions; with the workspace policy
    # require_approved_metrics an unapproved KPI refuses the bundle, here and again right before publishing.
    from analystos.evidence.verification import gate_publish
    from analystos.semantic.service import gate_bundle

    with session_scope() as s:
        # P7-01: a chart presenting a finding whose verdict is VOID is refused, here and right before publishing.
        gate_publish(s, ctx.run.id, [code for c in bundle.charts for code in c.insight_codes])
        # pending_ok: KPIs not approved yet stay in the bundle as proposed; the publication approval then carries them
        # to the approver, whose one decision approves both (`publish` approves them before the side effect)
        policy = ctx.policy.model_copy(update={"require_approved_metrics": False}) if pending_ok else ctx.policy
        return gate_bundle(s, ctx.workspace.id, policy, bundle)


def pending_metrics(ctx: RunContext, bundle: PublishBundle) -> list[str]:
    """The bundle's KPIs the workspace policy wants approved and that are not yet (gate_bundle stamps approved ones)."""
    if not getattr(ctx.policy, "require_approved_metrics", False):
        return []
    return [m.name for m in bundle.metrics if m.status != "approved"]


def governance_review(ctx: RunContext, bundle: PublishBundle) -> dict:
    """Runs as the Governance Agent identity. Re-validates every dataset SQL against the CURRENT scope
    (restricted columns cannot leak into a published dataset) and checks destination policy."""
    from analystos.publishing.preview import validate_bundle

    with session_scope() as s:
        gov = get_agent_spec(s, "governance")
    rt = ToolRuntime(user=ctx.user, identity=ctx.identity.model_copy(update={"agent_id": "governance"}), agent=gov)

    def review():
        problems = list(validate_bundle(bundle))
        for ds in bundle.datasets:
            try:
                from analystos.gateway.validator import validate_sql

                validate_sql(ctx.scope, ds.sql, max_rows=1)
            except AnalystOSError as exc:
                problems.append(f"dataset {ds.name}: {exc.message}")
        if bundle.destination not in ctx.policy.publish_destinations and bundle.destination != "preview":
            problems.append(f"destination {bundle.destination} not allowed by workspace policy")
        return {"ok": not problems, "problems": problems}

    return rt.invoke("governance.review", {"destination": bundle.destination, "datasets": [d.name for d in bundle.datasets]}, review)


def request_publication(ctx: RunContext) -> dict:
    destination = choose_destination(ctx)
    bundle = build_bundle(ctx, destination, pending_ok=True)
    pending = pending_metrics(ctx, bundle)
    review = governance_review(ctx, bundle)
    if not review["ok"]:
        ctx.say("Governance blocked publication: " + "; ".join(review["problems"][:5]), kind="decision")
        raise PolicyDenied("governance review failed: " + "; ".join(review["problems"][:3]))
    payload = bundle.model_dump()
    with session_scope() as s:
        user = s.get(User, ctx.user.id)
        decision = evaluate(s, user, ctx.identity, "publish", destination=destination if destination != "preview" else "superset",
                            scope=getattr(ctx, "scope", None))
        if decision.decision == "deny":
            raise denied(decision, "publication denied: " + ", ".join(decision.reasons))
        run = s.get(AnalysisRun, ctx.run.id)
        # No risk_check decision here (P4-T02): publication is always high tier and always needs an
        # approval, so a model opinion could not change the next step.
        approval = request_approval(
            s, workspace_id=ctx.workspace.id, run_id=run.id, action="publish_dashboard", payload=payload, plan_hash=run.plan_hash,
            policy_version=run.policy_version, requested_by=run.requested_by, risk_tier="high", destination=destination,
            affected_assets=[d.name for d in bundle.datasets] + [d.key for d in bundle.dashboards],
            evidence={"governance_review": review, "policy": decision.model_dump(),
                      "risk_tier_basis": "deterministic: publication is always high risk and approval-gated",
                      "charts": len(bundle.charts), "metrics": len(bundle.metrics),
                      **({"kpis_to_approve": [{"name": m.name, "expression": m.sql_expression, "definition": m.definition}
                                              for m in bundle.metrics if m.name in pending]} if pending else {})})
        task = s.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == "publish"))
        task.input = {**task.input, "approval_id": approval.id, "destination": destination}
    ctx.say(f"Publication to {destination} awaits approval {approval.id} (bundle hash {approval.payload_hash[:12]}; "
            f"{len(bundle.dashboards)} dashboards, {len(bundle.charts)} charts). Nothing is published until a person approves."
            + (f" Approving it also approves the {len(pending)} KPI definition(s) it publishes ({', '.join(pending)}), as the "
               "workspace requires approved KPIs." if pending else ""),
            kind="decision")
    return {"approval_id": approval.id, "destination": destination, "payload_hash": approval.payload_hash}


def record_contracts(ctx: RunContext, bundle: PublishBundle, result, pub_id: str) -> None:
    """P4-K04: an ODCS v3.2 contract per published dataset, into the workspace pack. Runs after the
    external side effect, so a failure here is reported and never undoes or fails the publication."""
    from analystos.evidence.odcs import write_contracts

    try:
        with session_scope() as s:
            written = write_contracts(s, workspace_id=ctx.workspace.id, bundle=bundle, external_ids=result.external_ids,
                                      urls=result.urls, run_id=ctx.run.id, author=f"agent:{ctx.agent.id}")
            for info in written.values():
                link(s, ctx.workspace.id, ("publication", pub_id), "described_by", ("data_contract", info["path"]),
                     run_id=ctx.run.id)
    except Exception as exc:  # noqa: BLE001 - the publication already happened; the contract is evidence
        ctx.say(f"ODCS data contract not recorded: {str(exc)[:200]}", kind="decision")
        return
    if written:
        ctx.say("ODCS data contracts: " + ", ".join(f"{i['path']} v{i['version']}" for i in written.values()))


def _comparable(bundle: dict) -> dict:
    """A bundle as the "changed after approval" check sees it: a KPI by its name and expression, so its approval (which
    stamps the approved definition on it) is not a change; `verify_for_execution` still binds the exact approved payload."""
    return {**bundle, "metrics": sorted((m.get("name"), m.get("sql_expression")) for m in bundle.get("metrics") or [])}


def approve_bundled_metrics(ctx: RunContext, approval_id: str, names: list[str]) -> None:
    """The publication's approver reviewed the KPI definitions in the bundle it approved: each still-proposed KPI is
    approved in that person's name through the ordinary metric decision (role and separation of duties apply: the
    run's requester cannot approve their own KPIs). A KPI that cannot be approved stops the publication."""
    if not names:
        return
    from analystos.semantic.service import decide_metric

    with session_scope() as s:
        approval = s.get(Approval, approval_id)
        approver = s.get(User, approval.decided_by) if approval and approval.decided_by else None
        if approver is None:
            raise PolicyDenied("the publication approval names no approver to approve its KPIs")
        for name in names:
            try:
                decide_metric(s, ctx.workspace.id, name, approver, approve=True,
                              reason=f"approved with publication approval {approval_id}")
            except AnalystOSError as exc:
                raise PolicyDenied(f"KPI {name} could not be approved with the publication: {exc.message}",
                                   details={"metric": name}) from exc
    ctx.say(f"Approved {len(names)} KPI definition(s) with the publication approval ({', '.join(names)}).", kind="decision")


def publish(ctx: RunContext) -> dict:
    from analystos.publishing.base import get_publisher

    approval_id = ctx.task.input.get("approval_id")
    destination = ctx.task.input.get("destination", "superset")
    with session_scope() as s:
        approval = s.get(Approval, approval_id)
        payload = dict(approval.payload)
        run = s.get(AnalysisRun, ctx.run.id)
        # Re-verify immediately before the side effect: status, payload hash, plan hash, policy version, authorization.
        verify_for_execution(s, approval_id, payload=payload, plan_hash=run.plan_hash)
    from analystos.services.platform_settings import get as platform

    if destination == "superset" and not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing was turned off by the administrator after this approval was granted")
    current = build_bundle(ctx, destination, pending_ok=True)
    if stable_hash(_comparable(current.model_dump())) != stable_hash(_comparable(payload)):
        with session_scope() as s:
            a = s.get(Approval, approval_id)
            a.status, a.reason = "invalidated", "artifacts changed after approval"
        raise ApprovalRequired("artifacts changed after approval; a new approval is required")
    ctx.check_control()
    approve_bundled_metrics(ctx, approval_id, pending_metrics(ctx, current))
    bundle = PublishBundle.model_validate(payload)
    ctx.check_output("publication", payload)  # the agent's output contract (FND-006), before the side effect
    key = f"{ctx.run.id}:{approval.payload_hash[:32]}"
    with session_scope() as s:
        pub = s.scalar(select(Publication).where(Publication.idempotency_key == key))
        if pub and pub.status == "succeeded":
            return {"status": "succeeded", "reused": True, "external_ids": pub.external_ids}
        previous = dict(pub.external_ids) if pub else None
        if pub is None:
            pub = Publication(id=new_id("pub"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, approval_id=approval_id,
                              destination=destination, idempotency_key=key, status="started")
            s.add(pub)
        pub_id = pub.id
    publisher = get_publisher(destination, get_settings())
    try:
        result = ctx.tools().invoke(f"{'superset' if destination == 'superset' else 'preview'}.publish",
                                    {"_approval_verified": True, "approval_id": approval_id, "destination": destination},
                                    lambda: publisher.publish(bundle, idempotency_key=key, previous=previous))
    except AnalystOSError as exc:
        with session_scope() as s:
            p = s.get(Publication, pub_id)
            p.status, p.error = "failed", exc.message[:1000]
        raise
    with session_scope() as s:
        p = s.get(Publication, pub_id)
        p.status, p.external_ids, p.error = result.status, result.external_ids, "; ".join(result.errors) or None
        a = s.get(Approval, approval_id)
        if result.status == "succeeded":
            a.status = "executed"
        arts = {(x.type, x.name): x for x in s.scalars(select(Artifact).where(Artifact.run_id == ctx.run.id))}
        for kind, mapping in (("chart", result.external_ids.get("charts") or {}), ("dashboard", result.external_ids.get("dashboards") or {}),
                              ("dataset", result.external_ids.get("datasets") or {})):
            for name, ext in mapping.items():
                art = arts.get((kind, name))
                if art:
                    art.platform, art.external_id, art.status = destination, str(ext), "published"
                    art.external_url = result.urls.get(name)
                    link(s, ctx.workspace.id, (kind, art.id), "published_as", ("publication", pub_id), run_id=ctx.run.id)
        emit(ctx.workspace.id, "dashboard.published", {"destination": destination, "status": result.status, "urls": result.urls},
             run_id=ctx.run.id, session=s)
        audit(f"agent:{ctx.agent.id}", "publication.executed", workspace_id=ctx.workspace.id, run_id=ctx.run.id, target=pub_id,
              decision="allow", details={"destination": destination, "status": result.status, "approval_id": approval_id}, session=s)
    if result.status in ("succeeded", "partial"):
        record_contracts(ctx, bundle, result, pub_id)
    if result.status != "succeeded":
        ctx.say(f"Publication {result.status}: {'; '.join(result.errors[:3])}. Recorded for reconcile-before-retry.", kind="decision")
        if result.status == "failed":
            raise AnalystOSError("publication failed: " + "; ".join(result.errors[:3]))
    ctx.say(f"Published to {destination}: " + ", ".join(f"{k} → {v}" for k, v in result.urls.items()))
    return {"status": result.status, "external_ids": result.external_ids, "urls": result.urls, "publication_id": pub_id}

"""Read-only knowledge sources of a workspace: configure, sync now, sync on a schedule (P2-10).

A sync is: snapshot the row (short transaction) -> resolve the `secret_ref` and fetch through the SSRF-safe client
(no transaction open) -> screen every file and merge the results into the workspace pack as drafts in one revision
(`knowledge/drafts.write_drafts`: a curated document is kept, an unchanged one is not rewritten). Nothing becomes
knowledge without a person reviewing the draft (CLAUDE.md rule 7); nothing is deleted when a remote file goes
away (it is reported as `removed`).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, ApprovalRequired, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Approval, KnowledgeSource, User
from analystos.governance.approvals import request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_in_workspace, require_role
from analystos.knowledge import sources as ks
from analystos.knowledge.drafts import write_drafts
from analystos.services.facets import Facets
from analystos.services.knowledge_ingest import _finish

log = logging.getLogger(__name__)
MAX_SOURCES_PER_WORKSPACE = 20
BIND_ACTION = "knowledge_source.bind"


def _payload(row: KnowledgeSource) -> dict[str, Any]:
    return ks.binding_payload(row.workspace_id, row.kind, row.config, row.secret_ref)


def _request_binding(session: Session, user: User, row: KnowledgeSource) -> None:
    """Ask a second person to approve this (workspace, kind, host, target, secret_ref). Until it is approved the
    source cannot sync; any later change of those fields needs a new approval."""
    payload = _payload(row)
    ws = get_workspace(session, row.workspace_id)
    apr = request_approval(session, workspace_id=row.workspace_id, run_id=None, action=BIND_ACTION, payload=payload,
                           plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium",
                           destination=payload["host"], affected_assets=[row.id],
                           evidence={"source": row.name, "target": payload["target"], "secret_ref": row.secret_ref})
    row.binding_approval_id = apr.id


def view(row: KnowledgeSource, binding_status: str | None = None) -> dict[str, Any]:
    """The row as the API shows it: no sync state (paths and hashes), a credential only as its reference."""
    return {"id": row.id, "workspace_id": row.workspace_id, "kind": row.kind, "name": row.name, "config": row.config,
            "secret_ref": row.secret_ref, "enabled": row.enabled, "tracked_files": len(row.state or {}),
            "last_sync_at": row.last_sync_at, "last_status": row.last_status, "last_report": row.last_report or {},
            "binding_approval_id": row.binding_approval_id, "binding_status": binding_status or "none",
            "created_by": row.created_by, "created_at": row.created_at}


def _check_ref(ref: str | None) -> str | None:
    from analystos.connectors.secrets import check_secret_ref

    ref = (ref or "").strip() or None
    if ref is not None:
        check_secret_ref(ref)  # Forbidden when the operator's policy does not allow the reference
    return ref


def list_sources(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    rows = session.scalars(select(KnowledgeSource).where(KnowledgeSource.workspace_id == workspace_id)
                           .order_by(KnowledgeSource.created_at))
    out = []
    for r in rows:
        apr = session.get(Approval, r.binding_approval_id) if r.binding_approval_id else None
        out.append(view(r, apr.status if apr else None))
    return out


def create_source(session: Session, user: User, workspace_id: str, *, kind: str, name: str, config: dict[str, Any],
                  secret_ref: str | None = None, enabled: bool = True) -> dict[str, Any]:
    require_role(session, user, workspace_id, "editor")
    name = (name or "").strip()
    if not 1 <= len(name) <= 200:
        raise InvalidInput("name must be 1-200 characters")
    clean = ks.validate_config(kind, config)
    ref = _check_ref(secret_ref)
    if kind == "confluence" and ref is None:
        raise InvalidInput("a Confluence source needs a secret_ref (user:api_token, or a bearer token)")
    count = len(session.scalars(select(KnowledgeSource.id).where(KnowledgeSource.workspace_id == workspace_id)).all())
    if count >= MAX_SOURCES_PER_WORKSPACE:
        raise InvalidInput(f"a workspace may have at most {MAX_SOURCES_PER_WORKSPACE} knowledge sources")
    row = KnowledgeSource(id=new_id("ksrc"), workspace_id=workspace_id, kind=kind, name=name, config=clean, secret_ref=ref,
                          enabled=enabled, state={}, last_report={}, created_by=user.id)
    session.add(row)
    session.flush()
    _request_binding(session, user, row)
    audit(f"user:{user.id}", "knowledge.source.created", workspace_id=workspace_id, target=row.id,
          details={"kind": kind, "name": name, "secret_ref": ref}, session=session)
    return view(row, "pending")


def update_source(session: Session, user: User, workspace_id: str, source_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    row = load_in_workspace(session, KnowledgeSource, source_id, workspace_id, user=user, minimum="editor",
                            label="knowledge source", for_update=True)
    if "name" in patch and patch["name"] is not None:
        if not 1 <= len(str(patch["name"]).strip()) <= 200:
            raise InvalidInput("name must be 1-200 characters")
        row.name = str(patch["name"]).strip()
    before = _payload(row)
    if patch.get("config") is not None:
        row.config = ks.validate_config(row.kind, patch["config"])
        row.state = {}  # another location: nothing fetched so far applies
    if "secret_ref" in patch:
        row.secret_ref = _check_ref(patch["secret_ref"])
        if row.kind == "confluence" and row.secret_ref is None:
            raise InvalidInput("a Confluence source needs a secret_ref")
    if patch.get("enabled") is not None:
        row.enabled = bool(patch["enabled"])
    if _payload(row) != before:  # a new place or a new credential: the earlier approval no longer applies
        row.state = {}
        _request_binding(session, user, row)
    audit(f"user:{user.id}", "knowledge.source.updated", workspace_id=workspace_id, target=row.id,
          details={"fields": sorted(patch)}, session=session)
    apr = session.get(Approval, row.binding_approval_id) if row.binding_approval_id else None
    return view(row, apr.status if apr else None)


def delete_source(session: Session, user: User, workspace_id: str, source_id: str) -> dict[str, Any]:
    row = load_in_workspace(session, KnowledgeSource, source_id, workspace_id, user=user, minimum="editor",
                            label="knowledge source")
    session.delete(row)
    audit(f"user:{user.id}", "knowledge.source.deleted", workspace_id=workspace_id, target=source_id,
          details={"name": row.name}, session=session)
    return {"deleted": source_id}  # drafts already in the pack stay: a person reviews or removes them


# ------------------------------------------------------------------------------------ sync
def sync_source(source_id: str, *, actor: str, resolver: Any = None, transport: Any = None,
                secret_resolver: Any = None) -> dict[str, Any]:
    """Fetch one source and merge it into the pack as drafts. `actor` is `user:<id>` or `schedule:<id>`.
    Raises AnalystOSError after recording the failure on the row. `resolver`/`transport`/`secret_resolver`
    exist for tests (no live call)."""
    from analystos.connectors.secrets import resolve_secret
    from analystos.core.config import get_settings

    with session_scope() as s:
        row = s.get(KnowledgeSource, source_id)
        if row is None:
            raise NotFound("knowledge source not found")
        denied: ApprovalRequired | None = None
        try:
            if not row.binding_approval_id:
                raise ApprovalRequired("this source has no approval yet; a second person must approve its host, target and secret_ref")
            verify_for_execution(s, row.binding_approval_id, payload=_payload(row), plan_hash=None)
        except ApprovalRequired as exc:
            denied = exc
        snap = {"workspace_id": row.workspace_id, "kind": row.kind, "name": row.name, "config": dict(row.config or {}),
                "secret_ref": row.secret_ref, "state": {k: dict(v) for k, v in (row.state or {}).items()}}
    if denied is not None:  # recorded after the transaction that may have marked the approval invalidated
        _record(source_id, actor, "failed", {"error": f"approval_required: {denied.message}"[:300]})
        raise denied
    kind, config = snap["kind"], ks.validate_config(snap["kind"], snap["config"])
    try:
        secret = (secret_resolver or resolve_secret)(snap["secret_ref"], purpose="source") if snap["secret_ref"] else None
        fetch = ks.make_fetch(secret=secret, auth=config.get("auth", "bearer"), settings=get_settings(),
                              resolver=resolver, transport=transport)
        plan = ks.plan(kind, config, fetch, snap["state"])
    except AnalystOSError as exc:
        _record(source_id, actor, "failed", {"error": f"{exc.code}: {exc.message}"[:300]})
        raise
    docs, failed = ks.build_drafts(source_id, snap["name"], kind, config, plan.items)
    state = plan.state
    for f in failed:  # not rendered: keep the old state so the next sync tries again
        if f["path"] in snap["state"]:
            state[f["path"]] = snap["state"][f["path"]]
        else:
            state.pop(f["path"], None)
    with session_scope() as s:
        row = s.scalar(select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update())
        if row is None:
            raise NotFound("knowledge source not found")
        facets = Facets()
        facets.run("source.fetch", lambda: {"count": len(plan.items)})
        written = write_drafts(s, snap["workspace_id"], docs, author=ks.SOURCE_ACTOR, reason=f"{kind} source {snap['name']}",
                               origin=f"source:{kind}", meta={"knowledge_source": {"id": source_id, "kind": kind}})
        report = {"fetched": len(plan.items), "documents": len(docs), "written": len(written.written),
                  "unchanged_documents": len(written.unchanged), "kept_curated": len(written.kept_curated),
                  "skipped_unchanged": plan.skipped_unchanged, "skipped_too_large": plan.skipped_too_large[:50],
                  "skipped_type": plan.skipped_type, "removed_remote": plan.removed[:50], "truncated": plan.truncated,
                  "failed": failed[:50], "bytes": plan.bytes, "requests": plan.requests, "revision": written.revision}
        row.state = state
        row.last_sync_at = utcnow()
        row.last_status = "partial" if failed or plan.truncated else "ok"
        row.last_report = report
        _finish(s, snap["workspace_id"], actor, f"source_{kind}", facets, written,
                {"source_id": source_id, "source": snap["name"], **report})
        status = row.last_status
    return {"id": source_id, "status": status, **report}


def _record(source_id: str, actor: str, status: str, report: dict[str, Any]) -> None:
    with session_scope() as s:
        row = s.get(KnowledgeSource, source_id)
        if row is None:
            return
        row.last_sync_at, row.last_status, row.last_report = utcnow(), status, report
        audit(actor, "knowledge.source.sync_failed", workspace_id=row.workspace_id, target=source_id, decision="deny",
              details=report, session=s)


def sync_workspace(workspace_id: str, *, actor: str, source_ids: list[str] | None = None) -> dict[str, Any]:
    """Every enabled source of the workspace (or `source_ids`), one failure never stopping the others."""
    with session_scope() as s:
        stmt = select(KnowledgeSource.id).where(KnowledgeSource.workspace_id == workspace_id, KnowledgeSource.enabled.is_(True))
        if source_ids:
            stmt = stmt.where(KnowledgeSource.id.in_(source_ids))
        ids = list(s.scalars(stmt))
    out: dict[str, Any] = {}
    for sid in ids:
        try:
            r = sync_source(sid, actor=actor)
            out[sid] = {k: r[k] for k in ("status", "fetched", "written", "skipped_unchanged", "truncated")}
        except AnalystOSError as exc:
            out[sid] = {"error": exc.message}
    if ids and all("error" in v for v in out.values()):
        raise InvalidInput("every knowledge source failed: " + "; ".join(v["error"] for v in out.values())[:500])
    return {"sources": out}

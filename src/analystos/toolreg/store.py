"""Registry rows: version resolution, signed registration, status changes, listing, usage and health (S10).

A tool version is immutable once registered (its manifest and signature never change; re-signing after a
key revocation writes a new signature over the same content). Resolution never returns a quarantined,
refused or retired version; a deprecated one only when the caller pins it. An unpinned call gets the
latest certified version.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.tool_manifest import (
    INVOCABLE_STATUSES,
    ToolManifestV2,
    matches_pin,
    semver_key,
    split_ref,
)
from analystos.core.errors import InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import RegisteredTool, ToolInvocation
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.toolreg import signing


class ToolQuarantined(PolicyDenied):
    code = "tool_quarantined"


def body_hash(m: ToolManifestV2) -> str:
    """Content identity without version and signature: re-publishing the same thing is idempotent."""
    return stable_hash({k: v for k, v in m.signing_payload().items() if k != "version"})


def versions(session: Session, workspace_id: str, tool_id: str) -> list[RegisteredTool]:
    rows = list(session.scalars(select(RegisteredTool).where(RegisteredTool.workspace_id == workspace_id,
                                                             RegisteredTool.tool_id == tool_id)))
    return sorted(rows, key=lambda r: semver_key(r.version))


def resolve(session: Session, workspace_id: str, ref: str) -> RegisteredTool:
    """`tool_id` or `tool_id@pin` -> the version a call runs, or why none may run."""
    try:
        tool_id, pin = split_ref(ref)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from None
    rows = versions(session, workspace_id, tool_id)
    if pin:
        rows = [r for r in rows if matches_pin(r.version, pin)]
    if not rows:
        raise NotFound(f"tool {ref} not found")
    allowed = INVOCABLE_STATUSES if pin else ("certified",)
    runnable = [r for r in rows if r.status in allowed]
    if runnable:
        return runnable[-1]
    latest = rows[-1]
    if latest.status == "quarantined":
        raise ToolQuarantined(f"tool {latest.tool_id}@{latest.version} is quarantined until an owner classifies it",
                              details={"tool_id": tool_id, "version": latest.version, "status": latest.status})
    if latest.status == "deprecated":
        raise PolicyDenied(f"tool {tool_id} is deprecated: pin a version (e.g. {tool_id}@{latest.major}) to keep using it",
                           details={"tool_id": tool_id, "status": "deprecated"})
    raise PolicyDenied(f"tool {latest.tool_id}@{latest.version} is {latest.status}: it cannot be invoked",
                       details={"tool_id": tool_id, "version": latest.version, "status": latest.status})


def next_version(session: Session, workspace_id: str, candidate: ToolManifestV2,
                 source_version: int | None) -> tuple[str, RegisteredTool | None]:
    """(version, existing row when the same content is already registered and still current)."""
    rows = versions(session, workspace_id, candidate.tool_id)
    if not rows:
        return f"{max(1, source_version or 1)}.0.0", None
    latest = rows[-1]
    same = body_hash(ToolManifestV2.model_validate(latest.manifest)) == body_hash(candidate)
    if same and latest.status in ("certified", "quarantined", "refused"):
        return latest.version, latest
    major, minor, _ = semver_key(latest.version)
    if source_version and source_version > major:
        return f"{source_version}.0.0", None
    return f"{major}.{minor + 1}.0", None


def register(session: Session, workspace_id: str, manifest: ToolManifestV2, *, status: str, source: dict[str, Any],
             actor: str, description_hash: str | None = None, screening: dict[str, Any] | None = None,
             supersede: bool = True) -> RegisteredTool:
    """Sign and store one version. Older certified versions become deprecated (still callable when pinned)."""
    from analystos.artifacts.registry import link

    signed = signing.sign(session, signing.tenant_of(session, workspace_id), manifest, actor=actor)
    if supersede:
        for old in versions(session, workspace_id, manifest.tool_id):
            if old.status == "certified" and status == "certified":
                old.status, old.status_reason, old.status_by = "deprecated", f"superseded by {manifest.version}", actor
    row = RegisteredTool(id=new_id("tool"), workspace_id=workspace_id, tool_id=manifest.tool_id, version=manifest.version,
                         major=manifest.major, kind=manifest.kind, owner=manifest.owner, status=status,
                         side_effect=manifest.side_effect, manifest=signed.model_dump(mode="json"),
                         manifest_hash=signed.content_hash(), source=dict(source), description_hash=description_hash,
                         screening=dict(screening or {}), created_by=actor)
    session.add(row)
    session.flush()
    if source.get("type") and source.get("id"):
        link(session, workspace_id, ("tool", row.id), "derived_from", (str(source["type"]), str(source["id"])))
    audit(actor, "tool.published", workspace_id=workspace_id, target=f"{row.tool_id}@{row.version}", decision="allow",
          details={"kind": row.kind, "status": status, "side_effect": row.side_effect, "source": source,
                   "manifest_hash": row.manifest_hash, "key_id": signed.signature.key_id if signed.signature else None},
          session=session)
    emit(workspace_id, "tool.quarantined" if status == "quarantined" else "tool.published",
         {"tool_id": row.tool_id, "version": row.version, "kind": row.kind, "status": status}, actor=actor, session=session)
    return row


def set_status(session: Session, row: RegisteredTool, status: str, *, actor: str, reason: str | None = None) -> None:
    before = row.status
    row.status, row.status_reason, row.status_by = status, (reason or "")[:1000] or None, actor
    audit(actor, f"tool.{status}", workspace_id=row.workspace_id, target=f"{row.tool_id}@{row.version}",
          decision="deny" if status in ("retired", "refused", "quarantined") else "allow",
          details={"from": before, "to": status, "reason": reason}, session=session)
    emit(row.workspace_id, f"tool.{'deprecated' if status == 'deprecated' else 'status_changed'}",
         {"tool_id": row.tool_id, "version": row.version, "from": before, "to": status}, actor=actor, session=session)


def verified(session: Session, row: RegisteredTool) -> ToolManifestV2:
    """The signed manifest, verified now (unsigned, tampered or revoked-key manifests are refused)."""
    return signing.verify(session, signing.tenant_of(session, row.workspace_id), row.manifest,
                          manifest_hash=row.manifest_hash)


def resign_all(session: Session, tenant: str, *, actor: str) -> int:
    """After a key rotation: re-sign every version of the tenant's workspaces with the active key, if its content
    still matches its hash (a tampered row is never blessed by re-signing)."""
    from analystos.db.models import Workspace

    n = 0
    workspaces = select(Workspace.id).where(Workspace.tenant_id == tenant)
    for row in session.scalars(select(RegisteredTool).where(RegisteredTool.workspace_id.in_(workspaces))):
        m = ToolManifestV2.model_validate(row.manifest)
        if m.content_hash() != row.manifest_hash:
            continue
        row.manifest = signing.sign(session, tenant, m.model_copy(update={"signature": None}), actor=actor).model_dump(mode="json")
        n += 1
    audit(actor, "tool.resigned", decision="allow", details={"tenant": tenant, "count": n}, session=session)
    return n


# ------------------------------------------------------------------------------------ read models
def manifest_out(row: RegisteredTool) -> dict[str, Any]:
    return {"id": row.id, "tool_id": row.tool_id, "version": row.version, "kind": row.kind, "owner": row.owner,
            "status": row.status, "status_reason": row.status_reason, "side_effect": row.side_effect,
            "manifest": row.manifest, "manifest_hash": row.manifest_hash, "source": row.source,
            "screening": row.screening, "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def summary(row: RegisteredTool) -> dict[str, Any]:
    m = row.manifest or {}
    return {"tool_id": row.tool_id, "version": row.version, "kind": row.kind, "owner": row.owner, "status": row.status,
            "side_effect": row.side_effect, "title": m.get("title") or row.tool_id, "description": m.get("description") or "",
            "exposure": m.get("exposure") or {}, "data_classification_max": m.get("data_classification_max")}


def current_rows(session: Session, workspace_id: str, *, kind: str | None = None, q: str | None = None,
                 statuses: tuple[str, ...] = ("certified",)) -> list[RegisteredTool]:
    """The newest version per tool id among `statuses` (certified first, then the newest of the rest)."""
    stmt = select(RegisteredTool).where(RegisteredTool.workspace_id == workspace_id, RegisteredTool.status.in_(statuses))
    if kind:
        stmt = stmt.where(RegisteredTool.kind == kind)
    best: dict[str, RegisteredTool] = {}
    for r in session.scalars(stmt):
        cur = best.get(r.tool_id)
        rank = (r.status == "certified", semver_key(r.version))
        if cur is None or rank > (cur.status == "certified", semver_key(cur.version)):
            best[r.tool_id] = r
    out = sorted(best.values(), key=lambda r: r.tool_id)
    if q:
        needle = q.casefold().strip()
        out = [r for r in out if needle in r.tool_id.casefold() or needle in str((r.manifest or {}).get("title", "")).casefold()
               or needle in str((r.manifest or {}).get("description", "")).casefold()]
    return out


def usage(session: Session, workspace_id: str, tool_id: str, *, days: int = 7) -> dict[str, Any]:
    since = utcnow() - timedelta(days=max(1, min(days, 90)))
    base = select(ToolInvocation).where(ToolInvocation.workspace_id == workspace_id, ToolInvocation.tool_id == tool_id,
                                        ToolInvocation.created_at >= since)
    by_status: dict[str, int] = {}
    rows = list(session.scalars(base.order_by(ToolInvocation.created_at.desc()).limit(5000)))
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    callers: dict[str, int] = {}
    for r in rows:
        callers[r.caller] = callers.get(r.caller, 0) + 1
    ok = sorted(r.latency_ms for r in rows if r.status == "ok")
    return {"tool_id": tool_id, "days": days, "calls": len(rows), "by_status": by_status,
            "by_channel": {c: sum(1 for r in rows if r.channel == c) for c in sorted({r.channel for r in rows})},
            "top_callers": sorted(callers.items(), key=lambda kv: -kv[1])[:10],
            "latency_ms_p50": ok[len(ok) // 2] if ok else None, "cost_usd": round(sum(r.cost_usd or 0 for r in rows), 6),
            "recent": [{"at": r.created_at.isoformat() if r.created_at else None, "version": r.version, "caller": r.caller,
                        "channel": r.channel, "status": r.status, "reason": r.reason, "latency_ms": r.latency_ms,
                        "row_count": r.row_count, "query_id": r.query_id} for r in rows[:20]]}


def health(session: Session, row: RegisteredTool) -> dict[str, Any]:
    """Signature, source currency and recent error rate: whether a call would run, and how it has gone."""
    out: dict[str, Any] = {"status": row.status}
    try:
        verified(session, row)
        out["signature"] = "valid"
    except signing.ManifestRefused as exc:
        out["signature"] = (exc.details or {}).get("reason", "refused")
    out["source_current"] = source_current(session, row)
    recent = list(session.scalars(select(ToolInvocation).where(
        ToolInvocation.workspace_id == row.workspace_id, ToolInvocation.tool_id == row.tool_id,
        ToolInvocation.created_at >= utcnow() - timedelta(days=1)).limit(2000)))
    errors = sum(1 for r in recent if r.status == "error")
    out["calls_24h"], out["error_rate_24h"] = len(recent), (round(errors / len(recent), 3) if recent else None)
    last_ok = max((r.created_at for r in recent if r.status == "ok"), default=None)
    out["last_ok_at"] = last_ok.isoformat() if last_ok else None
    out["invocable"] = row.status in INVOCABLE_STATUSES and out["signature"] == "valid" and out["source_current"] is not False
    return out


def source_current(session: Session, row: RegisteredTool) -> bool | None:
    """Whether the certified thing the tool was published from is still the current certified version."""
    src = row.source or {}
    kind = src.get("type")
    if kind == "metric":
        from analystos.semantic.service import approved_metrics

        m = approved_metrics(session, row.workspace_id).get(src.get("name") or "")
        return m is not None and m.version == src.get("version")
    if kind == "verified_query":
        from analystos.db.models import VerifiedQuery

        vq = session.get(VerifiedQuery, src.get("id"))
        return vq is not None and vq.status == "active" and stable_hash(vq.sql_template) == src.get("content_hash")
    if kind == "dataset":
        from analystos.db.models import Artifact

        a = session.get(Artifact, src.get("id"))
        return a is not None and a.status == "published" and a.content_hash == src.get("content_hash")
    if kind == "definition":
        from analystos.contracts.definition import RUNNABLE_STATUSES
        from analystos.db.models import Definition

        d = session.get(Definition, src.get("id"))
        return d is not None and d.status in RUNNABLE_STATUSES
    return None

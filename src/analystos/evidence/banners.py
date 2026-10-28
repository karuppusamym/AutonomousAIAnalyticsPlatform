"""Void sweep -> publication banner -> Desk task (spec S07-T7).

When a verdict turns VOID (any fingerprint component changed: metric version, data snapshot, policy version,
context identity ...; `evidence.verification.void_dependents`, events and the nightly sweep alike), every
*published* artifact that presents the finding gets a banner in the same transaction, and one Request Desk
task (S13) per artifact asks a person to re-verify or withdraw it:

    insight --visualized_by--> chart --included_in--> dashboard          (lineage, `agents.visualization`)

`Artifact.banner` = {state: "void", reason, findings: [{insight_id, code, record_id, kind, reason}], request_id,
raised_at}. A second void on the same artifact joins the open task (a `void_added` event) instead of opening
another. When every listed finding has a live verified record again (re-verification writes a new record), the
banner is cleared and the event `publication.banner_cleared` says so; the Desk task stays for its owner to close.
Banner text is built from stored causes only: nothing in it comes from data rows.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger

log = get_logger(__name__)
PRESENTERS = ("chart", "dashboard")
MAX_FINDINGS = 50


def _published_presenters(session: Session, workspace_id: str, insight_ids: list[str]) -> dict[str, set[str]]:
    """artifact id -> the insight ids it presents, for published charts and the dashboards that hold them."""
    from analystos.db.models import Artifact, LineageEdge

    charts: dict[str, set[str]] = {}
    for from_id, to_id in session.execute(select(LineageEdge.from_id, LineageEdge.to_id).where(
            LineageEdge.workspace_id == workspace_id, LineageEdge.from_type == "insight",
            LineageEdge.from_id.in_(insight_ids), LineageEdge.relation == "visualized_by",
            LineageEdge.to_type == "chart")):
        charts.setdefault(to_id, set()).add(from_id)
    if not charts:
        return {}
    presents = dict(charts)
    for from_id, to_id in session.execute(select(LineageEdge.from_id, LineageEdge.to_id).where(
            LineageEdge.workspace_id == workspace_id, LineageEdge.from_type == "chart",
            LineageEdge.from_id.in_(list(charts)), LineageEdge.relation == "included_in",
            LineageEdge.to_type == "dashboard")):
        presents.setdefault(to_id, set()).update(charts[from_id])
    published = set(session.scalars(select(Artifact.id).where(Artifact.id.in_(list(presents)),
                                                               Artifact.status == "published",
                                                               Artifact.type.in_(PRESENTERS))))
    return {a: ids for a, ids in presents.items() if a in published}


def raise_for_voids(session: Session, record_ids: Iterable[str]) -> list[str]:
    """Banner every published artifact presenting a finding these records voided; returns artifact ids."""
    from analystos.db.models import Artifact, Insight, VerificationRecord

    recs = [r for r in session.scalars(select(VerificationRecord).where(VerificationRecord.id.in_(list(record_ids))))
            if r.subject_type == "insight" and r.state == "VOID"]
    if not recs:
        return []
    by_ws: dict[str, list[Any]] = {}
    for r in recs:
        by_ws.setdefault(r.workspace_id, []).append(r)
    touched: list[str] = []
    for ws, rs in sorted(by_ws.items()):
        insights = {i.id: i for i in session.scalars(select(Insight).where(Insight.id.in_([r.subject_id for r in rs])))}
        by_insight = {r.subject_id: r for r in rs}
        for art_id, ins_ids in sorted(_published_presenters(session, ws, list(by_insight)).items()):
            art = session.get(Artifact, art_id)
            entries = [{"insight_id": i, "code": insights[i].code if i in insights else None,
                        "record_id": by_insight[i].id, "kind": by_insight[i].void_kind,
                        "reason": by_insight[i].void_reason} for i in sorted(ins_ids)]
            _banner(session, art, entries)
            touched.append(art_id)
    return touched


def _banner(session: Session, art: Any, entries: list[dict[str, Any]]) -> None:
    from analystos.events.bus import emit

    old = dict(art.banner or {})
    known = {f.get("insight_id"): f for f in old.get("findings") or []}
    added = [e for e in entries if known.get(e["insight_id"], {}).get("record_id") != e["record_id"]]
    if not added and old.get("state") == "void":
        return
    findings = list({**known, **{e["insight_id"]: e for e in entries}}.values())[:MAX_FINDINGS]
    lead = added[0] if added else entries[0]
    reason = (f"{lead['code'] or 'a finding'} is void: {lead['reason'] or 'a dependency changed'}"
              + (f" (and {len(findings) - 1} more)" if len(findings) > 1 else ""))
    request_id = _desk_task(session, art, findings, reason, old.get("request_id"), added or entries)
    art.banner = {"state": "void", "reason": reason, "findings": findings, "request_id": request_id,
                  "raised_at": old.get("raised_at") or utcnow().isoformat(), "updated_at": utcnow().isoformat()}
    emit(art.workspace_id, "publication.void_banner",
         {"artifact_id": art.id, "type": art.type, "name": art.name, "reason": reason, "request_id": request_id,
          "findings": [f["code"] for f in findings]}, run_id=art.run_id, session=session)


def _requester(session: Session, art: Any) -> str | None:
    from analystos.db.models import AnalysisRun, Workspace

    if art.creator_user:
        return art.creator_user
    run = session.get(AnalysisRun, art.run_id) if art.run_id else None
    if run is not None and run.requested_by:
        return run.requested_by
    ws = session.get(Workspace, art.workspace_id)
    return ws.created_by if ws is not None else None


def _desk_task(session: Session, art: Any, findings: list[dict[str, Any]], reason: str, request_id: str | None,
               added: list[dict[str, Any]]) -> str | None:
    """One open Desk task per bannered artifact (S13): escalated to a person, with the void causes as its data pack."""
    from analystos.desk import service as desk
    from analystos.desk.escalation import desk_settings, pick_assignee
    from analystos.desk.models import Request
    from analystos.desk.states import TERMINAL, sla_due

    actor = "system:verification"
    if request_id:
        req = session.get(Request, request_id)
        if req is not None and req.state not in TERMINAL:
            desk._event(session, req, "void_added", actor, payload={"artifact_id": art.id,
                                                                     "findings": [f["code"] for f in added]})
            req.data_pack = {**(req.data_pack or {}), "findings": findings}
            return req.id
    requester = _requester(session, art)
    if requester is None:
        log.warning("void banner on %s: no requester to own a Desk task", art.id)
        return None
    now = utcnow()
    text = f"Re-verify or withdraw the published {art.type} '{art.name}': {reason}."[:4000]
    req = Request(id=new_id("rq"), workspace_id=art.workspace_id, requester_id=requester, channel="system", text=text,
                  normalized_intent={"key": f"void:{art.id}", "artifact_id": art.id}, state="new", priority="high",
                  request_class="R9", run_ids=[art.run_id] if art.run_id else [], followers=[], cost_usd=0.0, revision=1,
                  triage={"class": "R9", "answered_by": "verification", "confidence": 1.0, "flags": ["void_banner"],
                          "reason": "a finding this published artifact presents is VOID"})
    session.add(req)
    session.flush()
    desk._event(session, req, "created", actor, to_state="new", payload={"channel": "system", "priority": "high",
                                                                         "artifact_id": art.id})
    desk.move(session, req, "triaged", actor, type_="triaged", payload={"class": "R9", "flags": ["void_banner"]}, now=now)
    req.sla_due_at = sla_due(now, req.priority, desk_settings(session, req.workspace_id).get("sla_hours"))
    req.assignee_id = pick_assignee(session, req.workspace_id, exclude=None)
    req.data_pack = {"reason": reason, "findings": findings,
                     "evidence": [{"kind": art.type, "id": art.id, "name": art.name, "url": art.external_url},
                                  *({"kind": "verification_record", "id": f["record_id"]} for f in findings),
                                  *({"kind": "run", "id": r} for r in req.run_ids)],
                     "remedy": "re-run the analysis so REV verifies these findings against the current dependencies, "
                               "then republish; or withdraw the artifact"}
    desk.move(session, req, "escalated", actor, type_="escalated", payload={"reason": reason, "artifact_id": art.id}, now=now)
    return req.id


def clear_for(session: Session, workspace_id: str, insight_id: str) -> list[str]:
    """A finding has a live verified record again: drop it from banners; clear a banner with nothing left."""
    from analystos.db.models import Artifact
    from analystos.events.bus import emit
    from analystos.evidence.verification import insight_states

    cleared = []
    for art in session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id,
                                                      Artifact.status == "published", Artifact.banner.is_not(None))):
        banner = dict(art.banner or {})
        findings = banner.get("findings") or []
        if not any(f.get("insight_id") == insight_id for f in findings):
            continue
        states = insight_states(session, [f.get("insight_id") for f in findings])
        left = [f for f in findings if not (states.get(f.get("insight_id"), {}).get("state") == "ACTIVE"
                                            and states[f.get("insight_id")].get("verdict") == "verified")]
        if left:
            art.banner = {**banner, "findings": left, "updated_at": utcnow().isoformat()}
            continue
        art.banner = None
        cleared.append(art.id)
        emit(workspace_id, "publication.banner_cleared", {"artifact_id": art.id, "request_id": banner.get("request_id")},
             run_id=art.run_id, session=session)
    return cleared


def banner_of(art: Any) -> dict[str, Any] | None:
    """What a viewer of the artifact sees (None when nothing it presents is void)."""
    b = getattr(art, "banner", None)
    if not b:
        return None
    return {"state": b.get("state"), "message": f"Needs re-verification: {b.get('reason')}",
            "findings": [{k: f.get(k) for k in ("code", "kind", "reason")} for f in b.get("findings") or []],
            "request_id": b.get("request_id"), "raised_at": b.get("raised_at")}


__all__ = ["banner_of", "clear_for", "raise_for_voids"]

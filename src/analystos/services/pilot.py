"""Named owners and controlled-pilot readiness (P4-09; contracts in `contracts/pilot.py`).

Owners are recorded on the workspace and on each source (`owners` JSON: {"business": {...}, "technical": {...}}).
Readiness reads records and evidence files only: no query runs, no connection is opened, no model is asked.
VALUE-05 adds what the conversational task cards need: reviewed definitions for Ask and dashboards by conversation,
and a rehearsal of every feature on the cards before real analysts start.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.connectors.certification import EVIDENCE_DIR, certification
from analystos.contracts.investigation import PILOT_SUBJECTS
from analystos.contracts.pilot import OWNER_ROLES, OwnersIn, PilotCheck, PilotReadiness
from analystos.core.errors import InvalidInput
from analystos.core.ids import utcnow
from analystos.db.models import PilotReview, SemanticMetric, Source, User, VerifiedQuery, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_in_workspace, require_role, scoped_loader

# Sources the platform writes itself (recipe and pipeline outputs) have no external owner or connector.
INTERNAL_KINDS = frozenset({"recipe"})
DRILL_FILE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-recovery-drill\.md$")


def _owners_doc(session: Session, body: OwnersIn) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    for role in OWNER_ROLES:
        owner = getattr(body, role)
        if owner is None:
            continue
        if owner.user_id and session.get(User, owner.user_id) is None:
            raise InvalidInput(f"{role} owner: user {owner.user_id} does not exist")
        doc[role] = owner.model_dump(exclude_none=True)
    return doc


def set_workspace_owners(session: Session, user: User, workspace_id: str, body: OwnersIn) -> Workspace:
    """Only a workspace owner names who is accountable for it."""
    require_role(session, user, workspace_id, "owner")
    ws = get_workspace(session, workspace_id)
    before, ws.owners = dict(ws.owners or {}), _owners_doc(session, body)
    audit(f"user:{user.id}", "workspace.owners_changed", workspace_id=workspace_id, target=workspace_id, decision="allow",
          details={"before": before, "after": ws.owners}, session=session)
    emit(workspace_id, "workspace.owners_changed", {"owners": ws.owners}, actor=f"user:{user.id}", session=session)
    return ws


@scoped_loader
def set_source_owners(session: Session, user: User, workspace_id: str, source_id: str, body: OwnersIn) -> Source:
    src = load_in_workspace(session, Source, source_id, workspace_id, user=user, minimum="owner", label="source")
    if src.kind in INTERNAL_KINDS:
        raise InvalidInput(f"source {source_id} holds platform outputs; its owners are the workspace's")
    before, src.owners = dict(src.owners or {}), _owners_doc(session, body)
    audit(f"user:{user.id}", "source.owners_changed", workspace_id=workspace_id, target=source_id, decision="allow",
          details={"before": before, "after": src.owners}, session=session)
    emit(workspace_id, "source.owners_changed", {"source_id": source_id, "owners": src.owners}, actor=f"user:{user.id}",
         session=session)
    return src


def _owner_checks(owners: dict[str, Any] | None, subject: str, where: str, label: str = "this workspace") -> list[PilotCheck]:
    out = []
    for role in OWNER_ROLES:
        o = (owners or {}).get(role)
        if o and o.get("name") and o.get("email"):
            out.append(PilotCheck(check=f"owner.{role}", subject=subject, status="pass", reason=f"{o['name']} <{o['email']}>"))
        else:
            out.append(PilotCheck(check=f"owner.{role}", subject=subject, status="missing",
                                  reason=f"no named {role} owner",
                                  remediation=f"a workspace owner names one: PUT {where}/owners",
                                  action=f"Name the {role} owner of {label}", who="workspace_owner", screen="members_policy"))
    return out


def latest_drill(evidence_dir: Path | None = None) -> tuple[str, bool] | None:
    """(path, passed) of the newest recovery-drill evidence file, if any."""
    folder = evidence_dir or EVIDENCE_DIR
    if not folder.is_dir():
        return None
    found = sorted((p for p in folder.glob("*-recovery-drill.md") if DRILL_FILE.match(p.name)), key=lambda p: p.name)
    if not found:
        return None
    path = found[-1]
    passed = "**Result: PASSED**" in path.read_text(encoding="utf-8", errors="replace")[:4000]
    return str(path.relative_to(folder.parents[2]) if len(folder.parents) > 2 else path), passed


def _identity_check(ws: Workspace) -> PilotCheck:
    from analystos.security import oidc

    if not oidc.enabled():
        return PilotCheck(check="identity.sso", subject="platform", status="missing",
                          reason="single sign-on is not configured: users sign in with local passwords",
                          remediation="set ANALYSTOS_OIDC_ISSUER / ANALYSTOS_OIDC_CLIENT_ID and map groups in config/oidc.yaml",
                          action="Set up single sign-on so people use their company sign-in", who="platform_admin")
    try:
        mapping = oidc.load_mapping()
    except InvalidInput as exc:
        return PilotCheck(check="identity.sso", subject="platform", status="fail", reason=exc.message,
                          remediation="fix config/oidc.yaml", action="Fix the single sign-on settings", who="platform_admin")
    grants = [g for g in mapping.workspace_roles if g.workspace in (ws.id, ws.name)]
    if not grants:
        return PilotCheck(check="identity.sso", subject="workspace", status="missing",
                          reason="single sign-on is on, but no identity-provider group maps to a role in this workspace",
                          remediation="add a workspace_roles entry for this workspace in config/oidc.yaml",
                          action="Give this workspace's sign-in groups their roles", who="platform_admin")
    return PilotCheck(check="identity.sso", subject="workspace", status="pass",
                      reason="groups map to roles: " + ", ".join(f"{g.group} -> {g.role}" for g in grants))


# Plain names for the features on the pilot task cards (docs/60-delivery/pilot/02-task-cards.md).
FEATURE_LABEL = {"investigation": "investigation", "ask": "Ask", "dashboard": "dashboard by conversation",
                 "report": "report by conversation", "digest": "scheduled digest"}


def _definitions_check(session: Session, ws: Workspace) -> PilotCheck:
    """Ask and dashboards by conversation answer from approved metrics and verified queries first; without any,
    the pilot would only measure generated SQL."""
    metrics = session.scalar(select(func.count()).select_from(SemanticMetric)
                             .where(SemanticMetric.workspace_id == ws.id, SemanticMetric.status == "approved")) or 0
    queries = session.scalar(select(func.count()).select_from(VerifiedQuery)
                             .where(VerifiedQuery.workspace_id == ws.id, VerifiedQuery.status == "active")) or 0
    if metrics or queries:
        return PilotCheck(check="conversation.definitions", subject="workspace", status="pass",
                          reason=f"{metrics} approved metric{'s' * (metrics != 1)}, {queries} verified quer{'ies' if queries != 1 else 'y'}")
    return PilotCheck(check="conversation.definitions", subject="workspace", status="missing",
                      reason="no approved metric or verified query: Ask and dashboards by conversation have no reviewed definitions to answer from",
                      remediation="approve the pilot's key metrics (Catalog → Metrics) or save a checked Ask answer as a verified query",
                      action="Approve 3 to 5 key metrics for the pilot, or save a checked Ask answer as a verified question",
                      who="editor", screen="metrics")


def _rehearsal_check(session: Session, ws: Workspace) -> PilotCheck:
    done = set(session.scalars(select(PilotReview.subject_type).where(PilotReview.workspace_id == ws.id,
                                                                      PilotReview.evidence_kind == "rehearsal").distinct()))
    missing = [FEATURE_LABEL[f] for f in PILOT_SUBJECTS if f not in done]
    if not missing:
        return PilotCheck(check="pilot.rehearsal", subject="workspace", status="pass",
                          reason="every task-card feature has a recorded rehearsal: " + ", ".join(FEATURE_LABEL.values()))
    return PilotCheck(check="pilot.rehearsal", subject="workspace", status="missing",
                      reason="not rehearsed here yet: " + ", ".join(missing),
                      remediation="walk the task cards once and record each as a rehearsal in the pilot scorecard",
                      action="Try each task card once and record it as a rehearsal in the pilot scorecard",
                      who="analyst", screen="scorecard")


def readiness(session: Session, user: User, workspace_id: str, *, evidence_dir: Path | None = None) -> PilotReadiness:
    require_role(session, user, workspace_id, "viewer")
    ws = get_workspace(session, workspace_id)
    checks = _owner_checks(ws.owners, "workspace", f"/api/workspaces/{ws.id}")
    sources = list(session.scalars(select(Source).where(Source.workspace_id == ws.id, Source.kind.not_in(INTERNAL_KINDS))
                                   .order_by(Source.created_at)))
    if not sources:
        checks.append(PilotCheck(check="connector.health", subject="workspace", status="missing",
                                 reason="the workspace has no source", remediation="register the pilot's source",
                                 action="Connect the pilot's data", who="editor", screen="sources"))
    for src in sources:
        subject = f"source:{src.id} ({src.name})"
        checks += _owner_checks(src.owners, subject, f"/api/workspaces/{ws.id}/sources/{src.id}", f"the {src.name} source")
        cert = certification(src.kind, evidence_dir)
        if cert["status"] == "certified":
            checks.append(PilotCheck(check="connector.certified", subject=subject, status="pass", evidence=cert["evidence"],
                                     reason=f"{src.kind} certified live on {cert['evidence_date']} ({cert['engine']})"))
        else:
            checks.append(PilotCheck(check="connector.certified", subject=subject, status="missing",
                                     reason=f"{src.kind} has catalog and unit tests only, no live certification",
                                     remediation=f"run scripts/certify_connectors.py --kinds {src.kind} against a live "
                                                 f"{src.kind} before the pilot relies on it",
                                     action=f"Have the {src.kind} connection tested against your live system before the pilot "
                                            "relies on it", who="platform_admin"))
        if src.status == "error" or src.last_error:  # a successful crawl clears last_error
            usable = "" if src.status == "error" else f" (status {src.status}: data already staged stays readable)"
            checks.append(PilotCheck(check="connector.health", subject=subject, status="fail",
                                     reason=f"the last connection failed: {(src.last_error or 'no detail recorded')[:300]}{usable}",
                                     remediation="fix the connection and discover the source again",
                                     action=f"Fix the connection to {src.name} and check the source again", who="editor",
                                     screen="sources"))
        elif src.last_discovered_at is None:
            checks.append(PilotCheck(check="connector.health", subject=subject, status="missing",
                                     reason="the source was never discovered", remediation="discover the source",
                                     action=f"Check the {src.name} source so its tables are known", who="editor",
                                     screen="sources"))
        else:
            checks.append(PilotCheck(check="connector.health", subject=subject, status="pass",
                                     reason=f"discovered {src.last_discovered_at:%Y-%m-%d %H:%M} UTC, status {src.status}"))
    checks.append(_identity_check(ws))
    checks += [_definitions_check(session, ws), _rehearsal_check(session, ws)]
    drill = latest_drill(evidence_dir)
    if drill is None:
        checks.append(PilotCheck(check="recovery.drill", subject="platform", status="missing", reason="no recovery drill on record",
                                 remediation="run scripts/recovery_drill.py --report auto (docs/30-runbooks/06-backup-and-recovery.md)",
                                 action="Run a backup and recovery drill", who="platform_admin"))
    else:
        checks.append(PilotCheck(check="recovery.drill", subject="platform", status="pass" if drill[1] else "fail",
                                 evidence=drill[0], reason="the latest drill passed" if drill[1] else "the latest drill failed",
                                 remediation=None if drill[1] else "fix what the drill reported and run it again",
                                 action=None if drill[1] else "Fix what the recovery drill reported and run it again",
                                 who=None if drill[1] else "platform_admin"))
    missing = sum(c.status == "missing" for c in checks)
    failing = sum(c.status == "fail" for c in checks)
    return PilotReadiness(workspace_id=ws.id, workspace_name=ws.name, verdict="ready" if not (missing or failing) else "not_ready",
                          missing=missing, failing=failing, checks=checks, generated_at=utcnow())


def overview(session: Session, user: User) -> list[dict[str, Any]]:
    """Every active workspace's verdict and counts (administrators)."""
    out = []
    for ws in session.scalars(select(Workspace).where(Workspace.deleted_at.is_(None), Workspace.status == "active")
                              .order_by(Workspace.created_at)):
        r = readiness(session, user, ws.id)
        out.append({"workspace_id": ws.id, "workspace_name": ws.name, "verdict": r.verdict, "missing": r.missing,
                    "failing": r.failing, "gaps": [f"{c.check} ({c.subject})" for c in r.checks if c.status != "pass"]})
    return out

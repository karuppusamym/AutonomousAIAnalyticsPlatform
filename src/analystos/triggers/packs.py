"""Recurring packs (spec S09-T4, FR-050): a schedule of a template playbook (`kpi_review`, `variance`,
`funnel`, `cohort`, `dq_review`) at a pinned version, plus an output template (dashboard refresh, PDF/email
brief, Teams card).

Delivery policy, decided in code after each fire's run completes:

* the first run always goes to a person for review;
* a later run is delivered automatically when the previous `auto_deliver_after_n_clean_runs` (default 3)
  runs were clean and none of this run's verification records is VOID;
* otherwise it routes to review.

A run is *clean* when it was delivered (automatically, or after an approved review) with no VOID verification;
a rejected review, a VOID record or a failed run breaks the streak. A review is a hash-bound approval of exactly
this report snapshot and these destinations (`verify_for_execution` before anything is sent), and every send
still re-verifies its destination's own authorization (N-3).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import Approval, Artifact, DeliveryDestination, Schedule, ScheduleRun, User, VerificationRecord
from analystos.events.bus import emit
from analystos.governance.audit import audit

TEMPLATES = ("kpi_review", "variance", "funnel", "cohort", "dq_review")
OUTPUTS: dict[str, dict[str, Any]] = {
    "dashboard_refresh": {"report": {"kind": "executive", "formats": ["html"]}, "destinations": ()},
    "pdf_brief": {"report": {"kind": "executive", "formats": ["pdf", "html"]}, "destinations": ("email", "webhook")},
    "email_brief": {"report": {"kind": "executive", "formats": ["html", "pdf"]}, "destinations": ("email",)},
    "teams_card": {"report": {"kind": "executive", "formats": ["html"]}, "destinations": ("teams", "slack")},
}
REVIEW_ACTION = "pack_delivery.review"
DEFAULT_CLEAN_RUNS = 3
KEYS = {"template", "version", "question", "output", "deliver_to", "auto_deliver_after_n_clean_runs", "source_ids",
        "publish", "demo"}


def validate_shape(config: dict[str, Any]) -> None:
    extra = set(config) - KEYS
    if extra:
        raise InvalidInput(f"unknown pack schedule fields: {sorted(extra)}")
    if config.get("template") not in TEMPLATES:
        raise InvalidInput(f"config.template must be one of {list(TEMPLATES)}")
    output = config.get("output") or {}
    if not isinstance(output, dict) or output.get("kind", "pdf_brief") not in OUTPUTS:
        raise InvalidInput(f"config.output.kind must be one of {list(OUTPUTS)}")
    n = config.get("auto_deliver_after_n_clean_runs", DEFAULT_CLEAN_RUNS)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 50:
        raise InvalidInput("auto_deliver_after_n_clean_runs must be an integer from 1 to 50")
    if config.get("question") is not None and (not isinstance(config["question"], str) or len(config["question"]) > 2000):
        raise InvalidInput("config.question must be text (at most 2000 characters)")


def prepare(session: Session, workspace_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Pin the template's installed version (an upgrade is a schedule edit, never silent) and check the
    destinations fit the output template."""
    from analystos.services.definitions import resolve_runnable

    validate_shape(config)
    ref, _ = resolve_runnable(session, workspace_id, definition(config), trigger="schedule")
    kind = (config.get("output") or {}).get("kind", "pdf_brief")
    allowed = OUTPUTS[kind]["destinations"]
    for dest_id in config.get("deliver_to") or []:
        dest = session.get(DeliveryDestination, dest_id)
        if dest is not None and dest.kind not in allowed:
            raise InvalidInput(f"a {kind} pack delivers to {list(allowed) or 'nothing outside the platform'}, "
                               f"not to the {dest.kind} destination {dest.name}")
    return {**config, "version": str(ref.version), "output": {"kind": kind}}


def definition(config: dict[str, Any]) -> dict[str, Any]:
    ref: dict[str, Any] = {"key": f"playbook.{config['template']}"}
    if config.get("version"):
        ref.update(version=str(config["version"]), source="builtin")  # the installed template, pinned by semver
    return ref


def execute(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Start the pinned template as a run; the run's finalize step completes the schedule_run."""
    from analystos.services.runs import create_run

    kind = (config.get("output") or {}).get("kind", "pdf_brief")
    objective = config.get("question") or f"Recurring {config['template'].replace('_', ' ')} for this workspace"
    run = create_run(owner, workspace_id, objective=objective, source_ids=config.get("source_ids"),
                     definition=definition(config),
                     origin={"type": "schedule", "schedule_id": schedule_id, "schedule_run_id": srun_id,
                             "publish": "skip", "pack": config["template"], "report": OUTPUTS[kind]["report"]})
    return {"_pending": True, "run_id": run.id, "pack": {"template": config["template"], "version": config.get("version"),
                                                         "output": kind}}


# ------------------------------------------------------------------------------------ delivery policy
def void_records(session: Session, run_id: str | None) -> list[str]:
    if not run_id:
        return []
    return list(session.scalars(select(VerificationRecord.id).where(VerificationRecord.run_id == run_id,
                                                                    VerificationRecord.state == "VOID")))


def clean_streak(session: Session, schedule_id: str, *, exclude: str | None = None, limit: int = 50) -> int:
    """Consecutive clean runs before this one, newest first; anything not clean (a rejected or pending review,
    a VOID record, a failure) ends the count."""
    streak = 0
    for srun in session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id)
                                .order_by(ScheduleRun.scheduled_for.desc(), ScheduleRun.started_at.desc()).limit(limit)):
        if srun.id == exclude or srun.status in ("started", "running"):
            continue
        if srun.status == "succeeded" and ((srun.result or {}).get("pack") or {}).get("clean") is True:
            streak += 1
            continue
        break
    return streak


def review_payload(session: Session, sch: Schedule, srun_id: str, run_id: str | None, report_id: str) -> dict[str, Any]:
    """What a reviewer approves: this exact report snapshot to these exact destinations."""
    art = session.get(Artifact, report_id)
    dests = []
    for dest_id in (sch.config or {}).get("deliver_to") or []:
        dest = session.get(DeliveryDestination, dest_id)
        if dest is not None:
            dests.append({"id": dest.id, "destination_hash": dest.destination_hash})
    return {"schedule_id": sch.id, "schedule_run_id": srun_id, "run_id": run_id, "report_artifact_id": report_id,
            "report_content_hash": art.content_hash if art is not None else None,
            "template": (sch.config or {}).get("template"), "version": (sch.config or {}).get("version"),
            "destinations": dests}


def _send(session: Session, sch: Schedule, srun: ScheduleRun, report_id: str) -> list[str]:
    from analystos.services.notifications import deliver, notify

    notify(session, sch.workspace_id, kind="report", title=f"{sch.name}: new brief", body="",
           link={"type": "report", "id": report_id})
    return deliver(session, sch.workspace_id, (sch.config or {}).get("deliver_to"), subject_type="report",
                   subject_id=report_id, origin={"schedule_id": sch.id, "schedule_run_id": srun.id})


def route(session: Session, sch: Schedule, srun: ScheduleRun, report_id: str) -> dict[str, Any]:
    """After a fire's run completed with a report: deliver now, or ask a person to review it."""
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace
    from analystos.services.notifications import notify

    run_id = (srun.result or {}).get("run_id")
    void = void_records(session, run_id)
    streak = clean_streak(session, sch.id, exclude=srun.id)
    need = int((sch.config or {}).get("auto_deliver_after_n_clean_runs", DEFAULT_CLEAN_RUNS))
    if not void and streak >= need:
        queued = _send(session, sch, srun, report_id)
        out = {"mode": "auto", "clean": True, "streak": streak, "void": [], "deliveries": queued}
        audit(f"schedule:{sch.id}", "pack.auto_delivered", workspace_id=sch.workspace_id, target=srun.id, decision="allow",
              details={"streak": streak, "required": need, "report_artifact_id": report_id}, session=session)
        return out
    if void:
        reason = f"{len(void)} verification record(s) of this run are VOID"
    elif streak == 0 and not _has_previous(session, sch.id, srun.id):
        reason = "first run: a person reviews it before anything is delivered"
    else:
        reason = f"{streak} of {need} clean runs so far"
    ws = get_workspace(session, sch.workspace_id)
    approval = request_approval(session, workspace_id=sch.workspace_id, run_id=None, action=REVIEW_ACTION,
                                payload=review_payload(session, sch, srun.id, run_id, report_id), plan_hash=None,
                                policy_version=ws.policy_version, requested_by=sch.owner_id, risk_tier="medium",
                                destination=f"pack:{sch.name}"[:120], affected_assets=[],
                                evidence={"summary": f"Review the {sch.name} brief before it is delivered ({reason})",
                                          "void_records": void})
    notify(session, sch.workspace_id, kind="approval", title=f"Review needed: {sch.name}", body=reason,
           link={"type": "approval", "id": approval.id})
    audit(f"schedule:{sch.id}", "pack.review_requested", workspace_id=sch.workspace_id, target=srun.id,
          decision="approval_required", details={"reason": reason, "approval_id": approval.id}, session=session)
    return {"mode": "review", "clean": None, "approval_id": approval.id, "reason": reason, "streak": streak, "void": void}


def _has_previous(session: Session, schedule_id: str, srun_id: str) -> bool:
    return session.scalar(select(ScheduleRun.id).where(ScheduleRun.schedule_id == schedule_id, ScheduleRun.id != srun_id,
                                                       ScheduleRun.status.in_(["succeeded", "failed"])).limit(1)) is not None


def apply_review(session: Session, approval: Approval) -> ScheduleRun | None:
    """After the reviewer decides: approved -> verify the approval against the snapshot and destinations as they
    are now, deliver, and count the run clean (unless VOID); rejected -> not clean."""
    from analystos.governance.approvals import consume, verify_for_execution

    if approval.action != REVIEW_ACTION:
        return None
    srun = session.get(ScheduleRun, (approval.payload or {}).get("schedule_run_id"))
    if srun is None:
        raise NotFound("the reviewed schedule run no longer exists")
    sch = session.get(Schedule, srun.schedule_id)
    pack = dict((srun.result or {}).get("pack") or {})
    if approval.status == "rejected":
        pack.update(clean=False, mode="rejected")
    elif approval.status == "approved":
        report_id = approval.payload["report_artifact_id"]
        payload = review_payload(session, sch, srun.id, (srun.result or {}).get("run_id"), report_id)
        verify_for_execution(session, approval.id, payload=payload, plan_hash=None)
        consume(session, approval)  # single use: this snapshot, once
        void = void_records(session, (srun.result or {}).get("run_id"))
        pack.update(mode="reviewed", clean=not void, void=void, deliveries=_send(session, sch, srun, report_id))
    else:
        return srun
    srun.result = {**(srun.result or {}), "pack": pack}
    emit(srun.workspace_id, "pack.reviewed", {"schedule_run_id": srun.id, "approval_id": approval.id,
                                              "status": approval.status, "clean": pack.get("clean")}, session=session)
    return srun

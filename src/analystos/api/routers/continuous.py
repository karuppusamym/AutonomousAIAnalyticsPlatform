"""Phase 3 API: schedules, monitors, alerts, notifications, reports (§37-§38, §43)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import Alert, AnalysisRun, Artifact, Monitor, Schedule, ScheduleRun, User
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role
from analystos.services import monitors as mon_svc
from analystos.services import notifications as note_svc
from analystos.services import reports as report_svc
from analystos.services import schedules as sch_svc

router = APIRouter(prefix="/api", tags=["continuous"])


class ScheduleIn(BaseModel):
    name: str
    kind: str
    cron: str
    timezone: str = "UTC"
    config: dict = {}


class SchedulePatch(BaseModel):
    name: str | None = None
    cron: str | None = None
    timezone: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class UpgradeIn(BaseModel):
    upgrade_hash: str | None = None  # from the pin status the owner reviewed


class MonitorIn(BaseModel):
    name: str
    kind: str
    config: dict = {}
    auto_investigate: bool = False


class MonitorPatch(BaseModel):
    enabled: bool | None = None
    auto_investigate: bool | None = None
    config: dict | None = None
    name: str | None = None


class ReportIn(BaseModel):
    run_id: str | None = None
    kind: str = "executive"
    formats: list[str] = ["html", "pdf", "xlsx"]


class ReadIn(BaseModel):
    ids: list[int]


# ---------------------------------------------------------------------------------- schedules
def schedule_out(sch: Schedule) -> dict:
    """A schedule with its pins summarized (refs, versions, revision; not the bound manifests)."""
    from analystos.services.pins import view

    return {**row(sch, exclude={"pins"}), "pins": view(sch)}


@router.post("/workspaces/{workspace_id}/schedules")
def create_schedule(workspace_id: str, body: ScheduleIn, response: Response, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function"),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    from analystos.api.http import set_etag
    from analystos.services.idempotency import Key, begin_in, complete_in

    require_role(session, user, workspace_id, "editor")
    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="schedule.create",
                 request=body.model_dump())
    if key is not None and (replay := begin_in(session, key)) is not None:
        sch = load_in_workspace(session, Schedule, replay.resource_id, workspace_id, user=user, label="schedule")
        response.headers["Idempotent-Replayed"] = "true"
    else:
        sch = sch_svc.create_schedule(session, session.merge(user), workspace_id, **body.model_dump())
        session.flush()
        if key is not None:
            complete_in(session, key, response={"id": sch.id}, resource_type="schedule", resource_id=sch.id)
    set_etag(response, sch.revision)
    return schedule_out(sch)


@router.get("/workspaces/{workspace_id}/schedules")
def list_schedules(workspace_id: str, limit: int | None = None, cursor: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """Oldest first as an array (existing clients); with `limit`/`cursor`, newest-first `{items, next_cursor}` pages."""
    from analystos.api.http import page, wants_page

    require_role(session, user, workspace_id, "viewer")

    def render(schedules) -> list[dict]:
        out = []
        for sch in schedules:
            runs = session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == sch.id).order_by(ScheduleRun.started_at.desc()).limit(10))
            out.append({**schedule_out(sch), "recent_runs": rows(runs)})
        return out
    stmt = select(Schedule).where(Schedule.workspace_id == workspace_id)
    if wants_page(limit, cursor):
        return page(session, stmt, Schedule, limit=limit, cursor=cursor, scope={"workspace": workspace_id, "list": "schedules"},
                    render=render)
    return render(session.scalars(stmt.order_by(Schedule.created_at)))


@router.get("/workspaces/{workspace_id}/schedules/{schedule_id}")
def get_schedule(workspace_id: str, schedule_id: str, response: Response, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """The schedule, its ETag revision and its pin status computed now: upgrade available (with the
    diff), deprecated (keeps running, with a warning) or blocked (a retired or rejected pin)."""
    from analystos.api.http import set_etag
    from analystos.services.pins import refresh

    sch = load_in_workspace(session, Schedule, schedule_id, workspace_id, user=user, label="schedule")
    status = refresh(session, sch)
    set_etag(response, sch.revision)
    return {**schedule_out(sch), "pin_status": status.model_dump(mode="json")}


@router.post("/workspaces/{workspace_id}/schedules/{schedule_id}/upgrade")
def accept_schedule_upgrade(workspace_id: str, schedule_id: str, body: UpgradeIn, response: Response,
                            user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                            if_match: str | None = Header(default=None)):
    """The owner accepts the available upgrade: a new schedule revision whose next fire is the new
    baseline. Requires If-Match (the revision) and the `upgrade_hash` the owner reviewed."""
    from analystos.api.http import expected_revision, set_etag
    from analystos.services.pins import accept_upgrade

    sch = load_in_workspace(session, Schedule, schedule_id, workspace_id, user=user, minimum="editor", label="schedule",
                            for_update=True)
    out = accept_upgrade(session, session.merge(user), sch, expected_revision=expected_revision(if_match, required=True),
                         upgrade_hash=body.upgrade_hash)
    set_etag(response, sch.revision)
    return out


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: SchedulePatch, response: Response, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """Edit a schedule. `If-Match` (its ETag) makes a stale edit 412; clients that predate revisions may omit it."""
    from analystos.api.http import expected_revision, set_etag

    load_in_workspace(session, Schedule, schedule_id, user=user, label="schedule")
    sch = sch_svc.update_schedule(session, session.merge(user), schedule_id, body.model_dump(),
                                  expected_revision=expected_revision(if_match, required=False))
    set_etag(response, sch.revision)
    return schedule_out(sch)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    sch = load_in_workspace(session, Schedule, schedule_id, user=user, minimum="editor", label="schedule")
    audit(f"user:{user.id}", "schedule.deleted", workspace_id=sch.workspace_id, target=sch.id, session=session)
    session.delete(sch)
    return {"deleted": True}


@router.post("/schedules/{schedule_id}/run")
def run_schedule_now(schedule_id: str, user: User = Depends(current_user)):
    from analystos.db.base import session_scope

    with session_scope() as s:
        load_in_workspace(s, Schedule, schedule_id, user=user, label="schedule")
    srun = sch_svc.run_now(user, schedule_id)
    with session_scope() as s:
        return row(s.get(ScheduleRun, srun))


# ---------------------------------------------------------------------------------- monitors
@router.post("/workspaces/{workspace_id}/monitors")
def create_monitor(workspace_id: str, body: MonitorIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    m = mon_svc.create_monitor(session, session.merge(user), workspace_id, **body.model_dump())
    session.flush()
    return row(m)


@router.get("/workspaces/{workspace_id}/monitors")
def list_monitors(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id).order_by(Monitor.created_at)))


@router.patch("/monitors/{monitor_id}")
def patch_monitor(monitor_id: str, body: MonitorPatch, response: Response, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """Edit a monitor. `If-Match` (its ETag) makes a stale edit 412; clients that predate revisions may omit it (P4-06)."""
    from analystos.api.http import expected_revision, set_etag
    from analystos.core.errors import PreconditionFailed

    expected = expected_revision(if_match, required=False)
    m = load_in_workspace(session, Monitor, monitor_id, user=user, minimum="editor", label="monitor",
                          for_update=expected is not None)
    if expected is not None and expected != (m.revision or 1):
        raise PreconditionFailed(f"monitor {m.id} is at revision {m.revision or 1}, not {expected}",
                                 details={"current_revision": m.revision or 1})
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "config" in changes:
        mon_svc.validate_baseline(session, m.workspace_id, changes["config"])
        mon_svc.validate_delivery(session, m.workspace_id, changes["config"])
        mon_svc.validate_rule(session, m.workspace_id, changes["config"])
    for k, v in changes.items():
        setattr(m, k, v)
    if changes:
        m.revision = (m.revision or 1) + 1
    if "config" in changes:  # S09: new scope, recipients, playbook or limits -> the rule is approved again
        from analystos.triggers.rules import after_config_change

        after_config_change(session, session.merge(user), m)
    audit(f"user:{user.id}", "monitor.updated", workspace_id=m.workspace_id, target=m.id, details=body.model_dump(), session=session)
    set_etag(response, m.revision or 1)
    return row(m)


@router.post("/monitors/{monitor_id}/evaluate")
def evaluate_monitor(monitor_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, Monitor, monitor_id, user=user, minimum="analyst", label="monitor")
    session.commit()
    return mon_svc.evaluate_monitor(monitor_id, trigger=f"user:{user.id}")


@router.get("/monitors/{monitor_id}/series")
def monitor_series(monitor_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    m = load_in_workspace(session, Monitor, monitor_id, user=user, label="metric monitor")
    if m.kind == "data_quality":
        raise NotFound("metric monitor not found")
    owner = session.get(User, m.created_by)
    return mon_svc.metric_series(session, owner, m)


# ---------------------------------------------------------------------------------- alerts
@router.get("/workspaces/{workspace_id}/alerts")
def list_alerts(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Alert).where(Alert.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Alert.status == status)
    return rows(session.scalars(stmt.order_by(Alert.created_at.desc()).limit(200)))


@router.post("/alerts/{alert_id}/{action}")
def alert_action(alert_id: str, action: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    a = load_in_workspace(session, Alert, alert_id, user=user, minimum="analyst", label="alert")
    from analystos.decisions.calibration import record_signal

    if action == "investigate":
        record_signal(session, "alert.investigate", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
        session.commit()
        return {"run_id": mon_svc.start_investigation(alert_id, user)}
    if action == "acknowledge":
        a.status, a.acknowledged_by = "acknowledged", user.id
        record_signal(session, "alert.acknowledge", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
    elif action in ("resolve", "dismiss"):
        from analystos.core.ids import utcnow

        a.status, a.resolved_at = "resolved", utcnow()
        if action == "dismiss":  # "not worth attention": the labelled outcome that calibrates alert triage
            a.data = {**(a.data or {}), "dismissed_by": user.id}
            record_signal(session, "alert.dismiss", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
    else:
        raise NotFound("unknown action")
    audit(f"user:{user.id}", f"alert.{action}", workspace_id=a.workspace_id, target=a.id, session=session)
    return row(a)


# ---------------------------------------------------------------------------------- notifications
@router.get("/notifications")
def notifications(unread: bool = False, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [{**row(n), "read": user.id in (n.read_by or [])} for n in note_svc.list_for(session, user, unread_only=unread)]


@router.post("/notifications/read")
def read_notifications(body: ReadIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return {"marked": note_svc.mark_read(session, user, body.ids)}


# ---------------------------------------------------------------------------------- reports
@router.post("/workspaces/{workspace_id}/reports")
def create_report(workspace_id: str, body: ReportIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "analyst")
    run_id = body.run_id or session.scalar(select(AnalysisRun.id).where(
        AnalysisRun.workspace_id == workspace_id, AnalysisRun.status == "COMPLETED").order_by(AnalysisRun.finished_at.desc()))
    if not run_id:
        raise InvalidInput("no completed run to report on")
    run = session.get(AnalysisRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise NotFound("run not found in this workspace")
    art = report_svc.generate_report(session, run_id, kind=body.kind, formats=tuple(body.formats), actor=f"user:{user.id}")
    return row(art)


@router.get("/artifacts/{artifact_id}/download")
def download(artifact_id: str, format: str = "pdf", user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Download to the requesting user (audited). Sending outside the platform would need an approval."""
    art = load_in_workspace(session, Artifact, artifact_id, user=user, label="report")
    if art.type != "report":
        raise NotFound("report not found")
    content, mime, ext = report_svc.report_file(art, format)
    audit(f"user:{user.id}", "report.downloaded", workspace_id=art.workspace_id, target=art.id, details={"format": format}, session=session)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in art.name)[:60]
    return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'})

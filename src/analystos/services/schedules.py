"""Scheduling (§37, SCH-001..005).

Firing is claim-then-execute: due schedules are claimed with FOR UPDATE SKIP LOCKED, their next
fire time advanced, and a schedule_run inserted under a unique fire_key in the same transaction —
so several scheduler processes, restarts and manual "run now" never double-fire. Execution uses
the schedule owner's permissions as they are at fire time, not when the schedule was created.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Monitor, Schedule, ScheduleRun, Source, SourceAsset, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services import pins as pins_svc
from analystos.services.notifications import deliver, notify
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
KINDS = {"reanalysis": "analyst", "dataset_refresh": "editor", "report": "analyst", "monitor": "analyst", "crawl": "editor"}
KINDS["saved_analysis"] = "editor"
KINDS["step"] = "editor"  # a pinned step's frozen query (P7-04, services/step_pins.py)
KINDS["pipeline"] = "editor"  # P6-02: a published pipeline's incremental run, or a dry run proposing a materialization
MIN_INTERVAL_SECONDS = 15 * 60


def next_fire(cron: str, tz: str, after: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz)
    base = (after or utcnow()).astimezone(zone)
    return croniter(cron, base).get_next(datetime).astimezone(ZoneInfo("UTC"))


def validate(kind: str, cron: str, tz: str, config: dict) -> None:
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {sorted(KINDS)}")
    if not croniter.is_valid(cron):
        raise InvalidInput(f"invalid cron expression: {cron}")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise InvalidInput(f"unknown timezone {tz}") from exc
    first = next_fire(cron, tz)
    if (next_fire(cron, tz, first) - first).total_seconds() < MIN_INTERVAL_SECONDS:
        raise InvalidInput("schedules may not fire more often than every 15 minutes")
    deliver_to = config.get("deliver_to")
    if deliver_to is not None and (not isinstance(deliver_to, list) or not all(isinstance(d, str) for d in deliver_to)):
        raise InvalidInput("config.deliver_to must be a list of delivery destination ids")
    report = config.get("report")
    if report is not None and not isinstance(report, dict):
        raise InvalidInput("config.report must be an object like {kind, formats}")
    if config.get("publish", "skip") not in ("skip", "propose"):
        raise InvalidInput("config.publish must be skip or propose (publication always needs an approval)")
    if kind == "pipeline":
        if not isinstance(config.get("pipeline"), str) or not config["pipeline"]:
            raise InvalidInput("config.pipeline must name a published pipeline")
        if config.get("action", "run") not in ("run", "dry_run"):
            raise InvalidInput("config.action must be run or dry_run (a destination is only written after an approval)")
        if config.get("mode", "auto") not in ("auto", "full", "reconcile"):
            raise InvalidInput("config.mode must be auto, full or reconcile")
    if kind == "reanalysis":
        from analystos.registries.replay import validate_schedule_config

        validate_schedule_config(config)
        definition = config.get("definition")
        if definition is not None and not isinstance(definition, (dict, str)):
            raise InvalidInput("config.definition must name a playbook definition: {key, version} or a definition id")


def _check_definition(session: Session, workspace_id: str, config: dict) -> None:
    """A schedule may only name a runnable definition (published; a draft only in a dev workspace), and
    only deliver to destinations of its workspace that take reports (authorized or pending; N-3)."""
    if config.get("definition") is not None:
        from analystos.services.definitions import resolve_runnable

        resolve_runnable(session, workspace_id, config["definition"], trigger="schedule")
    if config.get("deliver_to"):
        from analystos.services.deliveries import check_targets

        check_targets(session, workspace_id, config["deliver_to"], "report")


def _pin_baseline(session: Session, sch: Schedule) -> None:
    """A re-analysis schedule created from a completed baseline run pins that run's versions now."""
    from analystos.services import pins

    baseline = sch.config.get("baseline_run_id")
    if sch.kind != "reanalysis" or not baseline or sch.pins:
        return
    run = session.get(AnalysisRun, baseline)
    if run is None or run.workspace_id != sch.workspace_id:
        raise InvalidInput("config.baseline_run_id is not a run of this workspace")
    if run.status == "COMPLETED":
        sch.pins = pins.capture(session, run, revision=1)
        pins.refresh(session, sch)


def create_schedule(session: Session, user: User, workspace_id: str, *, name: str, kind: str, cron: str, timezone: str = "UTC",
                    config: dict | None = None) -> Schedule:
    require_role(session, user, workspace_id, "editor")
    config = config or {}
    validate(kind, cron, timezone, config)
    _check_definition(session, workspace_id, config)
    if kind == "saved_analysis":
        from analystos.services.saved_analysis import verify

        verify(session, user, workspace_id, name, cron, timezone, config)
    if kind == "step":
        from analystos.services.step_pins import verify_schedule

        verify_schedule(session, user, workspace_id, name, cron, timezone, config)
    sch = Schedule(id=new_id("sch"), workspace_id=workspace_id, name=name, kind=kind, cron=cron, timezone=timezone, config=config,
                   owner_id=user.id, enabled=True, next_run_at=next_fire(cron, timezone), revision=1, pins={}, pin_status={})
    session.add(sch)
    _pin_baseline(session, sch)
    audit(f"user:{user.id}", "schedule.created", workspace_id=workspace_id, target=sch.id,
          details={"kind": kind, "cron": cron, "timezone": timezone, "pin_revision": (sch.pins or {}).get("revision")},
          session=session)
    return sch


def update_schedule(session: Session, user: User, schedule_id: str, patch: dict, *, expected_revision: int | None = None) -> Schedule:
    """Edits never change the pins (only an accepted upgrade does). `expected_revision` is the
    client's If-Match: a stale one is 412, so two editors cannot silently overwrite each other."""
    from analystos.core.errors import PreconditionFailed

    sch = session.get(Schedule, schedule_id, with_for_update=expected_revision is not None)
    if sch is None:
        raise NotFound("schedule not found")
    require_role(session, user, sch.workspace_id, "editor")
    if expected_revision is not None and expected_revision != sch.revision:
        raise PreconditionFailed(f"schedule {sch.id} is at revision {sch.revision}, not {expected_revision}",
                                 details={"current_revision": sch.revision})
    for key in ("name", "cron", "timezone", "config", "enabled"):
        if patch.get(key) is not None:
            setattr(sch, key, patch[key])
    validate(sch.kind, sch.cron, sch.timezone, sch.config)
    if patch.get("config") is not None:
        _check_definition(session, sch.workspace_id, sch.config)
    if sch.kind == "saved_analysis" and (patch.get("enabled") is not False or set(patch) != {"enabled"}):
        from analystos.services.saved_analysis import verify

        verify(session, session.get(User, sch.owner_id), sch.workspace_id, sch.name, sch.cron, sch.timezone, sch.config)
    if sch.kind == "step" and (patch.get("enabled") is not False or set(patch) != {"enabled"}):
        from analystos.services.step_pins import verify_schedule

        verify_schedule(session, session.get(User, sch.owner_id), sch.workspace_id, sch.name, sch.cron, sch.timezone, sch.config)
    sch.next_run_at = next_fire(sch.cron, sch.timezone) if sch.enabled else None
    sch.revision = (sch.revision or 1) + 1
    audit(f"user:{user.id}", "schedule.updated", workspace_id=sch.workspace_id, target=sch.id, details=patch, session=session)
    return sch


# ------------------------------------------------------------------------------------ firing
def _claim(session: Session, sch: Schedule, scheduled_for: datetime, trigger: str) -> str | None:
    fire_key = f"{sch.id}:{trigger}:{scheduled_for.isoformat()}"
    run_id = new_id("srun")
    inserted = session.execute(insert(ScheduleRun).values(
        id=run_id, schedule_id=sch.id, workspace_id=sch.workspace_id, fire_key=fire_key, scheduled_for=scheduled_for,
        trigger=trigger, status="started", result={}).on_conflict_do_nothing(index_elements=["fire_key"]).returning(ScheduleRun.id)).scalar()
    return inserted


@platform_loop
def claim_due(now: datetime | None = None, limit: int = 10) -> list[str]:
    now = now or utcnow()
    claimed = []
    with session_scope() as s:
        due = list(s.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                             .order_by(Schedule.next_run_at).limit(limit).with_for_update(skip_locked=True)))
        for sch in due:
            scheduled_for = sch.next_run_at
            sch.next_run_at = next_fire(sch.cron, sch.timezone, max(now, scheduled_for))
            sch.last_run_at = now
            srun = _claim(s, sch, scheduled_for, "cron")
            if srun:
                claimed.append(srun)
    return claimed


def run_now(user: User, schedule_id: str) -> str:
    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        if sch is None:
            raise NotFound("schedule not found")
        require_role(s, user, sch.workspace_id, KINDS[sch.kind])
        srun = _claim(s, sch, utcnow(), "manual")
    execute(srun)
    return srun


def _finish(srun_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        srun = s.get(ScheduleRun, srun_id)
        if srun is None or srun.status in ("succeeded", "failed", "skipped"):
            return
        srun.status, srun.result, srun.error = status, {**(srun.result or {}), **(result or {})}, error
        srun.finished_at = utcnow()
        sch = s.get(Schedule, srun.schedule_id)
        emit(srun.workspace_id, "schedule.executed", {"schedule": srun.schedule_id, "schedule_run": srun.id, "status": status,
                                                      "kind": sch.kind if sch else None, "error": error}, session=s)
        audit(f"schedule:{srun.schedule_id}", "schedule.executed", workspace_id=srun.workspace_id, target=srun.id,
              decision="allow" if status == "succeeded" else None, details={"status": status, "error": error}, session=s)
        if status == "failed" and sch:
            notify(s, srun.workspace_id, kind="schedule", title=f"Schedule failed: {sch.name}", body=error or "",
                   link={"type": "schedule", "id": sch.id}, user_id=sch.owner_id)
        report_id = (result or {}).get("report_artifact_id")
        if status == "succeeded" and sch and report_id and (sch.config or {}).get("deliver_to"):  # N-3
            queued = deliver(s, srun.workspace_id, sch.config["deliver_to"], subject_type="report", subject_id=report_id,
                             origin={"schedule_id": sch.id, "schedule_run_id": srun.id})
            srun.result = {**(srun.result or {}), "deliveries": queued}


@owned_by("schedule_run", "srun_id")
def execute(srun_id: str) -> None:
    """Run one claimed firing. Never raises: failures are recorded on the schedule_run."""
    with session_scope() as s:
        srun = s.get(ScheduleRun, srun_id)
        sch = s.get(Schedule, srun.schedule_id)
        owner = s.get(User, sch.owner_id)
        kind, config, workspace_id, schedule_id = sch.kind, dict(sch.config), sch.workspace_id, sch.id
        try:
            if owner is None or not owner.active:
                raise NotFound("schedule owner is inactive")
            require_role(s, owner, workspace_id, KINDS[kind])  # permissions as of now
        except AnalystOSError as exc:
            s.expunge_all()
            _finish(srun_id, "failed", error=f"authorization: {exc.message}")
            return
        blocked, pin_info = None, None
        if kind in pins_svc.PINNED_KINDS and sch.pins:
            st = pins_svc.refresh(s, sch)  # decided in code at every fire, not only when something was published
            if st.state == "blocked":
                blocked = "; ".join(st.blocking)
            else:
                pin_info = {"revision": st.revision, "state": st.state, "warnings": st.warnings,
                            "upgrade_available": st.state == "upgrade_available"}
        if blocked is None:
            srun.status = "running"
        s.flush()
        s.expunge_all()
    if blocked is not None:  # a retired or rejected pin: nothing runs until the owner upgrades (notified by refresh)
        _finish(srun_id, "skipped", {"blocked": True}, error=f"blocked by a retired or rejected pinned version: {blocked}")
        return
    try:
        from analystos.services.saved_analysis import execute as saved_analysis
        from analystos.services.step_pins import execute_schedule as step_pin

        result = {"dataset_refresh": _refresh, "reanalysis": _reanalysis, "report": _report, "monitor": _monitors,
                  "crawl": _crawl, "saved_analysis": saved_analysis, "step": step_pin,
                  "pipeline": _pipeline}[kind](
            owner, workspace_id, schedule_id, srun_id, config)
    except AnalystOSError as exc:
        _finish(srun_id, "failed", error=f"{exc.code}: {exc.message}")
        return
    except Exception as exc:  # recorded, never propagated into the scheduler loop
        log.exception("schedule %s failed", schedule_id)
        _finish(srun_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    if pin_info is not None:
        result["pins"] = pin_info  # pinned versions ran; an available upgrade or a deprecation is reported, not applied
    if result.pop("_pending", False):  # re-analysis: the run's finalize step completes the schedule_run
        with session_scope() as s:
            s.get(ScheduleRun, srun_id).result = result
        return
    _finish(srun_id, "succeeded", result)


def _refresh(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.sources import select_assets

    with session_scope() as s:
        # recipe outputs (kind `recipe`) are written by recipe runs, not extracted
        sources = list(s.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.execution_mode == "staged",
                                                      Source.status == "ready", Source.kind != "recipe")))
        if config.get("source_ids"):
            sources = [x for x in sources if x.id in config["source_ids"]]
        plan = {x.id: [a.name for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == x.id, SourceAsset.selected.is_(True)))]
                for x in sources}
    loaded = {}
    for source_id, assets in plan.items():
        if assets:
            loaded[source_id] = select_assets(owner, source_id, assets)["loaded"]
    return {"refreshed": loaded}


def _pipeline(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    """The published version of a pipeline (by name): `action: run` (default) runs it into the managed output,
    incremental by watermark when declared, so an overlapping or retried firing merges the same keys again
    instead of double counting; `action: dry_run` dry-runs it and, with a destination, requests the approval
    a materialization needs (a schedule never writes a destination by itself)."""
    from analystos.db.models import Pipeline
    from analystos.services import pipelines

    name = config.get("pipeline")
    with session_scope() as s:
        row = s.scalar(select(Pipeline).where(Pipeline.workspace_id == workspace_id, Pipeline.name == name,
                                              Pipeline.status == "published").order_by(Pipeline.version.desc()).limit(1))
        if row is None:
            raise NotFound(f"no published pipeline named {name!r}")
        pipeline_id = row.id
    if config.get("action", "run") == "dry_run":
        out = pipelines.dry_run(owner, pipeline_id, workspace_id)
    else:
        out = pipelines.run_pipeline(owner, pipeline_id, workspace_id, mode=config.get("mode", "auto"))
    return {"pipeline_run": out["id"], "status": out["status"], "approval_id": out.get("approval_id")}


def _crawl(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    """Crawl every registered source of the workspace (or config.source_ids); incremental by default."""
    from analystos.services.crawler import crawl_source

    with session_scope() as s:
        stmt = select(Source.id).where(Source.workspace_id == workspace_id, Source.status.in_(("discovered", "ready")),
                                       Source.kind != "recipe")
        if config.get("source_ids"):
            stmt = stmt.where(Source.id.in_(config["source_ids"]))
        ids = list(s.scalars(stmt))
    out: dict[str, Any] = {}
    for sid in ids:
        try:
            res = crawl_source(owner, sid, mode=config.get("mode"), include=config.get("include"), exclude=config.get("exclude"),
                               trigger="schedule", actor=f"schedule:{schedule_id}")
            out[sid] = {"crawl_id": res["crawl_id"], **{k: res["stats"].get(k) for k in ("new", "changed", "deprecated", "profiled")}}
        except AnalystOSError as exc:
            out[sid] = {"error": exc.message}
    if ids and all("error" in v for v in out.values()):
        raise InvalidInput("every crawl failed: " + "; ".join(v["error"] for v in out.values())[:500])
    return {"crawls": out}


def _previous_run(workspace_id: str, schedule_id: str) -> str | None:
    """The run this fire's deltas compare against: the last completed fire of the same pin revision, else
    that revision's baseline. After an accepted upgrade the first fire is the new baseline (None)."""
    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        pins = sch.pins or {}
        revision = pins.get("revision")
        for srun in s.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id, ScheduleRun.status == "succeeded")
                              .order_by(ScheduleRun.started_at.desc()).limit(10)):
            if revision is not None and (srun.result or {}).get("pin_revision") != revision:
                continue
            rid = (srun.result or {}).get("run_id")
            if rid and (r := s.get(AnalysisRun, rid)) and r.status == "COMPLETED":
                return rid
        if pins:
            return pins.get("baseline_run_id")
        if (cfg_prev := sch.config.get("baseline_run_id")):
            return cfg_prev
    return None


def _reanalysis(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    """A pinned schedule (ADR-0021) replays its frozen set: the baseline's manifests, metric and method
    versions and AnalysisSpecs, with no model call; the objective is never sent back to a planner. An
    unpinned one (no completed baseline yet) runs the named definition, and its first completed run
    becomes the baseline its pins are taken from."""
    from analystos.services.runs import create_run

    refreshed = _refresh(owner, workspace_id, schedule_id, srun_id, config) if config.get("refresh_first", True) else {}
    from analystos.registries.replay import novelty_config

    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        pinned = pins_svc.for_run(sch) if sch.pins else None
    previous = _previous_run(workspace_id, schedule_id)
    # Replay (default): the run re-tests the hypothesis registry with no model call; novelty is opt-in (P4-T05).
    run = create_run(owner, workspace_id, objective=config.get("objective"), source_ids=config.get("source_ids"),
                     definition=None if pinned else config.get("definition"), pins=pinned,
                     origin={"type": "schedule", "schedule_id": schedule_id, "schedule_run_id": srun_id,
                             "previous_run_id": previous, "publish": config.get("publish", "skip"),
                             "replay": bool(pinned) or bool(config.get("replay", True)),
                             "novelty": novelty_config(config.get("novelty")),
                             "registry_scope": config.get("registry_scope", "previous_run"),
                             "pin_revision": (pinned or {}).get("revision"),
                             "report": config.get("report", {"kind": "weekly_summary", "formats": ["html", "pdf", "xlsx"]})})
    return {"_pending": True, "run_id": run.id, "previous_run_id": previous, "refreshed": refreshed,
            "pin_revision": (pinned or {}).get("revision")}


def _report(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.reports import generate_report

    with session_scope() as s:
        run_id = config.get("run_id") or s.scalar(select(AnalysisRun.id).where(
            AnalysisRun.workspace_id == workspace_id, AnalysisRun.status == "COMPLETED").order_by(AnalysisRun.finished_at.desc()))
        if not run_id:
            raise InvalidInput("no completed run to report on")
        art = generate_report(s, run_id, kind=config.get("kind", "executive"), formats=tuple(config.get("formats", ["html", "pdf"])),
                              actor=f"schedule:{schedule_id}")
        return {"run_id": run_id, "report_artifact_id": art.id}


def _monitors(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.monitors import evaluate_monitor

    with session_scope() as s:
        stmt = select(Monitor.id).where(Monitor.workspace_id == workspace_id, Monitor.enabled.is_(True))
        if config.get("monitor_ids"):
            stmt = stmt.where(Monitor.id.in_(config["monitor_ids"]))
        ids = list(s.scalars(stmt))
    results = {}
    for mid in ids:
        try:
            r = evaluate_monitor(mid, trigger=f"schedule:{schedule_id}")
            results[mid] = {"alert": bool(r.get("alert")), "alert_id": r.get("alert_id"), "message": r.get("message")}
        except AnalystOSError as exc:
            results[mid] = {"error": exc.message}
    return {"monitors": results, "alerts": sum(1 for r in results.values() if r.get("alert"))}


@owned_by("analysis_run", "run_id")
def complete_from_run(run_id: str) -> None:
    """Called when a scheduled run reaches a terminal state."""
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        origin = run.origin or {}
        srun_id = origin.get("schedule_run_id")
        status, summary = run.status, dict(run.summary or {})
    if not srun_id:
        return
    if status == "COMPLETED":
        changes = summary.get("changes") or {}
        verdict = {"nothing_changed": changes.get("nothing_changed"), "summary": changes.get("summary")} if changes else \
            {"nothing_changed": None, "summary": "First run of this pinned set: it is the baseline later fires compare with."}
        with session_scope() as s:
            baseline = _record_baseline(s, origin, run_id)
            stale = _mark_superseded(s, origin.get("previous_run_id"), run_id)
        _finish(srun_id, "succeeded", {"run_id": run_id, "report_artifact_id": summary.get("report_artifact_id"),
                                       **({"report_error": summary["report_error"]} if summary.get("report_error") else {}),
                                       "changes": {k: len(changes.get(k, [])) for k in ("new", "persisting", "changed", "resolved", "new_questions")},
                                       **verdict, "baseline": baseline, "stale_narratives": stale})
    else:
        _finish(srun_id, "failed", {"run_id": run_id}, error=f"run {status}: {summary.get('error') or ''}".strip())


def _record_baseline(session: Session, origin: dict, run_id: str) -> bool:
    """An unpinned re-analysis schedule pins the first run that completes; the first fire after an
    accepted upgrade becomes that revision's baseline. True when this run is a baseline."""
    sch = session.get(Schedule, origin.get("schedule_id"), with_for_update=True) if origin.get("schedule_id") else None
    if sch is None or sch.kind != "reanalysis":
        return False
    if not sch.pins:
        sch.pins = pins_svc.capture(session, session.get(AnalysisRun, run_id), revision=1)
        pins_svc.refresh(session, sch)
        return True
    if origin.get("pin_revision") == sch.pins.get("revision") and not sch.pins.get("baseline_run_id"):
        sch.pins = {**sch.pins, "baseline_run_id": run_id}
        return True
    return False


def _mark_superseded(session: Session, previous_run_id: str | None, run_id: str) -> list[str]:
    """A narrative written for an earlier fire is stale once a newer fire has its own,
    regenerated from the new bound facts; it is marked, never rewritten."""
    from analystos.artifacts.registry import link
    from analystos.db.models import Artifact

    if not previous_run_id:
        return []
    marked = []
    for art in session.scalars(select(Artifact).where(Artifact.run_id == previous_run_id, Artifact.type == "narrative",
                                                      Artifact.status == "final")):
        art.status = "stale"
        link(session, art.workspace_id, (art.type, art.id), "superseded_by", ("run", run_id), run_id=run_id)
        marked.append(art.id)
    if marked:
        run = session.get(AnalysisRun, run_id)
        emit(run.workspace_id, "narrative.stale", {"artifacts": marked, "previous_run_id": previous_run_id,
                                                   "superseded_by": run_id}, run_id=run_id, session=session)
    return marked


DEMO_PREFIX = "[demo] "  # name prefix of schedules/monitors an evidence or demo script creates


def is_demo(name: str, config: dict | None) -> bool:
    """Demo items are marked by the scripts that create them: `config.demo = true` and a "[demo] " name."""
    return bool((config or {}).get("demo")) or (name or "").startswith(DEMO_PREFIX.strip())


def disable_demo(session: Session, *, include_all: bool = False, workspace_id: str | None = None,
                 dry_run: bool = False, actor: str = "cli") -> dict[str, Any]:
    """Disable the schedules and monitors demo/evidence scripts created (or, with `include_all`, every
    enabled one), so a dev stack stops spending model credit on them. Disabled, not deleted: history,
    alerts and series stay; re-enable in the UI. Audited."""
    out: dict[str, list[dict[str, str]]] = {"schedules": [], "monitors": []}
    for model, key in ((Schedule, "schedules"), (Monitor, "monitors")):
        stmt = select(model).where(model.enabled.is_(True))
        if workspace_id:
            stmt = stmt.where(model.workspace_id == workspace_id)
        for item in session.scalars(stmt):
            if include_all or is_demo(item.name, item.config):
                out[key].append({"id": item.id, "workspace_id": item.workspace_id, "name": item.name})
                if not dry_run:
                    item.enabled = False
                    if isinstance(item, Schedule):
                        item.next_run_at = None
    if not dry_run and (out["schedules"] or out["monitors"]):
        audit(actor, "schedules.demo_disabled", target=workspace_id or "all",
              details={"include_all": include_all, "schedules": [x["id"] for x in out["schedules"]],
                       "monitors": [x["id"] for x in out["monitors"]]}, session=session)
    return {"dry_run": dry_run, "include_all": include_all, **out}


@platform_loop
def housekeeping() -> dict[str, Any]:
    """Every scheduler iteration (P4-06): relay pending run dispatches, give orphaned NEW runs an outbox
    row, and drop idempotency records past retention. Failures are logged, never fatal to the loop."""
    from analystos.services import dispatch, idempotency

    out: dict[str, Any] = {}
    try:
        out["dispatch"] = dispatch.reconcile()
        with session_scope() as s:
            out["idempotency_purged"] = idempotency.purge_expired(s)
    except Exception:
        log.exception("scheduler housekeeping failed")
    from analystos.services.stale_work import sweep_quietly

    out["stale_work"] = sweep_quietly()  # Ask turns / ML experiments a dead process left running
    try:  # S01-T5: seal recent audit rows into each tenant's hash chain
        from analystos.governance.audit_chain import seal_all

        out["audit_sealed"] = seal_all()
    except Exception:
        log.exception("audit sealing failed")
    try:  # P6-03 operational alert: a destination whose good version is older than its pipeline's freshness
        from analystos.services.pipelines import check_freshness

        out["freshness_alerts"] = len(check_freshness())
    except Exception:
        log.exception("pipeline freshness check failed")
    from analystos.conversations import retention, summaries

    out["conversation_summaries"] = len(summaries.summarize_idle_quietly())  # S04: idle 30 min -> summary
    out["conversation_retention"] = retention.apply_retention_quietly()  # S04: at most hourly; skips legal holds
    try:  # S02-T3: result handles past their TTL (files deleted, rows marked purged)
        from analystos.gateway.handles import purge_expired_results

        out["results_purged"] = purge_expired_results()
    except Exception:
        log.exception("result handle purge failed")
    try:  # S13: Request Desk queue items past their SLA, flagged once each (as each workspace's tenant)
        from analystos.desk.rollup import sweep_sla_all

        out["desk_sla_breaches"] = sweep_sla_all()
    except Exception:
        log.exception("desk SLA sweep failed")
    try:  # N-3: approved external deliveries that are due (bounded retries; never fatal to the loop)
        from analystos.services.deliveries import process_due

        out["deliveries"] = process_due()
    except Exception:
        log.exception("external delivery failed")
    return out


@platform_loop
def nightly_calibration() -> None:
    """Decision calibration (P4-T09) once a day, whichever scheduler process gets the advisory lock."""
    from analystos.decisions.calibration import maybe_run_nightly

    try:
        with session_scope() as s:
            maybe_run_nightly(s)
    except Exception:
        log.exception("decision calibration failed")


@platform_loop
def nightly_verification_sweep() -> None:
    """P7-01 sweep once a day: re-check every live verdict's dependencies; late voids are logged defects."""
    from analystos.evidence.verification import maybe_sweep_nightly

    try:
        with session_scope() as s:
            maybe_sweep_nightly(s)
    except Exception:
        log.exception("verification sweep failed")


@platform_loop
def nightly_audit_chain() -> None:
    """S01-T5 once a day: seal every audit row, verify every tenant's hash chain, export new links."""
    from analystos.governance.audit_chain import maybe_run_nightly

    try:
        maybe_run_nightly()
    except Exception:
        log.exception("audit chain nightly failed")


@platform_loop
def nightly_desk_value() -> None:
    """S13 once a day: roll the Request Desk value metrics into desk_daily, per workspace as its tenant."""
    from analystos.desk.rollup import maybe_rollup_nightly

    try:
        maybe_rollup_nightly()
    except Exception:
        log.exception("desk value rollup failed")


@platform_loop
def run_scheduler(poll_seconds: float = 15.0, *, once: bool = False, stop: threading.Event | None = None,
                  configure: bool = True) -> None:
    """The schedule/monitor loop: `analystos scheduler`, or a thread of the API process in the lite
    profile (`start_inprocess_scheduler`). Claim-then-execute keeps several loops from double firing."""
    from analystos.core.logging import configure_logging

    if configure:
        configure_logging()
    log.info("scheduler started (poll %ss)", poll_seconds)
    while True:
        try:
            for srun in claim_due():
                execute(srun)
        except Exception:
            log.exception("scheduler iteration failed")
        housekeeping()
        nightly_calibration()
        nightly_verification_sweep()
        nightly_audit_chain()
        nightly_desk_value()
        if once:
            return
        if stop is None:
            time.sleep(poll_seconds)
        elif stop.wait(poll_seconds):
            return


def start_inprocess_scheduler(poll_seconds: float = 15.0) -> threading.Event:
    """Run the scheduler in a daemon thread of this process (lite, ADR-0025); set the event to stop it."""
    stop = threading.Event()
    threading.Thread(target=run_scheduler, args=(poll_seconds,), kwargs={"stop": stop, "configure": False},
                     daemon=True, name="inprocess-scheduler").start()
    return stop

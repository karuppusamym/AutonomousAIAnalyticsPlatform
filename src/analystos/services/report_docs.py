"""Report documents shaped by conversation (CONV-04): persistence, the content catalog, exports, schedules.

A report document is an `artifact` of type `report_document` (content `ReportDoc`). Each applied op batch is
`ArtifactVersion + 1` with the ops as its `diff`; undo is a revert (a new version). Editing a `final`
document (one that was exported) forks a draft, so what was handed out never changes underneath; editing
one with a pending export returns it to draft and invalidates that approval.

Blocks are filled only from verified content the actor may read (`resolve`): a builder dashboard's chart
preview or KPI, a run's verified (not void) finding, a run's chart or validated metric, or one of the actor's
own answered Ask questions. Nothing here runs a query.

Exports are side effects bound to an approval of {artifact, version, content hash, snapshot hash, format,
requester}; `verify_for_execution` re-derives that payload from the stored artifact immediately before the
file is rendered, and the approval is single use. A document pinned to a schedule is refreshed on each fire,
rendered as a final `report` snapshot and handed to the existing delivery path (approved destinations only).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.dashboard_dsl import SCHEMA_VERSION as DASHBOARD_SCHEMA
from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.contracts.report_doc import (
    ARTIFACT_TYPE,
    SCHEMA_VERSION,
    ContentRef,
    ReportDoc,
    Section,
    TextBlock,
    derive_sensitivity,
)
from analystos.core.errors import AnalystOSError, ApprovalRequired, Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, ordered_utcnow, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.models import Approval, Artifact, ArtifactVersion, Schedule, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.reports.design import CatalogItem
from analystos.reports.document import (
    EXPORT_FORMATS,
    OpResult,
    ReportEnv,
    Unresolvable,
    apply_ops,
    parse_ops,
    to_report_data,
)

log = get_logger(__name__)
EXPORT_ACTION = "report_document.export"
STATUSES = ("draft", "approval_required", "final")
MAX_CATALOG = 120


def is_document(art: Artifact) -> bool:
    return art.type == ARTIFACT_TYPE and (art.content or {}).get("schema_version") == SCHEMA_VERSION


def document(art: Artifact) -> ReportDoc:
    return ReportDoc.model_validate(art.content)


@scoped_loader
def load(session: Session, user: User, doc_id: str, *, minimum: str = "viewer", for_update: bool = False) -> Artifact:
    art = load_in_workspace(session, Artifact, doc_id, user=user, minimum=minimum, label="report", for_update=for_update)
    if not is_document(art):
        raise InvalidInput(f"artifact {doc_id} is not a report document")
    return art


def view(art: Artifact) -> dict[str, Any]:
    return {"id": art.id, "workspace_id": art.workspace_id, "name": art.name, "version": art.version, "status": art.status,
            "content_hash": art.content_hash, "document": art.content,
            "updated_at": art.updated_at.isoformat() if art.updated_at else None}


def _event(session: Session, art: Artifact, kind: str, payload: dict[str, Any], actor: str) -> None:
    from analystos.events.bus import emit

    emit(art.workspace_id, kind, {"artifact_id": art.id, "version": art.version, **payload}, actor=actor, session=session)


def list_documents(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    rows = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == ARTIFACT_TYPE)
                           .order_by(Artifact.updated_at.desc()).limit(200))
    return [{k: v for k, v in view(a).items() if k != "document"} | {"title": (a.content or {}).get("title"),
                                                                       "schedule_id": (a.content or {}).get("schedule_id")}
            for a in rows if is_document(a)]


def create(session: Session, user: User, workspace_id: str, *, title: str, objective: str = "",
           doc: ReportDoc | None = None, origin: dict[str, Any] | None = None) -> Artifact:
    """Version 1: the given document, or an empty one with a Summary section."""
    from analystos.governance.audit import audit
    from analystos.reports.document import slug

    require_role(session, user, workspace_id, "editor")
    title = (title or "").strip()
    if doc is None:
        if not title:
            raise InvalidInput("give the report a title")
        doc = ReportDoc(id=slug(title, "r")[:60], title=title[:300], objective=objective[:2000],
                        sections=[Section(id="summary", title="Summary", blocks=[TextBlock(id="t_summary")])],
                        origin=origin or {"type": "conversation"})
    doc = derive_sensitivity(doc)
    content = doc.model_dump(mode="json")
    art = Artifact(id=new_id("art"), workspace_id=workspace_id, run_id=None, type=ARTIFACT_TYPE, name=f"report:{doc.id}",
                   version=1, status="draft", creator_user=user.id, content=content, content_hash=stable_hash(content),
                   created_at=ordered_utcnow())
    session.add(art)
    session.flush()
    session.add(ArtifactVersion(artifact_id=art.id, version=1, content=content, content_hash=art.content_hash,
                                created_by=f"user:{user.id}", diff=[{"op": "create", **({"origin": doc.origin} if doc.origin else {})}]))
    _event(session, art, "report_document.created", {"title": doc.title}, f"user:{user.id}")
    audit(f"user:{user.id}", "report_document.created", workspace_id=workspace_id, target=art.id, session=session)
    return art


# ------------------------------------------------------------------------------------------- content catalog
def _dashboard_label(doc: DashboardDoc, version: int, what: str, title: str) -> str:
    return f"Dashboard “{doc.title}” v{version}, {what} “{title}”"


def _builder_dashboards(session: Session, workspace_id: str) -> list[Artifact]:
    rows = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "dashboard",
                                                  Artifact.run_id.is_(None)).order_by(Artifact.updated_at.desc()).limit(50))
    return [a for a in rows if (a.content or {}).get("schema_version") == DASHBOARD_SCHEMA]


def catalog(session: Session, user: User, workspace_id: str) -> list[CatalogItem]:
    """The verified content this user may put in a report in this workspace, newest first."""
    from analystos.db.models import AskThread, AskTurn, Insight

    require_role(session, user, workspace_id, "viewer")
    out: list[CatalogItem] = []
    for art in _builder_dashboards(session, workspace_id):
        try:
            doc = DashboardDoc.model_validate(art.content)
        except ValueError:
            continue
        for c in doc.charts:
            if (c.preview or {}).get("rows"):
                out.append(CatalogItem(ref={"kind": "dashboard_chart", "artifact_id": art.id, "element_id": c.id},
                                       title=c.title, kind="chart", where=f"dashboard {doc.title}"))
        for k in doc.kpis:
            if k.value is not None:
                out.append(CatalogItem(ref={"kind": "dashboard_kpi", "artifact_id": art.id, "element_id": k.id},
                                       title=k.title, kind="kpi", where=f"dashboard {doc.title}"))
    for ins in session.scalars(select(Insight).where(Insight.workspace_id == workspace_id, Insight.status == "verified",
                                                     Insight.verified.is_(True)).order_by(Insight.created_at.desc()).limit(40)):
        out.append(CatalogItem(ref={"kind": "finding", "run_id": ins.run_id, "code": ins.code}, title=ins.title,
                               kind="finding", where=f"investigation {ins.run_id}"))
    for a in session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "chart",
                                                    Artifact.run_id.is_not(None)).order_by(Artifact.created_at.desc()).limit(40)):
        if ((a.content or {}).get("preview") or {}).get("rows"):
            out.append(CatalogItem(ref={"kind": "run_chart", "artifact_id": a.id}, title=str(a.content.get("title") or a.name),
                                   kind="chart", where=f"investigation {a.run_id}"))
    for a in session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "metric",
                                                    Artifact.run_id.is_not(None)).order_by(Artifact.created_at.desc()).limit(40)):
        if ((a.content or {}).get("validation") or {}).get("value") is not None:
            out.append(CatalogItem(ref={"kind": "run_metric", "artifact_id": a.id},
                                   title=str(a.content.get("display_name") or a.name), kind="kpi", where=f"investigation {a.run_id}"))
    turns = session.scalars(select(AskTurn).join(AskThread, AskThread.id == AskTurn.thread_id).where(
        AskTurn.workspace_id == workspace_id, AskThread.user_id == user.id, AskTurn.status == "answered")
        .order_by(AskTurn.created_at.desc()).limit(30))
    for t in turns:
        if ((t.result or {}).get("columns")):
            out.append(CatalogItem(ref={"kind": "ask_answer", "turn_id": t.id}, title=t.question[:300], kind="chart",
                                   where="your Ask answers"))
    return out[:MAX_CATALOG]


def _bounded_rows(rows: Any, width: int) -> list[list[Any]]:
    from analystos.contracts.dashboard_dsl import MAX_PREVIEW_ROWS

    out = []
    for r in list(rows or [])[:MAX_PREVIEW_ROWS]:
        if isinstance(r, list | tuple) and len(r) == width:
            out.append([v if v is None or isinstance(v, bool | int | float | str) else str(v) for v in r])
    return out


def resolver(session: Session, user: User, workspace_id: str):  # noqa: ANN201 - a closure the engine calls
    """ref -> block content, read now with the user's rights; Unresolvable says why not (shown to the person)."""
    from analystos.dashboards import service as dashboards
    from analystos.db.models import AnalysisRun, Insight
    from analystos.evidence.verification import insight_states, void_cause

    def chart_content(title: str, chart_type: str, columns: list[Any], rows: Any, source: dict[str, Any],
                      sensitivity: str = "internal") -> dict[str, Any]:
        cols = [str(c) for c in columns or []]
        bounded = _bounded_rows(rows, len(cols))
        if not cols or not bounded:
            raise Unresolvable(f"“{title}” has no data to show yet")
        source["content_hash"] = stable_hash({"title": title, "columns": cols, "rows": bounded})
        return {"kind": "chart", "title": title[:300], "chart_type": chart_type, "columns": cols, "rows": bounded,
                "sensitivity": sensitivity, "source": source}

    def resolve(ref: ContentRef) -> dict[str, Any]:
        try:
            if ref.kind in ("dashboard_chart", "dashboard_kpi"):
                art = dashboards.load(session, user, ref.artifact_id or "")
                if art.workspace_id != workspace_id:
                    raise NotFound("dashboard not found")
                doc = dashboards.document(art)
                if ref.kind == "dashboard_chart":
                    chart = doc.chart(ref.element_id or "")
                    if chart is None:
                        raise Unresolvable(f"dashboard “{doc.title}” has no chart {ref.element_id!r}")
                    return chart_content(chart.title, chart.type, (chart.preview or {}).get("columns") or [],
                                         (chart.preview or {}).get("rows"),
                                         {"label": _dashboard_label(doc, art.version, "chart", chart.title),
                                          "assets": list(chart.provenance.sources)[:50], "version": art.version,
                                          "labels": list(chart.labels)}, chart.sensitivity)
                kpi = next((k for k in doc.kpis if k.id == ref.element_id), None)
                if kpi is None:
                    raise Unresolvable(f"dashboard “{doc.title}” has no KPI {ref.element_id!r}")
                if kpi.value is None:
                    raise Unresolvable(f"the KPI “{kpi.title}” has no value yet")
                return {"kind": "kpi", "title": kpi.title, "value": kpi.value, "unit": kpi.unit, "sensitivity": kpi.sensitivity,
                        "source": {"label": _dashboard_label(doc, art.version, "KPI", kpi.title), "version": art.version,
                                   "assets": list(kpi.provenance.sources)[:50], "labels": list(kpi.labels),
                                   "content_hash": stable_hash({"value": kpi.value, "metric": kpi.metric.label})}}
            if ref.kind == "finding":
                run = load_in_workspace(session, AnalysisRun, ref.run_id, workspace_id, user=user, label="investigation")
                ins = session.scalar(select(Insight).where(Insight.run_id == run.id, Insight.code == ref.code))
                if ins is None or ins.status != "verified" or not ins.verified:
                    raise Unresolvable(f"finding {ref.code} of that investigation is not verified")
                void = void_cause(insight_states(session, [ins.id])[ins.id])
                if void:
                    raise Unresolvable(f"finding {ref.code} needs re-verification ({void}); it cannot go in a report")
                qids = [e["id"] for e in ins.evidence or [] if e.get("type") == "query"][:20]
                return {"kind": "finding", "title": ins.title[:300], "text": ins.finding[:8000], "confidence": ins.confidence,
                        "verified": True, "caveats": list(ins.caveats or [])[:20], "evidence": qids,
                        "source": {"label": f"Investigation “{run.objective[:120]}”, finding {ins.code}",
                                   "content_hash": stable_hash({"finding": ins.finding, "confidence": ins.confidence})}}
            if ref.kind in ("run_chart", "run_metric"):
                art = load_in_workspace(session, Artifact, ref.artifact_id, workspace_id, user=user, label="output")
                c = art.content or {}
                if ref.kind == "run_chart":
                    if art.type != "chart":
                        raise Unresolvable("that output is not a chart")
                    prev = c.get("preview") or {}
                    return chart_content(str(c.get("title") or art.name), str(c.get("chart_type") or "table"),
                                         prev.get("columns") or [], prev.get("rows"),
                                         {"label": f"Investigation {art.run_id}, chart “{c.get('title') or art.name}”",
                                          "version": art.version})
                if art.type != "metric":
                    raise Unresolvable("that output is not a metric")
                value = (c.get("validation") or {}).get("value")
                if value is None or isinstance(value, bool) or not isinstance(value, int | float):
                    raise Unresolvable(f"the metric {art.name} has no validated value")
                fmt = c.get("format") if c.get("format") in ("number", "percent", "hours", "currency") else "number"
                return {"kind": "kpi", "title": str(c.get("display_name") or art.name), "value": value, "format": fmt,
                        "source": {"label": f"Investigation {art.run_id}, metric {art.name}", "version": art.version,
                                   "content_hash": stable_hash({"value": value})}}
            if ref.kind == "ask_answer":
                from analystos.services.ask import _turn_for

                turn = _turn_for(session, user, ref.turn_id or "", workspace_id)
                if turn.status != "answered":
                    raise Unresolvable("that question has no answer to include")
                res = turn.result or {}
                chart_type = str((turn.chart or {}).get("type") or (turn.chart or {}).get("chart_type") or "table")
                assets = (turn.provenance or {}).get("assets") or []
                names = [str(a.get("asset") or a.get("name") or "") if isinstance(a, dict) else str(a)
                         for a in (assets if isinstance(assets, list) else [])]
                return chart_content(turn.question[:300], chart_type, res.get("columns") or [], res.get("rows"),
                                     {"label": f"Ask answer “{turn.question[:120]}”", "assets": [n for n in names if n][:50]})
        except Unresolvable:
            raise
        except (NotFound, Forbidden) as exc:
            raise Unresolvable(f"you cannot use that content: {exc.message}") from None
        raise Unresolvable(f"unknown content kind {ref.kind}")

    return resolve


def _schedule_check(session: Session, user: User, workspace_id: str):  # noqa: ANN201
    def check(schedule_id: str) -> str | None:
        sch = session.get(Schedule, schedule_id)
        if sch is None or sch.workspace_id != workspace_id:
            return "no such schedule in this workspace"
        if not sch.enabled:
            return f"the schedule “{sch.name}” is paused"
        return None

    return check


def env(session: Session, user: User, workspace_id: str) -> ReportEnv:
    return ReportEnv(actor=f"user:{user.id}", resolve=resolver(session, user, workspace_id),
                     schedule_check=_schedule_check(session, user, workspace_id))


def schedules_by_name(session: Session, workspace_id: str) -> dict[str, str]:
    return {s.name: s.id for s in session.scalars(select(Schedule).where(Schedule.workspace_id == workspace_id).limit(200))}


# ------------------------------------------------------------------------------------------- versions
def _new_version(session: Session, art: Artifact, doc: ReportDoc, diff: list[dict[str, Any]], actor: str) -> None:
    content = doc.model_dump(mode="json")
    art.version += 1
    art.content, art.content_hash = content, stable_hash(content)
    session.add(ArtifactVersion(artifact_id=art.id, version=art.version, content=content, content_hash=art.content_hash,
                                created_by=actor, diff=diff))


def _invalidate_pending(session: Session, art: Artifact, reason: str) -> None:
    for a in session.scalars(select(Approval).where(Approval.workspace_id == art.workspace_id, Approval.action == EXPORT_ACTION,
                                                    Approval.status.in_(["pending", "approved"]))):
        if art.id in (a.affected_assets or []):
            a.status, a.reason = "invalidated", reason


def fork(session: Session, user: User, art: Artifact) -> Artifact:
    """A draft copy of a final (exported) report; the final one is untouched."""
    from analystos.artifacts.registry import link

    n = 1 + len(session.scalars(select(Artifact.id).where(Artifact.workspace_id == art.workspace_id,
                                                          Artifact.name.like(f"{art.name}~draft%"))).all())
    content = {**art.content, "schedule_id": None}  # the pin stays with the original
    draft = Artifact(id=new_id("art"), workspace_id=art.workspace_id, run_id=None, type=ARTIFACT_TYPE,
                     name=f"{art.name}~draft{n}", version=1, status="draft", creator_user=user.id, content=content,
                     content_hash=stable_hash(content), created_at=ordered_utcnow())
    session.add(draft)
    session.flush()
    session.add(ArtifactVersion(artifact_id=draft.id, version=1, content=content, content_hash=draft.content_hash,
                                created_by=f"user:{user.id}",
                                diff=[{"op": "fork", "from": {"artifact_id": art.id, "version": art.version}}]))
    link(session, art.workspace_id, (ARTIFACT_TYPE, draft.id), "forked_from", (ARTIFACT_TYPE, art.id))
    _event(session, draft, "report_document.forked", {"from": art.id, "from_version": art.version}, f"user:{user.id}")
    return draft


def _editable(session: Session, user: User, art: Artifact) -> Artifact:
    if art.status == "final":
        return fork(session, user, art)
    if art.status == "approval_required":
        _invalidate_pending(session, art, "report edited after the export request")
    return art


def _check_base(art: Artifact, base_version: int | None) -> None:
    if base_version is not None and base_version != art.version:
        raise Conflict(f"the report is at version {art.version}, not {base_version}; reload and retry",
                       details={"version": art.version})


def apply(session: Session, user: User, doc_id: str, raw_ops: list[dict[str, Any]], *, base_version: int | None = None,
          origin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and apply an op batch; {status: applied|refused|unchanged, report, refused, deferred[, approval]}."""
    from analystos.governance.audit import audit

    try:
        ops = parse_ops(raw_ops)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from None
    art = load(session, user, doc_id, minimum="editor", for_update=True)
    _check_base(art, base_version)
    result: OpResult = apply_ops(document(art), ops, env(session, user, art.workspace_id))
    out: dict[str, Any] = {"refused": [r.as_dict() for r in result.refused],
                           "deferred": [d.model_dump(mode="json") for d in result.deferred]}
    if not result.ok:
        return {**out, "status": "refused", "report": view(art)}
    target = art
    if result.changed:
        target = _editable(session, user, art)
        diff = [{**o, **({"origin": origin} if origin and i == 0 else {})} for i, o in enumerate(result.applied)]
        _new_version(session, target, result.doc, diff, f"user:{user.id}")
        target.status = "draft"
        _event(session, target, "report_document.version_created", {"ops": [o["op"] for o in result.applied]},
               f"user:{user.id}")
        audit(f"user:{user.id}", "report_document.ops_applied", workspace_id=target.workspace_id, target=target.id,
              details={"version": target.version, "ops": [o["op"] for o in result.applied],
                       **({"forked_from": art.id} if target is not art else {})}, session=session)
    session.flush()
    out.update(status="applied" if result.changed else "unchanged", report=view(target),
               **({"forked_from": art.id} if target is not art else {}))
    if result.deferred:
        out["export"] = request_export(session, user, target.id, result.deferred[0].format)
        out["report"] = view(target)
    return out


def revert(session: Session, user: User, doc_id: str, to_version: int, *, base_version: int | None = None) -> dict[str, Any]:
    art = load(session, user, doc_id, minimum="editor", for_update=True)
    _check_base(art, base_version)
    old = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id, ArtifactVersion.version == to_version))
    if old is None:
        raise InvalidInput(f"the report has no version {to_version}")
    if old.content_hash == art.content_hash:
        return {"status": "unchanged", "report": view(art)}
    target = _editable(session, user, art)
    _new_version(session, target, ReportDoc.model_validate(old.content), [{"op": "revert", "to_version": to_version,
                                                                           "from_artifact": art.id}], f"user:{user.id}")
    target.status = "draft"
    _event(session, target, "report_document.version_created", {"ops": ["revert"], "to_version": to_version}, f"user:{user.id}")
    session.flush()
    return {"status": "applied", "report": view(target), **({"forked_from": art.id} if target is not art else {})}


def history(session: Session, user: User, doc_id: str) -> list[dict[str, Any]]:
    art = load(session, user, doc_id)
    rows = session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id)
                           .order_by(ArtifactVersion.version.desc()).limit(200))
    return [{"version": v.version, "diff": v.diff or [], "content_hash": v.content_hash, "created_by": v.created_by,
             "created_at": v.created_at.isoformat() if v.created_at else None} for v in rows]


def version(session: Session, user: User, doc_id: str, n: int) -> dict[str, Any]:
    art = load(session, user, doc_id)
    v = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id, ArtifactVersion.version == n))
    if v is None:
        raise InvalidInput(f"the report has no version {n}")
    return {"version": v.version, "document": v.content, "diff": v.diff or [], "content_hash": v.content_hash}


# ------------------------------------------------------------------------------------------- conversation
def _call_ctx(session: Session, workspace_id: str, task_id: str, prompt_version: str, agent_id: str) -> Any:
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.llm.router import CallContext

    workspace = get_workspace(session, workspace_id)
    policy = load_policy(session, workspace)
    return CallContext.for_policy(policy, workspace_id=workspace_id, task_id=task_id, agent_id=agent_id,
                                  prompt_version=prompt_version, tenant_id=workspace.tenant_id,
                                  policy_version=workspace.policy_version)


def default_router() -> Any:
    from analystos.runtime import context as runtime_context

    return runtime_context.default_services().router


def converse(session: Session, user: User, doc_id: str, utterance: str, *, focus: str | None = None,
             base_version: int | None = None, apply_now: bool = True, router: Any = None) -> dict[str, Any]:
    """One report-builder chat turn: the `report_design` ladder proposes ops, the engine decides."""
    from analystos.reports.design import PROMPT_VERSION, design

    if not utterance.strip():
        raise InvalidInput("say what to change")
    art = load(session, user, doc_id, minimum="editor")
    router = router if router is not None else default_router()
    proposal = design(document(art), utterance[:4000], router=router,
                      call_ctx=_call_ctx(session, art.workspace_id, art.id, PROMPT_VERSION, "report_builder"), focus=focus,
                      catalog=catalog(session, user, art.workspace_id), schedules=schedules_by_name(session, art.workspace_id))
    out: dict[str, Any] = {"proposal": proposal.as_dict()}
    if not proposal.ops or not apply_now:
        return {**out, "status": "proposed" if proposal.ops else "no_ops", "report": view(art), "refused": [], "deferred": []}
    applied = apply(session, user, doc_id, proposal.as_dict()["ops"], base_version=base_version,
                    origin={"utterance": utterance[:500], "answered_by": proposal.answered_by})
    return {**out, **applied}


# ------------------------------------------------------------------------------------------- snapshot, preview, export
def _version_time(session: Session, art: Artifact) -> str:
    """The current version's creation time: the snapshot of one version always carries the same date."""
    v = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id, ArtifactVersion.version == art.version))
    ts = (v.created_at if v is not None and v.created_at else art.created_at) or utcnow()
    return ts.replace(microsecond=0).isoformat()


def snapshot(session: Session, art: Artifact) -> Any:
    from analystos.governance.policy import get_workspace

    ws = get_workspace(session, art.workspace_id)
    return to_report_data(document(art), artifact_id=art.id, version=art.version, generated_at=_version_time(session, art),
                          workspace_name=ws.name)


def snapshot_hash(data: Any) -> str:
    return stable_hash(data.snapshot())


def preview(session: Session, user: User, doc_id: str) -> dict[str, Any]:
    """The in-platform HTML preview of the current version (not an export: no file leaves the platform)."""
    from analystos.reports.narrative import render_html

    art = load(session, user, doc_id)
    data = snapshot(session, art)
    return {"html": render_html(data), "snapshot_hash": snapshot_hash(data), "version": art.version}


def export_payload(art: Artifact, fmt: str, user: User, snap_hash: str) -> dict[str, Any]:
    return {"artifact_id": art.id, "version": art.version, "content_hash": art.content_hash, "snapshot_hash": snap_hash,
            "format": fmt, "requested_by": user.id}


def _check_format(fmt: str) -> None:
    from analystos.reports import format_reason

    if fmt not in EXPORT_FORMATS:
        raise InvalidInput(f"unknown report format {fmt!r}; use {', '.join(EXPORT_FORMATS)}")
    if reason := format_reason(fmt):
        from analystos.core.errors import FeatureUnavailable

        raise FeatureUnavailable(f"{fmt.upper()} reports are unavailable here: {reason}")


def request_export(session: Session, user: User, doc_id: str, fmt: str) -> dict[str, Any]:
    """An export is a side effect: an editor requests it and it waits for an approval bound to this exact version."""
    from analystos.contracts.dashboard_dsl import SENSITIVITY_ORDER
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace

    _check_format(fmt)
    art = load(session, user, doc_id, minimum="editor", for_update=True)
    doc = document(art)
    if not doc.blocks():
        raise InvalidInput("the report is empty; add a section and some content before exporting it")
    data = snapshot(session, art)
    snap = snapshot_hash(data)
    ws = get_workspace(session, art.workspace_id)
    approval = request_approval(session, workspace_id=art.workspace_id, run_id=None, action=EXPORT_ACTION,
                                payload=export_payload(art, fmt, user, snap), plan_hash=None, policy_version=ws.policy_version,
                                requested_by=user.id,
                                risk_tier="high" if SENSITIVITY_ORDER.get(doc.sensitivity, 1) >= 2 else "medium",
                                destination=f"export:{fmt}", affected_assets=[art.id],
                                evidence={"version": art.version, "sensitivity": doc.sensitivity, "snapshot_hash": snap,
                                          "title": doc.title})
    if art.status != "final":
        art.status = "approval_required"
    _event(session, art, "report_document.export_requested", {"approval_id": approval.id, "format": fmt},
           f"user:{user.id}")
    session.flush()
    return {"status": "approval_required", "format": fmt, "snapshot_hash": snap, "sensitivity": doc.sensitivity,
            "approval": {"id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier}}


def export(session: Session, user: User, doc_id: str, fmt: str, *, approval_id: str | None) -> tuple[bytes, str, str]:
    """(file bytes, media type, file name). Re-derives the payload and verifies the approval immediately before rendering."""
    from analystos.governance.approvals import consume, verify_for_execution
    from analystos.governance.audit import audit
    from analystos.reports import render

    _check_format(fmt)
    art = load(session, user, doc_id, minimum="editor", for_update=True)
    if not approval_id:
        raise ApprovalRequired("exporting a report needs an approval; request one first")
    approval = session.get(Approval, approval_id)
    if approval is None or approval.action != EXPORT_ACTION or art.id not in (approval.affected_assets or []) \
            or approval.workspace_id != art.workspace_id:
        raise InvalidInput("this approval is not an export approval for this report")
    if approval.requested_by != user.id:
        raise Forbidden("this export approval was not granted to you")
    data = snapshot(session, art)
    snap = snapshot_hash(data)
    verify_for_execution(session, approval_id, payload=export_payload(art, fmt, user, snap), plan_hash=None)
    consume(session, approval)
    content, mime, ext = render(data, fmt)  # type: ignore[arg-type]
    digest = hashlib.sha256(content).hexdigest()
    art.status = "final"
    audit(f"user:{user.id}", "report_document.exported", workspace_id=art.workspace_id, target=art.id, decision="allow",
          details={"format": fmt, "version": art.version, "approval_id": approval_id, "snapshot_hash": snap,
                   "sha256": digest, "bytes": len(content)}, session=session)
    _event(session, art, "report_document.exported", {"format": fmt, "sha256": digest, "snapshot_hash": snap},
           f"user:{user.id}")
    session.flush()
    return content, mime, f"{document(art).id}-v{art.version}.{ext}"


def render_snapshot(session: Session, art: Artifact, *, actor: str, formats: tuple[str, ...] = ("html", "pdf"),
                    kind: str = "document") -> Artifact:
    """A final `report` artifact rendered from the document's current version: what the delivery path sends."""
    from analystos.artifacts.registry import link

    doc = document(art)
    report = save_rendered(session, art.workspace_id, snapshot(session, art), doc, name=f"{kind}:{art.id}", kind=kind,
                           source={"report_document": art.id, "version": art.version}, formats=formats)
    link(session, art.workspace_id, (ARTIFACT_TYPE, art.id), "rendered_as", ("report", report.id))
    return report


def save_rendered(session: Session, workspace_id: str, data: Any, doc: ReportDoc, *, name: str, kind: str,
                  source: dict[str, Any], formats: tuple[str, ...] = ("html", "pdf")) -> Artifact:
    """Render a snapshot and store it as a final `report` artifact (upserted by name: same content, same artifact)."""
    from analystos.artifacts.registry import save_artifact
    from analystos.core.config import get_settings
    from analystos.reports import render, unavailable_formats

    snap = snapshot_hash(data)
    formats = tuple(f for f in formats if f not in unavailable_formats(formats)) or ("html",)
    directory = Path(get_settings().artifact_dir) / workspace_id / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    stem = name.replace(":", "-")[:80]
    for fmt in formats:
        content, mime, ext = render(data, fmt)  # type: ignore[arg-type]
        path = directory / f"{stem}-{snap[:12]}.{ext}"
        path.write_bytes(content)
        files[fmt] = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "mime": mime,
                      "ext": ext}
    blocks = doc.blocks()
    report = save_artifact(session, workspace_id=workspace_id, run_id=None, type_="report", name=name,
                           content={"kind": kind, "title": doc.title, "report_data_hash": snap, "files": files,
                                    "source": source, "insights": sum(1 for b in blocks if b.kind == "finding"),
                                    "metrics": sum(1 for b in blocks if b.kind == "kpi"), "alerts": 0},
                           creator_user=None, creator_agent="report_builder", status="final")
    session.flush()
    return report


# ------------------------------------------------------------------------------------------- schedules
def pinned_to(session: Session, schedule: Schedule) -> list[Artifact]:
    rows = session.scalars(select(Artifact).where(Artifact.workspace_id == schedule.workspace_id, Artifact.type == ARTIFACT_TYPE)
                           .limit(500))
    return [a for a in rows if is_document(a) and (a.content or {}).get("schedule_id") == schedule.id]


def refresh_document(session: Session, owner: User, art: Artifact, *, actor: str) -> dict[str, Any]:
    """Re-read every block's source with the owner's current rights; a change is a new version (diff `refresh`)."""
    from analystos.governance.audit import audit
    from analystos.reports.document import refresh

    doc = document(art)
    rewrite = None
    if (doc.origin or {}).get("type") == "story" and doc.origin.get("dashboard_id"):
        from analystos.services.stories import story_rewriter

        rewrite = story_rewriter(session, owner, doc.origin["dashboard_id"])
    result = refresh(doc, env(session, owner, art.workspace_id), story_text=rewrite)
    if result.changed or result.doc.model_dump(mode="json") != art.content:
        _new_version(session, art, result.doc, [{"op": "refresh", "changed": result.changed_blocks, "held": result.held,
                                                 "unavailable": result.unavailable}], actor)
        _event(session, art, "report_document.refreshed", {"changed": result.changed_blocks, "held": result.held}, actor)
        audit(actor, "report_document.refreshed", workspace_id=art.workspace_id, target=art.id,
              details={"version": art.version, "changed": result.changed_blocks, "held": result.held,
                       "unavailable": result.unavailable}, session=session)
    return {"version": art.version, "changed": result.changed_blocks, "held": result.held, "unavailable": result.unavailable}


def after_schedule_fire(session: Session, schedule: Schedule, srun: Any) -> dict[str, Any]:
    """A succeeded fire refreshes every report pinned to the schedule, renders a final snapshot of each and queues
    it for the schedule's approved destinations; `config.digest_dashboards` adds a dashboard digest the same way.
    Never raises: a report problem is recorded on the fire, it does not fail the schedule."""
    from analystos.services.notifications import deliver

    out: dict[str, Any] = {}
    owner = session.get(User, schedule.owner_id)
    if owner is None or not owner.active:
        return out
    targets = (schedule.config or {}).get("deliver_to") or []
    origin = {"schedule_id": schedule.id, "schedule_run_id": srun.id}
    docs: dict[str, Any] = {}
    for art in pinned_to(session, schedule):
        try:
            with session.begin_nested():
                info = refresh_document(session, owner, art, actor=f"schedule:{schedule.id}")
                report = render_snapshot(session, art, actor=f"schedule:{schedule.id}")
                info["report_artifact_id"] = report.id
                info["deliveries"] = deliver(session, schedule.workspace_id, targets, subject_type="report",
                                             subject_id=report.id, origin=origin)
        except (AnalystOSError, ValueError) as exc:
            info = {"error": getattr(exc, "message", str(exc))[:300]}
        docs[art.id] = info
    if docs:
        out["report_documents"] = docs
    dashboards = (schedule.config or {}).get("digest_dashboards") or []
    if dashboards:
        from analystos.services.stories import attach_digests

        out["digests"] = attach_digests(session, owner, schedule.workspace_id, dashboards, targets, origin=origin)
    return out

"""Report generation (RPT-001..003): ReportData is assembled from persisted, verified evidence
(never from model memory), rendered deterministically, stored as a versioned `report` artifact,
and downloaded through an audited endpoint."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.artifacts.registry import link, save_artifact
from analystos.contracts.reports import ReportAlert, ReportChart, ReportData, ReportInsight, ReportMetric, ReportQuery
from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import stable_hash, utcnow
from analystos.db.models import Alert, AnalysisRun, Artifact, Hypothesis, Insight, QueryExecution, Workspace
from analystos.events.bus import emit
from analystos.evidence.verification import insight_states, void_cause
from analystos.governance.audit import audit
from analystos.services.notifications import notify

FORMATS = ("md", "html", "pdf", "xlsx", "pptx", "docx")
KINDS = ("executive", "operational", "statistical", "exception", "weekly_summary")


def build_report_data(session: Session, run_id: str, kind: str = "executive", *, finalizing: bool = False) -> ReportData:
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {KINDS}")
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound("run not found")
    if run.status != "COMPLETED" and not finalizing:
        raise InvalidInput(f"run is {run.status}; reports are built from completed runs")
    ws = session.get(Workspace, run.workspace_id)
    changes = (run.summary or {}).get("changes") or {}
    change_of = {c["code"]: "new" for c in changes.get("new", [])} | {c["code"]: "persisting" for c in changes.get("persisting", [])} \
        | {c["code"]: "changed" for c in changes.get("changed", [])}
    insights = []
    found = list(session.scalars(select(Insight).where(Insight.run_id == run_id, Insight.status == "verified")
                                 .order_by(Insight.confidence.desc())))
    states = insight_states(session, [i.id for i in found])  # P7-01: the record's state, not the old boolean
    for ins in found:
        void = void_cause(states[ins.id])
        qids = [e["id"] for e in ins.evidence if e.get("type") == "query"]
        queries = [ReportQuery(id=q.id, sql=q.sql, row_count=q.row_count, result_hash=q.result_hash)
                   for q in session.scalars(select(QueryExecution).where(QueryExecution.id.in_(qids)))]
        insights.append(ReportInsight(code=ins.code, title=ins.title, finding=ins.finding, confidence=ins.confidence,
                                      verified=ins.verified and void is None, void_reason=void,
                                      caveats=ins.caveats, business_impact=ins.business_impact, evidence_queries=queries,
                                      change=change_of.get(ins.code) if changes else None,
                                      validation=ins.validation, stale=ins.stale_since is not None))
    prev_values = {m["name"]: m.get("previous_value") for m in changes.get("metrics", [])}
    metrics = [ReportMetric(name=a.name, display_name=a.content.get("display_name", a.name), definition=a.content.get("definition", ""),
                            value=(a.content.get("validation") or {}).get("value"), previous_value=prev_values.get(a.name),
                            format=a.content.get("format", "number"))
               for a in session.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "metric")
                                             .order_by(Artifact.created_at, Artifact.name))]
    charts = [ReportChart(key=a.name, title=a.content.get("title", a.name), chart_type=a.content.get("chart_type", "table"),
                          columns=(a.content.get("preview") or {}).get("columns", []), rows=(a.content.get("preview") or {}).get("rows", [])[:200])
              for a in session.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "chart").order_by(Artifact.created_at))]
    quality = session.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "quality_report"))
    hyps = [{"code": h.code, "statement": h.statement, "status": h.status, "conclusion": h.conclusion}
            for h in session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.status != "superseded")
                                     .order_by(Hypothesis.created_at))]
    alerts = [ReportAlert(severity=a.severity, title=a.title, message=a.message, metric=(a.data or {}).get("metric"))
              for a in session.scalars(select(Alert).where(Alert.workspace_id == run.workspace_id, Alert.status == "open")
                                       .order_by(Alert.created_at.desc()).limit(20))]
    resolved = [ReportInsight(code=c["code"], title=c["title"], finding=c["finding"], confidence=0.0, verified=True)
                for c in changes.get("resolved", [])]
    return ReportData(kind=kind, title=f"{ws.name} — {kind.replace('_', ' ')} report", workspace_name=ws.name, objective=run.objective,
                      run_id=run.id, generated_at=(run.finished_at or utcnow()).isoformat(), summary_markdown=(run.summary or {}).get("summary_markdown", ""),
                      insights=insights, resolved_insights=resolved, metrics=metrics, charts=charts,
                      quality_issues=[{k: i.get(k) for k in ("severity", "asset", "column", "message")}
                                      for i in ((quality.content if quality else {}) or {}).get("issues", [])[:50]],
                      hypotheses=hyps, alerts=alerts,
                      lineage_note=f"Every finding links to its evidence queries; full lineage for run {run.id} is in AnalystOS.",
                      caveats=sorted({c for i in insights for c in i.caveats})[:10])


def generate_report(session: Session, run_id: str, *, kind: str = "executive", formats: tuple[str, ...] = ("html", "pdf", "xlsx"),
                    actor: str = "system", finalizing: bool = False) -> Artifact:
    from analystos.reports import render
    from analystos.services.platform_settings import get as platform

    if not platform().features.reports:
        raise InvalidInput("report generation is turned off by the administrator")
    bad = [f for f in formats if f not in FORMATS]
    if bad or not formats:
        raise InvalidInput(f"formats must be within {FORMATS}")
    from analystos.reports import unavailable_formats

    unavailable = unavailable_formats(formats)
    if unavailable and len(unavailable) == len(formats):
        from analystos.core.errors import FeatureUnavailable

        raise FeatureUnavailable(f"report formats {', '.join(formats)} are unavailable: {next(iter(unavailable.values()))}",
                                 details={"unavailable_formats": unavailable})
    formats = tuple(f for f in formats if f not in unavailable)  # the rest render; the artifact says why not all
    data = build_report_data(session, run_id, kind, finalizing=finalizing)
    run = session.get(AnalysisRun, run_id)
    directory = Path(get_settings().artifact_dir) / run.workspace_id / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    data_hash = stable_hash(data.snapshot())
    files: dict[str, Any] = {}
    for fmt in formats:
        content, mime, ext = render(data, fmt)
        digest = hashlib.sha256(content).hexdigest()
        path = directory / f"{run_id}-{kind}-{data_hash[:12]}.{ext}"
        path.write_bytes(content)
        files[fmt] = {"path": str(path), "sha256": digest, "bytes": len(content), "mime": mime, "ext": ext}
    art = save_artifact(session, workspace_id=run.workspace_id, run_id=run_id, type_="report", name=f"{kind} report",
                        content={"kind": kind, "title": data.title, "report_data_hash": data_hash, "files": files,
                                 **({"unavailable_formats": unavailable} if unavailable else {}),
                                 "insights": len(data.insights), "metrics": len(data.metrics), "alerts": len(data.alerts)},
                        creator_agent="insight" if actor == "system" else None,
                        creator_user=None if actor == "system" else actor.split(":", 1)[-1], status="final")
    session.flush()
    link(session, run.workspace_id, ("run", run_id), "reported_by", (art.type, art.id), run_id=run_id)
    for i in data.insights:
        link(session, run.workspace_id, (art.type, art.id), "cites", ("insight", i.code), run_id=run_id)
    for chart in session.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "chart",
                                                        Artifact.name.in_([c.key for c in data.charts]))):
        link(session, run.workspace_id, (art.type, art.id), "includes", (chart.type, chart.id), run_id=run_id)
    emit(run.workspace_id, "report.generated", {"artifact_id": art.id, "kind": kind, "formats": list(formats)}, run_id=run_id,
         session=session)
    notify(session, run.workspace_id, kind="report", title=f"Report ready: {data.title}"[:300],
           body=f"{sum(1 for i in data.insights if i.verified)} verified findings"
                + (f" ({sum(1 for i in data.insights if i.void_reason)} void: need re-verification)"
                   if any(i.void_reason for i in data.insights) else "")
                + f", {len(data.alerts)} open alerts.", link={"type": "artifact", "id": art.id})
    audit(actor, "report.generated", workspace_id=run.workspace_id, run_id=run_id, target=art.id,
          details={"kind": kind, "formats": list(formats), "report_data_hash": data_hash}, session=session)
    return art


def report_file(art: Artifact, fmt: str) -> tuple[bytes, str, str]:
    info = (art.content.get("files") or {}).get(fmt)
    if not info:
        why = (art.content.get("unavailable_formats") or {}).get(fmt)
        raise NotFound(f"format {fmt} not generated for this report" + (f": {why}" if why else ""))
    try:
        content = Path(info["path"]).read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        raise NotFound(f"the stored {fmt} file of this report is missing from the artifact store; generate the report again",
                       details={"artifact_id": art.id, "format": fmt, "remedy": "regenerate"}) from None
    if hashlib.sha256(content).hexdigest() != info["sha256"]:
        raise InvalidInput("stored report does not match its recorded hash")
    return content, info["mime"], info["ext"]

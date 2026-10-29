"""Dashboard stories and digests (CONV-05): the story of a builder dashboard, as a report and as a delivery.

The facts come from `reports/story.py` (deterministic, from the chart previews, KPI values, the previous
version and the workspace's threshold monitors); wording goes through the `data_story` purpose. A story can
become a report document ("turn this into a report"), and a schedule (`config.digest_dashboards`) or a
monitor (`config.attach_digest`) can send it as a final report snapshot through the existing delivery path,
so every send still needs an approved destination that accepts reports.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.contracts.report_doc import ContentRef, DataStory, ReportDoc, Section, TextBlock, derive_sensitivity
from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.logging import get_logger
from analystos.db.models import Artifact, ArtifactVersion, Monitor, User

log = get_logger(__name__)


def thresholds(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Enabled threshold monitors of the workspace, as {metric, op, value, name}."""
    out = []
    for m in session.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id, Monitor.enabled.is_(True),
                                                   Monitor.kind == "metric_threshold").limit(100)):
        cfg = m.config or {}
        if cfg.get("metric") and cfg.get("op") and isinstance(cfg.get("value"), int | float):
            out.append({"metric": cfg["metric"], "op": cfg["op"], "value": cfg["value"], "name": m.name})
    return out


def _previous(session: Session, art: Artifact) -> tuple[DashboardDoc | None, int | None]:
    """The newest earlier version whose content differs (a refresh or an edit), else None."""
    for v in session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id,
                                                           ArtifactVersion.version < art.version)
                             .order_by(ArtifactVersion.version.desc()).limit(20)):
        if v.content_hash != art.content_hash:
            try:
                return DashboardDoc.model_validate(v.content), v.version
            except ValueError:
                return None, None
    return None, None


def compute(session: Session, user: User, dashboard_id: str, *, router: Any = None, word_it: bool = True) -> tuple[Artifact, DataStory]:
    from analystos.dashboards import service as dashboards
    from analystos.reports.story import PROMPT_VERSION, build_story, word
    from analystos.services.report_docs import _call_ctx

    art = dashboards.load(session, user, dashboard_id)
    doc = dashboards.document(art)
    before, prev_version = _previous(session, art)
    story = build_story(doc, version=art.version, before=before, previous_version=prev_version,
                        thresholds=thresholds(session, art.workspace_id), dashboard_id=art.id)
    if word_it:
        story = word(story, router=router, call_ctx=_call_ctx(session, art.workspace_id, art.id, PROMPT_VERSION, "data_story"))
    return art, story


def story(session: Session, user: User, dashboard_id: str, *, router: Any = None) -> dict[str, Any]:
    from analystos.reports.story import digest_markdown
    from analystos.services.report_docs import default_router

    _, s = compute(session, user, dashboard_id, router=router if router is not None else default_router())
    return {**s.model_dump(mode="json"), "markdown": digest_markdown(s)}


def _changes_markdown(s: DataStory) -> str:
    if s.changes:
        return "\n".join(f"- {f.text}" for f in s.changes)
    return "No earlier version to compare with yet." if s.previous_version is None else "Nothing changed since the previous version."


def _summary_markdown(s: DataStory) -> str:
    return "\n".join([s.shows, "", *(f"- {f.text}" for f in s.facts)]).strip()


def story_doc(session: Session, user: User, art: Artifact, s: DataStory, *, title: str | None = None) -> ReportDoc:
    """A report document from a story: the summary and changes as story text, then each chart and KPI with data."""
    from analystos.dashboards import service as dashboards
    from analystos.reports.document import build_block, slug
    from analystos.reports.story import derived_values
    from analystos.services.report_docs import resolver

    dash = dashboards.document(art)
    doc = ReportDoc(id=slug(dash.id, "r")[:56] + "_story", title=(title or f"{dash.title}: summary")[:300],
                    objective=f"What {dash.title} shows and what changed", derived=derived_values(s),
                    origin={"type": "story", "dashboard_id": art.id, "version": art.version, "story_hash": s.story_hash},
                    sections=[Section(id="summary", title="Summary",
                                      blocks=[TextBlock(id="t_summary", markdown=_summary_markdown(s), generated="story")]),
                              Section(id="changes", title="What changed",
                                      blocks=[TextBlock(id="t_changes", markdown=_changes_markdown(s), generated="story")])])
    resolve = resolver(session, user, art.workspace_id)
    blocks: list[Any] = []
    refs = [ContentRef(kind="dashboard_kpi", artifact_id=art.id, element_id=k.id) for k in dash.kpis if k.value is not None]
    refs += [ContentRef(kind="dashboard_chart", artifact_id=art.id, element_id=c.id) for c in dash.charts
             if (c.preview or {}).get("rows")]
    for ref in refs:
        try:
            content = resolve(ref)
        except Exception:  # noqa: BLE001 - content without data is left out of the story report
            continue
        draft = doc.model_copy(update={"sections": [*doc.sections, Section(id="details", title="Details", blocks=blocks)]})
        blocks.append(build_block(draft, ref, content))
    if blocks:
        doc = doc.model_copy(update={"sections": [*doc.sections, Section(id="details", title="The numbers", blocks=blocks)]})
    return derive_sensitivity(doc)


def to_report(session: Session, user: User, dashboard_id: str, *, title: str | None = None, router: Any = None) -> Artifact:
    """"Turn this into a report": a new report document from the dashboard's story (rules wording by default)."""
    from analystos.reports.document import unbacked
    from analystos.services import report_docs

    art, s = compute(session, user, dashboard_id, router=router)
    doc = story_doc(session, user, art, s, title=title)
    for b in doc.blocks():  # the story's own numbers are derived values; anything else is a bug, never written
        if isinstance(b, TextBlock) and unbacked(doc, b.markdown):
            raise InvalidInput(f"the story quotes numbers it cannot back: {', '.join(unbacked(doc, b.markdown)[:5])}")
    return report_docs.create(session, user, art.workspace_id, title=doc.title, doc=doc)


def story_rewriter(session: Session, owner: User, dashboard_id: str) -> Callable[[ReportDoc], ReportDoc]:
    """For a refresh: rewrite a story report's story text and derived values from the dashboard as it is now."""
    def rewrite(doc: ReportDoc) -> ReportDoc:
        from analystos.reports.story import derived_values

        try:
            _, s = compute(session, owner, dashboard_id, word_it=False)
        except AnalystOSError as exc:
            log.info("story refresh of %s skipped: %s", dashboard_id, exc.message)
            return doc
        text = {"t_summary": _summary_markdown(s), "t_changes": _changes_markdown(s)}
        sections = []
        for sec in doc.sections:
            blocks = [b.model_copy(update={"markdown": text[b.id], "held": None})
                      if isinstance(b, TextBlock) and b.generated == "story" and b.id in text else b for b in sec.blocks]
            sections.append(sec.model_copy(update={"blocks": blocks}))
        return doc.model_copy(update={"sections": sections, "derived": derived_values(s),
                                      "origin": {**doc.origin, "story_hash": s.story_hash}})

    return rewrite


def check_digest_target(session: Session, workspace_id: str, dashboard_id: Any) -> None:
    """`attach_digest` / `digest_dashboards` must name builder dashboards of this workspace."""
    from analystos.dashboards.service import is_builder

    ids = dashboard_id if isinstance(dashboard_id, list) else [dashboard_id]
    for did in ids:
        art = session.get(Artifact, did) if isinstance(did, str) else None
        if art is None or art.workspace_id != workspace_id or not is_builder(art):
            raise InvalidInput(f"{did!r} is not a builder dashboard of this workspace (a digest needs one)")


def digest_report(session: Session, owner: User, dashboard_id: str) -> Artifact:
    """The digest as a final `report` artifact: the story document rendered, upserted per dashboard."""
    from analystos.governance.policy import get_workspace
    from analystos.reports.document import to_report_data
    from analystos.services.report_docs import save_rendered

    art, s = compute(session, owner, dashboard_id, word_it=False)
    doc = story_doc(session, owner, art, s, title=f"{s.title}: digest")
    v = session.scalar(select(ArtifactVersion).where(ArtifactVersion.artifact_id == art.id, ArtifactVersion.version == art.version))
    ts = (v.created_at if v is not None and v.created_at else art.created_at)
    data = to_report_data(doc, artifact_id=art.id, version=art.version, generated_at=ts.replace(microsecond=0).isoformat(),
                          workspace_name=get_workspace(session, art.workspace_id).name)
    return save_rendered(session, art.workspace_id, data, doc, name=f"digest:{art.id}", kind="digest",
                         source={"dashboard": art.id, "version": art.version, "story_hash": s.story_hash})


def attach_digests(session: Session, owner: User, workspace_id: str, dashboard_ids: list[str], targets: list[str] | None,
                   *, origin: dict[str, Any]) -> dict[str, Any]:
    """Render each dashboard's digest and queue it for `targets`. Never raises: problems are returned per dashboard."""
    from analystos.services.notifications import deliver

    out: dict[str, Any] = {}
    for did in dashboard_ids[:20]:
        try:
            with session.begin_nested():
                report = digest_report(session, owner, did)
                out[did] = {"report_artifact_id": report.id,
                            "deliveries": deliver(session, workspace_id, targets, subject_type="report", subject_id=report.id,
                                                  origin=origin)}
        except (AnalystOSError, ValueError) as exc:
            out[did] = {"error": getattr(exc, "message", str(exc))[:300]}
    return out

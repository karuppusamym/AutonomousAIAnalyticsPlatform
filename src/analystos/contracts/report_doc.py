"""Report document contract (CONV-04): a report a person shapes by conversation.

A report document is an `artifact` of type `report_document` whose content is a `ReportDoc`
(`schema_version: report_doc.v1`). Like a builder dashboard it is never edited directly: typed ops
(`reports/document.py`) change it and every applied batch is a new artifact version.

Chart, KPI and finding blocks hold a bounded copy of verified content that already exists (a builder
dashboard's chart preview or KPI, a run's verified finding, chart or metric, an answered Ask question) and
a `ContentRef` naming where it came from, so a refresh re-reads the source and the renderers never query.
Text blocks are free prose, but every number in them must be a value the document's blocks show (the
numbers guard); one it cannot back is refused, not written.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from analystos.contracts.dashboard_dsl import MAX_PREVIEW_ROWS, Identifier, Sensitivity, max_sensitivity

SCHEMA_VERSION = "report_doc.v1"
ARTIFACT_TYPE = "report_document"
MAX_SECTIONS = 40
MAX_BLOCKS = 60  # per section

RefKind = Literal["dashboard_chart", "dashboard_kpi", "finding", "run_chart", "run_metric", "ask_answer"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentRef(_Strict):
    """Where a block's content lives. Exactly the identifiers its kind needs (checked by the engine)."""

    kind: RefKind
    artifact_id: str | None = Field(default=None, max_length=40)  # dashboard, run chart or run metric artifact
    element_id: str | None = Field(default=None, max_length=64)  # chart or KPI id inside a dashboard
    run_id: str | None = Field(default=None, max_length=40)  # finding
    code: str | None = Field(default=None, max_length=40)  # finding code
    turn_id: str | None = Field(default=None, max_length=40)  # Ask answer

    def key(self) -> str:
        parts = [self.kind, self.artifact_id, self.element_id, self.run_id, self.code, self.turn_id]
        return ":".join(p for p in parts if p)


class SourceInfo(_Strict):
    """What a block was read from, as of when it was added or last refreshed."""

    label: str = Field(max_length=400)  # e.g. "Dashboard 'Operations' v4, chart 'Incidents per week'"
    assets: list[str] = Field(default_factory=list, max_length=50)
    version: int | None = None  # the source artifact's version
    content_hash: str = ""  # the resolved content's hash: a refresh that changes it is a new version
    labels: list[str] = Field(default_factory=list)  # e.g. "experimental"


class TextBlock(_Strict):
    kind: Literal["text"] = "text"
    id: Identifier
    markdown: str = Field(default="", max_length=20_000)
    # Set when a refresh changed the values this text quoted: it is not rendered until someone rewrites it.
    held: str | None = None
    generated: Literal["story"] | None = None  # written from a dashboard story; a refresh rewrites it


class ChartBlock(_Strict):
    kind: Literal["chart"] = "chart"
    id: Identifier
    ref: ContentRef
    title: str = Field(min_length=1, max_length=300)
    chart_type: str = "table"
    columns: list[str] = Field(default_factory=list, max_length=60)
    rows: list[list[Any]] = Field(default_factory=list, max_length=MAX_PREVIEW_ROWS)
    caption: str = Field(default="", max_length=1000)
    sensitivity: Sensitivity = "internal"
    source: SourceInfo


class KpiBlock(_Strict):
    kind: Literal["kpi"] = "kpi"
    id: Identifier
    ref: ContentRef
    title: str = Field(min_length=1, max_length=300)
    value: float | None = None
    previous_value: float | None = None
    format: Literal["number", "percent", "hours", "currency"] = "number"
    unit: str = ""
    sensitivity: Sensitivity = "internal"
    source: SourceInfo


class FindingBlock(_Strict):
    kind: Literal["finding"] = "finding"
    id: Identifier
    ref: ContentRef
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=8000)
    confidence: float = 0.0
    verified: bool = True
    caveats: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[str] = Field(default_factory=list, max_length=20)  # query execution ids
    sensitivity: Sensitivity = "internal"
    source: SourceInfo


Block = Annotated[TextBlock | ChartBlock | KpiBlock | FindingBlock, Field(discriminator="kind")]


class Section(_Strict):
    id: Identifier
    title: str = Field(min_length=1, max_length=200)
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)


class ReportDoc(_Strict):
    schema_version: Literal["report_doc.v1"] = SCHEMA_VERSION
    id: Identifier
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=2000)
    sections: list[Section] = Field(default_factory=list, max_length=MAX_SECTIONS)
    sensitivity: Sensitivity = "internal"  # derived: the maximum over its blocks
    schedule_id: str | None = Field(default=None, max_length=40)  # pinned: refreshed and delivered on each fire
    # Numbers computed by code from the blocks (a dashboard story's changes and shares), each with how it was
    # computed: text may quote them as it quotes a cell. A refresh recomputes or drops them.
    derived: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    origin: dict[str, Any] = Field(default_factory=dict)  # e.g. {"type": "story", "dashboard_id": ...}

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def blocks(self) -> list[Any]:
        return [b for s in self.sections for b in s.blocks]

    def block(self, block_id: str) -> tuple[Section, Any] | None:
        for s in self.sections:
            for b in s.blocks:
                if b.id == block_id:
                    return s, b
        return None

    def ids(self) -> set[str]:
        return {s.id for s in self.sections} | {b.id for b in self.blocks()}


def derive_sensitivity(doc: ReportDoc) -> ReportDoc:
    """The document is as sensitive as its most sensitive block (never lowered by an edit)."""
    levels = [getattr(b, "sensitivity", "internal") for b in doc.blocks()]
    return doc.model_copy(update={"sensitivity": max_sensitivity("public", doc.sensitivity if doc.blocks() else "internal",
                                                                 *levels)})


def report_doc_json_schema() -> dict[str, Any]:
    return ReportDoc.model_json_schema()


# ------------------------------------------------------------------------------------------- data stories
class StoryFact(BaseModel):
    """One deterministic fact about a dashboard (CONV-05). `values` are the numbers its sentence may quote,
    each computed by code from the chart previews or the version diff."""

    kind: Literal["top_mover", "leader", "latest_change", "trend_change", "threshold_breach", "kpi_change",
                  "structure_change", "overview"]
    text: str
    score: float = 0.0
    chart_id: str | None = None
    values: list[float] = Field(default_factory=list)
    how: str = ""  # how the values were computed, in plain words


class DataStory(BaseModel):
    dashboard_id: str
    version: int
    previous_version: int | None = None
    title: str
    shows: str  # what the dashboard shows
    facts: list[StoryFact] = Field(default_factory=list)  # the 3-5 notable facts
    changes: list[StoryFact] = Field(default_factory=list)  # since the previous version
    wording: Literal["rules", "llm_small"] = "rules"
    notes: list[str] = Field(default_factory=list)
    story_hash: str = ""

    def known_numbers(self) -> list[float]:
        return [v for f in [*self.facts, *self.changes] for v in f.values]

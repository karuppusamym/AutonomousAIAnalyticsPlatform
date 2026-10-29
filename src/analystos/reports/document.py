"""Typed report-document ops and the verified snapshot (CONV-04).

The conversation never edits the report JSON. Rules or a model propose ops; this module validates each
against the current document and applies a batch atomically (one refused op refuses the batch). Content
comes in only through `ReportEnv.resolve`, which reads existing verified content (dashboard previews and
KPIs, verified findings, run charts and metrics, answered Ask questions) with the actor's rights; the
engine itself is pure, so the same ops replay identically. `export.request` is never applied here: it is
returned as deferred and goes to the hash-bound approval flow.

Every number in a text block, a section title or the report title must be a value the document's blocks
show, or one code derived from them (`ReportDoc.derived`): `dashboards.ops.numbers_guard` decides, with the
same tolerance rules as the dashboard builder.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from analystos.contracts.report_doc import (
    MAX_BLOCKS,
    MAX_SECTIONS,
    ChartBlock,
    ContentRef,
    FindingBlock,
    KpiBlock,
    ReportDoc,
    Section,
    TextBlock,
    derive_sensitivity,
)
from analystos.contracts.reports import (
    ReportBlock,
    ReportChart,
    ReportData,
    ReportInsight,
    ReportMetric,
    ReportQuery,
    ReportSection,
)
from analystos.dashboards.ops import numbers_guard
from analystos.reports._common import to_number

EXPORT_FORMATS = ("pdf", "docx", "pptx", "html")
HELD_NOTE = "This paragraph quoted numbers that changed in the latest refresh; it is held until someone rewrites it."


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetTitle(_Op):
    op: Literal["report.set_title"] = "report.set_title"
    title: str = Field(min_length=1, max_length=300)


class SectionAdd(_Op):
    op: Literal["section.add"] = "section.add"
    title: str = Field(min_length=1, max_length=200)
    section_id: str | None = None
    position: int | None = Field(default=None, ge=0)  # index among the sections; default: before the sources (last)


class SectionRemove(_Op):
    op: Literal["section.remove"] = "section.remove"
    section_id: str


class SectionRename(_Op):
    op: Literal["section.rename"] = "section.rename"
    section_id: str
    title: str = Field(min_length=1, max_length=200)


class SectionMove(_Op):
    op: Literal["section.move"] = "section.move"
    section_id: str
    direction: Literal["up", "down", "top", "bottom"] | None = None
    position: int | None = Field(default=None, ge=0)


class TextSet(_Op):
    op: Literal["text.set"] = "text.set"
    section_id: str
    block_id: str | None = None  # replace this text block; else the section's first text block, else a new one
    markdown: str = Field(max_length=20_000)


class TextShorten(_Op):
    op: Literal["text.shorten"] = "text.shorten"
    section_id: str
    block_id: str | None = None
    max_sentences: int | None = Field(default=None, ge=1, le=50)  # default: half, at least one


class BlockAdd(_Op):
    op: Literal["block.add"] = "block.add"
    section_id: str
    ref: ContentRef
    position: int | None = Field(default=None, ge=0)
    caption: str = Field(default="", max_length=1000)


class BlockRemove(_Op):
    op: Literal["block.remove"] = "block.remove"
    block_id: str


class BlockMove(_Op):
    op: Literal["block.move"] = "block.move"
    block_id: str
    section_id: str | None = None  # move into this section (default: its own)
    direction: Literal["up", "down", "top", "bottom"] | None = None
    position: int | None = Field(default=None, ge=0)


class PinSchedule(_Op):
    op: Literal["report.pin_schedule"] = "report.pin_schedule"
    schedule_id: str | None = None  # None unpins


class ExportRequest(_Op):
    op: Literal["export.request"] = "export.request"
    format: Literal["pdf", "docx", "pptx", "html"] = "pdf"


OP_MODELS = (SetTitle, SectionAdd, SectionRemove, SectionRename, SectionMove, TextSet, TextShorten, BlockAdd, BlockRemove,
             BlockMove, PinSchedule, ExportRequest)
Op = Annotated[SetTitle | SectionAdd | SectionRemove | SectionRename | SectionMove | TextSet | TextShorten | BlockAdd |
               BlockRemove | BlockMove | PinSchedule | ExportRequest, Field(discriminator="op")]
OP_KINDS = tuple(m.model_fields["op"].default for m in OP_MODELS)
_OPS = TypeAdapter(list[Op])


def op_guide() -> str:
    """One line per op kind with its fields (* = required), for the model rung."""
    lines = []
    for model in OP_MODELS:
        fields = [f"{name}{'*' if info.is_required() else ''}" for name, info in model.model_fields.items() if name != "op"]
        lines.append(f"- {model.model_fields['op'].default}: {', '.join(fields)}")
    return "\n".join(lines)


def parse_ops(raw: list[dict[str, Any]]) -> list[Any]:
    try:
        return _OPS.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValueError(f"invalid op at {list(first['loc'])}: {first['msg']}") from None


# -- environment -------------------------------------------------------------------------------------
class Unresolvable(Exception):  # noqa: N818 - a reason, not a failure of the engine
    """The content a ref names does not exist, is not verified, or the actor may not read it."""


def _no_catalog(ref: ContentRef) -> dict[str, Any]:
    raise Unresolvable("no content catalog")


@dataclass
class ReportEnv:
    actor: str
    # ref -> {"kind": chart|kpi|finding, ...block fields without id/ref}; raises Unresolvable with the reason
    resolve: Callable[[ContentRef], dict[str, Any]] = _no_catalog
    # schedule id -> None when it may be pinned, else why not
    schedule_check: Callable[[str], str | None] = lambda schedule_id: "no schedules here"


@dataclass
class Refusal:
    index: int
    op: str
    reason: str
    suggestion: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "op": self.op, "reason": self.reason,
                **({"suggestion": self.suggestion} if self.suggestion else {})}


@dataclass
class OpResult:
    doc: ReportDoc
    applied: list[dict[str, Any]] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    deferred: list[ExportRequest] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused

    @property
    def changed(self) -> bool:
        return bool(self.applied)


class _Refused(Exception):
    def __init__(self, reason: str, suggestion: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason, self.suggestion = reason, suggestion


# -- helpers -----------------------------------------------------------------------------------------
def slug(text: str, prefix: str = "s") -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48] or prefix
    return base if base[0].isalpha() else f"{prefix}_{base}"


def unique_id(doc: ReportDoc, base: str) -> str:
    taken = doc.ids()
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    return out


def _section(doc: ReportDoc, section_id: str) -> Section:
    sec = doc.section(section_id)
    if sec is None:
        raise _Refused(f"no section {section_id!r} (sections: {', '.join(s.id for s in doc.sections) or 'none'})")
    return sec


def _replace(doc: ReportDoc, sec: Section) -> ReportDoc:
    return doc.model_copy(update={"sections": [sec if s.id == sec.id else s for s in doc.sections]})


def _cell_numbers(rows: Iterable[Any]) -> Iterable[float]:
    for row in rows:
        for v in row if isinstance(row, list | tuple) else []:
            if isinstance(v, bool):
                continue
            n = to_number(v)
            if n is not None:
                yield n


_TEXT_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


def known_numbers(doc: ReportDoc) -> list[float]:
    """Every value the document can back: chart cells, KPI values, the numbers of verified finding text and
    the values code derived from the blocks."""
    values: list[float] = []
    for b in doc.blocks():
        if isinstance(b, ChartBlock):
            values.extend(_cell_numbers(b.rows))
        elif isinstance(b, KpiBlock):
            values.extend(v for v in (b.value, b.previous_value) if v is not None)
        elif isinstance(b, FindingBlock):  # the finding's text is verified evidence: its numbers are backed
            values.extend(float(t.replace(",", "")) for t in _TEXT_NUMBER.findall(b.text) if t.replace(",", "").lstrip("+-"))
            values.append(round(b.confidence * 100, 6))
    for d in doc.derived:
        n = to_number(d.get("value"))
        if n is not None:
            values.append(n)
    return values


def unbacked(doc: ReportDoc, text: str) -> list[str]:
    return numbers_guard(text, known_numbers(doc))


def _guard(doc: ReportDoc, text: str, what: str) -> None:
    bad = unbacked(doc, text)
    if bad:
        raise _Refused(f"{what} quotes numbers this report cannot back: {', '.join(bad[:5])}. Add the chart, KPI or "
                       "finding that shows them first, or leave the number out")


def _place(items: list[Any], idx: int, direction: str | None, position: int | None) -> int:
    n = len(items)
    if position is not None:
        return min(position, n - 1)
    return {"up": max(idx - 1, 0), "down": min(idx + 1, n - 1), "top": 0, "bottom": n - 1}.get(direction or "", idx)


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def shorten(markdown: str, max_sentences: int | None = None) -> str:
    """Fewer sentences, never new words: the first `max_sentences` sentences of the prose (default half, at
    least one) and the first bullets of each list. Shortening only removes text, so no number becomes unbacked."""
    from analystos.reports._common import parse_markdown

    paras = [p for p in re.split(r"\n\s*\n", (markdown or "").strip()) if p.strip()]
    blocks = parse_markdown(markdown or "")
    sentences = [s for p in paras if not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", p) for s in _SENTENCE.split(p.strip())]
    bullets = [ln for p in paras for ln in p.splitlines() if re.match(r"^\s*(?:[-*+]|\d+[.)])\s", ln)]
    total = len(sentences) + len(bullets)
    if total <= 1 and len(blocks) <= 1:
        return markdown.strip()
    keep = max_sentences or max(1, math.ceil(total / 2))
    out_s = sentences[:keep]
    out_b = bullets[: max(0, keep - len(out_s))] if len(out_s) < keep else []
    parts = [" ".join(s.strip() for s in out_s)] if out_s else []
    if out_b:
        parts.append("\n".join(b.rstrip() for b in out_b))
    return "\n\n".join(p for p in parts if p).strip()


# -- appliers ------------------------------------------------------------------------------------------
def _set_title(doc: ReportDoc, op: SetTitle, env: ReportEnv) -> ReportDoc:
    _guard(doc, op.title, "the title")
    return doc.model_copy(update={"title": op.title.strip()})


def _add_section(doc: ReportDoc, op: SectionAdd, env: ReportEnv) -> ReportDoc:
    if len(doc.sections) >= MAX_SECTIONS:
        raise _Refused(f"a report has at most {MAX_SECTIONS} sections")
    _guard(doc, op.title, "the section title")
    if op.section_id is not None and op.section_id in doc.ids():
        raise _Refused(f"{op.section_id!r} already exists")
    sid = op.section_id or unique_id(doc, slug(op.title))
    sec = Section(id=sid, title=op.title.strip())
    sections = list(doc.sections)
    sections.insert(len(sections) if op.position is None else min(op.position, len(sections)), sec)
    return doc.model_copy(update={"sections": sections})


def _remove_section(doc: ReportDoc, op: SectionRemove, env: ReportEnv) -> ReportDoc:
    _section(doc, op.section_id)
    return doc.model_copy(update={"sections": [s for s in doc.sections if s.id != op.section_id]})


def _rename_section(doc: ReportDoc, op: SectionRename, env: ReportEnv) -> ReportDoc:
    sec = _section(doc, op.section_id)
    _guard(doc, op.title, "the section title")
    return _replace(doc, sec.model_copy(update={"title": op.title.strip()}))


def _move_section(doc: ReportDoc, op: SectionMove, env: ReportEnv) -> ReportDoc:
    _section(doc, op.section_id)
    if op.direction is None and op.position is None:
        raise _Refused("say where to move the section (up, down, top, bottom or a position)")
    sections = list(doc.sections)
    idx = next(i for i, s in enumerate(sections) if s.id == op.section_id)
    to = _place(sections, idx, op.direction, op.position)
    if to == idx:
        raise _Refused(f"the section {op.section_id!r} is already {'first' if idx == 0 else 'last' if idx == len(sections) - 1 else 'there'}")
    sec = sections.pop(idx)
    sections.insert(to, sec)
    return doc.model_copy(update={"sections": sections})


def _text_block(sec: Section, block_id: str | None) -> TextBlock | None:
    if block_id:
        b = next((b for b in sec.blocks if b.id == block_id), None)
        if b is None:
            raise _Refused(f"section {sec.id!r} has no block {block_id!r}")
        if not isinstance(b, TextBlock):
            raise _Refused(f"{block_id!r} is a {b.kind} block, not text")
        return b
    return next((b for b in sec.blocks if isinstance(b, TextBlock)), None)


def _set_text(doc: ReportDoc, op: TextSet, env: ReportEnv) -> ReportDoc:
    sec = _section(doc, op.section_id)
    _guard(doc, op.markdown, "the text")
    existing = _text_block(sec, op.block_id)
    if existing is not None:
        block = existing.model_copy(update={"markdown": op.markdown, "held": None, "generated": None})
        return _replace(doc, sec.model_copy(update={"blocks": [block if b.id == existing.id else b for b in sec.blocks]}))
    if len(sec.blocks) >= MAX_BLOCKS:
        raise _Refused(f"a section has at most {MAX_BLOCKS} blocks")
    bid = op.block_id if op.block_id and op.block_id not in doc.ids() else unique_id(doc, f"t_{sec.id}"[:56])
    block = TextBlock(id=bid, markdown=op.markdown)
    return _replace(doc, sec.model_copy(update={"blocks": [block, *sec.blocks]}))  # prose leads its section


def _shorten_text(doc: ReportDoc, op: TextShorten, env: ReportEnv) -> ReportDoc:
    sec = _section(doc, op.section_id)
    block = _text_block(sec, op.block_id)
    if block is None or not block.markdown.strip():
        raise _Refused(f"the {sec.title} section has no text to shorten")
    short = shorten(block.markdown, op.max_sentences)
    if short == block.markdown.strip():
        raise _Refused(f"the {sec.title} text is already as short as it gets (one sentence); rewrite it instead")
    new = block.model_copy(update={"markdown": short})
    return _replace(doc, sec.model_copy(update={"blocks": [new if b.id == block.id else b for b in sec.blocks]}))


_BLOCK_TYPES: dict[str, type] = {"chart": ChartBlock, "kpi": KpiBlock, "finding": FindingBlock}


def build_block(doc: ReportDoc, ref: ContentRef, content: dict[str, Any], block_id: str | None = None,
                caption: str = "") -> Any:
    kind = content.get("kind")
    model = _BLOCK_TYPES.get(str(kind))
    if model is None:
        raise _Refused(f"{ref.kind} content cannot be shown in a report")
    bid = block_id or unique_id(doc, slug(f"{kind}_{content.get('title', ref.key())}", "b")[:56])
    data = {k: v for k, v in content.items() if k in model.model_fields}
    if caption and "caption" in model.model_fields:
        data["caption"] = caption
    try:
        return model.model_validate({**data, "id": bid, "ref": ref.model_dump()})
    except ValidationError as exc:
        raise _Refused(f"the {ref.kind} content is not usable in a report: {exc.errors()[0]['msg']}") from None


def _check_ref(ref: ContentRef) -> None:
    need = {"dashboard_chart": ("artifact_id", "element_id"), "dashboard_kpi": ("artifact_id", "element_id"),
            "finding": ("run_id", "code"), "run_chart": ("artifact_id",), "run_metric": ("artifact_id",),
            "ask_answer": ("turn_id",)}[ref.kind]
    missing = [k for k in need if not getattr(ref, k)]
    if missing:
        raise _Refused(f"a {ref.kind} reference needs {', '.join(missing)}")


def _add_block(doc: ReportDoc, op: BlockAdd, env: ReportEnv) -> ReportDoc:
    sec = _section(doc, op.section_id)
    _check_ref(op.ref)
    if any(getattr(b, "ref", None) == op.ref for b in sec.blocks):
        raise _Refused(f"the {sec.title} section already includes that content")
    if len(sec.blocks) >= MAX_BLOCKS:
        raise _Refused(f"a section has at most {MAX_BLOCKS} blocks")
    try:
        content = env.resolve(op.ref)
    except Unresolvable as exc:
        raise _Refused(str(exc)) from None
    if op.caption:
        _guard(doc, op.caption, "the caption")
    block = build_block(doc, op.ref, content, caption=op.caption)
    blocks = list(sec.blocks)
    blocks.insert(len(blocks) if op.position is None else min(op.position, len(blocks)), block)
    return _replace(doc, sec.model_copy(update={"blocks": blocks}))


def _remove_block(doc: ReportDoc, op: BlockRemove, env: ReportEnv) -> ReportDoc:
    found = doc.block(op.block_id)
    if found is None:
        raise _Refused(f"no block {op.block_id!r} in this report")
    sec, _ = found
    return _replace(doc, sec.model_copy(update={"blocks": [b for b in sec.blocks if b.id != op.block_id]}))


def _move_block(doc: ReportDoc, op: BlockMove, env: ReportEnv) -> ReportDoc:
    found = doc.block(op.block_id)
    if found is None:
        raise _Refused(f"no block {op.block_id!r} in this report")
    src, block = found
    if op.section_id and op.section_id != src.id:
        dest = _section(doc, op.section_id)
        if len(dest.blocks) >= MAX_BLOCKS:
            raise _Refused(f"a section has at most {MAX_BLOCKS} blocks")
        doc = _replace(doc, src.model_copy(update={"blocks": [b for b in src.blocks if b.id != block.id]}))
        dest = _section(doc, dest.id)
        blocks = list(dest.blocks)
        blocks.insert(len(blocks) if op.position is None else min(op.position, len(blocks)), block)
        return _replace(doc, dest.model_copy(update={"blocks": blocks}))
    if op.direction is None and op.position is None:
        raise _Refused("say where to move the block (another section, up, down, top, bottom or a position)")
    blocks = list(src.blocks)
    idx = blocks.index(block)
    to = _place(blocks, idx, op.direction, op.position)
    if to == idx:
        raise _Refused(f"{block.id} is already there")
    blocks.insert(to, blocks.pop(idx))
    return _replace(doc, src.model_copy(update={"blocks": blocks}))


def _pin(doc: ReportDoc, op: PinSchedule, env: ReportEnv) -> ReportDoc:
    if op.schedule_id is None:
        if doc.schedule_id is None:
            raise _Refused("the report is not pinned to a schedule")
        return doc.model_copy(update={"schedule_id": None})
    why = env.schedule_check(op.schedule_id)
    if why:
        raise _Refused(why)
    if doc.schedule_id == op.schedule_id:
        raise _Refused("the report is already pinned to that schedule")
    return doc.model_copy(update={"schedule_id": op.schedule_id})


_APPLY: dict[str, Callable[[ReportDoc, Any, ReportEnv], ReportDoc]] = {
    "report.set_title": _set_title, "section.add": _add_section, "section.remove": _remove_section,
    "section.rename": _rename_section, "section.move": _move_section, "text.set": _set_text,
    "text.shorten": _shorten_text, "block.add": _add_block, "block.remove": _remove_block, "block.move": _move_block,
    "report.pin_schedule": _pin,
}


def apply_ops(doc: ReportDoc, ops: list[Any], env: ReportEnv) -> OpResult:
    """Validate and apply `ops` in order, atomically; sensitivity is re-derived after the batch."""
    current = doc
    applied: list[dict[str, Any]] = []
    refused: list[Refusal] = []
    deferred: list[ExportRequest] = []
    for i, op in enumerate(ops):
        if isinstance(op, ExportRequest):
            deferred.append(op)
            continue
        try:
            current = _APPLY[op.op](current, op, env)
        except _Refused as exc:
            refused.append(Refusal(i, op.op, exc.reason, exc.suggestion))
            continue
        except ValidationError as exc:
            refused.append(Refusal(i, op.op, exc.errors()[0]["msg"]))
            continue
        applied.append(op.model_dump(mode="json", exclude_none=True))
    if refused:
        return OpResult(doc, [], refused, deferred)
    return OpResult(derive_sensitivity(current), applied, [], deferred)


# -- refresh -------------------------------------------------------------------------------------------
@dataclass
class RefreshResult:
    doc: ReportDoc
    changed_blocks: list[str] = field(default_factory=list)
    unavailable: list[dict[str, str]] = field(default_factory=list)  # {block_id, reason}: kept as last read
    held: list[str] = field(default_factory=list)  # text blocks whose numbers are no longer backed

    @property
    def changed(self) -> bool:
        return bool(self.changed_blocks or self.held)


def refresh(doc: ReportDoc, env: ReportEnv, *, story_text: Callable[[ReportDoc], ReportDoc] | None = None) -> RefreshResult:
    """Re-read every block's source (a schedule fire). A block whose source changed is replaced by the new
    content; one that can no longer be read keeps its last content and is reported. Then every text block is
    checked again: text that quotes a number the refreshed blocks no longer show is held, not rendered.
    `story_text` rewrites story-generated text (and the derived values) from the refreshed dashboard."""
    out = RefreshResult(doc)
    sections = []
    for sec in doc.sections:
        blocks = []
        for b in sec.blocks:
            ref = getattr(b, "ref", None)
            if ref is None:
                blocks.append(b)
                continue
            try:
                content = env.resolve(ref)
            except Unresolvable as exc:
                out.unavailable.append({"block_id": b.id, "reason": str(exc)})
                blocks.append(b)
                continue
            new = build_block(doc, ref, content, block_id=b.id, caption=getattr(b, "caption", ""))
            if new.model_dump() != b.model_dump():
                out.changed_blocks.append(b.id)
            blocks.append(new)
        sections.append(sec.model_copy(update={"blocks": blocks}))
    current = doc.model_copy(update={"sections": sections})
    if story_text is not None:
        current = story_text(current)
    final = []
    for sec in current.sections:
        blocks = []
        for b in sec.blocks:
            if isinstance(b, TextBlock) and b.held is None and b.markdown.strip() and unbacked(current, b.markdown):
                b = b.model_copy(update={"held": HELD_NOTE})
                out.held.append(b.id)
            blocks.append(b)
        final.append(sec.model_copy(update={"blocks": blocks}))
    out.doc = derive_sensitivity(current.model_copy(update={"sections": final}))
    return out


# -- the verified snapshot -------------------------------------------------------------------------------
def _num(v: Any) -> float | int | None:
    n = to_number(v)
    if n is None:
        return None
    return int(n) if n.is_integer() and abs(n) < 1e15 else n


def sources(doc: ReportDoc) -> list[str]:
    """One line per distinct source, in the order the blocks cite them."""
    lines: dict[str, None] = {}
    for b in doc.blocks():
        src = getattr(b, "source", None)
        if src is None:
            continue
        line = src.label + (f" (reads {', '.join(src.assets[:6])})" if src.assets else "")
        if src.labels:
            line += f" [{', '.join(src.labels)}]"
        lines.setdefault(line, None)
    return list(lines)


def to_report_data(doc: ReportDoc, *, artifact_id: str, version: int, generated_at: str, workspace_name: str) -> ReportData:
    """The verified snapshot the renderers draw: every number is a value of the document's blocks. Held text
    becomes a note; a sources section closes the document."""
    sections: list[ReportSection] = []
    for sec in doc.sections:
        blocks: list[ReportBlock] = []
        for b in sec.blocks:
            if isinstance(b, TextBlock):
                if b.held:
                    blocks.append(ReportBlock(kind="note", markdown=HELD_NOTE))
                elif b.markdown.strip():
                    blocks.append(ReportBlock(kind="text", markdown=b.markdown))
            elif isinstance(b, ChartBlock):
                blocks.append(ReportBlock(kind="chart", source=b.source.label, chart=ReportChart(
                    key=b.id, title=b.title, chart_type=b.chart_type, columns=list(b.columns), rows=[list(r) for r in b.rows])))
                if b.caption:
                    blocks.append(ReportBlock(kind="text", markdown=b.caption))
            elif isinstance(b, KpiBlock):
                blocks.append(ReportBlock(kind="kpi", source=b.source.label, metric=ReportMetric(
                    name=b.id, display_name=b.title, value=_num(b.value), previous_value=_num(b.previous_value), format=b.format)))
            elif isinstance(b, FindingBlock):
                blocks.append(ReportBlock(kind="finding", source=b.source.label, insight=ReportInsight(
                    code=b.ref.code or b.id, title=b.title, finding=b.text, confidence=b.confidence, verified=b.verified,
                    caveats=list(b.caveats), evidence_queries=[ReportQuery(id=q, sql="") for q in b.evidence])))
        sections.append(ReportSection(key=sec.id, title=sec.title, blocks=blocks))
    src = sources(doc)
    if src:
        sections.append(ReportSection(key="sources", title="Sources",
                                      blocks=[ReportBlock(kind="text", markdown="\n".join(f"- {line}" for line in src))]))
    caveats = sorted({c for b in doc.blocks() if isinstance(b, FindingBlock) for c in b.caveats})[:10]
    return ReportData(kind="document", title=doc.title, workspace_name=workspace_name, objective=doc.objective,
                      run_id=artifact_id, generated_at=generated_at, sections=sections, origin=f"{artifact_id} v{version}",
                      lineage_note="Every chart, KPI and finding is a copy of verified content named in Sources; every "
                                   "number in the text is a value those blocks show.",
                      caveats=caveats)

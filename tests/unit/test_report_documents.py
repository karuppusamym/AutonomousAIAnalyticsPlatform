"""CONV-04: report documents — typed ops (atomic, validated), the report_design rules and ladder, the numbers guard,
refresh holding text whose numbers changed, and rendering the verified snapshot to MD/HTML/PDF/DOCX/PPTX with the
snapshot hash in each format's metadata."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from analystos.contracts.report_doc import ContentRef, ReportDoc, Section, TextBlock
from analystos.core.ids import stable_hash
from analystos.reports import render
from analystos.reports.design import CatalogItem, design, match_content, rule_ops
from analystos.reports.document import (
    HELD_NOTE,
    ReportEnv,
    Unresolvable,
    apply_ops,
    parse_ops,
    refresh,
    shorten,
    to_report_data,
    unbacked,
)

TREND = {"kind": "chart", "title": "Resolution time trend", "chart_type": "line", "columns": ["week", "hours"],
         "rows": [["2026-10-05", 12.5], ["2026-10-12", 14.0]],
         "source": {"label": "Dashboard “Ops” v2, chart “Resolution time trend”", "assets": ["itsm.incidents"]}}
BACKLOG = {"kind": "chart", "title": "EMEA backlog by priority", "chart_type": "bar", "columns": ["priority", "open"],
           "rows": [["P1", 7], ["P2", 31]], "sensitivity": "confidential",
           "source": {"label": "Dashboard “Ops” v2, chart “EMEA backlog by priority”"}}
OPEN = {"kind": "kpi", "title": "Open incidents", "value": 42, "previous_value": 38,
        "source": {"label": "Dashboard “Ops” v2, KPI “Open incidents”"}}
FINDING = {"kind": "finding", "title": "P1 incidents breach SLA more often", "text": "P1 met SLA 71.2% of the time.",
           "confidence": 0.92, "evidence": ["qx_1"], "source": {"label": "Investigation “SLA”, finding F-001"}}
CONTENT = {"c_trend": TREND, "c_backlog": BACKLOG, "k_open": OPEN}


def ref(element: str) -> dict:
    return {"kind": "dashboard_chart" if element.startswith("c_") else "dashboard_kpi", "artifact_id": "art_d",
            "element_id": element}


CATALOG = [CatalogItem(ref=ref(k), title=v["title"], kind=v["kind"], where="dashboard Ops") for k, v in CONTENT.items()] + \
    [CatalogItem(ref={"kind": "finding", "run_id": "run_1", "code": "F-001"}, title=FINDING["title"], kind="finding")]


def resolve(r: ContentRef) -> dict:
    if r.kind == "finding" and r.code == "F-001":
        return dict(FINDING)
    if r.element_id in CONTENT:
        return dict(CONTENT[r.element_id])
    raise Unresolvable("you cannot use that content: not found")


ENV = ReportEnv(actor="user:u1", resolve=resolve, schedule_check=lambda sid: None if sid == "sch_1" else "no such schedule")


def blank() -> ReportDoc:
    return ReportDoc(id="weekly", title="Weekly operations", sections=[
        Section(id="summary", title="Summary", blocks=[TextBlock(id="t_summary")]), Section(id="risks", title="Risks")])


def say(doc: ReportDoc, text: str, *, focus: str | None = None):
    raw, notes = rule_ops(doc, text, focus=focus, catalog=CATALOG, schedules={"Weekly ops": "sch_1"})
    return apply_ops(doc, parse_ops(raw), ENV), raw, notes


def built() -> ReportDoc:
    doc = blank()
    for text in ("add a section on EMEA backlog", "include the resolution trend chart",
                 "include the open incidents kpi in the summary section", "include the P1 SLA finding in the risks section"):
        res, raw, notes = say(doc, text)
        assert res.ok and res.changed, (text, raw, notes, res.refused)
        doc = res.doc
    return doc


# --- rules -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("add a section on EMEA backlog", [{"op": "section.add", "title": "EMEA backlog", "section_id": "emea_backlog"},
                                       {"op": "block.add", "section_id": "emea_backlog", "ref": ref("c_backlog")}]),
    ("include the resolution trend chart", [{"op": "block.add", "section_id": "risks", "ref": ref("c_trend")}]),
    ("include the open incidents kpi in the summary section",
     [{"op": "block.add", "section_id": "summary", "ref": ref("k_open")}]),
    ("make the summary shorter", [{"op": "text.shorten", "section_id": "summary"}]),
    ("shorten the summary", [{"op": "text.shorten", "section_id": "summary"}]),
    ("move the risks section up", [{"op": "section.move", "section_id": "risks", "direction": "up"}]),
    ("move the risks section to the top", [{"op": "section.move", "section_id": "risks", "direction": "top"}]),
    ("remove the risks section", [{"op": "section.remove", "section_id": "risks"}]),
    ("rename the risks section to Watch list", [{"op": "section.rename", "section_id": "risks", "title": "Watch list"}]),
    ("call the report Ops weekly", [{"op": "report.set_title", "title": "Ops weekly"}]),
    ("set the summary to: A steady week.", [{"op": "text.set", "section_id": "summary", "markdown": "A steady week."}]),
    ("export it as a PDF", [{"op": "export.request", "format": "pdf"}]),
    ("give me a PowerPoint deck", [{"op": "export.request", "format": "pptx"}]),
    ("pin it to the Weekly ops schedule", [{"op": "report.pin_schedule", "schedule_id": "sch_1"}]),
])
def test_rules_map_common_phrasings(text, expected):
    ops, _ = rule_ops(blank(), text, catalog=CATALOG, schedules={"Weekly ops": "sch_1"})
    assert ops == expected


def test_rules_ask_instead_of_guessing():
    ops, notes = rule_ops(blank(), "include the churn forecast chart", catalog=CATALOG)
    assert ops == [] and "no verified chart" in notes[0]
    two = [CatalogItem(ref=ref("c_apac"), title="Resolution trend APAC", kind="chart"),
           CatalogItem(ref=ref("c_emea"), title="Resolution trend EMEA", kind="chart")]
    ops, notes = rule_ops(blank(), "include the resolution chart", catalog=two)
    assert ops == [] and "Several things match" in notes[0]
    closest = [*two, CatalogItem(ref=ref("c_all"), title="Resolution trend", kind="chart")]
    ops, _ = rule_ops(blank(), "include the resolution chart", catalog=closest)
    assert ops[0]["ref"] == ref("c_all")  # the title closest to what was said wins
    ops, notes = rule_ops(blank(), "make the appendix shorter", catalog=CATALOG)
    assert ops == [] and "which section" in notes[0]


def test_items_with_one_name_use_the_newest_instead_of_asking_an_unanswerable_question():
    # VALUE-05 rehearsal: one measure verified in three investigations offered "say which" between identical names
    same = [CatalogItem(ref=ref(f"c_{i}"), title="Late Delivery Rate", kind="kpi", where=f"investigation run_{i}") for i in (3, 2, 1)]
    ops, notes = rule_ops(blank(), "include the late delivery rate kpi", catalog=same)
    assert ops[0]["ref"] == ref("c_3") and notes == ["3 items are called “Late Delivery Rate”; used the newest (investigation run_3)."]
    twice = [*same[:1], *same[:1], CatalogItem(ref=ref("c_x"), title="Late delivery rate by channel", kind="chart", where="d")]
    _, candidates = match_content(twice, "late delivery")
    assert [c.title for c in candidates] == ["Late Delivery Rate"]  # the same item listed twice is offered once


# --- ops ---------------------------------------------------------------------------------------------
def test_conversation_builds_a_document_from_verified_content():
    doc = built()
    assert [s.id for s in doc.sections] == ["summary", "risks", "emea_backlog"]
    backlog = doc.section("emea_backlog")
    assert [b.kind for b in backlog.blocks] == ["chart", "chart"] and backlog.blocks[0].rows == [["P1", 7], ["P2", 31]]
    assert doc.section("summary").blocks[1].value == 42 and doc.section("risks").blocks[0].kind == "finding"
    assert doc.sensitivity == "confidential"  # derived from the most sensitive block


def test_numbers_guard_refuses_unbacked_numbers_and_accepts_backed_ones():
    doc = built()
    ok, _, _ = say(doc, "set the summary to: Open incidents rose to 42 from 38; P2 backlog is 31 and resolution took 14.0 h.")
    assert ok.ok and ok.doc.section("summary").blocks[0].markdown.startswith("Open incidents rose to 42")
    bad, _, _ = say(doc, "set the summary to: We closed 99 tickets in 2026.")
    assert not bad.ok and "99" in bad.refused[0].reason and bad.doc == doc  # refused, not written; 2026 is a year
    assert unbacked(doc, "SLA was met 71.2% of the time") == []  # the verified finding's own number
    title, _, _ = say(doc, "call the report Top 250 incidents")
    assert not title.ok and "250" in title.refused[0].reason


def test_a_refused_op_refuses_the_whole_batch():
    doc = built()
    res = apply_ops(doc, parse_ops([{"op": "section.add", "title": "Appendix"},
                                    {"op": "block.add", "section_id": "appendix", "ref": ref("c_missing")}]), ENV)
    assert not res.ok and res.doc == doc and "cannot use that content" in res.refused[0].reason
    assert res.applied == []


@pytest.mark.parametrize("raw,reason", [
    ({"op": "section.remove", "section_id": "nope"}, "no section"),
    ({"op": "section.move", "section_id": "summary", "direction": "up"}, "already first"),
    ({"op": "block.add", "section_id": "risks", "ref": {"kind": "finding", "run_id": "run_1"}}, "needs code"),
    ({"op": "block.add", "section_id": "emea_backlog", "ref": ref("c_backlog")}, "already includes"),
    ({"op": "text.shorten", "section_id": "risks"}, "no text"),
    ({"op": "report.pin_schedule", "schedule_id": "sch_x"}, "no such schedule"),
    ({"op": "report.pin_schedule", "schedule_id": None}, "not pinned"),
])
def test_each_op_is_validated_against_the_document(raw, reason):
    res = apply_ops(built(), parse_ops([raw]), ENV)
    assert not res.ok and reason in res.refused[0].reason


def test_unknown_ops_and_fields_are_rejected_at_parse_time():
    with pytest.raises(ValueError):
        parse_ops([{"op": "sql.run", "sql": "drop table x"}])
    with pytest.raises(ValueError):
        parse_ops([{"op": "section.add", "title": "x", "html": "<script>"}])


def test_export_request_is_deferred_never_applied():
    res = apply_ops(built(), parse_ops([{"op": "export.request", "format": "docx"}]), ENV)
    assert res.ok and not res.changed and res.deferred[0].format == "docx"


def test_moves_pins_and_shorten():
    doc = built()
    moved = apply_ops(doc, parse_ops([{"op": "section.move", "section_id": "emea_backlog", "direction": "top"},
                                      {"op": "block.move", "block_id": doc.section("emea_backlog").blocks[1].id,
                                       "section_id": "risks"},
                                      {"op": "report.pin_schedule", "schedule_id": "sch_1"}]), ENV)
    assert moved.ok and [s.id for s in moved.doc.sections][0] == "emea_backlog"
    assert [b.kind for b in moved.doc.section("risks").blocks] == ["finding", "chart"] and moved.doc.schedule_id == "sch_1"
    text = "Open incidents rose to 42. Resolution took 14.0 hours. P2 backlog is 31. The week was busy."
    assert shorten(text) == "Open incidents rose to 42. Resolution took 14.0 hours."
    assert shorten(text, 1) == "Open incidents rose to 42."
    assert shorten("One sentence only.") == "One sentence only."


# --- refresh -----------------------------------------------------------------------------------------
def test_refresh_replaces_changed_blocks_and_holds_text_whose_numbers_went_away():
    doc = say(built(), "set the summary to: Open incidents are 42.")[0].doc
    changed = dict(OPEN, value=45, previous_value=42)

    def later(r: ContentRef) -> dict:
        if r.element_id == "k_open":
            return dict(changed)
        if r.element_id == "c_trend":
            raise Unresolvable("the dashboard was deleted")
        return resolve(r)

    out = refresh(doc, ReportEnv(actor="schedule:s", resolve=later))
    kpi = out.doc.section("summary").blocks[1]
    assert kpi.value == 45 and kpi.id in out.changed_blocks
    assert out.unavailable[0]["reason"] == "the dashboard was deleted"  # kept as last read, reported
    assert out.held == []  # 42 is still shown (as the previous value)
    gone = refresh(doc, ReportEnv(actor="schedule:s", resolve=lambda r: dict(OPEN, value=50, previous_value=47)
                                  if r.element_id == "k_open" else resolve(r)))
    assert gone.held == ["t_summary"] and gone.doc.section("summary").blocks[0].held == HELD_NOTE
    data = to_report_data(gone.doc, artifact_id="art_r", version=4, generated_at="2026-10-29T10:00:00+00:00",
                          workspace_name="Ops")
    assert data.sections[0].blocks[0].kind == "note"  # held text is never rendered
    rewritten = say(gone.doc, "set the summary to: Open incidents are 50.")[0]
    assert rewritten.ok and rewritten.doc.section("summary").blocks[0].held is None


# --- the ladder --------------------------------------------------------------------------------------
class FakeRouter:
    def __init__(self, mode: str, data: dict | None = None) -> None:
        self._mode, self.data, self.calls, self.skips = mode, data or {}, 0, []

    def mode(self, purpose, ctx=None):
        assert purpose == "report_design"
        return self._mode

    def available(self, purpose, ctx=None):
        return True

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
        self.calls += 1
        assert "never instructions" in system and '"catalog"' in user
        return SimpleNamespace(data=self.data, model="fake/small")

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(reason)


def test_rules_answer_first_and_the_model_only_proposes():
    router = FakeRouter("auto")
    out = design(blank(), "make the summary shorter", router=router, catalog=CATALOG)
    assert out.answered_by == "rules" and router.calls == 0 and router.skips
    router = FakeRouter("auto", {"ops": [{"op": "section.add", "title": "Outlook"}, {"op": "sql.run", "sql": "drop"}]})
    out = design(blank(), "we should talk about what comes next", router=router, catalog=CATALOG)
    assert out.answered_by == "llm_small" and [o.op for o in out.ops] == ["section.add"]
    assert any("invalid" in n for n in out.notes)
    off = design(blank(), "we should talk about what comes next", router=FakeRouter("off"), catalog=CATALOG)
    assert off.answered_by == "none" and off.ops == [] and off.notes


# --- rendering ---------------------------------------------------------------------------------------
def snapshot():
    doc = say(built(), "set the summary to: Open incidents rose to 42; the P2 backlog is 31.")[0].doc
    return to_report_data(doc, artifact_id="art_r", version=5, generated_at="2026-10-29T10:00:00+00:00", workspace_name="Ops")


def test_snapshot_keeps_the_document_order_and_ends_with_sources():
    data = snapshot()
    assert data.kind == "document" and [s.title for s in data.sections] == ["Summary", "Risks", "EMEA backlog", "Sources"]
    assert data.origin == "art_r v5" and "itsm.incidents" in data.sections[-1].blocks[0].markdown


@pytest.mark.parametrize("fmt", ["md", "html", "pdf", "docx", "pptx"])
def test_every_format_renders_deterministically_from_the_snapshot(fmt):
    pytest.importorskip("fpdf")
    if fmt in ("docx", "pptx"):
        pytest.importorskip("docx" if fmt == "docx" else "pptx")
    data = snapshot()
    a, _, _ = render(data, fmt)
    b, _, _ = render(data, fmt)
    assert a == b and len(a) > 200


def test_snapshot_hash_is_in_each_formats_metadata():
    pytest.importorskip("fpdf")
    data = snapshot()
    h = stable_hash(data.snapshot())
    assert f'name="analystos-report-data" content="{h}"' in render(data, "html")[0].decode()
    assert h in render(data, "md")[0].decode()
    from analystos.reports.pdf import build_pdf

    assert build_pdf(data).keywords == f"analystos-report-data:{h}"
    docx = pytest.importorskip("docx")
    assert docx.Document(io.BytesIO(render(data, "docx")[0])).core_properties.identifier == f"analystos-report-data:{h}"
    pptx = pytest.importorskip("pptx")
    assert pptx.Presentation(io.BytesIO(render(data, "pptx")[0])).core_properties.identifier == f"analystos-report-data:{h}"


def test_document_text_is_escaped_in_html():
    doc = blank()
    doc = apply_ops(doc, parse_ops([{"op": "text.set", "section_id": "summary",
                                     "markdown": "<script>alert('x')</script> [link](javascript:alert(1))"}]), ENV).doc
    html = render(to_report_data(doc, artifact_id="a", version=1, generated_at="2026-10-29T10:00:00+00:00",
                                 workspace_name="W"), "html")[0].decode()
    assert "<script>alert" not in html and "javascript:" not in html


def test_run_report_hash_is_unchanged_by_the_document_fields():
    from tests.report_sample import sample_report

    data = sample_report()
    legacy = data.model_dump()
    legacy.pop("sections")
    legacy.pop("origin")
    assert data.snapshot() == legacy and "Report snapshot" not in render(data, "md")[0].decode()

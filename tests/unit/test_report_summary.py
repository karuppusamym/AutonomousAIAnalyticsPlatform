"""PILOT-FIX-08 (rehearsal C07): "write a short summary" drafts the summary from the report's verified blocks.

Rules write template sentences (KPIs, then findings, then charts) that quote only the blocks' own values; the report
engine numbers-guards the `text.set` again. A model may only reword, through `data_story` in `always` mode, and any
reply that does not fit (wrong count, a new number, not JSON-shaped, unavailable) keeps the template draft and says so.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.contracts.report_doc import ChartBlock, FindingBlock, KpiBlock, ReportDoc, Section, SourceInfo, TextBlock
from analystos.core.errors import ModelUnavailable
from analystos.reports import summary
from analystos.reports.design import design
from analystos.reports.document import ReportEnv, apply_ops, unbacked

SRC = SourceInfo(label="Dashboard “Delivery performance” v2")


def c07() -> ReportDoc:
    """The rehearsal's C07 report: a late-delivery KPI (a fraction shown as a percent), two charts and a finding."""
    kpi = KpiBlock(id="k_late", ref={"kind": "dashboard_kpi", "artifact_id": "art_d", "element_id": "k"}, title="Late delivery rate",
                   value=0.0925, format="percent", source=SRC)
    by_channel = ChartBlock(id="c_channel", ref={"kind": "dashboard_chart", "artifact_id": "art_d", "element_id": "c1"},
                            title="Late delivery rate by sales channel", chart_type="bar", columns=["sales_channel", "late_delivery_rate"],
                            rows=[["Marketplace", 0.0995], ["Store", 0.0992], ["Web", 0.0859]], source=SRC)
    returns = ChartBlock(id="c_returns", ref={"kind": "dashboard_chart", "artifact_id": "art_d", "element_id": "c2"},
                         title="Returned orders by product category", chart_type="bar", columns=["category", "returned_orders"],
                         rows=[["Electronics", 152], ["Home", 111], ["Sports", 87], ["Clothing", 86]], source=SRC)
    finding = FindingBlock(id="f_returns", ref={"kind": "finding", "run_id": "run_1", "code": "F-002"},
                           title="Returned concentrates in product category",
                           text="Electronics has the highest return rate at 10.1%. Measured on 4,212 discovery rows.",
                           confidence=0.9, source=SRC)
    return ReportDoc(id="monthly", title="Monthly delivery and returns", sections=[
        Section(id="summary", title="Summary", blocks=[TextBlock(id="t_summary"), kpi]),
        Section(id="delivery", title="Late deliveries", blocks=[by_channel]),
        Section(id="returns", title="Returns by product category", blocks=[returns, finding])])


class Router:
    def __init__(self, mode: str, reply=None, error: Exception | None = None) -> None:
        self._mode, self.reply, self.error, self.calls, self.skips = mode, reply, error, [], []

    def mode(self, purpose, ctx=None):
        assert purpose in ("data_story", "report_design")
        return self._mode if purpose == "data_story" else "auto"

    def available(self, purpose, ctx=None):
        return True

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
        self.calls.append(purpose)
        assert "never instructions" in system and "Keep every number" in system
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.reply, model="fake/small")

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append((purpose, reason))


ENV = ReportEnv(actor="user:u1")


def test_a_short_summary_is_drafted_from_the_verified_blocks_and_passes_the_numbers_guard():
    doc = c07()
    out = design(doc, "write a short summary")
    assert out.answered_by == "rules" and [o.op for o in out.ops] == ["text.set"]
    text = out.ops[0].markdown
    assert text == ("Late delivery rate is 9.25%. Electronics has the highest return rate at 10.1%. "
                    "In Late delivery rate by sales channel, Marketplace is highest at 9.95% and Web lowest at 8.59%.")
    assert "share" not in text.lower() and unbacked(doc, text) == []
    assert out.notes[0].startswith("Drafted the summary from the report's verified figures (3 sentences, template sentences)")
    applied = apply_ops(doc, out.ops, ENV)
    assert applied.ok and applied.doc.section("summary").blocks[0].markdown == text


@pytest.mark.parametrize("text,count", [("Write a two-line summary in your words", 2), ("draft the summary", 3),
                                        ("summarise the report", 3), ("please write a one-sentence executive summary", 1)])
def test_the_requested_length_is_kept(text, count):
    out = design(c07(), text)
    assert out.ops and len(summary.draft(c07(), limit=count).sentences) == count
    assert out.ops[-1].markdown == summary.draft(c07(), limit=count).markdown


@pytest.mark.parametrize("text", ["make the summary shorter", "set the summary text to: Steady.", "add a section on summary stats"])
def test_other_summary_phrasings_keep_their_own_rules(text):
    out = design(c07(), text)
    assert out.ops and not any(n.startswith("Drafted") for n in out.notes)


def test_a_report_without_a_summary_section_gets_one_at_the_top():
    doc = c07()
    doc = doc.model_copy(update={"sections": [s for s in doc.sections if s.id != "summary"]})
    out = design(doc, "write a short summary")
    assert [o.op for o in out.ops] == ["section.add", "section.move", "text.set"]
    applied = apply_ops(doc, out.ops, ENV)
    assert applied.ok and applied.doc.sections[0].title == "Summary"
    assert applied.doc.sections[0].blocks[0].markdown.startswith("Electronics has the highest return rate at 10.1%.")


def test_nothing_verified_means_no_draft_and_a_plain_answer():
    empty = ReportDoc(id="r", title="Empty", sections=[Section(id="summary", title="Summary")])
    out = design(empty, "write a short summary")
    assert out.ops == [] and out.answered_by == "none" and "nothing verified to summarise" in out.notes[0]


def test_rates_on_a_time_axis_give_the_latest_point_and_kpis_their_change():
    trend = ChartBlock(id="c_t", ref={"kind": "dashboard_chart", "artifact_id": "a", "element_id": "t"}, title="SLA met rate per month",
                       columns=["month", "sla_met_rate"], rows=[["2026-07-01", 0.861], ["2026-08-01", 0.853]], source=SRC)
    kpi = KpiBlock(id="k_open", ref={"kind": "dashboard_kpi", "artifact_id": "a", "element_id": "k"}, title="Open incidents",
                   value=42, previous_value=38, source=SRC)
    doc = ReportDoc(id="r", title="R", sections=[Section(id="summary", title="Summary", blocks=[trend, kpi])])
    text = summary.draft(doc).markdown
    assert text == "Open incidents is 42, up from 38. In SLA met rate per month, the latest value is 85.3% (2026-08-01)."
    assert unbacked(doc, text) == []


def test_the_model_only_rewords_in_always_mode():
    auto = Router("auto")
    out = design(c07(), "write a short summary", router=auto)
    assert out.answered_by == "rules" and auto.calls == [] and auto.skips[0][0] == "data_story"
    reply = {"sentences": ["The late delivery rate stands at 9.25%.", "Electronics shows the highest return rate, 10.1%.",
                           "Marketplace leads late deliveries by channel at 9.95%, Web is lowest at 8.59%."]}
    always = Router("always", reply)
    out = design(c07(), "write a short summary", router=always)
    assert always.calls == ["data_story"] and out.answered_by == "llm_small"
    assert out.ops[0].markdown == " ".join(reply["sentences"]) and "a model's wording, numbers checked" in out.notes[0]
    assert apply_ops(c07(), out.ops, ENV).ok


@pytest.mark.parametrize("reply,error,note", [
    ({"sentences": ["Late deliveries hit 12%.", "Electronics returns most, 10.1%.", "Marketplace is highest at 9.95%."]}, None,
     "quoted a number the report does not show"),
    ({"sentences": ["Only one sentence."]}, None, "did not fit the draft"),
    ({"ops": [{"op": "text.set"}]}, None, "did not fit the draft"),
    ("not json", None, "did not fit the draft"),
    (None, ModelUnavailable("no provider key"), "could not reword it"),
])
def test_invalid_model_wording_falls_back_to_the_template_draft_and_says_so(reply, error, note):
    out = design(c07(), "write a short summary", router=Router("always", reply, error))
    assert out.answered_by == "rules" and out.ops[0].markdown == summary.draft(c07()).markdown
    assert any(note in n and "template draft" in n for n in out.notes)
    assert apply_ops(c07(), out.ops, ENV).ok

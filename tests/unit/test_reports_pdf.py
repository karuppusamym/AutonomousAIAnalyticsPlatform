"""PDF report rendering with fpdf2 and the bundled DejaVu font (text is read back with pypdf)."""
from __future__ import annotations

import io
import re
from functools import cache

import pytest
from pypdf import PdfReader
from tests.report_sample import sample_report

from analystos.contracts.reports import ReportData, ReportInsight
from analystos.reports import render
from analystos.reports.pdf import FONT_DIR, build_pdf, font_coverage, pdf_text, render_pdf


def _pages(pdf: bytes) -> list[str]:
    return [p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages]


def _text(pdf: bytes) -> str:
    """All pages, whitespace collapsed (extraction splits wrapped lines and widens some spaces)."""
    return re.sub(r"\s+", " ", " ".join(_pages(pdf)))


@cache
def _doc(kind: str) -> tuple[int, bytes]:
    doc = build_pdf(sample_report(kind))
    return doc.pages_count, bytes(doc.output())


@pytest.mark.parametrize("kind", ["executive", "operational", "statistical", "exception", "weekly_summary"])
def test_pdf_renders_each_kind(kind):
    n, b = _doc(kind)
    assert b.startswith(b"%PDF-") and b.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", b)) == n >= 2
    txt = _text(b)
    assert "ServiceNow SLA analysis — week 38" in txt  # the em dash prints as written
    assert re.findall(r"Page (\d+)/(\d+)", txt) == [(str(i), str(n)) for i in range(1, n + 1)]  # footer on every page
    assert txt.count("run run_0f3a9c2e") == n
    assert "not proof of causation" in txt
    assert "/Image" in b.decode("latin-1")  # chart PNGs embedded


def test_pdf_sections_follow_kind():
    ex = _text(_doc("executive")[1])
    assert "Top verified findings" in ex and "Data quality issues" not in ex
    st = _text(_doc("statistical")[1])
    assert "Evidence queries" in st and "SELECT priority, AVG" in st
    assert st.index("Hypotheses and conclusions") < st.index("Findings")
    exc = _text(_doc("exception")[1])
    assert exc.index("Alerts") < exc.index("Summary")


def test_bundled_unicode_font_is_embedded_with_its_licence():
    b = _doc("executive")[1]
    assert re.search(rb"/BaseFont /[A-Z]{6}\+DejaVuSans", b) and b"/FontFile2" in b and b"/ToUnicode" in b
    assert b"/Helvetica" not in b and b"/Courier" not in b  # no latin-1-only core fonts left
    assert "Bitstream" in (FONT_DIR / "LICENSE_DEJAVU").read_text(encoding="utf-8")


def test_non_latin_text_renders_as_written():
    d = ReportData(title="Отчёт Ελληνικά Zürich Łódź Ærø", workspace_name="Škoda ws", objective="χ² ≥ 3.84 → reject; √n … “ok” — €",
                   run_id="r", generated_at="2026-09-21T08:30:00+02:00",
                   insights=[ReportInsight(code="Ж-1", title="Время решения выросло", finding="Δ = +12 %", confidence=0.5,
                                           verified=True, caveats=["ψ ≠ ω"])])
    txt = _text(render_pdf(d))
    for s in ("Отчёт Ελληνικά Zürich Łódź Ærø", "χ² ≥ 3.84 → reject; √n … “ok” — €", "Ж-1", "Время решения выросло",
              "Δ = +12 %", "ψ ≠ ω", "Škoda ws"):
        assert s in txt, s


def test_uncovered_characters_fall_back_instead_of_blank_boxes():
    cov = font_coverage()
    assert all(ord(c) in cov for c in "ЖΩЁ→≥…€日한")
    assert ord("🚀") not in cov and ord("ह") not in cov  # Devanagari: no bundled face (Tamil has one, S08)
    assert pdf_text("a → b 日本語 🚀\tz\x07​") == "a → b 日本語 ?    z"
    assert pdf_text("a → b", "DejaVuMono") == "a → b"
    b = render_pdf(ReportData(title="報告 🚀", workspace_name="ws", objective="Ω\x00", run_id="r",
                              generated_at="2026-09-21T08:30:00+02:00"))
    assert b.startswith(b"%PDF-") and "報告 ?" in _text(b)


def test_cjk_falls_back_to_the_bundled_noto_font():
    d = ReportData(title="Отчёт 報告 → ≥ …", workspace_name="東京 Zürich", objective="한국어 中文 日本語 Ω",
                   run_id="r", generated_at="2026-09-21T08:30:00+02:00",
                   insights=[ReportInsight(code="Ж", title="χ²", finding="√ 中文 日本語 한국어", confidence=0.5,
                                           verified=True, caveats=["€"])])
    b = render_pdf(d)
    txt = _text(b)
    for phrase in ("Отчёт 報告 → ≥ …", "東京 Zürich", "한국어 中文 日本語 Ω", "√ 中文 日本語 한국어"):
        assert phrase in txt, phrase
    assert re.search(rb"/BaseFont /[A-Z]{6}\+NotoSansCJK", b) and re.search(rb"/BaseFont /[A-Z]{6}\+DejaVuSans", b)
    assert "SIL OPEN FONT LICENSE" in (FONT_DIR / "OFL.txt").read_text(encoding="utf-8").upper()
    assert render_pdf(d) == b  # still deterministic with the fallback font


def test_pdf_is_deterministic_and_dispatch():
    body, mime, ext = render(sample_report("operational"), "pdf")
    assert body == _doc("operational")[1]
    assert mime == "application/pdf" and ext == "pdf" and body.startswith(b"%PDF")
    assert b"D:20260921083000" in body  # creation date pinned to generated_at

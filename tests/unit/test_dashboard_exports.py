"""S08-T6: export renderers carry the watermark; PDF text in Tamil, CJK and Arabic prints with bundled glyphs."""
from __future__ import annotations

import io
import re
from types import SimpleNamespace

from openpyxl import load_workbook
from pypdf import PdfReader
from tests.unit.test_dashboard_dsl import sample

from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.dashboards.exports import needs_approval, render_csv, render_pdf, render_xlsx, tables, watermark
from analystos.reports.pdf import FONT_DIR, font_coverage, pdf_text

TAMIL, CJK, ARABIC = "தமிழ் அறிக்கை", "数据分析报告", "تقرير البيانات"


def doc(title: str = "Operations overview") -> DashboardDoc:
    raw = sample()
    raw["title"] = title
    raw["charts"][0]["preview"]["rows"].append(["2026-10-19", 99, f"{TAMIL} / {CJK} / {ARABIC}"])
    return DashboardDoc.model_validate(raw)


def mark(d: DashboardDoc) -> str:
    user = SimpleNamespace(email="analyst@analystos.local")
    return watermark(user, "2026-10-27T10:00:00+00:00", d, SimpleNamespace(id="art_1", version=3))


def test_watermark_names_requester_time_and_classification():
    m = mark(doc())
    assert "analyst@analystos.local" in m and "2026-10-27T10:00:00+00:00" in m and "INTERNAL" in m and "art_1 v3" in m


def test_csv_and_xlsx_carry_the_watermark_and_the_bounded_previews():
    d = doc()
    text = render_csv(d, mark(d)).decode("utf-8-sig")
    assert text.splitlines()[0].startswith("# Exported by analyst@analystos.local")
    assert "Incidents per week" in text and TAMIL in text
    wb = load_workbook(io.BytesIO(render_xlsx(d, mark(d))))
    ws = wb.active
    assert ws["A1"].value.startswith("Exported by analyst@analystos.local")
    assert "analyst@analystos.local" in ws.oddHeader.center.text and "analyst@analystos.local" in ws.oddFooter.center.text
    assert [t for t, _, _ in tables(d)] == ["KPIs", "Incidents per week", "Incidents by priority"]


def test_pdf_unicode():
    """Tamil, CJK and Arabic glyphs render from bundled fonts (no `?` fallbacks, fonts embedded)."""
    cov = font_coverage()
    for text in (TAMIL, CJK, ARABIC):
        assert all(ord(c) in cov for c in text if not c.isspace()), text
        assert pdf_text(text) == text
    d = doc(f"{TAMIL} {CJK} {ARABIC}")
    pdf = render_pdf(d, mark(d))
    assert pdf.startswith(b"%PDF-")
    for face in (rb"NotoSansTamil", rb"NotoSansCJK", rb"DejaVuSans"):
        assert re.search(rb"/BaseFont /[A-Z]{6}\+" + face, pdf), face
    text = re.sub(r"\s+", "", " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages))
    assert CJK in text and "analyst@analystos.local" in text
    assert "?" not in pdf_text(f"{TAMIL}{CJK}{ARABIC}")
    assert "SIL OPEN FONT LICENSE" in (FONT_DIR / "OFL.txt").read_text(encoding="utf-8").upper()


def test_classification_policy():
    assert not needs_approval("internal", "confidential")
    assert needs_approval("confidential", "confidential") and needs_approval("restricted", "confidential")
    assert needs_approval("internal", "internal") and not needs_approval("public", "internal")

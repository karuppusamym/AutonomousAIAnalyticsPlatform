"""PDF rendering of ReportData with fpdf2 and the bundled DejaVu and Noto CJK fonts (N-6-font; no network).

DejaVu Sans (Bitstream Vera licence, `fonts/LICENSE_DEJAVU`) is embedded as a subset, so Latin,
Latin-extended, Greek, Cyrillic, typography (→ ≥ … “ ” —) and most symbols print as written. Noto Sans
CJK SC (SIL OFL 1.1, `fonts/OFL.txt`) is the fallback for what DejaVu lacks, so Chinese, Japanese and
Korean print too. A character neither font has (Indic scripts, emoji) passes through `pdf_text`, which
maps it to an ASCII look-alike, folds accents or prints '?', so no text vanishes as an empty glyph box.
The creation date comes from ReportData.generated_at, so the same input yields the same bytes.
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.fonts import FontFace

from analystos.contracts.reports import ReportData, ReportInsight
from analystos.reports import _common as C
from analystos.reports.charts import render_chart_png

_MAP = {
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "↑": "^", "↓": "v", "▲": "^", "▼": "v",
    "≥": ">=", "≤": "<=", "≠": "!=", "≈": "~", "±": "+/-", "×": "x", "−": "-", "–": "-", "—": "-",
    "…": "...", "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "„": '"', "•": "-", "·": "-",
    "€": "EUR", "™": "(TM)", "✓": "v", "✔": "v", "✗": "x", "✘": "x", " ": " ", "​": "",
    "\t": "    ",
}
_ALWAYS = {"\t": "    ", "​": ""}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

FONT_DIR = Path(__file__).parent / "fonts"
FONT, MONO = "DejaVu", "DejaVuMono"
FONT_FILES = {(FONT, ""): "DejaVuSans.ttf", (FONT, "B"): "DejaVuSans-Bold.ttf", (FONT, "I"): "DejaVuSans-Oblique.ttf",
              (MONO, ""): "DejaVuSansMono.ttf"}
CJK, CJK_FILE = "NotoSansCJK", "NotoSansCJKsc-VF.ttf"  # fallback for every style (fpdf2 fakes no bold/italic)
TAMIL, TAMIL_FILE = "NotoSansTamil", "NotoSansTamil-Regular.ttf"  # S08-T6: Tamil
ARABIC, ARABIC_FILE = "NotoSansArabic", "NotoSansArabic-Regular.ttf"  # S08-T6: DejaVu has Arabic in regular only
FALLBACKS = ((CJK, CJK_FILE), (TAMIL, TAMIL_FILE), (ARABIC, ARABIC_FILE))


def shaping_available() -> bool:
    """Complex-script shaping (Arabic joining and right-to-left, Tamil vowel signs) needs HarfBuzz
    (`uharfbuzz`, optional). Without it every glyph still prints, unjoined."""
    try:
        import uharfbuzz  # noqa: F401
    except ImportError:
        return False
    return True


def add_report_fonts(pdf: FPDF) -> None:
    """Register DejaVu plus the CJK, Tamil and Arabic fallbacks on `pdf`; shape text when HarfBuzz is installed."""
    for (family, style), file in FONT_FILES.items():
        pdf.add_font(family, style, FONT_DIR / file)
    for family, file in FALLBACKS:
        pdf.add_font(family, "", FONT_DIR / file)
    pdf.set_fallback_fonts([f for f, _ in FALLBACKS], exact_match=False)
    if shaping_available():
        pdf.set_text_shaping(True)


@cache
def font_coverage(family: str = FONT) -> frozenset[int]:
    """Code points every style of `family` has a glyph for (a style missing one would print a blank box)."""
    maps = [set(TTFont(FONT_DIR / f, lazy=True).getBestCmap()) for (fam, _), f in FONT_FILES.items() if fam == family]
    return frozenset(set.intersection(*maps)) | _fallback_coverage()


@cache
def _fallback_coverage() -> frozenset[int]:
    covered: set[int] = set()
    for _, file in FALLBACKS:
        font = TTFont(FONT_DIR / file, lazy=True)
        try:
            covered |= set(font.getBestCmap())
        finally:
            font.close()
    return frozenset(covered)


def pdf_text(v: object, family: str = FONT) -> str:
    """Text the bundled font can draw: covered characters as written, the rest mapped or folded; never raises."""
    s = "" if v is None else str(v)
    s = _CTRL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))
    covered = font_coverage(family)
    out = []
    for ch in s:
        if ch in _ALWAYS:
            out.append(_ALWAYS[ch])
        elif ord(ch) in covered or ch == "\n":
            out.append(ch)
        elif ch in _MAP:
            out.append(_MAP[ch])
        else:
            folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append(folded or "?")
    return "".join(out)


ACCENT = (47, 93, 138)
TEXT = (31, 35, 40)
MUTED = (89, 99, 110)
LINE = (209, 217, 224)
SOFT = (246, 248, 250)
SEV = {"critical": (207, 34, 46), "warning": (154, 103, 0), "info": ACCENT}
CHART_W, CHART_H = 900, 420


class _ReportPDF(FPDF):
    def __init__(self, data: ReportData) -> None:
        super().__init__(orientation="portrait", unit="mm", format="A4")
        self.data = data
        add_report_fonts(self)
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(auto=True, margin=20)
        self.alias_nb_pages()

    def footer(self) -> None:
        self.set_y(-14)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_font(FONT, "", 7)
        self.set_text_color(*MUTED)
        left = pdf_text(f"{self.data.title} | run {self.data.run_id} | generated {C.generated_label(self.data)}")
        if self.get_string_width(left) > self.epw - 30:
            while left and self.get_string_width(left + "...") > self.epw - 30:
                left = left[:-1]
            left += "..."
        self.cell(self.epw - 30, 6, left, align="L")
        self.cell(30, 6, f"Page {self.page_no()}/{{nb}}", align="R")


# ---------------------------------------------------------------------------------------------
def _h1(pdf: _ReportPDF, text: str) -> None:
    pdf.set_font(FONT, "B", 20)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 9, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _h2(pdf: _ReportPDF, text: str) -> None:
    if pdf.get_y() > pdf.page_break_trigger - 30:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_font(FONT, "B", 13)
    pdf.set_text_color(*ACCENT)
    pdf.multi_cell(0, 7, pdf_text(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*LINE)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)


def _h3(pdf: _ReportPDF, text: str) -> None:
    if pdf.get_y() > pdf.page_break_trigger - 20:
        pdf.add_page()
    pdf.set_font(FONT, "B", 10.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 5.5, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _p(pdf: _ReportPDF, text: str, *, size: float = 9.5, style: str = "", color=TEXT, indent: float = 0) -> None:
    pdf.set_font(FONT, style, size)
    pdf.set_text_color(*color)
    if indent:
        pdf.set_x(pdf.l_margin + indent)
    pdf.multi_cell(pdf.epw - indent, size * 0.5, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _bullets(pdf: _ReportPDF, items: list[str], *, size: float = 9.5, indent: float = 3) -> None:
    for it in items:
        _p(pdf, f"- {it}", size=size, indent=indent)


def _code(pdf: _ReportPDF, code: str) -> None:
    pdf.set_font(MONO, "", 7.5)
    pdf.set_text_color(*TEXT)
    pdf.set_fill_color(*SOFT)
    pdf.set_draw_color(*LINE)
    pdf.multi_cell(0, 3.6, pdf_text(code.rstrip(), MONO), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)


def _table(pdf: _ReportPDF, headers: list[str], rows: list[list[str]], widths: tuple[float, ...] | None = None,
           size: float = 8.5) -> None:
    if not rows:
        return
    pdf.set_font(FONT, "", size)
    pdf.set_text_color(*TEXT)
    pdf.set_draw_color(*LINE)
    pdf.set_fill_color(255, 255, 255)
    head = FontFace(emphasis="BOLD", fill_color=SOFT, color=TEXT)
    with pdf.table(col_widths=widths, text_align="LEFT", line_height=size * 0.5, first_row_as_headings=True,
                   headings_style=head, borders_layout="ALL", padding=1.2) as t:
        r = t.row()
        for h in headers:
            r.cell(pdf_text(h))
        for row in rows:
            r = t.row()
            for c in row:
                r.cell(pdf_text(c))
    pdf.ln(2)


def _insight(pdf: _ReportPDF, i: ReportInsight, with_evidence: bool) -> None:
    tag = f" [{i.change.upper()}]" if i.change else ""
    _h3(pdf, f"{i.code}  {i.title}{tag}")
    _p(pdf, f"{i.status_label().capitalize()} - confidence {C.confidence_pct(i.confidence)}"
       + (f" - VOID: {i.void_reason}; needs re-verification" if i.void_reason else ""), size=8, color=MUTED)
    _p(pdf, i.finding)
    if i.caveats:
        _p(pdf, "Caveats:", size=8.5, style="B")
        _bullets(pdf, i.caveats, size=8.5)
    if i.evidence_queries:
        if with_evidence:
            for q in i.evidence_queries:
                h = f", hash {q.result_hash}" if q.result_hash else ""
                _p(pdf, f"Evidence query {q.id} ({q.row_count} rows{h})", size=8, color=MUTED)
                _code(pdf, q.sql)
        else:
            _p(pdf, "Evidence: " + ", ".join(q.id for q in i.evidence_queries), size=8, color=MUTED)
    pdf.ln(2)


def _section(pdf: _ReportPDF, key: str, d: ReportData) -> None:
    kind = d.kind
    _h2(pdf, C.section_title(key, kind))
    if key == "summary":
        if not d.summary_markdown.strip():
            _p(pdf, C.auto_summary(d))
        for b in C.parse_markdown(d.summary_markdown):
            if b.kind == "para":
                _p(pdf, C.inline_plain(b.items[0]))
                pdf.ln(1.5)
            else:
                _bullets(pdf, [C.inline_plain(it) for it in b.items])
                pdf.ln(1.5)
    elif key == "kpis":
        if not d.metrics:
            _p(pdf, "No metrics in this run.")
            return
        rows = []
        for m in d.metrics:
            ch = C.metric_change(m)
            rows.append([m.display_name, C.format_value(m.value, m.format), C.format_value(m.previous_value, m.format),
                         f"{ch.arrow} {ch.text}".strip(), m.definition])
        _table(pdf, ["Metric", "Value", "Previous", "Change", "Definition"], rows, (30, 18, 18, 26, 58))
    elif key == "findings":
        items = C.shown_findings(d)
        if not items:
            _p(pdf, "No verified findings in this run." if kind == "executive" else "No findings in this run.")
        for i in items:
            _insight(pdf, i, kind == "statistical")
    elif key == "alerts":
        if not d.alerts:
            _p(pdf, "No alerts.")
        for a in C.sorted_alerts(d):
            pdf.set_font(FONT, "B", 9.5)
            pdf.set_text_color(*SEV.get(a.severity, MUTED))
            pdf.multi_cell(0, 5, pdf_text(f"{a.severity.upper()}  {a.title}"), new_x="LMARGIN", new_y="NEXT")
            _p(pdf, a.message + (f" (metric: {a.metric})" if a.metric else ""), indent=3)
            pdf.ln(1)
    elif key == "changes":
        g = C.change_groups(d)
        if not any(g.values()):
            _p(pdf, "No comparison with a previous run is available.")
            return
        for k, lab in (("new", "New"), ("changed", "Changed"), ("persisting", "Persisting"), ("resolved", "Resolved")):
            _p(pdf, f"{lab} ({len(g[k])})", style="B")
            _bullets(pdf, [f"{i.code}  {i.title}" for i in g[k]] or ["none"])
            pdf.ln(1)
    elif key == "quality":
        if not d.quality_issues:
            _p(pdf, "No data quality issues recorded.")
            return
        _table(pdf, ["Severity", "Asset", "Column", "Issue"],
               [[str(q.get("severity", "")), str(q.get("asset", "")), str(q.get("column", "") or ""), str(q.get("message", ""))]
                for q in C.sorted_quality(d)], (18, 36, 30, 66))
    elif key == "hypotheses":
        if not d.hypotheses:
            _p(pdf, "No hypotheses were tested.")
            return
        _table(pdf, ["Code", "Hypothesis", "Status", "Conclusion"],
               [[str(h.get("code", "")), str(h.get("statement", "")), str(h.get("status", "")), str(h.get("conclusion", "") or "")]
                for h in d.hypotheses], (16, 58, 24, 52))
    elif key == "evidence":
        qs = [(i.code, q) for i in d.insights for q in i.evidence_queries]
        if not qs:
            _p(pdf, "No evidence queries recorded.")
            return
        _table(pdf, ["Finding", "Query", "Rows", "Result hash"], [[c, q.id, str(q.row_count), q.result_hash or ""] for c, q in qs],
               (22, 40, 16, 72))
    elif key == "charts":
        if not d.charts:
            _p(pdf, "No charts in this run.")
        for ch in d.charts:
            rows = C.valid_rows(ch)
            _h3(pdf, ch.title)
            if not rows:
                _p(pdf, "No data.", size=8.5, color=MUTED)
                continue
            _table(pdf, [str(c) for c in ch.columns], [[C.cell_text(v) for v in r] for r in rows[: C.MAX_TABLE_ROWS]], size=7.5)
            if len(rows) > C.MAX_TABLE_ROWS:
                _p(pdf, f"Showing {C.MAX_TABLE_ROWS} of {len(rows)} rows.", size=8, color=MUTED)


def _charts(pdf: _ReportPDF, d: ReportData) -> None:
    images = [(ch, png) for ch in d.charts if (png := render_chart_png(ch, width_px=CHART_W, height_px=CHART_H))]
    if not images:
        return
    h = pdf.epw * CHART_H / CHART_W
    if pdf.get_y() + h + 16 > pdf.page_break_trigger:
        pdf.add_page()
    _h2(pdf, "Charts")
    for _, png in images:
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
        pdf.image(io.BytesIO(png), x=pdf.l_margin, w=pdf.epw, h=h)
        pdf.ln(3)


def _provenance(pdf: _ReportPDF, d: ReportData) -> None:
    _h2(pdf, C.SECTION_TITLES["provenance"])
    _p(pdf, f"Run: {d.run_id}", size=9)
    _p(pdf, f"Generated: {C.generated_label(d)}", size=9)
    if d.lineage_note:
        _p(pdf, f"Lineage: {d.lineage_note}", size=9)
    if d.caveats:
        _p(pdf, "Caveats:", size=9, style="B")
        _bullets(pdf, d.caveats, size=9)
    pdf.ln(2)
    _p(pdf, C.CAUSATION_NOTE, size=9, style="I")


def build_pdf(data: ReportData) -> FPDF:
    """The laid-out document (exposed so callers/tests can inspect page count before output)."""
    d = data
    pdf = _ReportPDF(d)
    created = C.parse_ts(d.generated_at) or datetime(1970, 1, 1, tzinfo=UTC)
    pdf.set_creation_date(created)
    pdf.set_title(pdf_text(d.title))
    pdf.set_subject(pdf_text(C.KIND_LABELS.get(d.kind, d.kind)))
    pdf.set_author(pdf_text(d.workspace_name))
    pdf.set_creator("AnalystOS")
    pdf.add_page()

    pdf.set_fill_color(*ACCENT)
    pdf.rect(pdf.l_margin, pdf.t_margin, pdf.epw, 1.6, style="F")
    pdf.ln(5)
    _p(pdf, C.KIND_LABELS.get(d.kind, d.kind).upper(), size=8.5, style="B", color=ACCENT)
    _h1(pdf, d.title)
    meta = f"{d.workspace_name}  |  generated {C.generated_label(d)}" + (f"  |  period {d.period}" if d.period else "")
    _p(pdf, meta, size=9, color=MUTED)
    pdf.ln(1)
    _p(pdf, f"Objective: {d.objective}", size=9.5)
    pdf.ln(2)

    for key in C.sections_for(d.kind):
        _section(pdf, key, d)
    _charts(pdf, d)
    _provenance(pdf, d)
    return pdf


def render_pdf(data: ReportData) -> bytes:
    return bytes(build_pdf(data).output())

"""Shared, format-neutral helpers for the report renderers.

Everything here is pure and deterministic: value formatting, KPI deltas, section order per report
kind, a tiny safe-markdown parser (paragraphs, bullets, bold, italic, code, http/https links) and
chart-data shaping used by both the PNG and the Excel renderers.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from analystos.contracts.reports import ReportChart, ReportData, ReportInsight, ReportMetric, ReportSection

CAUSATION_NOTE = (
    "Findings are associations in historical data, verified by re-running evidence queries; "
    "they are not proof of causation."
)

# Section keys, in render order, per report kind. The provenance footer is always appended.
SECTION_ORDER: dict[str, list[str]] = {
    "executive": ["summary", "kpis", "findings", "alerts", "changes"],
    "operational": ["summary", "kpis", "alerts", "findings", "changes", "quality", "hypotheses", "charts"],
    "statistical": ["summary", "hypotheses", "findings", "evidence", "kpis", "quality"],
    "exception": ["alerts", "summary", "kpis", "findings", "changes"],
    "weekly_summary": ["changes", "kpis", "summary", "findings", "alerts"],
    "document": [],  # CONV-04: a report document renders its own sections (ReportData.sections), in its order
}

SECTION_TITLES: dict[str, str] = {
    "summary": "Summary",
    "kpis": "Key metrics",
    "findings": "Findings",
    "alerts": "Alerts",
    "changes": "What changed",
    "quality": "Data quality issues",
    "hypotheses": "Hypotheses",
    "charts": "Chart data",
    "evidence": "Evidence queries",
    "provenance": "Provenance",
}

KIND_LABELS: dict[str, str] = {
    "executive": "Executive report",
    "operational": "Operational report",
    "statistical": "Statistical report",
    "exception": "Exception report",
    "weekly_summary": "Weekly summary",
    "document": "Report",
}

EXEC_TOP_FINDINGS = 5
MAX_TABLE_ROWS = 50  # chart data tables in md/html/pdf
SEVERITY_RANK = {"critical": 0, "error": 0, "high": 0, "warning": 1, "medium": 1, "info": 2, "low": 2}


def sections_for(kind: str) -> list[str]:
    return SECTION_ORDER.get(kind, SECTION_ORDER["executive"])


def origin_line(data: ReportData) -> tuple[str, str]:
    """(label, value) for the provenance block: the run a report was built from, or the report document."""
    return ("Report document", data.origin) if data.origin else ("Run", data.run_id)


def section_items(section: ReportSection) -> list[tuple[str, Any]]:
    """A document section as render items, in order: ("text", markdown), ("note", text), ("chart", (chart,
    source)), ("finding", (insight, source)) and ("kpis", [(metric, source)]) with consecutive KPI blocks
    grouped into one table."""
    out: list[tuple[str, Any]] = []
    for b in section.blocks:
        if b.kind == "kpi" and b.metric is not None:
            if out and out[-1][0] == "kpis":
                out[-1][1].append((b.metric, b.source))
            else:
                out.append(("kpis", [(b.metric, b.source)]))
        elif b.kind == "chart" and b.chart is not None:
            out.append(("chart", (b.chart, b.source)))
        elif b.kind == "finding" and b.insight is not None:
            out.append(("finding", (b.insight, b.source)))
        elif b.kind == "note":
            out.append(("note", b.markdown))
        elif b.kind == "text" and b.markdown.strip():
            out.append(("text", b.markdown))
    return out


def kpi_row(m: ReportMetric) -> list[str]:
    ch = metric_change(m)
    return [m.display_name, format_value(m.value, m.format), format_value(m.previous_value, m.format),
            f"{ch.arrow} {ch.text}".strip()]


DOC_KPI_HEADERS = ["Metric", "Value", "Previous", "Change"]


def section_title(key: str, kind: str) -> str:
    if key == "findings" and kind == "executive":
        return "Top verified findings"
    if key == "hypotheses" and kind == "statistical":
        return "Hypotheses and conclusions"
    return SECTION_TITLES[key]


# ---------------------------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------------------------
def to_number(v: Any) -> float | None:
    """Finite float for numeric-ish values (bool, int, float, numeric strings); else None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float):
        f = float(v)
    elif isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s or len(s) > 40:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def fmt_num(f: float, decimals: int = 2) -> str:
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f):,}"
    return f"{f:,.{decimals}f}"


def format_value(value: Any, fmt: str) -> str:
    """Human display of a metric value. `percent` values are fractions (0.873 -> 87.3%), matching the
    d3 `.1%` format the BI publisher uses."""
    if value is None:
        return "n/a"
    f = to_number(value)
    if f is None:
        return str(value)
    if fmt == "percent":
        return f"{f * 100:.1f}%"
    if fmt == "hours":
        return f"{f:,.1f} h"
    if fmt == "currency":
        return f"${f:,.2f}"
    return fmt_num(f)


@dataclass(frozen=True)
class Change:
    delta: float | None  # value - previous, in the metric's own units (fraction for percent)
    arrow: str  # "↑" | "↓" | "→" | ""
    text: str  # e.g. "+2.1 pp", "-3.4 h", "n/a"


def metric_change(m: ReportMetric) -> Change:
    cur, prev = to_number(m.value), to_number(m.previous_value)
    if cur is None or prev is None:
        return Change(None, "", "n/a")
    d = cur - prev
    if abs(d) < 1e-12:
        d = 0.0
    arrow = "↑" if d > 0 else "↓" if d < 0 else "→"
    if m.format == "percent":
        text = f"{d * 100:+.1f} pp"
    elif m.format == "hours":
        text = f"{d:+,.1f} h"
    elif m.format == "currency":
        text = f"{'+' if d >= 0 else '-'}${abs(d):,.2f}"
    else:
        text = f"{d:+,.2f}" if d != int(d) else f"{int(d):+,}"
    if prev != 0 and m.format != "percent":
        text += f" ({d / abs(prev) * 100:+.1f}%)"
    return Change(d, arrow, text)


def confidence_pct(c: float) -> str:
    f = to_number(c)
    if f is None:
        return "n/a"
    if f <= 1.0:
        f *= 100
    return f"{f:.0f}%"


def top_verified(insights: list[ReportInsight], n: int = EXEC_TOP_FINDINGS) -> list[ReportInsight]:
    ver = [i for i in insights if i.verified]
    return sorted(ver, key=lambda i: (-(to_number(i.confidence) or 0.0), i.code))[:n]


def shown_findings(data: ReportData) -> list[ReportInsight]:
    """The findings a report lists. Executive: the top verified ones plus every VOID one, labelled with its
    cause (P7-01: a void is shown, never hidden, and never counted as verified)."""
    if data.kind != "executive":
        return list(data.insights)
    return top_verified(data.insights) + [i for i in data.insights if i.void_reason]


def sorted_alerts(data: ReportData) -> list:
    return sorted(data.alerts, key=lambda a: SEVERITY_RANK.get(a.severity, 3))


def sorted_quality(data: ReportData) -> list[dict[str, Any]]:
    return sorted(data.quality_issues, key=lambda q: SEVERITY_RANK.get(str(q.get("severity", "")).lower(), 3))


def change_groups(data: ReportData) -> dict[str, list[ReportInsight]]:
    return {
        "new": [i for i in data.insights if i.change == "new"],
        "changed": [i for i in data.insights if i.change == "changed"],
        "persisting": [i for i in data.insights if i.change == "persisting"],
        "resolved": list(data.resolved_insights),
    }


def generated_label(data: ReportData) -> str:
    dt = parse_ts(data.generated_at)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else data.generated_at


def parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if not math.isfinite(v):
            return str(v)
        return fmt_num(v) if v == int(v) else f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def auto_summary(data: ReportData) -> str:
    ver = sum(1 for i in data.insights if i.verified)
    crit = sum(1 for a in data.alerts if a.severity == "critical")
    parts = [f"{len(data.insights)} findings ({ver} verified)", f"{len(data.metrics)} metrics"]
    if data.alerts:
        parts.append(f"{len(data.alerts)} alerts ({crit} critical)")
    return "This run produced " + ", ".join(parts) + "."


# ---------------------------------------------------------------------------------------------
# safe markdown subset
# ---------------------------------------------------------------------------------------------
@dataclass
class Inline:
    kind: str  # text | bold | italic | code | link
    text: str = ""
    children: list[Inline] = field(default_factory=list)
    href: str = ""


@dataclass
class Block:
    kind: str  # para | list
    items: list[list[Inline]] = field(default_factory=list)  # para: one item; list: one per bullet


_INLINE_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*(?=\S)(?:.+?)(?<=\S)\*\*)"
    r"|(?P<link>\[[^\]\n]{1,300}\]\((?:[^()\s]|\([^()\s]{0,200}\)){1,2000}\))"
    r"|(?P<ital>(?<![\w*])_(?=\S)(?:[^_\n]+?)(?<=\S)_(?!\w)|(?<![\w*])\*(?=[^\s*])(?:[^*\n]+?)(?<=[^\s*])\*(?!\w))"
)
_SAFE_SCHEME = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def safe_href(url: str) -> str | None:
    url = url.strip()
    return url if _SAFE_SCHEME.match(url) else None


def parse_inline(text: str, depth: int = 0) -> list[Inline]:
    out: list[Inline] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(Inline("text", text[pos : m.start()]))
        tok = m.group(0)
        if m.group("code"):
            out.append(Inline("code", tok[1:-1]))
        elif m.group("bold"):
            inner = tok[2:-2]
            out.append(Inline("bold", children=parse_inline(inner, depth + 1) if depth < 3 else [Inline("text", inner)]))
        elif m.group("link"):
            label, url = tok[1:].split("](", 1)
            url = url[:-1]
            href = safe_href(url)
            if href:
                out.append(Inline("link", label, href=href))
            else:
                out.append(Inline("text", label))  # unsafe scheme: keep the label only
        else:
            inner = tok[1:-1]
            out.append(Inline("italic", children=parse_inline(inner, depth + 1) if depth < 3 else [Inline("text", inner)]))
        pos = m.end()
    if pos < len(text):
        out.append(Inline("text", text[pos:]))
    return out


_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+(.*)$")


def parse_markdown(src: str) -> list[Block]:
    """Paragraphs and bullet lists only; headings/quotes/HTML are treated as plain text."""
    blocks: list[Block] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block("para", [parse_inline(" ".join(s.strip() for s in para))]))
            para.clear()
        if bullets:
            blocks.append(Block("list", [parse_inline(b) for b in bullets]))
            bullets.clear()

    for raw in (src or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        m = _BULLET.match(line)
        if m:
            if para:
                flush()
            bullets.append(m.group(1).strip())
        elif bullets and raw[:1].isspace():
            bullets[-1] += " " + line.strip()  # continuation of a bullet
        else:
            if bullets:
                flush()
            para.append(re.sub(r"^\s{0,3}#{1,6}\s+", "", line))
    flush()
    return blocks


def inline_plain(nodes: list[Inline]) -> str:
    parts = []
    for n in nodes:
        if n.kind in ("bold", "italic"):
            parts.append(inline_plain(n.children))
        elif n.kind == "link":
            parts.append(f"{n.text} ({n.href})")
        else:
            parts.append(n.text)
    return "".join(parts)


# ---------------------------------------------------------------------------------------------
# chart data shaping
# ---------------------------------------------------------------------------------------------
@dataclass
class WideSeries:
    x_name: str
    x: list[str]
    series: dict[str, list[float | None]]  # insertion-ordered
    pivoted: bool = False


def valid_rows(chart: ReportChart) -> list[list[Any]]:
    n = len(chart.columns)
    if n == 0:
        return []
    return [list(r) for r in chart.rows if isinstance(r, list | tuple) and len(r) == n]


def _numeric_col(rows: list[list[Any]], j: int) -> bool:
    vals = [r[j] for r in rows if r[j] is not None and r[j] != ""]
    return bool(vals) and all(to_number(v) is not None and not isinstance(v, bool) for v in vals)


def to_wide(chart: ReportChart, max_series: int = 12) -> WideSeries | None:
    """(label, num, num, ...) wide rows, or (label, series, num) long rows pivoted to wide.
    Returns None when there is no numeric measure."""
    rows = valid_rows(chart)
    cols = [str(c) for c in chart.columns]
    if not rows or len(cols) < 2:
        return None
    if len(cols) == 3 and not _numeric_col(rows, 1) and _numeric_col(rows, 2):
        xs: dict[str, None] = {}
        names: dict[str, None] = {}
        cells: dict[tuple[str, str], float | None] = {}
        for r in rows:
            x, s = cell_text(r[0]), cell_text(r[1])
            if s not in names:
                if len(names) >= max_series:
                    continue
                names[s] = None
            xs.setdefault(x, None)
            cells[(x, s)] = to_number(r[2])
        return WideSeries(cols[0], list(xs), {s: [cells.get((x, s)) for x in xs] for s in names}, pivoted=True)
    num_cols = [j for j in range(1, len(cols)) if _numeric_col(rows, j)][:max_series]
    if not num_cols:
        return None
    return WideSeries(cols[0], [cell_text(r[0]) for r in rows], {cols[j]: [to_number(r[j]) for r in rows] for j in num_cols})

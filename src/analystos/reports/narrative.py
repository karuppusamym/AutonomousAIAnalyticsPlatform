"""Markdown and self-contained HTML renderers for ReportData (RPT-001..003).

All text in ReportData is untrusted. HTML output escapes every value; `summary_markdown` is parsed
into a safe subset (paragraphs, bullet lists, **bold**, _italic_, `code`, http/https links) and
re-emitted, so raw HTML and non-http(s) links never reach the output. Markdown output escapes
markdown/HTML metacharacters in data text for the same reason.
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable

from analystos.contracts.reports import ReportData, ReportInsight, ReportSection
from analystos.core.ids import stable_hash
from analystos.reports import _common as C

# =============================================================================================
# Markdown
# =============================================================================================
_MD_SPECIAL = re.compile(r"([\\`*_\[\]|#~!])")


def md_escape(text: object, *, strip: bool = True) -> str:
    s = "" if text is None else str(text)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _MD_SPECIAL.sub(r"\\\1", s)
    s = re.sub(r"\s*[\r\n]+\s*", " ", s)
    return s.strip() if strip else s


def _md_inline(nodes: list[C.Inline]) -> str:
    out = []
    for n in nodes:
        if n.kind == "text":
            out.append(md_escape(n.text, strip=False))
        elif n.kind == "bold":
            out.append(f"**{_md_inline(n.children)}**")
        elif n.kind == "italic":
            out.append(f"_{_md_inline(n.children)}_")
        elif n.kind == "code":
            out.append(_md_code_span(n.text))
        elif n.kind == "link":
            href = n.href.replace("(", "%28").replace(")", "%29")
            out.append(f"[{md_escape(n.text)}]({href})")
    return "".join(out)


def _md_code_span(text: str) -> str:
    text = re.sub(r"\s*[\r\n]+\s*", " ", text)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_fence(code: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=2)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{code.rstrip()}\n{fence}"


def _md_summary(src: str) -> list[str]:
    lines: list[str] = []
    for b in C.parse_markdown(src):
        if b.kind == "para":
            lines += [_md_inline(b.items[0]), ""]
        else:
            lines += [f"- {_md_inline(it)}" for it in b.items] + [""]
    return lines


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(md_escape(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(md_escape(c) for c in r) + " |" for r in rows]
    return out + [""]


def _md_insight(i: ReportInsight, kind: str, with_evidence: bool) -> list[str]:
    status = i.status_label()
    tag = f" [{i.change.upper()}]" if i.change else ""
    evidence = i.evidence_label()
    line = (f"_{status}, {evidence}, review score {C.confidence_pct(i.confidence)} (uncalibrated)_" if evidence
            else f"_{status}, confidence {C.confidence_pct(i.confidence)}_")
    out = [f"### {md_escape(i.code)} - {md_escape(i.title)}{tag}", "", line, "", md_escape(i.finding), ""]
    if i.caveats:
        out += ["Caveats:"] + [f"- {md_escape(c)}" for c in i.caveats] + [""]
    if with_evidence and i.evidence_queries:
        for q in i.evidence_queries:
            h = f", hash {_md_code_span(q.result_hash)}" if q.result_hash else ""
            out += [f"Evidence query {_md_code_span(q.id)} ({q.row_count} rows{h}):", "", _md_fence(q.sql, "sql"), ""]
    elif i.evidence_queries:
        out += ["Evidence: " + ", ".join(_md_code_span(q.id) for q in i.evidence_queries), ""]
    return out


def _md_section(key: str, d: ReportData) -> list[str]:
    kind = d.kind
    out = [f"## {C.section_title(key, kind)}", ""]
    if key == "summary":
        body = _md_summary(d.summary_markdown) if d.summary_markdown.strip() else [md_escape(C.auto_summary(d)), ""]
        return out + body
    if key == "kpis":
        if not d.metrics:
            return out + ["No metrics in this run.", ""]
        rows = []
        for m in d.metrics:
            ch = C.metric_change(m)
            rows.append([m.display_name, C.format_value(m.value, m.format), C.format_value(m.previous_value, m.format),
                         f"{ch.arrow} {ch.text}".strip(), m.definition])
        return out + _md_table(["Metric", "Value", "Previous", "Change", "Definition"], rows)
    if key == "findings":
        items = C.shown_findings(d)
        if not items:
            return out + ["No verified findings in this run." if kind == "executive" else "No findings in this run.", ""]
        for i in items:
            out += _md_insight(i, kind, with_evidence=kind == "statistical")
        return out
    if key == "alerts":
        if not d.alerts:
            return out + ["No alerts.", ""]
        for a in C.sorted_alerts(d):
            metric = f" (metric: {md_escape(a.metric)})" if a.metric else ""
            out.append(f"- **{a.severity.upper()}** {md_escape(a.title)}: {md_escape(a.message)}{metric}")
        return out + [""]
    if key == "changes":
        g = C.change_groups(d)
        labels = {"new": "New", "changed": "Changed", "persisting": "Persisting", "resolved": "Resolved"}
        if not any(g.values()):
            return out + ["No comparison with a previous run is available.", ""]
        for k, lab in labels.items():
            items = g[k]
            out.append(f"**{lab} ({len(items)})**")
            out.append("")
            out += [f"- {md_escape(i.code)} - {md_escape(i.title)}" for i in items] or ["- none"]
            out.append("")
        return out
    if key == "quality":
        if not d.quality_issues:
            return out + ["No data quality issues recorded.", ""]
        rows = [[str(q.get("severity", "")), str(q.get("asset", "")), str(q.get("column", "") or ""), str(q.get("message", ""))]
                for q in C.sorted_quality(d)]
        return out + _md_table(["Severity", "Asset", "Column", "Issue"], rows)
    if key == "hypotheses":
        if not d.hypotheses:
            return out + ["No hypotheses were tested.", ""]
        rows = [[str(h.get("code", "")), str(h.get("statement", "")), str(h.get("status", "")), str(h.get("conclusion", "") or "")]
                for h in d.hypotheses]
        return out + _md_table(["Code", "Hypothesis", "Status", "Conclusion"], rows)
    if key == "evidence":
        qs = [(i.code, q) for i in d.insights for q in i.evidence_queries]
        if not qs:
            return out + ["No evidence queries recorded.", ""]
        rows = [[code, q.id, str(q.row_count), q.result_hash or ""] for code, q in qs]
        return out + _md_table(["Finding", "Query", "Rows", "Result hash"], rows)
    if key == "charts":
        if not d.charts:
            return out + ["No charts in this run.", ""]
        for ch in d.charts:
            out += [f"### {md_escape(ch.title)}", ""]
            rows = C.valid_rows(ch)
            if not rows:
                out += ["_No data._", ""]
                continue
            out += _md_table([str(c) for c in ch.columns], [[C.cell_text(v) for v in r] for r in rows[: C.MAX_TABLE_ROWS]])
            if len(rows) > C.MAX_TABLE_ROWS:
                out += [f"_Showing {C.MAX_TABLE_ROWS} of {len(rows)} rows._", ""]
        return out
    return []


def _md_doc_section(sec: ReportSection) -> list[str]:
    """A report document section (CONV-04): its blocks in order."""
    out = [f"## {md_escape(sec.title)}", ""]
    for kind, item in C.section_items(sec):
        if kind == "text":
            out += _md_summary(item)
        elif kind == "note":
            out += [f"_{md_escape(item)}_", ""]
        elif kind == "kpis":
            out += _md_table(C.DOC_KPI_HEADERS, [C.kpi_row(m) for m, _ in item])
            out += [f"_Source: {md_escape(src)}_" for src in dict.fromkeys(s for _, s in item if s)] + [""]
        elif kind == "chart":
            ch, src = item
            out += [f"### {md_escape(ch.title)}", ""]
            rows = C.valid_rows(ch)
            if rows:
                out += _md_table([str(c) for c in ch.columns], [[C.cell_text(v) for v in r] for r in rows[: C.MAX_TABLE_ROWS]])
            else:
                out += ["_No data._", ""]
            if src:
                out += [f"_Source: {md_escape(src)}_", ""]
        elif kind == "finding":
            ins, src = item
            out += _md_insight(ins, "document", with_evidence=False)
            if src:
                out += [f"_Source: {md_escape(src)}_", ""]
    return out


def _md_provenance(d: ReportData) -> list[str]:
    label, origin = C.origin_line(d)
    out = ["---", "", f"## {C.SECTION_TITLES['provenance']}", "",
           f"- {label}: {_md_code_span(origin)}", f"- Generated: {md_escape(C.generated_label(d))}"]
    if d.kind == "document":
        out.append(f"- Report snapshot: {_md_code_span(stable_hash(d.snapshot()))}")
    if d.lineage_note:
        out.append(f"- Lineage: {md_escape(d.lineage_note)}")
    if d.caveats:
        out.append("- Caveats:")
        out += [f"  - {md_escape(c)}" for c in d.caveats]
    return out + ["", f"_{C.CAUSATION_NOTE}_", ""]


def render_markdown(data: ReportData) -> str:
    d = data
    out = [f"# {md_escape(d.title)}", "",
           f"**{C.KIND_LABELS.get(d.kind, d.kind)}** · {md_escape(d.workspace_name)} · {md_escape(C.generated_label(d))}", ""]
    if d.period:
        out += [f"Period: {md_escape(d.period)}", ""]
    if d.objective or d.kind != "document":
        out += [f"Objective: {md_escape(d.objective)}", ""]
    for key in C.sections_for(d.kind):
        out += _md_section(key, d)
    for sec in d.sections:
        out += _md_doc_section(sec)
    out += _md_provenance(d)
    return "\n".join(out).rstrip() + "\n"


# =============================================================================================
# HTML
# =============================================================================================
def esc(v: object) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _html_inline(nodes: list[C.Inline]) -> str:
    out = []
    for n in nodes:
        if n.kind == "text":
            out.append(esc(n.text))
        elif n.kind == "bold":
            out.append(f"<strong>{_html_inline(n.children)}</strong>")
        elif n.kind == "italic":
            out.append(f"<em>{_html_inline(n.children)}</em>")
        elif n.kind == "code":
            out.append(f"<code>{esc(n.text)}</code>")
        elif n.kind == "link":
            out.append(f'<a href="{esc(n.href)}" rel="noopener noreferrer nofollow">{esc(n.text)}</a>')
    return "".join(out)


def _html_summary(src: str) -> str:
    parts = []
    for b in C.parse_markdown(src):
        if b.kind == "para":
            parts.append(f"<p>{_html_inline(b.items[0])}</p>")
        else:
            parts.append("<ul>" + "".join(f"<li>{_html_inline(it)}</li>" for it in b.items) + "</ul>")
    return "\n".join(parts)


def _html_table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    c = f' class="{cls}"' if cls else ""
    return f"<table{c}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{esc(text)}</span>'


def _html_insight(i: ReportInsight, kind: str, with_evidence: bool) -> str:
    badges = _badge(i.status_label(), "ok" if i.verified else "muted")
    evidence = i.evidence_label()
    if evidence:
        badges += " " + _badge(evidence, "muted" if i.validation != "confirmed" or i.stale or i.void_reason else "ok")
        badges += " " + _badge(f"review score {C.confidence_pct(i.confidence)} (uncalibrated)", "muted")
    else:
        badges += " " + _badge(f"confidence {C.confidence_pct(i.confidence)}", "muted")
    if i.change:
        badges += " " + _badge(i.change, "chg")
    parts = [f'<article class="finding"><h3><span class="code">{esc(i.code)}</span> {esc(i.title)}</h3>',
             f'<div class="badges">{badges}</div>', f"<p>{esc(i.finding)}</p>"]
    if i.caveats:
        parts.append('<div class="caveats"><strong>Caveats</strong><ul>' + "".join(f"<li>{esc(c)}</li>" for c in i.caveats) + "</ul></div>")
    if with_evidence and i.evidence_queries:
        for q in i.evidence_queries:
            h = f", hash <code>{esc(q.result_hash)}</code>" if q.result_hash else ""
            parts.append(f'<div class="evidence"><div>Evidence query <code>{esc(q.id)}</code> ({q.row_count} rows{h})</div>'
                         f'<pre><code class="language-sql">{esc(q.sql)}</code></pre></div>')
    elif i.evidence_queries:
        parts.append('<p class="small">Evidence: ' + ", ".join(f"<code>{esc(q.id)}</code>" for q in i.evidence_queries) + "</p>")
    parts.append("</article>")
    return "".join(parts)


def _html_section(key: str, d: ReportData) -> str:
    kind = d.kind
    h = f'<section class="sec-{key}"><h2>{esc(C.section_title(key, kind))}</h2>'
    body = ""
    if key == "summary":
        body = _html_summary(d.summary_markdown) if d.summary_markdown.strip() else f"<p>{esc(C.auto_summary(d))}</p>"
    elif key == "kpis":
        if not d.metrics:
            body = "<p>No metrics in this run.</p>"
        else:
            rows = []
            for m in d.metrics:
                ch = C.metric_change(m)
                cls = {"↑": "up", "↓": "down"}.get(ch.arrow, "flat")
                rows.append([f"<strong>{esc(m.display_name)}</strong>", f'<span class="num">{esc(C.format_value(m.value, m.format))}</span>',
                             esc(C.format_value(m.previous_value, m.format)),
                             f'<span class="chg {cls}">{esc(ch.arrow)} {esc(ch.text)}</span>', f'<span class="small">{esc(m.definition)}</span>'])
            body = _html_table(["Metric", "Value", "Previous", "Change", "Definition"], rows, "kpi")
    elif key == "findings":
        items = C.shown_findings(d)
        if not items:
            body = "<p>No verified findings in this run.</p>" if kind == "executive" else "<p>No findings in this run.</p>"
        else:
            body = "".join(_html_insight(i, kind, kind == "statistical") for i in items)
    elif key == "alerts":
        if not d.alerts:
            body = "<p>No alerts.</p>"
        else:
            body = "".join(
                f'<div class="alert {esc(a.severity)}"><strong>{esc(a.severity.upper())}</strong> {esc(a.title)}'
                f'<div>{esc(a.message)}</div>' + (f'<div class="small">Metric: {esc(a.metric)}</div>' if a.metric else "") + "</div>"
                for a in C.sorted_alerts(d))
    elif key == "changes":
        g = C.change_groups(d)
        if not any(g.values()):
            body = "<p>No comparison with a previous run is available.</p>"
        else:
            cols = []
            for k, lab in (("new", "New"), ("changed", "Changed"), ("persisting", "Persisting"), ("resolved", "Resolved")):
                lis = "".join(f'<li><span class="code">{esc(i.code)}</span> {esc(i.title)}</li>' for i in g[k]) or "<li>none</li>"
                cols.append(f'<div class="chgcol"><h4>{lab} ({len(g[k])})</h4><ul>{lis}</ul></div>')
            body = f'<div class="changes">{"".join(cols)}</div>'
    elif key == "quality":
        if not d.quality_issues:
            body = "<p>No data quality issues recorded.</p>"
        else:
            rows = [[esc(q.get("severity", "")), esc(q.get("asset", "")), esc(q.get("column", "") or ""), esc(q.get("message", ""))]
                    for q in C.sorted_quality(d)]
            body = _html_table(["Severity", "Asset", "Column", "Issue"], rows)
    elif key == "hypotheses":
        if not d.hypotheses:
            body = "<p>No hypotheses were tested.</p>"
        else:
            rows = [[f'<span class="code">{esc(x.get("code", ""))}</span>', esc(x.get("statement", "")),
                     _badge(str(x.get("status", "")), "muted"), esc(x.get("conclusion", "") or "")] for x in d.hypotheses]
            body = _html_table(["Code", "Hypothesis", "Status", "Conclusion"], rows)
    elif key == "evidence":
        qs = [(i.code, q) for i in d.insights for q in i.evidence_queries]
        body = "<p>No evidence queries recorded.</p>" if not qs else _html_table(
            ["Finding", "Query", "Rows", "Result hash"],
            [[esc(c), f"<code>{esc(q.id)}</code>", esc(q.row_count), f"<code>{esc(q.result_hash or '')}</code>"] for c, q in qs])
    elif key == "charts":
        if not d.charts:
            body = "<p>No charts in this run.</p>"
        for ch in d.charts:
            rows = C.valid_rows(ch)
            body += f'<div class="chart"><h3>{esc(ch.title)}</h3>'
            if not rows:
                body += "<p class=\"small\">No data.</p></div>"
                continue
            body += _html_table([str(c) for c in ch.columns], [[esc(C.cell_text(v)) for v in r] for r in rows[: C.MAX_TABLE_ROWS]], "data")
            if len(rows) > C.MAX_TABLE_ROWS:
                body += f'<p class="small">Showing {C.MAX_TABLE_ROWS} of {len(rows)} rows.</p>'
            body += "</div>"
    return h + body + "</section>"


def _html_doc_section(sec: ReportSection) -> str:
    """A report document section (CONV-04): its blocks in order; charts as bounded data tables."""
    parts = [f'<section class="sec-doc"><h2>{esc(sec.title)}</h2>']
    for kind, item in C.section_items(sec):
        if kind == "text":
            parts.append(_html_summary(item))
        elif kind == "note":
            parts.append(f'<p class="small note-held">{esc(item)}</p>')
        elif kind == "kpis":
            rows = []
            for m, _ in item:
                ch = C.metric_change(m)
                cls = {"↑": "up", "↓": "down"}.get(ch.arrow, "flat")
                rows.append([f"<strong>{esc(m.display_name)}</strong>", f'<span class="num">{esc(C.format_value(m.value, m.format))}</span>',
                             esc(C.format_value(m.previous_value, m.format)), f'<span class="chg {cls}">{esc(ch.arrow)} {esc(ch.text)}</span>'])
            parts.append(_html_table(C.DOC_KPI_HEADERS, rows, "kpi"))
            parts += [f'<p class="small">Source: {esc(src)}</p>' for src in dict.fromkeys(s for _, s in item if s)]
        elif kind == "chart":
            ch, src = item
            rows = C.valid_rows(ch)
            parts.append(f'<div class="chart"><h3>{esc(ch.title)}</h3>')
            if rows:
                parts.append(_html_table([str(c) for c in ch.columns], [[esc(C.cell_text(v)) for v in r] for r in rows[: C.MAX_TABLE_ROWS]], "data"))
                if len(rows) > C.MAX_TABLE_ROWS:
                    parts.append(f'<p class="small">Showing {C.MAX_TABLE_ROWS} of {len(rows)} rows.</p>')
            else:
                parts.append('<p class="small">No data.</p>')
            if src:
                parts.append(f'<p class="small">Source: {esc(src)}</p>')
            parts.append("</div>")
        elif kind == "finding":
            ins, src = item
            parts.append(_html_insight(ins, "document", False))
            if src:
                parts.append(f'<p class="small">Source: {esc(src)}</p>')
    return "".join(parts) + "</section>"


_CSS = """
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bg:#ffffff;--soft:#f6f8fa;--accent:#2f5d8a;
--ok:#1a7f37;--warn:#9a6700;--crit:#cf222e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px}
header{border-bottom:2px solid var(--accent);padding-bottom:12px;margin-bottom:8px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:28px 0 10px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:15px;margin:14px 0 4px}h4{font-size:13px;margin:0 0 4px}
.meta,.small{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;margin:6px 0 12px;font-size:13px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:var(--soft);font-weight:600}
table.kpi .num{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.chg.up{color:var(--ok)}.chg.down{color:var(--crit)}.chg.flat{color:var(--muted)}
.finding{border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:10px 0;break-inside:avoid}
.code{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--accent);font-size:12px}
.badge{display:inline-block;border-radius:10px;padding:0 8px;font-size:11px;border:1px solid var(--line);background:var(--soft)}
.badge.ok{color:var(--ok);border-color:var(--ok)}.badge.chg{color:var(--accent);border-color:var(--accent)}
.alert{border-left:4px solid var(--muted);background:var(--soft);padding:6px 10px;margin:6px 0;break-inside:avoid}
.alert.warning{border-color:var(--warn)}.alert.critical{border-color:var(--crit)}.alert.info{border-color:var(--accent)}
.changes{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.chgcol ul{margin:0;padding-left:18px}
pre{background:var(--soft);border:1px solid var(--line);padding:8px;overflow-x:auto;white-space:pre-wrap;font-size:12px}
code{font-family:ui-monospace,Menlo,Consolas,monospace}
footer{margin-top:32px;border-top:2px solid var(--accent);padding-top:10px;font-size:12px;color:var(--muted)}
footer .note{color:var(--fg);font-style:italic}
@media print{main{max-width:none;padding:0}h2{break-after:avoid}table,.chart{break-inside:auto}tr{break-inside:avoid}
a{color:inherit;text-decoration:none}@page{margin:16mm}}
"""


def _html_provenance(d: ReportData) -> str:
    label, origin = C.origin_line(d)
    items = [f"<li>{esc(label)}: <code>{esc(origin)}</code></li>", f"<li>Generated: {esc(C.generated_label(d))}</li>"]
    if d.kind == "document":
        items.append(f"<li>Report snapshot: <code>{esc(stable_hash(d.snapshot()))}</code></li>")
    if d.lineage_note:
        items.append(f"<li>Lineage: {esc(d.lineage_note)}</li>")
    if d.caveats:
        items.append("<li>Caveats:<ul>" + "".join(f"<li>{esc(c)}</li>" for c in d.caveats) + "</ul></li>")
    return (f'<footer><h2>{C.SECTION_TITLES["provenance"]}</h2><ul>{"".join(items)}</ul>'
            f'<p class="note">{esc(C.CAUSATION_NOTE)}</p></footer>')


def render_html(data: ReportData) -> str:
    d = data
    meta = [esc(C.KIND_LABELS.get(d.kind, d.kind)), esc(d.workspace_name), esc(C.generated_label(d))]
    if d.period:
        meta.append("Period " + esc(d.period))
    objective = f'<p><strong>Objective:</strong> {esc(d.objective)}</p>' if d.objective or d.kind != "document" else ""
    head = f'<header><h1>{esc(d.title)}</h1><div class="meta">{" · ".join(meta)}</div>{objective}</header>'
    sections = "\n".join([*(_html_section(k, d) for k in C.sections_for(d.kind)), *(_html_doc_section(s) for s in d.sections)])
    # A document snapshot names its hash in the head, as the PDF keywords and the Office core properties do.
    snapshot = (f'<meta name="analystos-report-data" content="{esc(stable_hash(d.snapshot()))}">'
                if d.kind == "document" else "")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        f"{snapshot}<title>{esc(d.title)}</title><style>{_CSS}</style></head>"
        f'<body><main class="kind-{esc(d.kind)}">{head}\n{sections}\n{_html_provenance(d)}</main></body></html>\n'
    )


RENDERERS: dict[str, Callable[[ReportData], str]] = {"md": render_markdown, "html": render_html}

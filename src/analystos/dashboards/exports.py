"""Dashboard exports (S08-T6): a publish-class side effect.

An export (CSV, XLSX, PDF) of a dashboard whose derived sensitivity is at or above the workspace policy's
`export_approval_min_sensitivity` is publish-class: an editor requests it, and it needs an approval bound to
{artifact, version, content hash, format, requester}; the requester alone executes it, once
(`verify_for_execution` then `consume`). Below the threshold any member may export. Every export
carries a watermark naming the requester and the time, and is audited with the file's SHA-256. The rows
are the dashboard's bounded previews (`MAX_PREVIEW_ROWS` per chart, ADR-014): an export never re-queries.
"""
from __future__ import annotations

import csv
import hashlib
import io
from typing import Any, Literal

from sqlalchemy.orm import Session

from analystos.contracts.dashboard_dsl import SENSITIVITY_ORDER, DashboardDoc
from analystos.core.errors import ApprovalRequired, Forbidden, InvalidInput
from analystos.core.ids import utcnow
from analystos.db.models import Approval, User

EXPORT_ACTION = "dashboard.export"
Format = Literal["csv", "xlsx", "pdf"]
MEDIA = {"csv": "text/csv; charset=utf-8",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}


def needs_approval(sensitivity: str, threshold: str) -> bool:
    return SENSITIVITY_ORDER.get(sensitivity, 1) >= SENSITIVITY_ORDER.get(threshold, 2)


def export_payload(art: Any, fmt: str, user: User) -> dict[str, Any]:
    return {"artifact_id": art.id, "version": art.version, "content_hash": art.content_hash, "format": fmt,
            "requested_by": user.id}


def watermark(user: User, at: str, doc: DashboardDoc, art: Any) -> str:
    return (f"Exported by {user.email} at {at} | {doc.sensitivity.upper()} | {doc.title} | "
            f"{art.id} v{art.version} | do not redistribute")


def tables(doc: DashboardDoc) -> list[tuple[str, list[str], list[list[Any]]]]:
    """(title, columns, rows) per chart preview and one KPI table."""
    out: list[tuple[str, list[str], list[list[Any]]]] = []
    if doc.kpis:
        out.append(("KPIs", ["kpi", "metric", "value", "unit"],
                    [[k.title, k.metric.label, k.value, k.unit] for k in doc.kpis]))
    for chart in doc.charts:
        prev = chart.preview or {}
        cols = [str(c) for c in prev.get("columns") or []]
        rows = [list(r) if isinstance(r, list | tuple) else [r.get(c) for c in cols] for r in prev.get("rows") or []]
        out.append((chart.title, cols, rows[:200]))
    return out


def render_csv(doc: DashboardDoc, mark: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([f"# {mark}"])
    for title, cols, rows in tables(doc):
        w.writerow([])
        w.writerow([f"# {title}"])
        if cols:
            w.writerow(cols)
        w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM: spreadsheets open non-Latin text correctly


def render_xlsx(doc: DashboardDoc, mark: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font

    def clean(v: Any) -> Any:
        return ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v

    wb = Workbook()
    ws = wb.active
    ws.title = "Dashboard"
    ws.append([clean(mark)])
    ws["A1"].font = Font(bold=True, color="9A6700")
    for title, cols, rows in tables(doc):
        ws.append([])
        ws.append([clean(title)])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        if cols:
            ws.append([clean(c) for c in cols])
        for r in rows:
            ws.append([clean(v) for v in r])
    for sheet in wb.worksheets:
        sheet.oddHeader.center.text = mark[:250]
        sheet.oddFooter.center.text = mark[:250]
    wb.properties.title = doc.title[:200]
    wb.properties.description = mark[:500]
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_pdf(doc: DashboardDoc, mark: str) -> bytes:
    from fpdf import FPDF

    from analystos.reports.pdf import FONT, add_report_fonts, pdf_text

    class _Export(FPDF):
        def header(self) -> None:
            self.set_font(FONT, "", 7)
            self.set_text_color(154, 103, 0)
            self.multi_cell(0, 4, pdf_text(mark), align="L", new_x="LMARGIN", new_y="NEXT")
            with self.rotation(35, self.w / 2, self.h / 2):  # diagonal watermark on every page
                self.set_font(FONT, "B", 28)
                self.set_text_color(225, 228, 232)
                self.text(self.w / 2 - 70, self.h / 2, pdf_text(doc.sensitivity.upper() + " · EXPORT"))
            self.set_text_color(31, 35, 40)
            self.ln(2)

        def footer(self) -> None:
            self.set_y(-12)
            self.set_font(FONT, "", 7)
            self.set_text_color(89, 99, 110)
            self.cell(0, 5, pdf_text(mark)[:180] + f"  |  page {self.page_no()}", align="C")

    pdf = _Export(orientation="portrait", unit="mm", format="A4")
    add_report_fonts(pdf)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_font(FONT, "B", 16)
    pdf.multi_cell(0, 8, pdf_text(doc.title), new_x="LMARGIN", new_y="NEXT")
    if doc.description:
        pdf.set_font(FONT, "", 9)
        pdf.multi_cell(0, 5, pdf_text(doc.description), new_x="LMARGIN", new_y="NEXT")
    for block in doc.text_blocks:
        pdf.set_font(FONT, "", 9)
        pdf.multi_cell(0, 5, pdf_text(block.markdown), new_x="LMARGIN", new_y="NEXT")
    for title, cols, rows in tables(doc):
        pdf.ln(3)
        pdf.set_font(FONT, "B", 11)
        pdf.multi_cell(0, 6, pdf_text(title), new_x="LMARGIN", new_y="NEXT")
        if not cols:
            continue
        pdf.set_font(FONT, "", 8)
        with pdf.table(text_align="LEFT", line_height=5) as table:
            table.row([pdf_text(c) for c in cols])
            for r in rows:
                table.row([pdf_text("" if v is None else v) for v in r])
    return bytes(pdf.output())


RENDER = {"csv": render_csv, "xlsx": render_xlsx, "pdf": render_pdf}


def _publish_rights(session: Session, user: User, workspace_id: str, sensitivity: str) -> None:
    """An approval-gated export is publish-class: an editor (or above) requests it, as for publishing."""
    from analystos.governance.policy import member_role
    from analystos.security.auth import role_at_least

    if not role_at_least(member_role(session, user, workspace_id), "editor"):
        raise Forbidden(f"exporting {sensitivity} data is a publish-class action; ask an editor of this workspace")


def request_export(session: Session, user: User, artifact_id: str, fmt: str) -> dict[str, Any]:
    """{status: allowed} when no approval is needed, else the pending approval for this exact export."""
    from analystos.dashboards import service
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace, load_policy

    if fmt not in RENDER:
        raise InvalidInput(f"unknown export format {fmt!r}; use csv, xlsx or pdf")
    art = service.load(session, user, artifact_id, minimum="viewer")
    doc = service.document(art)
    ws = get_workspace(session, art.workspace_id)
    threshold = load_policy(session, ws).export_approval_min_sensitivity
    if not needs_approval(doc.sensitivity, threshold):
        return {"status": "allowed", "sensitivity": doc.sensitivity}
    _publish_rights(session, user, art.workspace_id, doc.sensitivity)
    approval = request_approval(session, workspace_id=art.workspace_id, run_id=None, action=EXPORT_ACTION,
                                payload=export_payload(art, fmt, user), plan_hash=None, policy_version=ws.policy_version,
                                requested_by=user.id, risk_tier="high" if doc.sensitivity == "restricted" else "medium",
                                destination=f"export:{fmt}", affected_assets=[art.id],
                                evidence={"sensitivity": doc.sensitivity, "threshold": threshold})
    return {"status": "approval_required", "sensitivity": doc.sensitivity,
            "approval": {"id": approval.id, "status": approval.status, "risk_tier": approval.risk_tier}}


def export(session: Session, user: User, artifact_id: str, fmt: str, *, approval_id: str | None = None) -> tuple[bytes, str, str]:
    """(file bytes, media type, file name). Re-verifies the approval immediately before rendering."""
    from analystos.dashboards import service
    from analystos.events.bus import emit
    from analystos.governance.approvals import consume, verify_for_execution
    from analystos.governance.audit import audit
    from analystos.governance.policy import get_workspace, load_policy

    if fmt not in RENDER:
        raise InvalidInput(f"unknown export format {fmt!r}; use csv, xlsx or pdf")
    art = service.load(session, user, artifact_id, minimum="viewer")
    doc = service.document(art)
    threshold = load_policy(session, get_workspace(session, art.workspace_id)).export_approval_min_sensitivity
    if needs_approval(doc.sensitivity, threshold):
        _publish_rights(session, user, art.workspace_id, doc.sensitivity)
        if not approval_id:
            raise ApprovalRequired(f"exporting {doc.sensitivity} data needs an approval; request one first",
                                   details={"sensitivity": doc.sensitivity, "threshold": threshold})
        approval = session.get(Approval, approval_id)
        if approval is None or approval.action != EXPORT_ACTION or approval.requested_by != user.id:
            raise Forbidden("this export approval was not granted to you")
        verify_for_execution(session, approval_id, payload=export_payload(art, fmt, user), plan_hash=None)
        consume(session, approval)
    at = utcnow().replace(microsecond=0).isoformat()
    mark = watermark(user, at, doc, art)
    data = RENDER[fmt](doc, mark)
    digest = hashlib.sha256(data).hexdigest()
    audit(f"user:{user.id}", "export.created", workspace_id=art.workspace_id, target=art.id, decision="allow",
          details={"format": fmt, "version": art.version, "sensitivity": doc.sensitivity, "approval_id": approval_id,
                   "sha256": digest, "bytes": len(data), "watermark": mark}, session=session)
    emit(art.workspace_id, "export.created", {"artifact_id": art.id, "format": fmt, "sha256": digest},
         actor=f"user:{user.id}", session=session)
    return data, MEDIA[fmt], f"{doc.id}-v{art.version}.{fmt}"

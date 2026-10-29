"""Shared plumbing for the Office renderers (N-10: DOCX, PPTX).

Both formats are zip packages of XML: text must be XML 1.0-legal (python-docx/pptx raise on control
characters), the core properties carry the ReportData snapshot hash, and the package is repacked with
every entry dated `generated_at` so the same snapshot yields the same bytes (an approval binds a hash).
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Any

from analystos.contracts.reports import ReportData
from analystos.core.ids import stable_hash
from analystos.reports import _common as C

_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
EPOCH = datetime(1980, 1, 1, tzinfo=UTC)  # the earliest date a zip entry can carry


def xml_text(v: Any) -> str:
    """Text an OOXML part can hold: control characters and lone surrogates dropped, tabs as spaces."""
    s = "" if v is None else str(v)
    return _ILLEGAL.sub("", s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))


def snapshot_hash(data: ReportData) -> str:
    """The hash services/reports.generate_report records as `report_data_hash` for this snapshot."""
    return stable_hash(data.snapshot())


def snapshot_id(data: ReportData) -> str:
    return f"analystos-report-data:{snapshot_hash(data)}"


def generated(data: ReportData) -> datetime:
    ts = C.parse_ts(data.generated_at)
    return ts if ts and ts >= EPOCH else EPOCH


def set_core(props: Any, data: ReportData) -> None:
    """Core properties shared by DOCX and PPTX (python-docx and python-pptx expose the same names)."""
    ts = generated(data).replace(tzinfo=None)
    props.title = xml_text(data.title)[:255]
    props.subject = C.KIND_LABELS.get(data.kind, data.kind)
    props.author = xml_text(data.workspace_name)[:255]
    props.last_modified_by = "AnalystOS"
    props.identifier = snapshot_id(data)
    props.keywords = f"run {data.run_id}"
    props.comments = "Rendered from a verified report snapshot; every number is a value of that snapshot."
    props.revision = 1
    props.created = ts
    props.modified = ts
    props.last_printed = ts


def repack(raw: bytes, data: ReportData) -> bytes:
    """The same package with every entry dated `generated_at` (zip entries otherwise carry the render time)."""
    stamp = generated(data).timetuple()[:6]
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            entry = zipfile.ZipInfo(info.filename, date_time=stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            dst.writestr(entry, src.read(info.filename))
    return out.getvalue()


def kpi_rows(data: ReportData) -> list[list[str]]:
    rows = []
    for m in data.metrics:
        ch = C.metric_change(m)
        rows.append([m.display_name, C.format_value(m.value, m.format), C.format_value(m.previous_value, m.format),
                     f"{ch.arrow} {ch.text}".strip(), m.definition])
    return rows


def finding_meta(i: Any) -> str:
    return (f"{i.status_label().capitalize()} - confidence {C.confidence_pct(i.confidence)}"
            + (f" - VOID: {i.void_reason}; needs re-verification" if i.void_reason else ""))


KPI_HEADERS = ["Metric", "Value", "Previous", "Change", "Definition"]

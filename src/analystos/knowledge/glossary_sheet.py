"""Spreadsheet round trip for stewards (review P2-11): the workspace glossary as CSV or XLSX, and the same
format back in as review drafts.

Export is a download to the caller, limited to what the caller may see: a term linked to columns the caller's
data scope hides is left out (counted, never named) and so is a hidden column in a visible term's links.

Import never writes the glossary. Every row becomes a `glossary_term` suggestion (`knowledge/suggestions`) whose
fields carry provenance `import:csv` and confidence `IMPORT_CONFIDENCE`; the confidence becomes 1.0 only when a
reviewer approves it (`suggestions.confirm_imported`). `owner`, `trust` and `status` are informational on the way
in: trust is earned by review, never claimed by a file. Cell text is data: spreadsheet formulas are neutralised
on export, text that reads like an instruction to a model is screened on import, and a formula cell read back
is plain text. Re-importing the same file is idempotent (the draft's content hash).
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput
from analystos.knowledge import suggestions

COLUMNS = ["term", "definition", "synonyms", "linked_columns", "owner", "trust", "status"]
REQUIRED = ("term", "definition")
INFORMATIONAL = ("owner", "trust", "status")
ALIASES = {"name": "term", "glossary_term": "term", "body": "definition", "description": "definition",
           "synonym": "synonyms", "aliases": "synonyms", "columns": "linked_columns", "mapped_columns": "linked_columns",
           "links": "linked_columns"}
SEPARATOR = ";"
SOURCE = "import:csv"
IMPORT_CONFIDENCE = 0.5  # a file is a person's claim; 1.0 only once a reviewer approves it
MAX_BYTES = 2 * 1024 * 1024
MAX_XLSX_INFLATED = 20 * 1024 * 1024
MAX_ROWS = 2000
MAX_TERM, MAX_DEFINITION = 300, 4000
MAX_SYNONYMS, MAX_SYNONYM = 10, 100
MAX_LINKS, MAX_LINK = 50, 400
FORMATS = {"csv": ("text/csv; charset=utf-8", "csv"),
           "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")}
_TRIGGERS = "=+-@\t\r"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ------------------------------------------------------------------------------------ cell text
def csv_safe(value: Any) -> str:
    """A CSV cell a spreadsheet cannot run: text starting with `= + - @` (or tab/CR) gets a leading apostrophe.
    A text that already starts with an apostrophe before such a character gets one more, so `csv_unsafe` is exact."""
    text = "" if value is None else str(value)
    first = text[:1]
    if first and first in _TRIGGERS or (first == "'" and text[1:2] and (text[1:2] in _TRIGGERS or text[1:2] == "'")):
        return "'" + text
    return text


def csv_unsafe(text: str) -> str:
    return text[1:] if text[:1] == "'" and text[1:2] and (text[1:2] in _TRIGGERS or text[1:2] == "'") else text


def _items(text: str) -> list[str]:
    return [t for t in (" ".join(x.split()) for x in text.split(SEPARATOR)) if t]


def _join(values: list[str]) -> str:
    return f"{SEPARATOR} ".join(v.replace(SEPARATOR, ",") for v in values)


# ------------------------------------------------------------------------------------ export
@dataclass
class Export:
    rows: list[dict[str, str]]
    total: int
    omitted_terms: int = 0
    omitted_links: int = 0

    def summary(self) -> dict[str, int]:
        return {"terms": len(self.rows), "total": self.total, "omitted_terms": self.omitted_terms,
                "omitted_links": self.omitted_links}


def _visible_columns(session: Session, user: Any, workspace_id: str) -> dict[str, str]:
    """Lower-case -> exact `schema.table.column` for the columns the caller's data scope lets them see (selected
    tables, minus denied columns)."""
    from analystos.governance.policy import resolve_scope

    scope = resolve_scope(session, user, workspace_id, minimum_role="viewer")
    denied = {d.lower() for d in scope.denied_columns}
    out = {}
    for asset, names in scope.columns.items():
        for name in names:
            fq = f"{asset}.{name}"
            if fq.lower() not in denied and f"*.{name}".lower() not in denied:
                out[fq.lower()] = fq
    return out


def collect(session: Session, user: Any, workspace_id: str) -> Export:
    """The caller's view of the workspace's own glossary terms, ordered by name. Pack vocabulary is the pack's, not exported."""
    from analystos.db.models import ContextEntry

    visible = _visible_columns(session, user, workspace_id)
    entries = sorted(session.scalars(select(ContextEntry).where(ContextEntry.workspace_id == workspace_id,
                                                                ContextEntry.kind == "term")),
                     key=lambda e: (e.name.lower(), e.id))
    out = Export(rows=[], total=len(entries))
    for e in entries:
        mapped = [str(c) for c in e.mapped_columns or []]
        shown = [c for c in mapped if c.lower() in visible]
        if mapped and not shown:
            out.omitted_terms += 1
            continue
        out.omitted_links += len(mapped) - len(shown)
        out.rows.append({"term": e.name, "definition": e.body or "", "synonyms": _join([str(s) for s in e.synonyms or []]),
                         "linked_columns": _join(sorted(shown)), "owner": e.origin or "user",
                         "trust": "trusted" if e.trusted else "unreviewed", "status": "approved" if e.trusted else "draft"})
    return out


def render_csv(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([csv_safe(r[c]) for c in COLUMNS])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # the BOM makes Excel read UTF-8


def render_xlsx(rows: list[dict[str, str]]) -> bytes:
    """Every cell is written as text (`reports/excel.put`), so a formula-looking value stays literal."""
    from openpyxl import Workbook

    from analystos.reports.excel import HEADER_FILL, HEADER_FONT, put

    wb = Workbook()
    ws = wb.active
    ws.title = "Glossary"
    for j, name in enumerate(COLUMNS, 1):
        c = put(ws, 1, j, name)
        c.font, c.fill = HEADER_FONT, HEADER_FILL
    for i, r in enumerate(rows, 2):
        for j, name in enumerate(COLUMNS, 1):
            put(ws, i, j, r[name])
    ws.freeze_panes = "A2"
    for letter, width in zip("ABCDEFG", (28, 70, 30, 50, 12, 12, 12), strict=True):
        ws.column_dimensions[letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render(export: Export, fmt: str) -> bytes:
    if fmt == "csv":
        return render_csv(export.rows)
    if fmt == "xlsx":
        return render_xlsx(export.rows)
    raise InvalidInput(f"format must be one of {', '.join(FORMATS)}")


# ------------------------------------------------------------------------------------ parse
@dataclass
class ParsedRow:
    line: int
    cells: dict[str, str]
    unknown: list[str] = dc_field(default_factory=list)


def _header(cells: list[str]) -> tuple[list[str | None], list[str]]:
    names: list[str | None] = []
    unknown: list[str] = []
    for raw in cells:
        key = ALIASES.get(re.sub(r"[\s\-]+", "_", raw.strip().lower()), re.sub(r"[\s\-]+", "_", raw.strip().lower()))
        if key in COLUMNS and key not in names:
            names.append(key)
        else:
            names.append(None)
            if raw.strip():
                unknown.append(raw.strip()[:80])
    missing = [c for c in REQUIRED if c not in names]
    if missing:
        raise InvalidInput(f"the first row must name the columns; missing: {', '.join(missing)} (expected {', '.join(COLUMNS)})")
    return names, unknown


def _grid(filename: str, data: bytes) -> list[list[str]]:
    name = (filename or "").lower()
    if len(data) > MAX_BYTES:
        raise InvalidInput(f"the file is over {MAX_BYTES // 1024 // 1024} MB")
    if name.endswith(".xlsx"):
        return _xlsx_grid(data)
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidInput("the CSV must be UTF-8") from exc
        try:
            return [row for row in csv.reader(io.StringIO(text, newline=""))]
        except csv.Error as exc:
            raise InvalidInput(f"the CSV could not be read: {exc}") from exc
    raise InvalidInput("upload a .csv or .xlsx file")


def _xlsx_grid(data: bytes) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_XLSX_INFLATED:
                raise InvalidInput("the workbook is too large when unpacked")
    except zipfile.BadZipFile as exc:
        raise InvalidInput("not a valid .xlsx workbook") from exc
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:  # noqa: BLE001 - any parser failure is a bad upload
        raise InvalidInput("the workbook could not be read") from exc
    ws = wb["Glossary"] if "Glossary" in wb.sheetnames else wb.worksheets[0]
    grid: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        grid.append(["" if v is None else str(v) for v in row])  # a formula cell reads back as its text, never evaluated
        if len(grid) > MAX_ROWS + 1:
            break
    wb.close()
    return grid


def parse(filename: str, data: bytes) -> tuple[list[ParsedRow], list[str]]:
    """Rows of the first sheet keyed by canonical column, plus the header names that are not part of the format."""
    grid = _grid(filename, data)
    csv_input = (filename or "").lower().endswith(".csv")
    while grid and not any(c.strip() for c in grid[0]):
        grid.pop(0)
    if not grid:
        raise InvalidInput("the file is empty")
    names, unknown_headers = _header(grid[0])
    body = grid[1:]
    if len(body) > MAX_ROWS:
        raise InvalidInput(f"at most {MAX_ROWS} rows per import (the file has more); split it")
    out = []
    for i, cells in enumerate(body, 2):
        if not any(c.strip() for c in cells):
            continue
        row = ParsedRow(line=i, cells={})
        for j, raw in enumerate(cells):
            text = csv_unsafe(raw) if csv_input else raw
            key = names[j] if j < len(names) else None
            if key:
                row.cells[key] = text
            elif text.strip() and j < len(grid[0]) and grid[0][j].strip():
                row.unknown.append(grid[0][j].strip()[:80])
        out.append(row)
    return out, unknown_headers


# ------------------------------------------------------------------------------------ import
def _clean(text: str, limit: int) -> str:
    return _CTRL.sub("", text).strip()[:limit]


def _norm(text: str) -> str:
    from analystos.knowledge.glossary_scan import norm

    return norm(text)


def import_rows(session: Session, user: Any, workspace_id: str, filename: str, data: bytes, *, dry_run: bool = False) -> dict[str, Any]:
    """Queue every valid row as a review draft and say, per row, what happened. Nothing is certified here."""
    from analystos.core.ids import new_id
    from analystos.db.models import ContextEntry, KnowledgeSuggestion
    from analystos.knowledge import documents, glossary_scan

    rows, unknown_headers = parse(filename, data)
    visible = _visible_columns(session, user, workspace_id)  # a hidden column reads the same as a missing one
    existing = {_norm(e.name): e for e in session.scalars(select(ContextEntry).where(
        ContextEntry.workspace_id == workspace_id, ContextEntry.kind == "term"))}
    file_hash = hashlib.sha256(data).hexdigest()[:16]
    batch = new_id("gimp")
    seen: dict[str, int] = {}
    results: list[dict[str, Any]] = []
    counts = {"queued": 0, "already_pending": 0, "already_decided": 0, "unchanged": 0, "skipped": 0, "error": 0}

    def done(row: ParsedRow, status: str, term: str = "", **extra: Any) -> None:
        counts[status] += 1
        results.append({"row": row.line, "term": term, "status": status, **extra})

    for row in rows:
        c = row.cells
        term = " ".join(_clean(c.get("term", ""), MAX_TERM * 2).split())
        warnings = [f"unknown_column: {h}" for h in row.unknown]
        if not term or len(term) > MAX_TERM:
            done(row, "error", term[:80], error="term_required" if not term else "term_too_long")
            continue
        definition_raw = _clean(c.get("definition", ""), MAX_DEFINITION * 2)
        if not definition_raw:
            done(row, "error", term, error="definition_required")
            continue
        if len(definition_raw) > MAX_DEFINITION:
            done(row, "error", term, error="definition_too_long")
            continue
        key = _norm(term)
        if not key:
            done(row, "error", term, error="term_required")
            continue
        if key in seen:
            done(row, "error", term, error="duplicate_term", first_row=seen[key])
            continue
        seen[key] = row.line
        definition, screening = documents.screen(definition_raw)
        if screening.instruction_lines:
            warnings.append("instruction_like_text_removed")
        if screening.redactions:
            warnings.append("sensitive_values_redacted")
        if not definition.strip() or definition.strip() == documents._REMOVED:
            done(row, "error", term, error="definition_rejected", warnings=warnings)
            continue
        synonyms = list(dict.fromkeys(_clean(s, MAX_SYNONYM) for s in _items(c.get("synonyms", "")) if s))[:MAX_SYNONYMS]
        links: list[str] = []
        for ref in _items(c.get("linked_columns", "")):
            fq = visible.get(ref.lower())
            if fq is None:
                warnings.append(f"unknown_column: {ref[:80]}")
            elif len(fq) <= MAX_LINK and fq not in links:
                links.append(fq)
        links = links[:MAX_LINKS]
        ignored = [n for n in INFORMATIONAL if c.get(n, "").strip()]
        known = existing.get(key)
        if known is not None and known.body.strip() == definition.strip() \
                and {s.lower() for s in known.synonyms or []} >= {s.lower() for s in synonyms} \
                and {m.lower() for m in known.mapped_columns or []} >= {m.lower() for m in links}:
            done(row, "unchanged", term, warnings=warnings)
            continue
        prov = {"source": SOURCE, "format": "xlsx" if filename.lower().endswith(".xlsx") else "csv",
                "file_sha256": file_hash, "row": row.line}
        fields = {"name": suggestions.field(term, IMPORT_CONFIDENCE, **prov),
                  "body": suggestions.field(definition.strip(), IMPORT_CONFIDENCE, **prov),
                  "synonyms": suggestions.field(synonyms, IMPORT_CONFIDENCE, **prov),
                  "mapped_columns": suggestions.field(links, IMPORT_CONFIDENCE, **prov)}
        subject = glossary_scan.subject_for(key)
        extra = {"warnings": warnings, "existing_term": known.id if known is not None else None,
                 "ignored_columns": ignored}
        digest = suggestions.content_hash("glossary_term", subject, term, fields)
        same = session.scalar(select(KnowledgeSuggestion).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.subject == subject,
            KnowledgeSuggestion.content_hash == digest))
        if same is not None:
            done(row, "already_pending" if same.status == "pending" else "already_decided", term, suggestion_id=same.id, **extra)
            continue
        if dry_run:
            done(row, "queued", term, **extra)
            continue
        draft = suggestions.propose(session, workspace_id, kind="glossary_term", subject=subject, title=term, fields=fields,
                                    origin=SOURCE, proposed_by=f"user:{user.id}", batch=batch)
        if draft is None:
            done(row, "skipped", term, reason="this definition was rejected before", **extra)
        else:
            done(row, "queued", term, suggestion_id=draft.id, **extra)
    return {"batch": None if dry_run else batch, "dry_run": dry_run, "file_sha256": file_hash, "rows": len(rows),
            **counts, "ignored_headers": unknown_headers, "results": results}


__all__ = ["COLUMNS", "FORMATS", "IMPORT_CONFIDENCE", "SOURCE", "collect", "csv_safe", "csv_unsafe", "import_rows", "parse",
           "render", "render_csv", "render_xlsx"]

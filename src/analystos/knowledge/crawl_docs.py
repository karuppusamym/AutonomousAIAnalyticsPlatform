"""Crawl results as OKF v0.2 documents (P4-K06, spec v3 §6.1 layout). Pure renderers: no database.

    sources/<source id>.md                    type: Source
    sources/<source id>.query-patterns.md     type: Query Patterns (value-free, skills/query_history)
    tables/<schema.table>.md                  type: Table; columns beyond 100 in tables/<...>.columns-N.md

Documents are written as drafts by `knowledge/drafts.write_drafts`, which keeps curated documents and
only tightens tags. Nothing here carries a data value: descriptions and names come from the catalog
(already screened on the way in), statistics are left out so that re-crawling unchanged metadata
renders byte-identical documents and writes no revision.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from analystos.knowledge import okf

CRAWLER_ACTOR = "process:analystos-crawler"
COLUMNS_PER_DOC = 100
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_segment(text: str, limit: int = 120) -> str:
    """A path segment the publish policy accepts. A lossy rename gets a short hash so two names that
    sanitize alike ("a b", "a-b") never share a document."""
    s = _UNSAFE.sub("-", text.strip()).strip("-.")[:limit].lower()
    s = re.sub(r"^[^A-Za-z0-9]+", "", s) or "x"
    if s != text.lower():
        s = f"{s}-{hashlib.sha256(text.encode()).hexdigest()[:6]}"
    return s


def table_path(fq: str, part: int = 1) -> str:
    base = f"tables/{safe_segment(fq)}"
    return f"{base}.md" if part == 1 else f"{base}.columns-{part}.md"


def source_path(source_id: str) -> str:
    return f"sources/{safe_segment(source_id)}.md"


def query_patterns_path(source_id: str) -> str:
    return f"sources/{safe_segment(source_id)}.query-patterns.md"


def cell(value: Any, limit: int = 300) -> str:
    """Markdown table cell: one line, pipes escaped, bounded."""
    text = " ".join(str(value if value is not None else "").split())
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _column_rows(columns: list[Mapping[str, Any]]) -> list[str]:
    rows = ["| column | type | business name | description | tags |", "|---|---|---|---|---|"]
    for c in columns:
        rows.append(f"| {cell(c.get('name'))} | {cell(c.get('data_type'))} | {cell(c.get('business_name'))} | "
                    f"{cell(c.get('description'))} | {cell(', '.join(sorted(c.get('tags') or [])))} |")
    return rows


def table_documents(asset: Mapping[str, Any], columns: list[Mapping[str, Any]], *, source_id: str,
                    source_name: str) -> dict[str, str]:
    """{path: text} for one catalog asset. `asset`: schema_name, name, business_name, description,
    reviewed, description_origin, semantics, lifecycle, kind, fingerprint, id."""
    fq = f"{asset['schema_name']}.{asset['name']}"
    sem = asset.get("semantics") or {}
    tags = {"table"} | {f"{k}-{sem[k]}" for k in ("role", "domain") if sem.get(k)}
    tags |= {t for c in columns for t in c.get("tags") or []} & {"pii", "restricted", "sensitive"}
    reviewed = bool(asset.get("reviewed"))
    trusted = reviewed or asset.get("description_origin") in ("user", "rule")
    parts = [columns[i:i + COLUMNS_PER_DOC] for i in range(0, len(columns), COLUMNS_PER_DOC)] or [[]]
    ext: dict[str, Any] = {"kind": "table", "origin": "crawler", "trusted": bool(trusted), "source_id": source_id,
                           "asset_id": asset.get("id"), "fingerprint": asset.get("fingerprint"),
                           "mapped_columns": [f"{fq}.{c['name']}" for c in columns[:COLUMNS_PER_DOC]]}
    if asset.get("business_name"):
        ext["synonyms"] = [str(asset["business_name"])]
    bn = str(asset.get("business_name") or "")
    # a reader (and retrieval) looks for "Orders", not `src_src_1.orders`; the resource keeps the qualified name
    title = (f"{bn} ({asset['name']})" if bn.lower() != str(asset["name"]).lower() else bn) if bn else fq
    fm: dict[str, Any] = {"type": "Table", "title": title, "status": "deprecated" if asset.get("lifecycle") == "deprecated"
                          else ("stable" if reviewed else "draft"),
                          "tags": sorted(_tag(t) for t in tags), "generated": {"by": CRAWLER_ACTOR},
                          "resource": f"analystos://source/{source_id}/{fq}",
                          "sources": [{"id": "catalog", "resource": f"analystos://source/{source_id}",
                                       "title": f"{source_name} catalog"}],
                          "analystos": ext}
    desc = okf_description(asset.get("description"))
    if desc:
        fm["description"] = desc
    body = ["# Description", "",
            f"{asset.get('business_name') or asset['name']}: {asset.get('description') or 'No description yet.'}",
            "", f"Kind {asset.get('kind') or 'table'}; role {sem.get('role') or '-'}, domain {sem.get('domain') or '-'}, "
            f"grain {sem.get('grain') or '-'}. Source: {source_name}.", ""]
    guidelines = sem.get("guidelines") if isinstance(sem.get("guidelines"), dict) else None
    if guidelines and guidelines.get("text"):  # a person's notes for agents (context.lookup); written by people only
        body += ["# Guidelines", "", str(guidelines["text"]), ""]
    body += ["# Columns", ""]
    body += _column_rows(parts[0])
    if len(parts) > 1:
        body += ["", "More columns: " + ", ".join(f"[part {i}]({table_path(fq, i).rsplit('/', 1)[-1]})"
                                                  for i in range(2, len(parts) + 1))]
    out = {table_path(fq): okf.render_document(fm, "\n".join(body))}
    for i, chunk in enumerate(parts[1:], start=2):
        pfm = {"type": "Table Columns", "title": f"{title} (columns part {i})", "status": fm["status"], "tags": ["table"],
               "generated": {"by": CRAWLER_ACTOR},
               "analystos": {"kind": "table_columns", "origin": "crawler", "trusted": bool(trusted), "source_id": source_id,
                             "mapped_columns": [f"{fq}.{c['name']}" for c in chunk]}}
        pbody = ["# Columns", "", f"Continues [{fq}]({table_path(fq).rsplit('/', 1)[-1]}).", "", *_column_rows(chunk)]
        out[table_path(fq, i)] = okf.render_document(pfm, "\n".join(pbody))
    return out


def source_document(source: Mapping[str, Any], assets: Iterable[str], *, deprecated: Iterable[str] = ()) -> str:
    """One document for a source: what it is and which assets the crawl saw (links to their docs)."""
    assets = sorted(assets)
    fm = {"type": "Source", "title": str(source.get("name") or source["id"]), "status": "draft", "tags": ["source"],
          "generated": {"by": CRAWLER_ACTOR}, "resource": f"analystos://source/{source['id']}",
          "analystos": {"kind": "source", "origin": "crawler", "trusted": False, "source_id": source["id"],
                        "source_kind": source.get("kind"), "execution_mode": source.get("execution_mode")}}
    body = ["# Source", "", f"{source.get('name')} ({source.get('kind')}, {source.get('execution_mode') or 'default'} execution).",
            "", "# Assets", ""]
    body += [f"- [{a}](../{table_path(a)})" for a in assets] or ["- none crawled yet"]
    dep = sorted(deprecated)
    if dep:
        body += ["", "# Deprecated", "", *[f"- {a} (absent from the last full crawl)" for a in dep]]
    return okf.render_document(fm, "\n".join(body))


def query_patterns_document(source: Mapping[str, Any], patterns: Mapping[str, Any], *, window: tuple[str, str] | None,
                            known_tables: Iterable[str] | None = None) -> str:
    """Value-free usage patterns of one source (OKF §5.1 usage signals on the audit as the source)."""
    known = set(known_tables or [])
    fm: dict[str, Any] = {
        "type": "Query Patterns", "title": f"Query patterns: {source.get('name') or source['id']}", "status": "draft",
        "tags": ["query-history", "source"], "generated": {"by": CRAWLER_ACTOR},
        "sources": [{"id": "query-audit", "resource": f"analystos://query_execution?source={source['id']}",
                     "title": "Governed query audit (structure only, no values)", "author": "process:analystos-gateway",
                     "usage_count": int(patterns.get("parsed") or 0)}],
        "analystos": {"kind": "query_patterns", "origin": "crawler", "trusted": False, "source_id": source["id"],
                      "statements": patterns.get("statements"), "parsed": patterns.get("parsed"),
                      "unparsed": patterns.get("unparsed")}}
    if window:
        fm["usage_window"] = {"from": window[0], "to": window[1]}

    def link(table: str) -> str:
        return f"[{table}](../{table_path(table)})" if table in known else table

    body = ["# Join paths", "", "| left | right | queries |", "|---|---|---|"]
    body += [f"| {cell(j['left'])} | {cell(j['right'])} | {j['count']} |" for j in patterns.get("joins") or []] or ["| - | - | 0 |"]
    body += ["", "# Tables read", ""] + [f"- {link(t['table'])}: {t['count']} queries" for t in patterns.get("tables") or []]
    body += ["", "# Filters", "", "Column and operator class only; filter values are never recorded.", "",
             "| column | operator | queries |", "|---|---|---|"]
    body += [f"| {cell(f['column'])} | {cell(f['operator'])} | {f['count']} |" for f in patterns.get("filters") or []]
    body += ["", "# Groupings", ""] + [f"- {', '.join(g['columns'])}: {g['count']}" for g in patterns.get("groupings") or []]
    body += ["", "# Frequently used columns", "", "| column | usage | queries |", "|---|---|---|"]
    body += [f"| {cell(c['column'])} | {c['usage']} | {c['count']} |" for c in patterns.get("columns") or []]
    return okf.render_document(fm, "\n".join(body))


def okf_description(text: Any) -> str | None:
    from analystos.knowledge.entries import first_sentence

    return first_sentence(str(text)) if text else None


def _tag(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"

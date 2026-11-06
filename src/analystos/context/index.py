"""The generated context index (P2-8, light version of Google's "context set" root index).

One deterministic `index.md` per workspace and one per domain lists what exists (tables, metrics, glossary terms,
verified queries, documents) with a one-line summary and a path, so an agent reads the index first and fetches only
what it needs (progressive disclosure) instead of receiving every item's text.

* **Machine draft.** `refresh` writes the documents through `knowledge/drafts.write_drafts`: a curated document at the
  same path is kept byte for byte, an unchanged index writes no revision. The documents are `trusted: false` and of
  kind `index`, so neither retrieval nor the ledger treats them as knowledge (no recursion).
* **Per caller.** The pack copy is the workspace-wide view of *internal* entries. What an MCP client or a model
  reads is `view(...)`: the same builder over the caller's `Access` (data scope, denied columns, grant slice), so an
  entry the caller may not see never appears in a line or a count.
* **Domains.** A table's domain is its catalog `semantics.domain` (rule-derived, or a reviewed decision); an entry
  belongs to a domain when every table it references does, or when its provenance names the domain pack.
* **Deterministic.** Sorted, no timestamps; the same catalog and ledger give identical bytes.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.logging import get_logger
from analystos.knowledge import okf

log = get_logger(__name__)

INDEX_ACTOR = "process:analystos-index"
WORKSPACE_PATH = "index.md"
LINE_CHARS = 110
MAX_PER_SECTION = 200
GENERIC_DOMAINS = frozenset({"", "generic", "unknown"})
# section -> ledger kinds listed under it, in reading order: verified queries first (Google's progressive disclosure:
# verified queries, then table context, then concepts)
SECTIONS = (("verified_queries", ("verified_query",)), ("tables", ()), ("metrics", ("metric",)), ("glossary", ("glossary",)),
            ("documents", ("document_section",)))
TITLES = {"verified_queries": "Verified queries", "tables": "Tables", "metrics": "Metrics", "glossary": "Glossary terms",
          "documents": "Documents"}


def domain_path(domain: str) -> str:
    from analystos.knowledge.crawl_docs import safe_segment

    return f"domains/{safe_segment(domain)}/index.md"


def _line(text: Any, limit: int = LINE_CHARS) -> str:
    flat = " ".join(str(text or "").replace("|", "/").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _asset_rows(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    from analystos.db.models import SourceAsset
    from analystos.knowledge.crawl_docs import table_path

    out = []
    for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                       SourceAsset.lifecycle != "deprecated")):
        fq = f"{a.schema_name}.{a.name}"
        out.append({"key": fq, "title": a.business_name or fq, "summary": _line(a.description),
                    "domain": str((a.semantics or {}).get("domain") or "").lower(), "path": table_path(fq)})
    return sorted(out, key=lambda r: r["key"])


def domains(session: Session, workspace_id: str) -> list[str]:
    """Domains the catalog places tables in (reviewed decisions and the crawler's rule), `generic` excluded."""
    return sorted({r["domain"] for r in _asset_rows(session, workspace_id)} - GENERIC_DOMAINS)


def _entry_rows(session: Session, workspace_id: str, access: Any | None) -> list[Any]:
    from analystos.context import retrieval
    from analystos.context.models import ContextLedgerEntry

    if access is not None:
        return retrieval.candidates(session, access, kinds=None, trusts=retrieval.MODEL_TRUSTS)
    rows = session.scalars(select(ContextLedgerEntry).where(
        ContextLedgerEntry.workspace_id == workspace_id, ContextLedgerEntry.valid_to.is_(None),
        ContextLedgerEntry.trust.in_(("certified", "approved", "workspace")),
        ContextLedgerEntry.classification == "internal").order_by(ContextLedgerEntry.id).limit(retrieval.MAX_CANDIDATES))
    return list(rows)


def build(session: Session, workspace_id: str, *, domain: str | None = None, access: Any | None = None) -> dict[str, Any]:
    """The index as data: `{workspace_id, domain, sections: {name: [{key, title, summary, path}]}, counts}`."""
    assets = _asset_rows(session, workspace_id)
    if access is not None:
        allowed = set(access.assets) if access.slice_assets is None else set(access.assets) & set(access.slice_assets)
        assets = [r for r in assets if r["key"] in allowed]
    want = (domain or "").lower() or None
    domain_assets = {r["key"] for r in assets if r["domain"] == want} if want else None
    tables = [r for r in assets if domain_assets is None or r["key"] in domain_assets]
    sections: dict[str, list[dict[str, Any]]] = {"tables": tables}
    for entry in _entry_rows(session, workspace_id, access):
        if domain_assets is not None:
            refs = {".".join(str(r).split(".")[:2]) for r in entry.refs or []}
            named = str((entry.provenance or {}).get("domain_pack") or "").lower() == want
            if not named and not (refs and refs <= domain_assets):
                continue
        name = next((n for n, kinds in SECTIONS if entry.kind in kinds), None)
        if name is not None:
            sections.setdefault(name, []).append({"key": entry.id, "title": _line(entry.title, 80), "summary": _line(entry.summary),
                                                  "path": f"ledger/{entry.kind}/{entry.id}"})
    ordered = {n: sorted(sections.get(n, []), key=lambda r: (r["title"].lower(), r["key"])) for n, _ in SECTIONS}
    return {"workspace_id": workspace_id, "domain": want, "sections": ordered,
            "counts": {n: len(v) for n, v in ordered.items()}}


def render(model: dict[str, Any]) -> str:
    """The index as markdown: counts first, then one line per item (`- title — summary (path)`)."""
    scope = f"domain {model['domain']}" if model.get("domain") else "workspace"
    counts = model["counts"]
    out = [f"# Context index: {scope}", "",
           "Read this first. Open an item by its path (`context.get` for ledger entries, the table document for tables) "
           "only when it matters. Generated; edits are replaced unless the document is marked as reviewed.", "",
           "Contents: " + ", ".join(f"{TITLES[n].lower()} {counts[n]}" for n, _ in SECTIONS)]
    for name, _ in SECTIONS:
        items = model["sections"][name]
        if not items:
            continue
        out += ["", f"## {TITLES[name]} ({len(items)})", ""]
        out += [f"- {r['title']}" + (f" — {r['summary']}" if r["summary"] else "") + f" (`{r['path']}`)"
                for r in items[:MAX_PER_SECTION]]
        if len(items) > MAX_PER_SECTION:
            out.append(f"- … {len(items) - MAX_PER_SECTION} more: search with `context.search`")
    return "\n".join(out)


def document(model: dict[str, Any]) -> str:
    scope = model.get("domain")
    fm = {"type": "Index", "title": f"Context index: {scope or 'workspace'}", "status": "draft", "tags": ["index"],
          "generated": {"by": INDEX_ACTOR},
          "analystos": {"kind": "index", "origin": "index", "trusted": False, "domain": scope, "counts": model["counts"]}}
    return okf.render_document(fm, render(model))


def view(session: Session, user: Any, workspace_id: str, *, domain: str | None = None, domains_: Any = (),
         assets: Any = ()) -> dict[str, Any]:
    """The index a caller may read: built over their own access (and a grant's slice), never from the pack copy."""
    from analystos.context import retrieval

    access = retrieval.access_for(session, user, workspace_id, domains=domains_, assets=assets)
    model = build(session, workspace_id, domain=domain, access=access)
    return {**model, "markdown": render(model)}


def refresh(session: Session, workspace_id: str, *, reason: str = "context index refresh") -> dict[str, Any]:
    """Write the workspace index and one per domain as machine drafts (curated documents are kept)."""
    from analystos.knowledge.drafts import write_drafts

    docs = {WORKSPACE_PATH: document(build(session, workspace_id))}
    for d in domains(session, workspace_id):
        docs[domain_path(d)] = document(build(session, workspace_id, domain=d))
    report = write_drafts(session, workspace_id, docs, author=INDEX_ACTOR, reason=reason, origin="index")
    return report.as_dict()


def refresh_quietly(session: Session, workspace_id: str, *, reason: str) -> None:
    """After a crawl or a review: the owner's write must not fail because the index could not be refreshed."""
    try:
        with session.begin_nested():
            refresh(session, workspace_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        log.warning("context index: refresh of %s failed: %s", workspace_id, exc)

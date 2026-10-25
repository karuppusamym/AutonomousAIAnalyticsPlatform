"""Context service (§11). Layered retrieval:
  1 exact metadata  2 graph neighborhood  3 vector search  4 prior artifacts/episodes
  5 user-provided context (feedback)  6 source inspection happens in the metadata/profiler agents.

Knowledge comes from the workspace's `context_entry` rows and from the knowledge packs (platform
pack, workspace pack, imported bundles) through the knowledge index (P4-K01). Other knowledge
sources are context providers (knowledge/providers.py, P4-K09: `okf_import`, Atlas over `mcp`);
the speculative REST adapter to a Context2AI service was retired with them.
Context is DATA: agents quote it inside an untrusted-context envelope and it can never grant tools
or widen scope."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from analystos.context.embeddings import embed
from analystos.core.ids import new_id
from analystos.core.logging import get_logger
from analystos.db import vectors
from analystos.db.models import EMBEDDING_DIM, ContextEntry, KnowledgeDocument, SourceAsset, SourceColumn
from analystos.graph.projection import neighborhood
from analystos.skills.catalog import screen_for_prompt

log = get_logger(__name__)


def add_entry(session: Session, *, workspace_id: str, kind: str, name: str, body: str,
              synonyms: list[str] | None = None, mapped_columns: list[str] | None = None, origin: str = "user",
              trusted: bool = True) -> ContextEntry:
    if not workspace_id:
        raise ValueError("context entries belong to a workspace; platform knowledge lives in the platform pack")
    entry = ContextEntry(id=new_id("ctx"), workspace_id=workspace_id, kind=kind, name=name, body=body,
                         synonyms=synonyms or [], mapped_columns=mapped_columns or [], origin=origin, trusted=trusted,
                         embedding=embed(" ".join([name, body, *(synonyms or [])])))
    session.add(entry)
    return entry


def search(session: Session, workspace_id: str, query: str, *, limit: int = 8, kinds: list[str] | None = None) -> list[dict]:
    """The workspace's own entries (cosine over their hashing embeddings) merged with pack knowledge
    (hybrid index retrieval, scored by its vector similarity), best first."""
    from analystos.knowledge.index import retrieve

    rows = _nearest_entries(session, workspace_id, embed(query), kinds, limit)
    out = [{"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": e.synonyms,
            "mapped_columns": e.mapped_columns, "origin": e.origin, "score": round(1 - float(d), 4) if d is not None else 0.0}
           for e, d in rows]
    seen: set[str] = set()
    for h in retrieve(session, workspace_id, query, limit=limit * 3, kinds=kinds):
        if h.document_id in seen:
            continue  # one entry per document: its best section
        seen.add(h.document_id)
        out.append(_hit_entry(session, h))
        if len(seen) >= limit:
            break
    from types import SimpleNamespace

    from analystos.services.context_grounding import filter_entries

    allowed = {e.id for e in filter_entries(session, workspace_id, [SimpleNamespace(**e) for e in out])}
    out = [e for e in out if e["id"] in allowed]
    out.sort(key=lambda r: (-r["score"], r["name"], r["id"]))
    return out[:limit]


def _nearest_entries(session: Session, workspace_id: str, vec: list[float], kinds: list[str] | None,
                     limit: int) -> list[tuple[ContextEntry, float | None]]:
    """The workspace's entries nearest `vec`, entries without a vector last. pgvector orders in the
    database; the array backend scores the workspace's entries in-process (db/vectors.py)."""
    if vectors.uses_pgvector():
        rows = session.execute(text(
            "SELECT id, embedding <=> CAST(:q AS vector) AS d FROM context_entry WHERE workspace_id = :w"
            + (" AND kind = ANY(:k)" if kinds else "") + " ORDER BY d NULLS LAST, id LIMIT :n"),
            {"q": vectors.literal(vec), "w": workspace_id, "k": list(kinds or []), "n": limit}).all()
        ranked = [(r.id, None if r.d is None else float(r.d)) for r in rows]
    else:
        stmt = select(ContextEntry.id, ContextEntry.embedding).where(ContextEntry.workspace_id == workspace_id)
        if kinds:
            stmt = stmt.where(ContextEntry.kind.in_(kinds))
        candidates = session.execute(stmt).all()
        ranked = [*vectors.rank(vec, [(r.id, r.embedding) for r in candidates], limit),
                  *sorted((r.id, None) for r in candidates if r.embedding is None)][:limit]
    entries = {e.id: e for e in session.scalars(select(ContextEntry).where(ContextEntry.id.in_([i for i, _ in ranked])))}
    return [(entries[i], d) for i, d in ranked if i in entries]


def reembed_entries(session: Session) -> dict[str, Any]:
    """Recompute every workspace entry's vector, first retyping the column when the vector backend
    changed (`analystos knowledge reembed` runs it with the index re-embed)."""
    current = session.scalar(text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                                  "WHERE attrelid = 'context_entry'::regclass AND attname = 'embedding'"))
    target = vectors.column_sql(EMBEDDING_DIM)
    if current != target:
        session.execute(text(f"ALTER TABLE context_entry ALTER COLUMN embedding TYPE {target} USING NULL"))
        session.execute(text("DEALLOCATE ALL"))  # plans prepared against the old type fail after a retype
    rows = session.execute(select(ContextEntry.id, ContextEntry.name, ContextEntry.body, ContextEntry.synonyms)).all()
    if rows:
        session.execute(text(f"UPDATE context_entry SET embedding = {vectors.cast(':e')} WHERE id = :id"),
                        [{"id": r.id, "e": vectors.literal(embed(" ".join([r.name, r.body, *(r.synonyms or [])])))}
                         for r in rows])
    session.flush()
    return {"column": {"from": current, "to": target}, "entries": len(rows)}


def _hit_entry(session: Session, h: Any) -> dict[str, Any]:
    doc = session.get(KnowledgeDocument, h.document_id)
    ext = (doc.frontmatter or {}).get("analystos") if doc is not None else None
    ext = ext if isinstance(ext, dict) else {}
    return {"id": h.document_id, "kind": h.kind, "name": h.title, "body": h.text, "synonyms": list(ext.get("synonyms") or []),
            "mapped_columns": list(ext.get("mapped_columns") or []),
            "origin": str(ext.get("origin") or f"okf:{h.pack_slug}"), "score": round(float(h.similarity or 0.0), 4),
            "receipt": h.receipt()}


def external_context(session: Session, workspace_id: str, objective: str, *, user: Any = None,
                     run_id: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Results of the workspace's non-local context providers (imported bundles, Atlas over MCP).
    A provider that fails reports UNAVAILABLE; it never fails the run."""
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.knowledge.providers import for_workspace

    out = []
    for provider in for_workspace(load_policy(session, get_workspace(session, workspace_id))):
        if provider.kind == "local":
            continue
        try:
            out.append(provider.retrieve(session, workspace_id, objective, limit=limit, user=user, run_id=run_id).as_dict())
        except Exception as exc:  # noqa: BLE001 - context is best effort
            log.warning("context provider %s failed: %s", provider.kind, type(exc).__name__)
            out.append({"provider": provider.kind, "status": "UNAVAILABLE", "detail": {"reason": type(exc).__name__},
                        "items": [], "receipts": []})
    return out


def _draft(origin: str | None, reviewed: bool) -> bool:
    """An unreviewed model-written description: a draft, which never reaches a prompt (knowledge/suggestions.py)."""
    return origin == "model" and not reviewed


def build_context_package(session: Session, workspace_id: str, objective: str, assets: list[str],
                          extra_notes: list[str] | None = None, *, user: Any = None,
                          run_id: str | None = None, denied_columns: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    denied = set(denied_columns)
    # 1 exact metadata
    tables = []
    for fq in assets:
        schema, name = fq.split(".", 1)
        asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        if not asset:
            continue
        cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)))
        # Same deny filter as the prompt catalog (agents.common.catalog_for_prompt): a masked column is not listed.
        cols = [c for c in cols if f"{fq}.{c.name}" not in denied and f"*.{c.name}" not in denied]
        sem = asset.semantics or {}
        draft = _draft(asset.description_origin, asset.reviewed)
        # The package feeds agent prompts: catalog text of every origin is screened at build (P7-20).
        tables.append({"table": fq, "business_name": screen_for_prompt(
                           sem.get("business_name") if _draft(asset.business_name_origin, asset.reviewed) else asset.business_name,
                           max_chars=200) or None,
                       "description": screen_for_prompt(sem.get("description") if draft else asset.description) or None,
                       "row_count": asset.row_count,
                       "columns": [{"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type,
                                    "business_name": screen_for_prompt(c.business_name, max_chars=200) or None,
                                    "description": screen_for_prompt(
                                        None if _draft(c.description_origin, bool((c.semantics or {}).get("reviewed")))
                                        else c.description) or None, "tags": c.tags}
                                   for c in cols]})
    # 2 graph neighborhood
    graph = neighborhood(assets, workspace_id, session)
    # 3 vector search (+ the workspace's other context providers)
    terms = search(session, workspace_id, objective, limit=12)
    external = external_context(session, workspace_id, objective, user=user, run_id=run_id)
    # 4 prior episodes / known dashboards and metrics
    episodes = search(session, workspace_id, objective, limit=3, kinds=["episode"])
    metrics = search(session, workspace_id, objective, limit=8, kinds=["metric"])
    # Ambiguity: objective words that map to no term, table or column
    known = {t["name"].lower() for t in terms if t["score"] > 0.35} | {c["name"].lower() for t in tables for c in t["columns"]}
    return {"objective": objective, "tables": tables, "glossary": [t for t in terms if t["kind"] != "episode"],
            "metrics": metrics, "graph": graph, "episodes": episodes, "external": external,
            "user_notes": extra_notes or [], "known_terms": sorted(known)[:200],
            "retrieval_layers": ["exact_metadata", "graph_neighborhood", "vector_search", "prior_artifacts", "user_context"]}

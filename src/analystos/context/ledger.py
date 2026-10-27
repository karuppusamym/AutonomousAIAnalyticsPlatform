"""Context Ledger projection (spec S03 §3, task S03-T1).

The owner tables stay the system of record. Each owner table has a *projector*: a pure read of the workspace's
reusable rows of that table, as ``Projected`` values (kind, trust, title, summary, refs, ...). ``reconcile``
compares them with the ledger's open versions and opens, changes or retires versions, writing one
``context_ledger_change`` row per step (the change feed).

Writers do not call the ledger. A session hook marks ``(workspace, owner table)`` whenever an owner row is
flushed, and just before that transaction commits it reconciles the marked pairs in the same transaction (a
savepoint on Postgres, so a projection failure never loses the owner's write; ``analystos context reconcile``
catches the ledger up). The ledger therefore never runs ahead of its owner. Bulk Core updates bypass the hook;
call ``mark`` or ``reconcile`` after one.

Trust is derived by code from the owner's state, never from text: certified (an approved semantic metric, an
active verified query), approved (reviewed mapping, accepted relationship, verified finding, reviewed dataset,
desk-certified answer), workspace, personal (author only) and suggested (drafts, imports).
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from analystos.context.models import ContextLedgerChange, ContextLedgerEntry
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger

log = get_logger(__name__)

KINDS = ("glossary", "metric", "relationship", "verified_query", "finding", "decision", "conversation_summary",
         "correction", "playbook", "dataset", "document_section")
TRUSTS = ("certified", "approved", "workspace", "personal", "suggested")
VISIBILITIES = ("workspace", "tenant", "exported")
CLASSIFICATIONS = ("internal", "confidential", "restricted")
EMBEDDING_MODEL = "local-hash-256"
SUMMARY_CHARS = 1200
DIRTY_KEY = "analystos.ledger.dirty"
ENABLED = True  # tests and one-off maintenance may switch the hook off; reconcile still works explicitly

# context_entry.kind -> ledger kind; episodes, crawler table entries and dashboards are not reusable context
ENTRY_KINDS = {"term": "glossary", "definition": "glossary", "note": "glossary", "rule": "glossary",
               "metric": "metric", "correction": "correction", "negative": "correction",
               "attested_computation": "finding", "conversation_summary": "conversation_summary", "playbook": "playbook"}
# knowledge document / suggestion kind -> ledger kind (catalog drafts are the dataset projection's business)
DOC_KINDS = {"term": "glossary", "definition": "glossary", "note": "glossary", "rule": "glossary", "glossary_term": "glossary",
             "metric": "metric", "negative": "correction", "attested_computation": "finding", "document": "document_section",
             "playbook": "playbook", "verified_query": "verified_query"}
SKIPPED_DOC_KINDS = frozenset({"table", "column", "dashboard", "table_description", "column_description",
                               "description_question", "domain_candidate"})
_SENSITIVE_TAGS = {"pii": "confidential", "sensitive": "confidential", "restricted": "restricted"}


@dataclass(frozen=True)
class Projected:
    """What one owner row contributes to the ledger. `content_hash` covers every field, so any change to
    what the ledger shows (text, trust, refs, ...) opens a new version."""

    ref_table: str
    ref_id: str
    kind: str
    trust: str
    title: str
    summary: str
    source: str = "human"
    classification: str = "internal"
    fingerprint: str | None = None
    owner_id: str | None = None
    visibility: str = "workspace"
    aliases: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None  # an import keeps the pack's; otherwise the projection time

    def content_hash(self) -> str:
        return stable_hash({"kind": self.kind, "trust": self.trust, "title": self.title, "summary": self.summary,
                            "source": self.source, "classification": self.classification, "fingerprint": self.fingerprint,
                            "owner": self.owner_id, "visibility": self.visibility, "aliases": list(self.aliases),
                            "refs": list(self.refs), "provenance": self.provenance,
                            "valid_from": self.valid_from.isoformat() if self.valid_from else None})


def _text(value: Any, limit: int = SUMMARY_CHARS) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _uniq(values: Iterable[Any], limit: int = 20) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        s = " ".join(str(v or "").split())[:200]
        if s and s not in out:
            out.append(s)
    return tuple(out[:limit])


def _source_of(origin: str | None) -> str:
    o = (origin or "").lower()
    if o.startswith("import:okf"):
        return "import:okf"
    if o.startswith(("crawler", "rule", "model", "source", "dbt", "superset")):
        return "crawler"
    if o.startswith(("agent", "learning", "run")):
        return "run"
    if o.startswith("desk"):
        return "desk"
    if o.startswith(("conversation", "ask")):
        return "conversation"
    if o.startswith("mcp"):
        return "mcp"
    return "human"


class _Tags:
    """Column tags of a workspace's catalog, read once per reconcile: an entry's classification is the
    strictest tag of a column it references (pii/sensitive → confidential, restricted → restricted)."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        from analystos.db.models import SourceAsset, SourceColumn

        self.by_column: dict[str, set[str]] = {}
        self.by_asset: dict[str, set[str]] = defaultdict(set)
        rows = session.execute(select(SourceAsset.schema_name, SourceAsset.name, SourceColumn.name, SourceColumn.tags)
                               .join(SourceColumn, SourceColumn.asset_id == SourceAsset.id)
                               .where(SourceAsset.workspace_id == workspace_id)).all()
        for schema, table, col, tags in rows:
            t = {str(x).lower() for x in (tags or [])} & set(_SENSITIVE_TAGS)
            if t:
                self.by_column[f"{schema}.{table}.{col}"] = t
                self.by_asset[f"{schema}.{table}"] |= t

    def classify(self, refs: Iterable[str], *, asset_level: bool = False) -> str:
        level = 0
        for ref in refs:
            tags = self.by_asset.get(ref, set()) if asset_level else self.by_column.get(ref, set())
            for t in tags:
                level = max(level, CLASSIFICATIONS.index(_SENSITIVE_TAGS[t]))
        return CLASSIFICATIONS[level]


# ------------------------------------------------------------------------------------------- projectors
def _context_entries(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import ContextEntry

    out = []
    for e in session.scalars(select(ContextEntry).where(ContextEntry.workspace_id == ws,
                                                        ContextEntry.kind.in_(list(ENTRY_KINDS)))):
        refs = _uniq(e.mapped_columns or [], 50)
        trust = ("suggested" if not e.trusted else
                 "approved" if e.origin in ("reviewed_mapping", "desk_certified") else "workspace")
        out.append(Projected("context_entry", e.id, ENTRY_KINDS[e.kind], trust, _text(e.name, 300), _text(e.body),
                             source=_source_of(e.origin), classification=tags.classify(refs),
                             aliases=_uniq([e.name, *(e.synonyms or [])]), refs=refs,
                             provenance={"origin": e.origin, "entry_kind": e.kind}))
    return out


def _semantic_metrics(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import SemanticMetric

    rows = list(session.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == ws,
                                                             SemanticMetric.status.in_(("approved", "proposed")))
                                .order_by(SemanticMetric.name, SemanticMetric.version.desc())))
    latest: dict[tuple[str, str], Any] = {}
    for m in rows:  # the newest version per (name, status): older approved versions are deprecated anyway
        latest.setdefault((m.name, m.status), m)
    out = []
    for m in latest.values():
        d = m.definition or {}
        refs = _uniq([*(d.get("source_columns") or []), *([d["dataset"]] if d.get("dataset") else [])], 50)
        out.append(Projected("semantic_metric", m.id, "metric", "certified" if m.status == "approved" else "suggested",
                             _text(m.display_name or m.name, 300), _text(d.get("description") or m.expression),
                             source=_source_of(m.proposed_via), classification=tags.classify(refs),
                             fingerprint=m.content_hash, owner_id=m.owner_id or m.proposed_by,
                             aliases=_uniq([m.name, m.display_name]), refs=refs,
                             provenance={"name": m.name, "version": m.version, "status": m.status}))
    return out


def sql_tables(sql: str, dialect: str = "postgres") -> tuple[str, ...]:
    """schema.table names a statement reads (placeholders blanked); empty when it does not parse."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(re.sub(r"\{\{\s*\w+\s*\}\}", "NULL", sql or ""), read=dialect)
        return _uniq(sorted({f"{t.db}.{t.name}" if t.db else t.name for t in tree.find_all(exp.Table)}), 50)
    except Exception:  # noqa: BLE001 - refs are best effort; the gateway re-checks every execution
        return ()


def _verified_queries(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import VerifiedQuery

    out = []
    for q in session.scalars(select(VerifiedQuery).where(VerifiedQuery.workspace_id == ws, VerifiedQuery.status == "active")):
        refs = sql_tables(q.sql_template, q.dialect)
        out.append(Projected("verified_query", q.id, "verified_query", "certified", _text(q.name, 300),
                             _text(q.description or (q.patterns or [q.name])[0]),
                             source="desk" if (q.origin or {}).get("via") == "desk" else "human",
                             classification=tags.classify(refs, asset_level=True), fingerprint=stable_hash(q.sql_template)[:64],
                             owner_id=q.created_by, aliases=_uniq([q.name, *(q.patterns or [])], 12), refs=refs,
                             provenance={"origin": (q.origin or {}).get("type"), "parameters": [p.get("name") for p in q.parameters or []]}))
    return out


def _findings(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import Insight

    out = []
    for i in session.scalars(select(Insight).where(Insight.workspace_id == ws, Insight.status == "verified",
                                                   Insight.verified.is_(True), Insight.stale_since.is_(None))):
        ver = i.verification or {}
        out.append(Projected("insight", i.id, "finding", "approved", _text(i.title, 300), _text(i.finding),
                             source="run", fingerprint=(str(ver.get("fingerprint"))[:64] if ver.get("fingerprint") else None),
                             provenance={"run_id": i.run_id, "code": i.code, "governance": i.governance}))
    return out


def _relationships(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import SemanticRelationshipCandidate as Rel

    out = []
    for r in session.scalars(select(Rel).where(Rel.workspace_id == ws, Rel.status.in_(("accepted", "pending")))):
        left = [f"{r.from_asset}.{c}" for c in r.from_columns or []]
        right = [f"{r.to_asset}.{c}" for c in r.to_columns or []]
        title = r.relationship_name or f"{r.from_asset} → {r.to_asset}"
        summary = f"{', '.join(left)} = {', '.join(right)} ({r.cardinality})"
        refs = _uniq([*left, *right], 50)
        out.append(Projected("semantic_relationship_candidate", r.id, "relationship",
                             "approved" if r.status == "accepted" else "suggested", _text(title, 300), _text(summary),
                             source=_source_of(r.origin), classification=tags.classify(refs), refs=refs,
                             aliases=_uniq([title]), provenance={"cardinality": r.cardinality, "status": r.status}))
    return out


def _datasets(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import SourceAsset

    out = []
    for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.selected.is_(True),
                                                       SourceAsset.lifecycle == "active")):
        fq = f"{a.schema_name}.{a.name}"
        draft = a.description_origin == "model" and not a.reviewed  # an unreviewed model draft never reaches context
        title = a.business_name if a.business_name and not (a.business_name_origin == "model" and not a.reviewed) else fq
        out.append(Projected("source_asset", a.id, "dataset", "approved" if a.reviewed else "workspace", _text(title, 300),
                             _text(f"{fq}: {'' if draft else (a.description or '')}".rstrip(": ")),
                             source="crawler", classification=tags.classify([fq], asset_level=True),
                             aliases=_uniq([fq, a.name, a.source_name, title]), refs=(fq,),
                             provenance={"source_id": a.source_id}))
    return out


def _documents(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    from analystos.db.models import KnowledgeDocument, KnowledgePack

    out = []
    rows = session.execute(select(KnowledgeDocument, KnowledgePack.kind, KnowledgePack.slug)
                           .join(KnowledgePack, KnowledgePack.id == KnowledgeDocument.pack_id)
                           .where(KnowledgeDocument.workspace_id == ws)).all()
    for d, pack_kind, slug in rows:
        if d.kind in SKIPPED_DOC_KINDS:
            continue
        ext = (d.frontmatter or {}).get("analystos")
        ext = ext if isinstance(ext, dict) else {}
        refs = _uniq(ext.get("mapped_columns") or [], 50)
        if ext.get("trusted") is False or d.status == "draft":
            trust = "suggested"
        elif str(d.trust_tier).lower() in ("verified", "certified", "authoritative"):
            trust = "approved"
        else:
            trust = "workspace"
        out.append(Projected("knowledge_document", d.id, DOC_KINDS.get(d.kind, "document_section"), trust,
                             _text(d.title, 300), _text(d.description or d.body), source="human" if pack_kind == "workspace" else "pack",
                             classification=tags.classify(refs), fingerprint=d.sha256,
                             aliases=_uniq([d.title, *(ext.get("synonyms") or [])]), refs=refs,
                             provenance={"pack": slug, "pack_kind": pack_kind, "path": d.path, "revision": d.revision}))
    return out


def _suggestions(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    """Pending drafts, including OKF imports (`origin=import:okf`): trust `suggested`, never in a model input
    unless the caller asks. An imported document keeps what its pack declared in the `ledger` field."""
    from analystos.db.models import KnowledgeSuggestion
    from analystos.knowledge.suggestions import primary_field

    out = []
    for s in session.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == ws,
                                                               KnowledgeSuggestion.status == "pending")):
        fields = s.fields or {}
        declared = (fields.get("ledger") or {}).get("value") if isinstance(fields.get("ledger"), dict) else None
        if isinstance(declared, dict):
            kind = declared.get("kind") if declared.get("kind") in KINDS else DOC_KINDS.get(s.kind, "document_section")
            refs = _uniq(declared.get("refs") or [], 50)
            classification = declared.get("classification") if declared.get("classification") in CLASSIFICATIONS else "internal"
            out.append(Projected("knowledge_suggestion", s.id, kind, "suggested", _text(declared.get("title") or s.title, 300),
                                 str(declared.get("summary") or "")[:SUMMARY_CHARS * 4], source=_source_of(s.origin),
                                 classification=CLASSIFICATIONS[max(CLASSIFICATIONS.index(classification),
                                                                    CLASSIFICATIONS.index(tags.classify(refs)))],
                                 fingerprint=declared.get("fingerprint") or None,
                                 aliases=_uniq(declared.get("aliases") or []), refs=refs,
                                 provenance={"declared_trust": declared.get("trust") if declared.get("trust") in TRUSTS else "suggested",
                                             "okf_path": declared.get("path"), "pack": s.batch},
                                 valid_from=_instant(declared.get("valid_from"))))
            continue
        if s.kind in SKIPPED_DOC_KINDS or s.kind not in DOC_KINDS:
            continue
        main = (fields.get(primary_field(s.kind)) or {}).get("value") if isinstance(fields.get(primary_field(s.kind)), dict) else None
        out.append(Projected("knowledge_suggestion", s.id, DOC_KINDS[s.kind], "suggested", _text(s.title, 300), _text(main or s.title),
                             source=_source_of(s.origin), provenance={"origin": s.origin, "subject": s.subject}))
    return out


def _desk_playbooks(session: Session, ws: str, tags: _Tags) -> list[Projected]:
    """A Request Desk answer an analyst certified as a playbook (S13-T5): the request is its owner row."""
    from analystos.desk.models import Request

    out = []
    for r in session.scalars(select(Request).where(Request.workspace_id == ws, Request.state.in_(("answered", "accepted")))
                             .order_by(Request.created_at.desc()).limit(5000)):
        pb = (r.answer_ref or {}).get("playbook")
        if not isinstance(pb, dict):
            continue
        out.append(Projected("request", r.id, "playbook", "approved", _text(pb.get("title") or r.text, 300),
                             _text(pb.get("steps") or (r.answer_ref or {}).get("text") or ""), source="desk",
                             owner_id=pb.get("certified_by"), aliases=_uniq([pb.get("title"), r.text]),
                             provenance={"request_class": r.request_class, "certified_at": pb.get("at")}))
    return out


Projector = Callable[[Session, str, _Tags], list[Projected]]
PROJECTORS: dict[str, Projector] = {
    "context_entry": _context_entries, "semantic_metric": _semantic_metrics, "verified_query": _verified_queries,
    "insight": _findings, "semantic_relationship_candidate": _relationships, "source_asset": _datasets,
    "knowledge_document": _documents, "knowledge_suggestion": _suggestions, "request": _desk_playbooks,
}


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------------------- reconcile
def reconcile(session: Session, workspace_id: str, tables: Iterable[str] | None = None, *,
              now: datetime | None = None) -> dict[str, int]:
    """Bring the workspace's ledger in line with its owners for `tables` (all by default). Idempotent."""
    from analystos.db.models import Workspace

    if session.get(Workspace, workspace_id) is None:
        return {}
    now = now or utcnow()
    tags = _Tags(session, workspace_id)
    counts = {"added": 0, "changed": 0, "retired": 0}
    for table in tables or PROJECTORS:
        owned = {p.ref_id: p for p in PROJECTORS[table](session, workspace_id, tags)}
        open_rows = {r.ref_id: r for r in session.scalars(select(ContextLedgerEntry).where(
            ContextLedgerEntry.workspace_id == workspace_id, ContextLedgerEntry.ref_table == table,
            ContextLedgerEntry.valid_to.is_(None)))}
        top = dict(session.execute(select(ContextLedgerEntry.ref_id, func.max(ContextLedgerEntry.version)).where(
            ContextLedgerEntry.workspace_id == workspace_id, ContextLedgerEntry.ref_table == table)
            .group_by(ContextLedgerEntry.ref_id)).all())
        for ref_id in sorted(owned):
            p = owned[ref_id]
            digest = p.content_hash()
            cur = open_rows.pop(ref_id, None)
            if cur is not None and cur.content_hash == digest:
                continue
            if cur is not None:
                cur.valid_to = now
            version = int(top.get(ref_id) or 0) + 1
            entry = _entry(workspace_id, p, version, digest, now)
            session.add(entry)
            op = "changed" if cur is not None else "added"
            session.add(ContextLedgerChange(workspace_id=workspace_id, entry_id=entry.id, kind=p.kind, op=op, version=version,
                                            at=now))
            counts[op] += 1
        for r in sorted(open_rows.values(), key=lambda x: x.id):
            r.valid_to = now
            session.add(ContextLedgerChange(workspace_id=workspace_id, entry_id=r.id, kind=r.kind, op="retired",
                                            version=r.version, at=now))
            counts["retired"] += 1
    session.flush()
    return counts


def _entry(workspace_id: str, p: Projected, version: int, digest: str, now: datetime) -> ContextLedgerEntry:
    from analystos.context.embeddings import embed

    return ContextLedgerEntry(
        id=new_id("cle"), workspace_id=workspace_id, kind=p.kind, ref_table=p.ref_table, ref_id=p.ref_id, version=version,
        trust=p.trust, visibility=p.visibility, title=p.title or "untitled", summary=p.summary, classification=p.classification,
        valid_from=p.valid_from or now, valid_to=None, fingerprint=p.fingerprint,
        embedding=embed(" ".join([p.title, *p.aliases, p.summary[:600]])), embedding_model=EMBEDDING_MODEL,
        owner_id=p.owner_id, source=p.source, aliases=list(p.aliases), refs=list(p.refs), content_hash=digest,
        provenance=p.provenance)


def check_consistency(session: Session, workspace_id: str) -> dict[str, list[str]]:
    """The ledger-owner consistency check (spec S03 §7): every open ledger row resolves to an owner row that still
    qualifies with the same content, and every qualifying owner row has an open ledger row."""
    tags = _Tags(session, workspace_id)
    problems: dict[str, list[str]] = {"dangling": [], "missing": [], "stale": [], "unresolved": []}
    for table, projector in PROJECTORS.items():
        owned = {p.ref_id: p for p in projector(session, workspace_id, tags)}
        for r in session.scalars(select(ContextLedgerEntry).where(ContextLedgerEntry.workspace_id == workspace_id,
                                                                  ContextLedgerEntry.ref_table == table,
                                                                  ContextLedgerEntry.valid_to.is_(None))):
            if owner_of(session, r) is None:
                problems["unresolved"].append(r.id)
            p = owned.pop(r.ref_id, None)
            if p is None:
                problems["dangling"].append(r.id)
            elif p.content_hash() != r.content_hash:
                problems["stale"].append(r.id)
        problems["missing"] += [f"{table}:{ref}" for ref in sorted(owned)]
    return problems


OWNER_MODELS: dict[str, str] = {"context_entry": "ContextEntry", "semantic_metric": "SemanticMetric",
                                "verified_query": "VerifiedQuery", "insight": "Insight",
                                "semantic_relationship_candidate": "SemanticRelationshipCandidate",
                                "source_asset": "SourceAsset", "knowledge_document": "KnowledgeDocument",
                                "knowledge_suggestion": "KnowledgeSuggestion", "request": "Request"}


def owner_of(session: Session, entry: ContextLedgerEntry) -> Any:
    """The owner row a ledger row projects, in the entry's own workspace, or None."""
    from analystos.db import models

    name = OWNER_MODELS.get(entry.ref_table)
    model = getattr(models, name, None) if name else None
    row = session.get(model, entry.ref_id) if model is not None else None
    return row if row is not None and getattr(row, "workspace_id", None) == entry.workspace_id else None


# ------------------------------------------------------------------------------------------- the session hook
def _owner_tables() -> dict[type, str]:
    from analystos.db import models

    return {getattr(models, name): table for table, name in OWNER_MODELS.items()}


_TABLE_OF: dict[type, str] | None = None


def mark(session: Session, workspace_id: str, table: str) -> None:
    """Ask the hook to reconcile (workspace, table) when this transaction commits (after a bulk Core write)."""
    session.info.setdefault(DIRTY_KEY, set()).add((workspace_id, table))


def _relevant(obj: Any, table: str) -> bool:
    if table == "request":  # only a certified playbook makes a request part of the ledger
        return isinstance((getattr(obj, "answer_ref", None) or {}).get("playbook"), dict)
    return True


@event.listens_for(Session, "before_flush")
def _collect(session: Session, flush_context: Any, instances: Any) -> None:
    global _TABLE_OF
    if not ENABLED:
        return
    if _TABLE_OF is None:
        _TABLE_OF = _owner_tables()
    for obj in (*session.new, *session.dirty, *session.deleted):
        table = _TABLE_OF.get(type(obj))
        ws = getattr(obj, "workspace_id", None) if table else None
        if ws and _relevant(obj, table):
            mark(session, ws, table)


@event.listens_for(Session, "before_commit")
def _project(session: Session) -> None:
    if not ENABLED:
        return
    if session.new or session.dirty or session.deleted:
        session.flush()  # runs _collect for what this commit would flush anyway
    work = session.info.pop(DIRTY_KEY, None)
    if not work:
        return
    by_ws: dict[str, set[str]] = defaultdict(set)
    for ws, table in work:
        by_ws[ws].add(table)
    postgres = session.get_bind().dialect.name == "postgresql"
    for ws in sorted(by_ws):
        tables = sorted(by_ws[ws])
        try:
            if postgres:
                with session.begin_nested():
                    reconcile(session, ws, tables)
            else:
                reconcile(session, ws, tables)
        except Exception as exc:  # noqa: BLE001 - the owner's write must commit; `context reconcile` catches up
            log.warning("context ledger: projection of %s %s failed: %s", ws, tables, exc)
    session.info.pop(DIRTY_KEY, None)  # what the reconcile itself flushed is ledger rows, not owners


@event.listens_for(Session, "after_soft_rollback")
def _forget(session: Session, previous_transaction: Any) -> None:
    session.info.pop(DIRTY_KEY, None)

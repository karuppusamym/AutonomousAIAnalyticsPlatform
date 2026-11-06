"""Context Service reads over the ledger (spec S03 §4-§5, tasks S03-T2/T4): search, get, compile, changes.

Access is filtered **before** ranking, so an entry the caller may not see never reaches a score, a receipt or a
count. The filter is the caller's own: workspace membership (RLS already limits the tenant), trust (`personal`
only for its author, `suggested` only when asked for and never in a model input), and data scope: an entry that
references a catalogued table outside the caller's scope, or a column the policy denies them (PII without
clearance, restricted columns, attribute rules), is withheld. A reference to a table the workspace does not
catalogue reveals no data and does not withhold the entry.

Ranking is hybrid and deterministic: exact name/alias match → glossary synonyms (term overlap) → lineage
proximity (shares a table with an exact match) → vector similarity (the platform's local hashing embedding),
reranked by trust × freshness × in-scope × usage (a bounded lift for tables that distinct users and runs read in the
last 30 days, `context/usage.py`; it never admits what the filter withheld). Context is data: compiled text is screened and stays inside the
untrusted-context envelope of whichever prompt it reaches; nothing in it changes tools, scope or policy.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.context.models import ContextLedgerChange, ContextLedgerEntry
from analystos.core.errors import ContextConflict, ContextOverBudget, InvalidInput, NotFound
from analystos.core.ids import stable_hash, utcnow
from analystos.governance.policy import load_in_workspace, scoped_loader

TRUST_WEIGHT = {"certified": 1.0, "approved": 0.9, "workspace": 0.8, "personal": 0.75, "suggested": 0.4}
MODEL_TRUSTS = ("certified", "approved", "workspace", "personal")  # `suggested` never reaches a model input
SECTION_ORDER = ("glossary", "metric", "verified_query", "correction", "playbook", "relationship", "finding", "dataset",
                 "document_section", "decision", "conversation_summary")
MAX_CANDIDATES = 5000  # a workspace's ledger is small; this bounds a pathological one (ADR-014)
MIN_RELEVANCE = 0.12
CHARS_PER_TOKEN = 3.6  # the platform's estimate (agents/common.py budgets prompts the same way)
ITEM_CHARS = 600
PINNED_SECTION = "pinned_brief"
INDEX_SECTION = "index"
INDEX_ITEM_CHARS = 160  # with the index read first, an item is a pointer; `context.get` returns the full text
INDEX_LISTED = 25       # titles per kind in the compiled index digest
_WS = re.compile(r"\s+")


@dataclass
class Access:
    """What one caller may see in one workspace, resolved once per request."""

    workspace_id: str
    user_id: str
    role: str
    assets: set[str]
    denied: set[str]
    known_assets: set[str]
    scope: Any = None
    # P2-8: an MCP grant's slice. None = the whole workspace; otherwise the `schema.table` assets (and the domain
    # keys) an entry must stay inside: an entry that is out of the slice is withheld before ranking, like a denied one
    slice_assets: frozenset[str] | None = None
    slice_domains: frozenset[str] = frozenset()

    def in_slice(self, entry: Any) -> bool:
        if self.slice_assets is None:
            return True
        refs = [".".join(str(r).split(".")[:2]) for r in entry.refs or []]
        if refs:
            return all(r in self.slice_assets for r in refs)
        # no table reference: only an entry that names its domain pack (provenance) can be placed in a domain
        return str((entry.provenance or {}).get("domain_pack") or "") in self.slice_domains

    def allows(self, entry: Any) -> bool:
        if entry.trust == "personal" and entry.owner_id != self.user_id:
            return False
        if not self.in_slice(entry):
            return False
        for ref in entry.refs or []:
            parts = str(ref).split(".")
            asset = ".".join(parts[:2])
            if asset in self.known_assets and asset not in self.assets:
                return False
            if len(parts) >= 3 and (ref in self.denied or f"*.{parts[-1]}" in self.denied):
                return False
        return True

    def in_scope(self, entry: Any) -> bool:
        return all(".".join(str(r).split(".")[:2]) in self.assets for r in entry.refs or [])


def slice_assets(session: Session, workspace_id: str, domains: Iterable[str] = (), assets: Iterable[str] = ()) -> frozenset[str]:
    """`schema.table` assets of a grant's slice: the named assets plus every asset whose reviewed/derived catalog
    domain (`semantics.domain`) is one of `domains`. Names are matched case-insensitively against the catalog."""
    from analystos.db.models import SourceAsset

    want_domains = {str(d).lower() for d in domains}
    want_assets = {str(a).lower() for a in assets}
    out: set[str] = set()
    for schema, name, sem in session.execute(select(SourceAsset.schema_name, SourceAsset.name, SourceAsset.semantics)
                                             .where(SourceAsset.workspace_id == workspace_id)):
        key = f"{schema}.{name}"
        if key.lower() in want_assets or str((sem or {}).get("domain") or "").lower() in want_domains:
            out.add(key)
    return frozenset(out)


def access_for(session: Session, user: Any, workspace_id: str, *, domains: Iterable[str] = (),
               assets: Iterable[str] = ()) -> Access:
    from analystos.db.models import SourceAsset
    from analystos.governance.policy import require_role, resolve_scope

    role = require_role(session, user, workspace_id, "viewer")  # 404 for a workspace the caller cannot see
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    known = {f"{s}.{n}" for s, n in session.execute(select(SourceAsset.schema_name, SourceAsset.name)
                                                     .where(SourceAsset.workspace_id == workspace_id))}
    domains, assets = [str(d) for d in domains], [str(a) for a in assets]
    sliced = bool(domains or assets)
    return Access(workspace_id=workspace_id, user_id=user.id, role=role, assets=set(scope.assets),
                  denied=set(scope.denied_columns), known_assets=known, scope=scope,
                  slice_assets=slice_assets(session, workspace_id, domains, assets) if sliced else None,
                  slice_domains=frozenset(d.lower() for d in domains))


def _norm(text: Any) -> str:
    return _WS.sub(" ", str(text or "").lower().replace("_", " ")).strip(" ?.!")


def candidates(session: Session, access: Access, *, kinds: list[str] | None, trusts: tuple[str, ...]) -> list[Any]:
    stmt = select(ContextLedgerEntry).where(
        ContextLedgerEntry.workspace_id == access.workspace_id, ContextLedgerEntry.valid_to.is_(None),
        ContextLedgerEntry.trust.in_(trusts),
        or_(ContextLedgerEntry.trust != "personal", ContextLedgerEntry.owner_id == access.user_id))
    if kinds:
        stmt = stmt.where(ContextLedgerEntry.kind.in_(kinds))
    rows = session.scalars(stmt.order_by(ContextLedgerEntry.id).limit(MAX_CANDIDATES)).all()
    return [r for r in rows if access.allows(r)]  # the access pre-filter: before any scoring


@dataclass
class Hit:
    entry: Any
    score: float
    relevance: float
    why: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        e = self.entry
        return {"id": e.id, "kind": e.kind, "title": e.title, "summary": e.summary[:300], "trust": e.trust,
                "version": e.version, "score": round(self.score, 4), "why": {k: round(v, 4) for k, v in self.why.items()}}


def _freshness(valid_from: datetime | None, now: datetime) -> float:
    if valid_from is None:
        return 0.7
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=now.tzinfo)
    days = max(0.0, (now - valid_from).total_seconds() / 86400)
    return 0.7 + 0.3 * math.exp(-days / 365)


def rank(entries: list[Any], query: str, access: Access, *, now: datetime | None = None,
         usage: dict[str, float] | None = None) -> list[Hit]:
    """Score already-permitted entries for `query`. Pure apart from the embedding, so it is unit-tested directly.
    `usage` maps `schema.table` to its bounded usage factor (`context.usage.factors`); absent means no usage signal."""
    from analystos.context.compiler import terms
    from analystos.context.embeddings import embed
    from analystos.context.usage import entry_factor

    now = now or utcnow()
    q = _norm(query)
    q_terms = terms(query)
    q_vec = embed(query) if q else None
    hits: list[Hit] = []
    for e in entries:
        names = [_norm(n) for n in [e.title, *(e.aliases or [])] if n]
        exact = 1.0 if q and q in names else 0.0
        mention = 0.0
        if q and not exact:  # the name is in the question ("active users last week"), or the question in a name
            if any(len(n) >= 3 and re.search(rf"\b{re.escape(n)}\b", q) for n in names):
                mention = 0.7
            elif len(q) >= 4 and any(q in n for n in names):
                mention = 0.5
        name_terms = set().union(*(terms(n) for n in names)) if names else set()
        synonyms = len(q_terms & name_terms) / len(q_terms) if q_terms else 0.0
        body = len(q_terms & terms(e.summary)) / len(q_terms) if q_terms else 0.0
        vector = 0.0
        if q_vec is not None and e.embedding is not None:
            vector = max(0.0, float(sum(a * b for a, b in zip(q_vec, e.embedding, strict=False))))
        relevance = max(exact, mention) + 0.35 * synonyms + 0.15 * body + 0.25 * vector if q else 0.5
        hits.append(Hit(e, 0.0, relevance, {"exact": exact, "mention": mention, "synonyms": synonyms, "body": body,
                                            "vector": vector}))
    # lineage proximity: an entry sharing a table with an exact or named match is probably about the same thing
    anchors = {".".join(str(r).split(".")[:2]) for h in hits if h.why["exact"] or h.why["mention"] for r in h.entry.refs or []}
    for h in hits:
        tables = {".".join(str(r).split(".")[:2]) for r in h.entry.refs or []}
        prox = 0.15 if anchors and tables & anchors and not (h.why["exact"] or h.why["mention"]) else 0.0
        h.why["lineage"] = prox
        h.relevance += prox
        h.why["usage"] = entry_factor(h.entry.refs, usage)
        h.score = (h.relevance * TRUST_WEIGHT.get(h.entry.trust, 0.4) * _freshness(h.entry.valid_from, now)
                   * (1.0 if access.in_scope(h.entry) else 0.9) * h.why["usage"])
    hits = [h for h in hits if not q or h.relevance >= MIN_RELEVANCE]
    hits.sort(key=lambda h: (-h.score, h.entry.kind, h.entry.title, h.entry.id))
    return hits


def search(session: Session, user: Any, workspace_id: str, query: str, *, kinds: list[str] | None = None,
           include_suggested: bool = False, limit: int = 20, access: Access | None = None,
           use_usage: bool = True, domains: Iterable[str] = (), assets: Iterable[str] = ()) -> list[Hit]:
    from analystos.context.ledger import KINDS

    unknown = [k for k in kinds or [] if k not in KINDS]
    if unknown:
        raise InvalidInput(f"unknown context kinds: {', '.join(unknown)} (known: {', '.join(KINDS)})")
    access = access or access_for(session, user, workspace_id, domains=domains, assets=assets)
    trusts = (*MODEL_TRUSTS, "suggested") if include_suggested else MODEL_TRUSTS
    entries = candidates(session, access, kinds=kinds, trusts=trusts)
    usage = None
    if use_usage and any(e.refs for e in entries):
        from analystos.context.usage import factors

        usage = factors(session, workspace_id)
    return rank(entries, query, access, usage=usage)[: max(1, min(int(limit), 200))]


@scoped_loader
def get(session: Session, user: Any, entry_id: str, *, workspace_id: str | None = None,
        domains: Iterable[str] = (), assets: Iterable[str] = ()) -> dict[str, Any]:
    """One entry with its body read from the owner table, if the caller may see it (else 404, never 403)."""
    from analystos.context.ledger import owner_of

    entry = load_in_workspace(session, ContextLedgerEntry, entry_id, workspace_id, user=user, label="context entry")
    access = access_for(session, user, entry.workspace_id, domains=domains, assets=assets)
    if not access.allows(entry):
        raise NotFound("context entry not found")
    owner = owner_of(session, entry)
    return {"id": entry.id, "workspace_id": entry.workspace_id, "kind": entry.kind, "title": entry.title,
            "summary": entry.summary, "trust": entry.trust, "version": entry.version, "visibility": entry.visibility,
            "classification": entry.classification, "valid_from": _iso(entry.valid_from), "valid_to": _iso(entry.valid_to),
            "current": entry.valid_to is None, "fingerprint": entry.fingerprint, "source": entry.source,
            "aliases": list(entry.aliases or []), "refs": list(entry.refs or []), "provenance": dict(entry.provenance or {}),
            "ref": {"table": entry.ref_table, "id": entry.ref_id},
            "body": body_of(entry, owner, access) if owner is not None and entry.valid_to is None else None}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def body_of(entry: Any, owner: Any, access: Access) -> dict[str, Any]:
    """The owner's content for this entry. Definitions and statements only: never result rows."""
    t = entry.ref_table
    if t == "context_entry":
        return {"name": owner.name, "body": owner.body, "synonyms": list(owner.synonyms or []),
                "mapped_columns": list(owner.mapped_columns or [])}
    if t == "semantic_metric":
        return {"name": owner.name, "version": owner.version, "status": owner.status, "expression": owner.expression,
                "definition": owner.definition}
    if t == "verified_query":
        return {"name": owner.name, "description": owner.description, "patterns": list(owner.patterns or []),
                "parameters": list(owner.parameters or []), "sql_template": owner.sql_template, "dialect": owner.dialect}
    if t == "insight":
        return {"title": owner.title, "finding": owner.finding, "confidence": owner.confidence, "caveats": owner.caveats,
                "evidence": [{"type": e.get("type"), "id": e.get("id")} for e in owner.evidence or []], "run_id": owner.run_id}
    if t == "semantic_relationship_candidate":
        return {"from": owner.from_asset, "from_columns": owner.from_columns, "to": owner.to_asset,
                "to_columns": owner.to_columns, "cardinality": owner.cardinality, "status": owner.status}
    if t == "source_asset":
        from sqlalchemy.orm import object_session

        from analystos.db.models import SourceColumn

        fq = f"{owner.schema_name}.{owner.name}"
        cols = object_session(owner).scalars(select(SourceColumn).where(SourceColumn.asset_id == owner.id)
                                             .order_by(SourceColumn.ordinal)).all()
        return {"table": fq, "business_name": owner.business_name, "row_count": owner.row_count,
                "columns": [{"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type}
                            for c in cols if f"{fq}.{c.name}" not in access.denied and f"*.{c.name}" not in access.denied]}
    if t == "knowledge_document":
        return {"path": owner.path, "title": owner.title, "body": owner.body[:20000], "sha256": owner.sha256}
    if t == "knowledge_suggestion":
        return {"title": owner.title, "kind": owner.kind, "status": owner.status, "origin": owner.origin,
                "fields": {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (owner.fields or {}).items()}}
    if t == "request":
        ref = owner.answer_ref or {}
        return {"request": owner.text, "playbook": ref.get("playbook"), "answer": ref.get("text")}
    if t == "conversation_summary":  # S04: claims citing messages, corrections, open questions; never result rows
        summary = owner.summary or {}
        return {"conversation_id": owner.conversation_id, "version": owner.version, "trust": owner.trust,
                **{k: summary.get(k) for k in ("questions", "answers", "corrections", "open_questions")}}
    if t == "conversation_correction":
        return {"term": owner.term, "meaning": owner.meaning, "scope": owner.scope, "status": owner.status}
    if t == "context_federated_entry":  # S03-T7: a read-only copy of another workspace's entry
        return {"title": owner.title, "summary": owner.summary, "aliases": list(owner.aliases or []),
                "refs": list(owner.refs or []), "source_key": owner.source_key, "source_entry_id": owner.source_entry_id,
                "source_version": owner.source_version, "source_trust": owner.source_trust, "read_only": True}
    return {}


# ------------------------------------------------------------------------------------------- compile
def _conflicts(session: Session, access: Access, hits: list[Hit]) -> list[dict[str, Any]]:
    """Certified entries of the same kind and name that disagree: the bundle is blocked (v1 §6.2.4)."""
    names = {(h.entry.kind, _norm(h.entry.title)) for h in hits if h.entry.trust == "certified"}
    if not names:
        return []
    certified = [e for e in candidates(session, access, kinds=sorted({k for k, _ in names}), trusts=("certified",))]
    groups: dict[tuple[str, str], list[Any]] = {}
    for e in certified:
        for alias in {_norm(e.title), *(_norm(a) for a in e.aliases or [])}:
            if (e.kind, alias) in names:
                groups.setdefault((e.kind, alias), []).append(e)
    out = []
    for (kind, name), entries in sorted(groups.items()):
        distinct = {(e.fingerprint or e.content_hash) for e in {x.id: x for x in entries}.values()}
        if len(distinct) > 1:
            out.append({"kind": kind, "name": name, "entries": sorted({e.id for e in entries})})
    return out


def _item(entry: Any, text: str) -> dict[str, Any]:
    return {"id": entry.id, "version": entry.version, "title": entry.title, "trust": entry.trust, "text": text}


def _size(value: Any) -> int:
    from analystos.context.compiler import compact

    return len(compact(value)) + 1


def compile_bundle(session: Session, user: Any, workspace_id: str, *, purpose: str, objective: str,
                   budget_tokens: int = 2000, kinds: list[str] | None = None, pinned: dict[str, Any] | None = None,
                   pinned_receipts: list[dict[str, Any]] | None = None, run_id: str | None = None,
                   domains: Iterable[str] = (), assets: Iterable[str] = (),
                   index_first: bool = False, domain: str | None = None) -> dict[str, Any]:
    """`ContextBundle{sections, receipts, omissions, identity_hash, budget_used}` for one model purpose.

    Suggested entries never enter it. A run's reviewed brief (`run_id`) or a caller's `pinned` section is
    mandatory: if it alone does not fit the budget the compile is refused (ContextOverBudget), never trimmed.
    Conflicting certified definitions raise ContextConflict.

    `index_first` (P2-8, progressive disclosure): the bundle opens with the caller's context index (counts and titles per
    kind, built over the same access filter) and carries each item as a short pointer instead of its long excerpt, so the
    same budget covers more of the workspace; `domain` limits both to one domain."""
    from analystos.context.compiler import focused_excerpt, terms
    from analystos.security.injection import is_injection

    if not (purpose or "").strip():
        raise InvalidInput("purpose is required")
    budget_tokens = max(50, min(int(budget_tokens or 2000), 200_000))
    budget = int(budget_tokens * CHARS_PER_TOKEN)
    access = access_for(session, user, workspace_id, domains=domains, assets=assets)
    receipts: list[dict[str, Any]] = []
    if run_id:
        pinned, pinned_receipts = _run_brief(session, user, workspace_id, run_id, access)
    sections: dict[str, Any] = {}
    if pinned:
        sections[PINNED_SECTION] = pinned
        receipts += [dict(r, section=PINNED_SECTION) for r in pinned_receipts or []]
    mandatory = _size(sections)
    if mandatory > budget:
        raise ContextOverBudget(f"the pinned brief is {mandatory} chars, above the budget of {budget}: refused, not trimmed",
                                details={"section": PINNED_SECTION, "mandatory_chars": mandatory, "budget_chars": budget,
                                         "purpose": purpose})
    item_chars = INDEX_ITEM_CHARS if index_first else ITEM_CHARS
    if index_first:
        digest = _index_digest(session, workspace_id, access, domain)
        if digest:
            sections[INDEX_SECTION] = digest
            mandatory = _size(sections)
            if mandatory > budget:
                del sections[INDEX_SECTION]  # the index is a convenience: it never makes a compile refuse
    hits = search(session, user, workspace_id, objective, kinds=kinds, include_suggested=False, limit=80, access=access)
    conflicts = _conflicts(session, access, hits)
    if conflicts:
        raise ContextConflict("conflicting certified definitions block this context: resolve them first",
                              details={"conflicts": conflicts})
    focus = terms(objective)
    omissions: list[dict[str, Any]] = []
    used = _size(sections)
    for h in hits:
        e = h.entry
        text = focused_excerpt(e.summary, focus, item_chars) if focus else e.summary[:item_chars]
        if is_injection(text) or is_injection(e.title):  # context is data: text that instructs a model is withheld
            omissions.append({"id": e.id, "kind": e.kind, "reason": "screened: reads like an instruction to a model"})
            continue
        item = _item(e, text)
        cost = _size(item) + (0 if e.kind in sections else _size(e.kind))
        if used + cost > budget:
            omissions.append({"id": e.id, "kind": e.kind, "title": e.title, "reason": "budget"})
            continue
        sections.setdefault(e.kind, []).append(item)
        used += cost
        receipts.append({"id": e.id, "section": e.kind, "kind": e.kind, "version": e.version, "trust": e.trust,
                         "ref": f"{e.ref_table}:{e.ref_id}", "sha256": e.content_hash[:16], "score": round(h.score, 4)})
    ordered = {k: sections[k] for k in (PINNED_SECTION, INDEX_SECTION, *SECTION_ORDER) if k in sections}
    identity = stable_hash({"workspace": workspace_id, "purpose": purpose, "budget": budget_tokens,
                            "index": [index_first, domain] if index_first else None,
                            "pinned": stable_hash(pinned) if pinned else None,
                            "items": sorted((r["id"], r.get("version"), r.get("sha256")) for r in receipts)})
    return {"bundle": {"sections": ordered, "receipts": receipts, "omissions": omissions, "identity_hash": identity,
                       "budget_used": {"chars": used, "tokens": math.ceil(used / CHARS_PER_TOKEN),
                                       "budget_tokens": budget_tokens}},
            "receipts": receipts, "identity_hash": identity}


def _index_digest(session: Session, workspace_id: str, access: Access, domain: str | None) -> dict[str, Any]:
    """The compiled form of the context index: per section the count and the first titles (the full index is the
    `analystos://<workspace>/index/...` resource). Empty sections are left out."""
    from analystos.context import index as ctx_index
    from analystos.security.injection import is_injection

    model = ctx_index.build(session, workspace_id, domain=domain, access=access)
    out: dict[str, Any] = {}
    for name, items in model["sections"].items():
        titles = [r["title"] for r in items if not is_injection(r["title"])][:INDEX_LISTED]
        if titles:
            out[name] = {"count": len(items), "titles": titles}
    return out


def _run_brief(session: Session, user: Any, workspace_id: str, run_id: str,
               access: Access) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from analystos.db.models import AnalysisRun
    from analystos.services.context_grounding import pinned_prompt_context

    run = load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, label="run")
    return pinned_prompt_context(run, access.scope)


# ------------------------------------------------------------------------------------------- change feed
def changes(session: Session, user: Any, workspace_id: str, since: int = 0, limit: int = 200,
            domains: Iterable[str] = (), assets: Iterable[str] = ()) -> dict[str, Any]:
    """Ledger changes after cursor `since`, oldest first, for entries the caller may see. `cursor` is the
    position to pass next time (it advances past withheld changes too, so a poller never stalls)."""
    access = access_for(session, user, workspace_id, domains=domains, assets=assets)
    limit = max(1, min(int(limit), 1000))
    rows = session.execute(select(ContextLedgerChange, ContextLedgerEntry)
                           .join(ContextLedgerEntry, ContextLedgerEntry.id == ContextLedgerChange.entry_id)
                           .where(ContextLedgerChange.workspace_id == workspace_id, ContextLedgerChange.seq > int(since))
                           .order_by(ContextLedgerChange.seq).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    out = [{"seq": c.seq, "entry_id": c.entry_id, "kind": c.kind, "op": c.op, "version": c.version, "at": _iso(c.at),
            "title": e.title, "trust": e.trust}
           for c, e in rows if access.allows(e) and e.trust != "suggested"]
    return {"changes": out, "cursor": rows[-1][0].seq if rows else int(since), "more": more}

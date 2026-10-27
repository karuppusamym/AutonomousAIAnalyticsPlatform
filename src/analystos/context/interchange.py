"""OKF interchange over the Context Ledger (spec S03 §6, task S03-T6).

Export: one OKF v0.2 document per ledger entry the caller may see (pinned profile, `knowledge/okf.py`), plus
`index.md` and `log.md`. Deterministic: sorted YAML keys, paths derived from (kind, title) rather than database
ids, LF endings, no timestamps except each entry's `valid_from`. Entries the caller may not see are left out and
`log.md` lists how many per kind, nothing else about them. Suggestions are not exported, except content that
itself came from a pack (`source=import:okf`), which is forwarded with the trust its producer declared: the
receiving tenant's review state is its own, not part of the content. So `export → import (clean tenant) →
export` gives the same bytes (AT-16).

Import: every document lands as a knowledge suggestion (`origin=import:okf`, with the pack digest and path as
provenance) and is projected into the ledger as `trust=suggested`. Nothing is certified by an import: approval
in the review queue (a person) writes it into the workspace pack.
"""
from __future__ import annotations

import io
import zipfile
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.context.models import ContextLedgerEntry
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.knowledge import okf

EXPORT_TRUSTS = ("certified", "approved", "workspace")
KIND_TYPES = {"glossary": "Glossary Term", "metric": "Metric", "relationship": "Relationship",
              "verified_query": "Verified Query", "finding": "Finding", "decision": "Decision",
              "conversation_summary": "Conversation Summary", "correction": "Correction", "playbook": "Playbook",
              "dataset": "Dataset", "document_section": "Document"}
KIND_DIRS = {"glossary": "glossary", "metric": "metrics", "relationship": "relationships", "verified_query": "verified-queries",
             "finding": "findings", "decision": "decisions", "conversation_summary": "conversation-summaries",
             "correction": "corrections", "playbook": "playbooks", "dataset": "datasets", "document_section": "documents"}
# ledger kind -> knowledge suggestion kind an import queues (the ledger field keeps the real kind)
SUGGESTION_KINDS = {"glossary": "term", "metric": "metric", "correction": "rule"}
EXTENSION = "analystos"
MAX_IMPORT_DOCUMENTS = 5000


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _declared_trust(e: Any) -> str:
    if e.source == "import:okf":
        return str((e.provenance or {}).get("declared_trust") or "suggested")
    return e.trust


def exportable(e: Any) -> bool:
    return e.trust in EXPORT_TRUSTS or e.source == "import:okf"


def document(e: Any) -> tuple[dict[str, Any], str]:
    """The OKF frontmatter and body for one entry: content and its producer's claims, no local ids."""
    ledger = {"kind": e.kind, "trust": _declared_trust(e), "classification": e.classification,
              "aliases": list(e.aliases or []), "refs": list(e.refs or []), "valid_from": _instant(e.valid_from)}
    if e.fingerprint:
        ledger["fingerprint"] = e.fingerprint
    fm: dict[str, Any] = {"type": KIND_TYPES.get(e.kind, "Document"), "title": e.title, "status": "stable",
                          "tags": [e.kind], EXTENSION: {"ledger": ledger}}
    return fm, e.summary or e.title


def export_pack(session: Session, user: Any, workspace_id: str, *, kinds: list[str] | None = None) -> dict[str, Any]:
    """Files of the OKF pack for what `user` may see in `workspace_id`, plus counts of what was left out."""
    from analystos.context.ledger import KINDS
    from analystos.context.retrieval import access_for
    from analystos.knowledge.entries import slugify

    unknown = [k for k in kinds or [] if k not in KINDS]
    if unknown:
        raise InvalidInput(f"unknown context kinds: {', '.join(unknown)}")
    access = access_for(session, user, workspace_id)
    stmt = select(ContextLedgerEntry).where(ContextLedgerEntry.workspace_id == workspace_id,
                                            ContextLedgerEntry.valid_to.is_(None),
                                            or_(ContextLedgerEntry.trust.in_(EXPORT_TRUSTS),
                                                ContextLedgerEntry.source == "import:okf"))
    if kinds:
        stmt = stmt.where(ContextLedgerEntry.kind.in_(kinds))
    docs: list[tuple[str, str, str]] = []  # (base path, rendered text, title)
    omitted: Counter[str] = Counter()
    for e in session.scalars(stmt):
        if not exportable(e):
            continue
        if not access.allows(e):  # never written, never named: only counted
            omitted[e.kind] += 1
            continue
        fm, body = document(e)
        base = f"{KIND_DIRS.get(e.kind, 'documents')}/{slugify(e.title, 60)}"
        docs.append((base, okf.render_document(fm, body), e.title))
    files: dict[str, str] = {}
    titles: dict[str, tuple[str, str]] = {}
    for base, text, title in sorted(docs):  # by content, so equal packs name their files the same way
        path, n = f"{base}.md", 2
        while path in files:
            path, n = f"{base}-{n}.md", n + 1
        files[path] = text
        titles[path] = (title, base.split("/", 1)[0])
    files["index.md"] = _index(titles)
    files["log.md"] = _log(len(titles), omitted)
    data = {p: t.encode("utf-8") for p, t in sorted(files.items())}
    problems = okf.check_publish_policy(data)
    if problems:  # pragma: no cover - a renderer bug, never the caller's input
        raise InvalidInput("the export is not a valid OKF pack", details={"problems": [p.as_dict() for p in problems]})
    return {"files": data, "documents": len(titles), "omitted": dict(sorted(omitted.items())),
            "digest": okf.content_digest({p: stable_hash(t) for p, t in files.items()})}


def _index(titles: dict[str, tuple[str, str]]) -> str:
    lines = [f"- [{t.replace(']', '').replace('[', '')}]({p}) ({d})" for p, (t, d) in sorted(titles.items())]
    return "---\nokf_version: '0.2'\n---\n\n# Context ledger export\n\n" + ("\n".join(lines) or "(empty)") + "\n"


def _log(n: int, omitted: Counter[str]) -> str:
    lines = [f"- {kind}: {count}" for kind, count in sorted(omitted.items())]
    return ("# Export log\n\n" + f"Documents exported: {n}.\n\n"
            + "Entries left out because the exporting user may not see them (counts only):\n\n"
            + ("\n".join(lines) if lines else "- none") + "\n")


def zip_files(files: dict[str, bytes]) -> bytes:
    """A reproducible zip: sorted names, a fixed timestamp, no extra attributes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, files[path])
    return buf.getvalue()


def import_pack(session: Session, user: Any, workspace_id: str, data: bytes | dict[str, bytes]) -> dict[str, Any]:
    """Queue every document of an OKF pack as a suggestion. Needs editor. Never certifies anything."""
    from analystos.context.ledger import KINDS, TRUSTS, reconcile
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role
    from analystos.knowledge import bundle
    from analystos.knowledge.suggestions import primary_field, propose

    require_role(session, user, workspace_id, "editor")
    if isinstance(data, dict):
        files = dict(data)
    else:
        try:
            files, _ = bundle.read_zip(data)
        except Exception as exc:  # noqa: BLE001 - any unreadable archive is the caller's input error
            raise InvalidInput(f"not a readable OKF pack: {exc}") from None
    problems = okf.check_conformance(files)
    if problems:
        raise InvalidInput("the pack is not a conformant OKF v0.2 bundle",
                           details={"problems": [p.as_dict() for p in problems[:50]]})
    docs = sorted(p for p in files if okf.is_markdown(p) and p.rsplit("/", 1)[-1] not in okf.RESERVED)
    if len(docs) > MAX_IMPORT_DOCUMENTS:
        raise InvalidInput(f"a pack may hold at most {MAX_IMPORT_DOCUMENTS} documents")
    digest = okf.content_digest({p: stable_hash(files[p].decode("utf-8", "replace")) for p in sorted(files)})
    queued, skipped = 0, 0
    for path in docs:
        doc = okf.parse_document(path, files[path])
        meta = doc.frontmatter or {}
        ext = (meta.get(EXTENSION) or {}).get("ledger") if isinstance(meta.get(EXTENSION), dict) else None
        ext = ext if isinstance(ext, dict) else {}
        kind = ext.get("kind") if ext.get("kind") in KINDS else _kind_of(meta)
        body = doc.body.strip()
        declared = {"kind": kind, "title": str(meta.get("title") or doc.title)[:300], "summary": body,
                    "trust": ext.get("trust") if ext.get("trust") in TRUSTS else "suggested",
                    "classification": str(ext.get("classification") or "internal"),
                    "aliases": [str(a) for a in ext.get("aliases") or []][:20],
                    "refs": [str(r) for r in ext.get("refs") or []][:50],
                    "fingerprint": ext.get("fingerprint"), "valid_from": ext.get("valid_from"), "path": path}
        skind = SUGGESTION_KINDS.get(kind, "note")
        fields = {primary_field(skind): {"value": body or declared["title"], "confidence": 1.0,
                                         "provenance": {"source": "import:okf", "path": path}},
                  "ledger": {"value": declared, "confidence": 1.0, "provenance": {"pack": digest}}}
        columns = [r for r in declared["refs"] if r.count(".") >= 2]
        for name, value in (("synonyms", declared["aliases"][1:]), ("mapped_columns", columns)):
            if value:  # kept by an approval, which writes them into the workspace pack document
                fields[name] = {"value": value, "confidence": 1.0, "provenance": {"source": "import:okf"}}
        row = propose(session, workspace_id, kind=skind, subject=f"okf:{path}"[:200], title=declared["title"], fields=fields,
                      origin="import:okf", proposed_by=f"user:{user.id}", batch=digest[:80])
        if row is None:
            skipped += 1
        else:
            queued += 1
    reconcile(session, workspace_id, ["knowledge_suggestion"])
    audit(f"user:{user.id}", "context.imported", workspace_id=workspace_id, target=digest[:40], decision="allow",
          details={"documents": len(docs), "queued": queued, "skipped": skipped}, session=session)
    return {"documents": len(docs), "suggestions": queued, "skipped": skipped, "pack_digest": digest,
            "trust": "suggested", "note": "imported content is a suggestion until a person approves it"}


def _kind_of(meta: dict[str, Any]) -> str:
    from analystos.context.ledger import DOC_KINDS
    from analystos.knowledge.entries import doc_kind

    return DOC_KINDS.get(doc_kind(meta), "document_section")

"""Machine-written documents into a workspace pack, under the crawler invariants (P4-K04/K06).

Crawlers, ingesters and the REV write OKF documents into the workspace pack as one merge revision.
What a person curated is never overwritten, and tags only ever tighten:

* a document is **curated** when a human wrote or confirmed it: `generated.by` or any `verified[].by`
  is a `human:` actor (OKF §7), or the `analystos` extension says `origin: user` or `reviewed: true`.
  A curated document is kept byte for byte and reported as `kept_curated`;
* otherwise the new text replaces it, with `tags` = old tags ∪ new tags (a tag is never dropped);
* an unchanged document is not rewritten, so re-crawling identical metadata writes no revision.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from analystos.knowledge import okf


@dataclass
class DraftWrite:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept_curated: list[str] = field(default_factory=list)
    revision: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "unchanged": self.unchanged, "kept_curated": self.kept_curated,
                "revision": self.revision}


def _frontmatter(data: bytes) -> dict[str, Any] | None:
    try:
        meta, _ = okf.parse_frontmatter("", okf.decode("", data))
    except okf.OkfError:
        return None
    return meta


def is_curated(meta: Mapping[str, Any] | None) -> bool:
    if not meta:
        return False
    gen = meta.get("generated")
    if isinstance(gen, dict) and str(gen.get("by") or "").startswith("human:"):
        return True
    if okf.trust_tier(meta) == "human-reviewed":
        return True
    ext = meta.get("analystos")
    return isinstance(ext, dict) and (ext.get("origin") == "user" or ext.get("reviewed") is True)


def _tags(meta: Mapping[str, Any] | None) -> set[str]:
    tags = (meta or {}).get("tags")
    return {str(t) for t in tags} if isinstance(tags, list) else set()


def tighten_tags(new_text: str, old: bytes | None) -> str:
    """The new document with the old document's tags kept (union, sorted)."""
    if old is None:
        return new_text
    old_tags = _tags(_frontmatter(old))
    meta, body = okf.parse_frontmatter("", new_text)
    if meta is None or not (old_tags - _tags(meta)):
        return new_text
    meta["tags"] = sorted(_tags(meta) | old_tags)
    return okf.render_document(meta, body)


def write_drafts(session: Session, workspace_id: str, docs: Mapping[str, str], *, author: str, reason: str,
                 origin: str, meta: dict[str, Any] | None = None) -> DraftWrite:
    """Merge `docs` ({pack path: document text}) into the workspace pack as one revision."""
    from analystos.knowledge import store

    if not docs:
        return DraftWrite()
    files, report = prepare_drafts(session, workspace_id, docs)
    if files:
        pack = store.workspace_pack(session, workspace_id)
        rev = store.commit(session, pack, files, author=author, reason=reason, origin=origin, merge=True,
                           meta={**(meta or {}), "drafts": {"written": len(report.written),
                                                            "kept_curated": report.kept_curated[:100]}})
        report.revision = rev.number if rev is not None else None
    return report


def prepare_drafts(session: Session, workspace_id: str, docs: Mapping[str, str]) -> tuple[dict[str, bytes], DraftWrite]:
    """The files `write_drafts` would commit (curated kept, tags tightened, unchanged skipped), without committing,
    so another writer can put them in its own revision."""
    from analystos.knowledge import store

    report = DraftWrite()
    pack = store.workspace_pack(session, workspace_id)
    current = store.revision_files(session, pack)
    files: dict[str, bytes] = {}
    for path in sorted(docs):
        old = current.get(path)
        if old is not None and is_curated(_frontmatter(old)):
            report.kept_curated.append(path)
            continue
        data = tighten_tags(docs[path], old).encode("utf-8")
        if old == data:
            report.unchanged.append(path)
            continue
        files[path] = data
        report.written.append(path)
    return files, report

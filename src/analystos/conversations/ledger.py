"""The context-ledger interface S04 writes through (spec S03 §3, S04 §4.3–4.4).

S04 owns its rows (``conversation_summary``, ``conversation_correction``); the ledger is a projection of them that
other apps and agents search. The S03 ``context_ledger_entry`` table is built in parallel, so until it lands the
projection goes to ``conversation_memory_entry`` (same columns) through ``InterimLedger``. S03 backs the interface
with its own writer by calling ``set_ledger`` (or moves the rows over); callers here never touch either table.

Every write happens in the caller's transaction, so a projection never runs ahead of its owner row. ``remove``
deletes the projection *and its embedding*: a deletion request leaves no vector behind (S04 §4.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, utcnow

KINDS = ("conversation_summary", "correction")
TRUST = ("certified", "approved", "workspace", "personal", "suggested")
EMBEDDING_MODEL = "feature-hash-256"  # context/embeddings.py: local, no data leaves the platform


@dataclass(frozen=True)
class LedgerEntry:
    """One projected row. ``summary`` is prose or claims, never result rows."""

    workspace_id: str
    kind: str
    ref_table: str
    ref_id: str
    version: int
    trust: str
    title: str
    summary: str
    owner_id: str | None = None
    conversation_id: str | None = None
    visibility: str = "workspace"
    classification: str = "internal"
    source: str = "conversation"
    valid_to: datetime | None = None


class ContextLedger(Protocol):
    def upsert(self, session: Session, entry: LedgerEntry) -> None: ...

    def remove(self, session: Session, *, ref_table: str, ref_ids: list[str]) -> int: ...

    def remove_for_conversation(self, session: Session, conversation_id: str) -> int: ...


class InterimLedger:
    """``conversation_memory_entry`` as the ledger store. Older versions of a ref are closed (``valid_to``) when a
    newer one is written, as the S03 ledger does."""

    def upsert(self, session: Session, entry: LedgerEntry) -> None:
        from analystos.context.embeddings import embed
        from analystos.conversations.models import ConversationMemoryEntry as M

        if entry.kind not in KINDS or entry.trust not in TRUST:
            raise ValueError(f"ledger entry kind/trust not allowed here: {entry.kind}/{entry.trust}")
        now = utcnow()
        session.execute(update(M).where(M.ref_table == entry.ref_table, M.ref_id == entry.ref_id,
                                        M.version < entry.version, M.valid_to.is_(None)).values(valid_to=now))
        row = session.scalar(select(M).where(M.ref_table == entry.ref_table, M.ref_id == entry.ref_id,
                                             M.version == entry.version))
        if row is None:
            row = M(id=new_id("mem"), ref_table=entry.ref_table, ref_id=entry.ref_id, version=entry.version)
            session.add(row)
        row.workspace_id, row.kind, row.trust, row.visibility = entry.workspace_id, entry.kind, entry.trust, entry.visibility
        row.conversation_id, row.owner_id = entry.conversation_id, entry.owner_id
        row.title, row.summary = entry.title[:300], entry.summary
        row.classification, row.source, row.valid_to = entry.classification, entry.source, entry.valid_to
        row.embedding, row.embedding_model = embed(f"{entry.title}\n{entry.summary}"), EMBEDDING_MODEL
        session.flush()

    def remove(self, session: Session, *, ref_table: str, ref_ids: list[str]) -> int:
        from analystos.conversations.models import ConversationMemoryEntry as M

        if not ref_ids:
            return 0
        return int(session.execute(delete(M).where(M.ref_table == ref_table, M.ref_id.in_(ref_ids))).rowcount or 0)

    def remove_for_conversation(self, session: Session, conversation_id: str) -> int:
        from analystos.conversations.models import ConversationMemoryEntry as M

        return int(session.execute(delete(M).where(M.conversation_id == conversation_id)).rowcount or 0)


_ledger: ContextLedger = InterimLedger()


def get_ledger() -> ContextLedger:
    return _ledger


def set_ledger(ledger: ContextLedger) -> None:
    """S03 installs its ledger writer here at import time; tests may install a double."""
    global _ledger
    _ledger = ledger

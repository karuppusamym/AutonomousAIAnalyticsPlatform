"""Context Ledger tables (spec S03 §3, ADR-004). The ledger is a typed index over everything reusable: one place
for agents and other applications to look. The owner tables stay the system of record; a ledger row is a
versioned projection of one owner row (plus a few kinds the ledger owns through their owner row, such as a
certified Request Desk playbook).

Both tables carry ``workspace_id`` with a foreign key, so the tenancy trigger fills ``tenant_id`` from the workspace
and row-level security applies (ADR-009). Summaries never hold result rows."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base
from analystos.db.vectors import Embedding

LEDGER_EMBEDDING_DIM = 256  # the platform's local hashing embedding (context/embeddings.py)


class ContextLedgerEntry(Base):
    """One version of one reusable piece of context. A change to the owner closes the open version
    (``valid_to``) and opens the next, so readers can pin a version and the change feed can name it."""

    __tablename__ = "context_ledger_entry"
    __table_args__ = (UniqueConstraint("workspace_id", "ref_table", "ref_id", "version", name="uq_context_ledger_ref_version"),
                      Index("ix_context_ledger_ws_kind_open", "workspace_id", "kind", "valid_to"),
                      Index("ix_context_ledger_ref", "workspace_id", "ref_table", "ref_id"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    # glossary|metric|relationship|verified_query|finding|decision|conversation_summary|correction|playbook|dataset|
    # document_section
    kind: Mapped[str] = mapped_column(String(30))
    ref_table: Mapped[str] = mapped_column(String(60))
    ref_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    trust: Mapped[str] = mapped_column(String(12))  # certified|approved|workspace|personal|suggested
    visibility: Mapped[str] = mapped_column(String(12), default="workspace")  # workspace|tenant|exported
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)  # no raw result rows, ever
    classification: Mapped[str] = mapped_column(String(20), default="internal")  # internal|confidential|restricted
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # verification fingerprint (S07)
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(LEDGER_EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(30))  # crawler|human|conversation|import:okf|mcp|desk|run
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)  # exact-match names (synonyms, display names)
    refs: Mapped[list[str]] = mapped_column(JSON, default=list)  # schema.table[.column] the entry depends on
    content_hash: Mapped[str] = mapped_column(String(64))  # of what the projection read from the owner
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ContextLedgerChange(Base):
    """The ledger's change feed: one row per version opened or closed, in commit order (``seq``), so a
    downstream cache or agent can poll ``since=<seq>`` and refresh exactly what changed."""

    __tablename__ = "context_ledger_change"
    __table_args__ = (Index("ix_context_ledger_change_ws_seq", "workspace_id", "seq"),)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))
    op: Mapped[str] = mapped_column(String(10))  # added | changed | retired
    version: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

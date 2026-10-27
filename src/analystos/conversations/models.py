"""Conversation memory tables (spec S04). Each carries ``workspace_id`` with a foreign key, so the tenancy trigger
fills ``tenant_id`` from the workspace and row-level security applies (ADR-009).

The conversation and its messages are the existing ``ask_thread`` / ``ask_turn`` rows (spec S04 §4.1). These tables
add the T1 session state (versioned per message), summaries, typed corrections and the interim store behind the
context-ledger interface (``conversations/ledger.py``) until the S03 ledger table exists."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base
from analystos.db.vectors import Embedding

MEMORY_EMBEDDING_DIM = 256  # the platform's feature-hash embedding (context/embeddings.py)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ConversationState(Base):
    """T1 session state, one immutable version per change. ``at_seq`` is the last message seq the version saw, so
    a branch from message N restores exactly the latest version with ``at_seq <= N``."""

    __tablename__ = "conversation_state"
    __table_args__ = (UniqueConstraint("conversation_id", "version", name="uq_conversation_state_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("ask_thread.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    at_seq: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # the turn that produced it
    cause: Mapped[str] = mapped_column(String(20))  # created | message | correction | edit | branch
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class ConversationSummary(Base):
    """A structured summary ``{questions, answers[{claim, evidence_ref, label}], corrections, open_questions}``.
    Every number in a claim was checked against the cited turn's computed result (``guard``)."""

    __tablename__ = "conversation_summary"
    __table_args__ = (UniqueConstraint("conversation_id", "version", name="uq_conversation_summary_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("ask_thread.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(40))  # the conversation owner
    version: Mapped[int] = mapped_column(Integer)
    through_seq: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(20))  # idle | close | manual | mcp
    trust: Mapped[str] = mapped_column(String(20))  # workspace | personal
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    guard: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    produced_by: Mapped[str] = mapped_column(String(120))  # rules | model:<id>
    created_at: Mapped[datetime] = _ts()


class ConversationCorrection(Base):
    """A typed correction ``{term, meaning, scope}``. ``session`` lives in the conversation's state; ``personal``
    is the author's memory (T2, TTL); ``workspace_proposed`` is a knowledge suggestion an editor decides (T3).
    ``conversation_id`` is cleared, not cascaded, when a team definition outlives the conversation it came from."""

    __tablename__ = "conversation_correction"
    __table_args__ = (Index("ix_conversation_correction_memory", "workspace_id", "scope", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("ask_thread.id", ondelete="SET NULL"), nullable=True,
                                                        index=True)
    message_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    term: Mapped[str] = mapped_column(String(200))
    term_key: Mapped[str] = mapped_column(String(200))  # normalised: lower case, single spaces
    meaning: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(24))  # session | personal | workspace_proposed
    # active | superseded | expired | revoked (session, personal); proposed | approved | rejected | not_proposed (team)
    status: Mapped[str] = mapped_column(String(20))
    promoted_from: Mapped[str | None] = mapped_column(String(40), nullable=True)  # the correction this one promoted
    suggestion_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConversationMemoryEntry(Base):
    """Interim store behind the context-ledger interface: the columns of the S03 ``context_ledger_entry`` for the
    kinds S04 writes (``conversation_summary``, ``correction``). Never raw result rows. ``conversation_id`` lets a
    deletion request find every entry (and embedding) derived from a conversation."""

    __tablename__ = "conversation_memory_entry"
    __table_args__ = (UniqueConstraint("ref_table", "ref_id", "version", name="uq_conversation_memory_ref"),
                      Index("ix_conversation_memory_lookup", "workspace_id", "kind", "trust"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    ref_table: Mapped[str] = mapped_column(String(60))
    ref_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    trust: Mapped[str] = mapped_column(String(20))  # certified | approved | workspace | personal | suggested
    visibility: Mapped[str] = mapped_column(String(20), default="workspace")
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(20), default="internal")
    source: Mapped[str] = mapped_column(String(40), default="conversation")
    valid_from: Mapped[datetime] = _ts()
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(MEMORY_EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = _ts()

"""Context Ledger tables (spec S03 §3, ADR-004). The ledger is a typed index over everything reusable: one place
for agents and other applications to look. The owner tables stay the system of record; a ledger row is a
versioned projection of one owner row (plus a few kinds the ledger owns through their owner row, such as a
certified Request Desk playbook).

Both tables carry ``workspace_id`` with a foreign key, so the tenancy trigger fills ``tenant_id`` from the workspace
and row-level security applies (ADR-009). Summaries never hold result rows. Federation (S03-T7) adds a subscription
and the subscriber's read-only copies, both owned by the subscriber workspace."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class ContextSubscription(Base):
    """A workspace following another workspace's ledger through its change feed (spec S03 §6 federation, S03-T7).

    The subscriber workspace owns the row (RLS through ``workspace_id``). The source may sit in another tenant, so it
    has no foreign key; ``subject_id`` is the user whose access to the source decides what is shared, re-checked on
    every sync. ``cursor`` is the last source ``seq`` applied: delivery is at least once, the apply is idempotent."""

    __tablename__ = "context_subscription"
    __table_args__ = (UniqueConstraint("workspace_id", "source_workspace_id", name="uq_context_subscription_source"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    source_workspace_id: Mapped[str] = mapped_column(String(40))
    source_tenant_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_id: Mapped[str] = mapped_column(String(40))  # the subscribing owner; their access to the source applies
    kinds: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty: every kind
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | suspended | revoked
    cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ContextFederatedEntry(Base):
    """The subscriber's read-only copy of one source entry, keyed by the source's ``ref_table:ref_id``. It is a ledger
    owner table: the projection turns each live row into an ``approved`` entry of the subscriber's ledger, and a new
    ``source_version`` opens a new ledger version. ``retired`` rows are tombstones that keep a replay idempotent."""

    __tablename__ = "context_federated_entry"
    __table_args__ = (UniqueConstraint("subscription_id", "source_key", name="uq_context_federated_key"),
                      Index("ix_context_federated_ws_live", "workspace_id", "retired"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("context_subscription.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[str] = mapped_column(String(270))
    source_entry_id: Mapped[str] = mapped_column(String(40))
    source_version: Mapped[int] = mapped_column(Integer)
    source_trust: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(20), default="internal")
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ContextWebhook(Base):
    """A push subscription to the workspace's ledger change feed (review P1-6). Not the federation
    ``ContextSubscription`` above: that one pulls another workspace's ledger, this one pushes this workspace's change
    metadata to a webhook (``kind='webhook'``, standing external egress, so it is authorized by an approval bound to
    ``destination_hash`` = filters + destination) or to an internal consumer (``kind='internal'``, platform-defined,
    no egress). ``cursor`` is the last ledger ``seq`` scanned; it stays NULL until first authorized, so a new
    subscription sees changes from then on, never a replay."""

    __tablename__ = "context_webhook"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_context_webhook_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="webhook")  # webhook | internal
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # kinds, changes, tag_added, schema_changed
    destination_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|authorized|rejected|lapsed|revoked
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    authorized_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorized_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ContextWebhookDelivery(Base):
    """One push of one ledger change to one subscription: the delivery log. ``payload`` is ledger metadata only (ids,
    kind, change, version, signals), never a body. Retries are bounded exponential backoff ending in ``dead_letter``."""

    __tablename__ = "context_webhook_delivery"
    __table_args__ = (UniqueConstraint("webhook_id", "seq", name="uq_context_webhook_delivery_seq"),
                      Index("ix_context_webhook_delivery_due", "status", "next_attempt_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("context_webhook.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    entry_id: Mapped[str] = mapped_column(String(40))
    destination_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued|sending|retrying|delivered|dead_letter|refused
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

"""Conversation memory moves into the S03 context ledger (spec S04 §4.3–4.5): conversation summaries and corrections
become projected ledger kinds (``context/ledger.py``), so the interim ``conversation_memory_entry`` table goes.

Its open rows are copied into ``context_ledger_entry`` under the refs the S03 projectors use (a summary's latest
``conversation_summary`` row; a personal or approved ``conversation_correction``), each with an ``added`` change-feed
row. Their content hash is marked as migrated, so the next reconcile of the workspace replaces each with the
projector's own version. Suggested rows are not copied: the pending knowledge suggestion is already in the ledger.

Revision ID: 0063_conversation_ledger
Revises: 0062_conversations
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_conversation_ledger"
down_revision = "0062_conversations"
branch_labels = None
depends_on = None

MIGRATED = "migrated:conversation_memory_entry"

_COPY = """
INSERT INTO context_ledger_entry (id, workspace_id, kind, ref_table, ref_id, version, trust, visibility, title, summary,
    classification, valid_from, valid_to, fingerprint, embedding, embedding_model, owner_id, source, aliases, refs,
    content_hash, provenance, created_at)
SELECT 'cle_' || substr(md5(m.id || ':' || {ref}), 1, 12), m.workspace_id, m.kind, '{table}', {ref}, 1, m.trust,
    m.visibility, m.title, m.summary, m.classification, m.valid_from, NULL, NULL, m.embedding, m.embedding_model,
    m.owner_id, 'conversation', '[]', '[]', '{hash}', '{{"migrated_from": "conversation_memory_entry"}}', now()
FROM conversation_memory_entry m {join}
WHERE m.valid_to IS NULL {where}
ON CONFLICT (workspace_id, ref_table, ref_id, version) DO NOTHING
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(_COPY.format(
        table="conversation_summary", ref="s.id", hash=MIGRATED,
        join="JOIN conversation_summary s ON s.conversation_id = m.ref_id AND s.version = m.version",
        where="AND m.kind = 'conversation_summary'")))
    conn.execute(sa.text(_COPY.format(
        table="conversation_correction", ref="c.id", hash=MIGRATED,
        join="JOIN conversation_correction c ON c.id = m.ref_id",
        where="AND m.kind = 'correction' AND m.trust IN ('personal', 'approved')")))
    conn.execute(sa.text(
        "INSERT INTO context_ledger_change (workspace_id, entry_id, kind, op, version, at) "
        "SELECT e.workspace_id, e.id, e.kind, 'added', e.version, now() FROM context_ledger_entry e "
        "WHERE e.content_hash = :h AND NOT EXISTS (SELECT 1 FROM context_ledger_change c WHERE c.entry_id = e.id)"),
        {"h": MIGRATED})
    op.drop_table("conversation_memory_entry")  # drops its policy, fill trigger and indexes with it


def downgrade() -> None:
    """Recreate the interim table empty (0062's code writes it again); conversation projections leave the ledger."""
    from analystos.core.config import get_settings
    from analystos.db import vectors
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "conversation_memory_entry",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.String(40), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("ref_table", sa.String(60), nullable=False),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trust", sa.String(20), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.String(40), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", vectors.column_type(256), nullable=True),
        sa.Column("embedding_model", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ref_table", "ref_id", "version", name="uq_conversation_memory_ref"),
    )
    op.create_index("ix_conversation_memory_lookup", "conversation_memory_entry", ["workspace_id", "kind", "trust"])
    op.create_index("ix_conversation_memory_entry_workspace_id", "conversation_memory_entry", ["workspace_id"])
    op.create_index("ix_conversation_memory_entry_conversation_id", "conversation_memory_entry", ["conversation_id"])
    install(op.get_bind(), {"conversation_memory_entry": TenantSource("workspace", "workspace_id", nullable=False)},
            role=get_settings().tenancy_app_role, backfill=False)
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM context_ledger_change WHERE entry_id IN (SELECT id FROM context_ledger_entry "
                         "WHERE ref_table IN ('conversation_summary', 'conversation_correction'))"))
    conn.execute(sa.text("DELETE FROM context_ledger_entry WHERE ref_table IN ('conversation_summary', "
                         "'conversation_correction')"))

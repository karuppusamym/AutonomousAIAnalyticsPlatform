"""Context Ledger (spec S03 §3, ADR-004): context_ledger_entry and its change feed context_ledger_change. Both are
tenant-owned through their workspace; tenant isolation (column, fill trigger, row-level security) is installed here.
The ledger is a projection: `analystos context reconcile` (or the first write to an owner) fills it.

Revision ID: 0061_context_ledger
Revises: 0060_request_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_context_ledger"
down_revision = "0060_request_desk"
branch_labels = None
depends_on = None

TABLES = ("context_ledger_entry", "context_ledger_change")


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.db import vectors
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "context_ledger_entry",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ref_table", sa.String(60), nullable=False),
        sa.Column("ref_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trust", sa.String(12), nullable=False),
        sa.Column("visibility", sa.String(12), nullable=False, server_default="workspace"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False, server_default="internal"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("embedding", vectors.column_type(256), nullable=True),
        sa.Column("embedding_model", sa.String(60), nullable=True),
        sa.Column("owner_id", sa.String(40), nullable=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "ref_table", "ref_id", "version", name="uq_context_ledger_ref_version"),
    )
    op.create_index("ix_context_ledger_entry_workspace_id", "context_ledger_entry", ["workspace_id"])
    op.create_index("ix_context_ledger_ws_kind_open", "context_ledger_entry", ["workspace_id", "kind", "valid_to"])
    op.create_index("ix_context_ledger_ref", "context_ledger_entry", ["workspace_id", "ref_table", "ref_id"])

    op.create_table(
        "context_ledger_change",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        _ws(),
        sa.Column("entry_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("op", sa.String(10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_context_ledger_change_workspace_id", "context_ledger_change", ["workspace_id"])
    op.create_index("ix_context_ledger_change_ws_seq", "context_ledger_change", ["workspace_id", "seq"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

"""Read-only knowledge sources (P2-10): knowledge_source holds a workspace's GitHub path or Confluence space
(location filter, secret_ref, incremental sync state, last report). Tenant-owned through its workspace; tenant
isolation (column, fill trigger, row-level security) is installed here.

Expand-only: one new table; the previous release ignores it.

Revision ID: 0078_knowledge_sources
Revises: 0077_context_webhooks
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_knowledge_sources"
down_revision = "0077_context_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "knowledge_source",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_ref", sa.String(200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(20), nullable=True),
        sa.Column("last_report", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_source_workspace_id", "knowledge_source", ["workspace_id"])
    install(op.get_bind(), {"knowledge_source": TenantSource("workspace", "workspace_id", nullable=False)},
            role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_source_workspace_id", table_name="knowledge_source")
    op.drop_table("knowledge_source")

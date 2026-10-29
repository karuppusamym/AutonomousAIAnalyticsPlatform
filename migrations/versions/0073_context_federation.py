"""Context federation (spec S03 §6, FR-057, task S03-T7): context_subscription (a workspace following another
workspace's ledger by its change-feed cursor) and context_federated_entry (the subscriber's read-only copies, a ledger
owner table). Both are tenant-owned through the subscriber workspace; tenant isolation (column, fill trigger, row-level
security) is installed here. The source workspace may be in another tenant, so it has no foreign key.

Expand-only: two new tables; the previous release ignores them.

Revision ID: 0073_context_federation
Revises: 0072_platform_ops
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073_context_federation"
down_revision = "0072_platform_ops"
branch_labels = None
depends_on = None

TABLES = ("context_subscription", "context_federated_entry")


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "context_subscription",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("source_workspace_id", sa.String(40), nullable=False),
        sa.Column("source_tenant_id", sa.String(40), nullable=True),
        sa.Column("subject_id", sa.String(40), nullable=False),
        sa.Column("kinds", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "source_workspace_id", name="uq_context_subscription_source"),
    )
    op.create_index("ix_context_subscription_workspace_id", "context_subscription", ["workspace_id"])

    op.create_table(
        "context_federated_entry",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("subscription_id", sa.String(40), sa.ForeignKey("context_subscription.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_key", sa.String(270), nullable=False),
        sa.Column("source_entry_id", sa.String(40), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_trust", sa.String(12), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("classification", sa.String(20), nullable=False, server_default="internal"),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("subscription_id", "source_key", name="uq_context_federated_key"),
    )
    op.create_index("ix_context_federated_entry_workspace_id", "context_federated_entry", ["workspace_id"])
    op.create_index("ix_context_federated_entry_subscription_id", "context_federated_entry", ["subscription_id"])
    op.create_index("ix_context_federated_ws_live", "context_federated_entry", ["workspace_id", "retired"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

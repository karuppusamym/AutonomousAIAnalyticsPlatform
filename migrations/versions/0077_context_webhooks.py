"""Context change push subscriptions (review P1-6, spec S03 §5): context_webhook (filters + a webhook destination
authorized by a hash-bound approval, or an internal consumer) and context_webhook_delivery (the delivery log with
retries and a dead-letter state). Both are tenant-owned through their workspace; tenant isolation (column, fill
trigger, row-level security) is installed here.

Expand-only: two new tables; the previous release ignores them. Based on head 0076_run_leases.

Revision ID: 0077_context_webhooks
Revises: 0076_run_leases
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077_context_webhooks"
down_revision = "0076_run_leases"
branch_labels = None
depends_on = None

TABLES = ("context_webhook", "context_webhook_delivery")


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "context_webhook",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="webhook"),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("secret_ref", sa.String(200), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("authorized_hash", sa.String(64), nullable=True),
        sa.Column("authorized_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.BigInteger(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_context_webhook_name"),
    )
    op.create_index("ix_context_webhook_workspace_id", "context_webhook", ["workspace_id"])

    op.create_table(
        "context_webhook_delivery",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("webhook_id", sa.String(40), sa.ForeignKey("context_webhook.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("entry_id", sa.String(40), nullable=False),
        sa.Column("destination_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("webhook_id", "seq", name="uq_context_webhook_delivery_seq"),
    )
    op.create_index("ix_context_webhook_delivery_workspace_id", "context_webhook_delivery", ["workspace_id"])
    op.create_index("ix_context_webhook_delivery_webhook_id", "context_webhook_delivery", ["webhook_id"])
    op.create_index("ix_context_webhook_delivery_due", "context_webhook_delivery", ["status", "next_attempt_at"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

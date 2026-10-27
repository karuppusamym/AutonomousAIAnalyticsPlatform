"""Request Desk nightly value rollup (spec S13 §3.6, FR-053): desk_daily, tenant-owned through its workspace (column,
fill trigger and row-level security installed here).

Revision ID: 0066_desk_daily
Revises: 0065_tool_registry
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_desk_daily"
down_revision = "0065_tool_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    def count(name: str) -> sa.Column:
        return sa.Column(name, sa.Integer(), nullable=False, server_default="0")

    op.create_table(
        "desk_daily",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("request_class", sa.String(10), nullable=False),
        count("n"), count("duplicates"), count("closed"), count("accepted"), count("auto_resolved"),
        sa.Column("auto_resolution_rate", sa.Float(), nullable=True),
        sa.Column("accepted_rate", sa.Float(), nullable=True),
        sa.Column("tta_p50_seconds", sa.Float(), nullable=True),
        sa.Column("tta_p95_seconds", sa.Float(), nullable=True),
        count("human_minutes"),
        sa.Column("est_minutes_saved", sa.Float(), nullable=False, server_default="0"),
        count("unbaselined"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "day", "request_class", name="uq_desk_daily_ws_day_class"),
    )
    op.create_index("ix_desk_daily_workspace_id", "desk_daily", ["workspace_id"])
    install(op.get_bind(), {"desk_daily": TenantSource("workspace", "workspace_id", nullable=False)},
            role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_table("desk_daily")  # drops its policy and fill trigger with it

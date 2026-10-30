"""Run leases for `analystos driver` processes (ORC-T7, ADR-015 phase 2): run_lease says which driver may drive a
local/langgraph run (claimed with FOR UPDATE SKIP LOCKED, renewed by heartbeat, fenced by `epoch`), and run_driver
holds each driver process's heartbeat for /api/health. run_lease is tenant-owned through analysis_run (column, fill
trigger, row-level security installed here); run_driver is a platform table with no tenant data.

Expand-only: two new tables; the previous release ignores them.

Revision ID: 0076_run_leases
Revises: 0075_pilot_review_subjects
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076_run_leases"
down_revision = "0075_pilot_review_subjects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "run_lease",
        sa.Column("run_id", sa.String(40), sa.ForeignKey("analysis_run.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("holder", sa.String(120), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_run_lease_holder", "run_lease", ["holder"])
    op.create_table(
        "run_driver",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("backend", sa.String(20), nullable=False),
        sa.Column("host", sa.String(200), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_run_driver_heartbeat_at", "run_driver", ["heartbeat_at"])
    sources = {"run_lease": TenantSource("analysis_run", "run_id", nullable=False)}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_index("ix_run_driver_heartbeat_at", table_name="run_driver")
    op.drop_table("run_driver")
    op.drop_index("ix_run_lease_holder", table_name="run_lease")
    op.drop_table("run_lease")

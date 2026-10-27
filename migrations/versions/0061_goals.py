"""Autonomy engine (spec S06): goal, run_plan_version, analysis_run.limits. Both new tables are tenant-owned
through their workspace; tenant isolation (column, fill trigger, row-level security) is installed for them here.

Revision ID: 0061_goals
Revises: 0060_request_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_goals"
down_revision = "0060_request_desk"
branch_labels = None
depends_on = None

TABLES = ("goal", "run_plan_version")


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.add_column("analysis_run", sa.Column("limits", JSONB(), nullable=False, server_default="{}"))

    op.create_table(
        "goal",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("data_scope", JSONB(), nullable=False, server_default="{}"),
        sa.Column("requested_outputs", JSONB(), nullable=False, server_default="[]"),
        sa.Column("playbook", sa.String(120), nullable=True),
        sa.Column("limits", JSONB(), nullable=False, server_default="{}"),
        sa.Column("approval_policy", JSONB(), nullable=False, server_default="{}"),
        sa.Column("complexity", JSONB(), nullable=False, server_default="{}"),
        sa.Column("request_id", sa.String(80), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_goal_workspace_id", "goal", ["workspace_id"])
    op.create_index("ix_goal_run_id", "goal", ["run_id"])

    op.create_table(
        "run_plan_version",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("plan", JSONB(), nullable=False, server_default="{}"),
        sa.Column("plan_hash", sa.String(64), nullable=True),
        sa.Column("tasks", JSONB(), nullable=False, server_default="[]"),
        sa.Column("reason_code", sa.String(30), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(80), nullable=True),
        _ts("superseded_at"),
        sa.UniqueConstraint("run_id", "version", name="uq_run_plan_version"),
    )
    op.create_index("ix_run_plan_version_workspace_id", "run_plan_version", ["workspace_id"])
    op.create_index("ix_run_plan_version_run_id", "run_plan_version", ["run_id"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it
    op.drop_column("analysis_run", "limits")

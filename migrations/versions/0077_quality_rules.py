"""Declared quality rules (review item P1-7, spec S09): quality_rule (a rule an owner declared or approved on one
catalog asset or column) and quality_rule_result (each evaluation: counts, pass rate or value, the gateway query
id; never rows). Both are tenant-owned through the workspace; tenant isolation (column, fill trigger, row-level
security) is installed here.

Expand-only: two new tables; the previous release ignores them.

Revision ID: 0077_quality_rules
Revises: 0076_run_leases
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077_quality_rules"
down_revision = "0076_run_leases"
branch_labels = None
depends_on = None

TABLES = ("quality_rule", "quality_rule_result")


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "quality_rule",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("asset_id", sa.String(40), nullable=True),
        sa.Column("asset", sa.String(330), nullable=False),
        sa.Column("column", sa.String(200), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("dimension", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="warning"),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("suggestion_id", sa.String(40), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("approved_by", sa.String(40), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(10), nullable=True),
        sa.Column("last_pass_rate", sa.Float(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "content_hash", name="uq_quality_rule_content"),
    )
    op.create_index("ix_quality_rule_workspace_id", "quality_rule", ["workspace_id"])
    op.create_index("ix_quality_rule_ws_asset", "quality_rule", ["workspace_id", "asset", "status"])

    op.create_table(
        "quality_rule_result",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _ws(),
        sa.Column("rule_id", sa.String(40), sa.ForeignKey("quality_rule.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=True),
        sa.Column("evaluated", sa.BigInteger(), nullable=True),
        sa.Column("passed_rows", sa.BigInteger(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("query_id", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trigger", sa.String(80), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quality_rule_result_workspace_id", "quality_rule_result", ["workspace_id"])
    op.create_index("ix_quality_rule_result_rule_at", "quality_rule_result", ["rule_id", "run_at"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

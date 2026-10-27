"""S02 query gateway: result handles (ADR-010), cost estimate and actual, reason codes and hashed SQL on
query_execution. `result_handle` is tenant-owned through its workspace; tenant isolation (column, fill
trigger, row-level security) is installed for it here.

Revision ID: 0061_query_gateway_s02
Revises: 0060_request_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_query_gateway_s02"
down_revision = "0060_request_desk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.add_column("query_execution", sa.Column("reason_code", sa.String(60), nullable=True))
    op.add_column("query_execution", sa.Column("sql_hash", sa.String(64), nullable=True))
    op.add_column("query_execution", sa.Column("cost_estimate", JSONB(), nullable=True))
    op.add_column("query_execution", sa.Column("result_bytes", sa.BigInteger(), nullable=True))
    op.add_column("query_execution", sa.Column("result_uri", sa.String(300), nullable=True))

    op.create_table(
        "result_handle",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("source_id", sa.String(40), nullable=True),
        sa.Column("uri", sa.String(300), nullable=False),
        sa.Column("result_schema", JSONB(), nullable=False, server_default="[]"),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("classification_max", sa.String(20), nullable=False, server_default="internal"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_result_handle_workspace_id", "result_handle", ["workspace_id"])
    op.create_index("ix_result_handle_run_id", "result_handle", ["run_id"])
    op.create_index("ix_result_handle_expires_at", "result_handle", ["expires_at"])
    install(op.get_bind(), {"result_handle": TenantSource("workspace", "workspace_id", nullable=False)},
            role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_table("result_handle")
    for col in ("result_uri", "result_bytes", "cost_estimate", "sql_hash", "reason_code"):
        op.drop_column("query_execution", col)

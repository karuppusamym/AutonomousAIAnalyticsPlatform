"""Tool, agent and data-product registry (spec S10, ADR-007): registered_tool (signed v2 manifests),
tool_invocation (usage records) and tool_signing_key (tenant public keys); MCP grants gain tool
patterns, a per-minute rate and an expiry; mcp_usage keys widen for registry tool ids and the
per-minute rate counter. Tenant isolation (column, fill trigger, row-level security) is installed
for the new tables here.

Revision ID: 0061_tool_registry
Revises: 0060_request_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_tool_registry"
down_revision = "0060_request_desk"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "registered_tool",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("tool_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("major", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("side_effect", sa.String(20), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("source", JSONB(), nullable=False, server_default="{}"),
        sa.Column("description_hash", sa.String(64), nullable=True),
        sa.Column("screening", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_by", sa.String(80), nullable=True),
        sa.UniqueConstraint("workspace_id", "tool_id", "version", name="uq_registered_tool_version"),
    )
    op.create_index("ix_registered_tool_workspace_id", "registered_tool", ["workspace_id"])
    op.create_index("ix_registered_tool_ws_tool", "registered_tool", ["workspace_id", "tool_id"])

    op.create_table(
        "tool_invocation",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("tool_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(40), nullable=True),
        sa.Column("kind", sa.String(20), nullable=True),
        sa.Column("caller", sa.String(120), nullable=False),
        sa.Column("client_id", sa.String(40), nullable=True),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(80), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("query_id", sa.String(40), nullable=True),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_tool_invocation_workspace_id", "tool_invocation", ["workspace_id"])
    op.create_index("ix_tool_invocation_client_id", "tool_invocation", ["client_id"])
    op.create_index("ix_tool_invocation_ws_tool_at", "tool_invocation", ["workspace_id", "tool_id", "created_at"])

    op.create_table(
        "tool_signing_key",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("tenant.id"), nullable=True),
        sa.Column("key_id", sa.String(80), nullable=False),
        sa.Column("alg", sa.String(20), nullable=False, server_default="ed25519"),
        sa.Column("public_key", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        _ts("retired_at", True),
        sa.UniqueConstraint("tenant_id", "key_id", name="uq_tool_signing_key"),
    )

    op.add_column("mcp_grant", sa.Column("tool_patterns", JSONB(), nullable=False, server_default="[]"))
    op.add_column("mcp_grant", sa.Column("rate_per_min", sa.Integer(), nullable=True))
    op.add_column("mcp_grant", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("mcp_usage", "tool", type_=sa.String(200), existing_type=sa.String(80), existing_nullable=False)
    op.alter_column("mcp_usage", "day", type_=sa.String(16), existing_type=sa.String(10), existing_nullable=False)

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in ("registered_tool", "tool_invocation")}
    sources["tool_signing_key"] = TenantSource(None, None, nullable=False)
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.execute("DELETE FROM mcp_usage WHERE length(day) > 10 OR length(tool) > 80")
    op.alter_column("mcp_usage", "day", type_=sa.String(10), existing_type=sa.String(16), existing_nullable=False)
    op.alter_column("mcp_usage", "tool", type_=sa.String(80), existing_type=sa.String(200), existing_nullable=False)
    op.drop_column("mcp_grant", "expires_at")
    op.drop_column("mcp_grant", "rate_per_min")
    op.drop_column("mcp_grant", "tool_patterns")
    for table in ("tool_signing_key", "tool_invocation", "registered_tool"):
        op.drop_table(table)  # drops its policy and fill trigger with it

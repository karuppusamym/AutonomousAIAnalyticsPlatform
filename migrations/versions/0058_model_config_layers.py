"""Layered model configuration (spec S05-T3): versioned tenant / workspace overrides that narrow the
models file and the platform settings. Tenant-owned: row-level security like every tenant table.

Revision ID: 0058_model_config_layers
Revises: 0057_tenancy
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_model_config_layers"
down_revision = "0057_tenancy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "model_config_override",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scope_key", sa.String(100), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scope_key", "version", name="uq_model_config_override_version"),
    )
    op.create_index("ix_model_config_override_scope_key", "model_config_override", ["scope_key"])
    op.create_index("ix_model_config_override_tenant_id", "model_config_override", ["tenant_id"])
    op.create_index("ix_model_config_override_workspace_id", "model_config_override", ["workspace_id"])
    install(op.get_bind(), {"model_config_override": TenantSource("workspace", "workspace_id", True)},
            role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_table("model_config_override")

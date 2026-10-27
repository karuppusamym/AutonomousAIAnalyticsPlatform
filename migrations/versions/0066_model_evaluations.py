"""Model-switch evaluation gate (spec S05-T5): golden-suite scores per (suite, purpose, model). Platform
data (prompts and grades only), so a global table without tenant row-level security.

Revision ID: 0066_model_evaluations
Revises: 0065_tool_registry
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_model_evaluations"
down_revision = "0065_tool_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_evaluation",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("suite", sa.String(80), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("profile", sa.String(80), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for col in ("suite", "purpose", "model"):
        op.create_index(f"ix_model_evaluation_{col}", "model_evaluation", [col])
    # The restricted application role gets access through the default privileges tenancy.ddl.install set.


def downgrade() -> None:
    op.drop_table("model_evaluation")

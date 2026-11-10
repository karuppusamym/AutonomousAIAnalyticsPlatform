"""MCP grant slice (P2-8): optional `domains` and `assets` on a grant limit an external agent to one slice of a
workspace's context. Empty lists mean the whole workspace, so existing grants are unchanged.

Expand-only: two nullable-free JSON columns with an empty-list default.

Revision ID: 0079_mcp_grant_slice
Revises: 0078_knowledge_sources
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_mcp_grant_slice"
down_revision = "0078_knowledge_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("domains", "assets"):
        op.add_column("mcp_grant", sa.Column(col, sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    for col in ("assets", "domains"):
        op.drop_column("mcp_grant", col)

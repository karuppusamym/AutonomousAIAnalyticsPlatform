"""Knowledge sources bind their credential by approval (P2-10 governance review): knowledge_source.binding_approval_id
names the approval (action knowledge_source.bind) over workspace, kind, host, target and secret_ref.

Expand-only: one nullable column; existing rows have no approval and cannot sync until one is requested and approved.

Revision ID: 0078_knowledge_source_binding
Revises: 0077_knowledge_sources
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_knowledge_source_binding"
down_revision = "0077_knowledge_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_source", sa.Column("binding_approval_id", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_source", "binding_approval_id")

"""Playbook signing (spec S06-T6): `definition.signature` holds a playbook's signing state, the hash-bound approval
while it is a draft and the tenant signature the runtime verifies once it is published.

Expand-only: one new nullable column; the previous release ignores it.

Revision ID: 0073_playbook_signature
Revises: 0072_platform_ops
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073_playbook_signature"
down_revision = "0072_platform_ops"
branch_labels = None
depends_on = None

JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("definition", sa.Column("signature", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("definition", "signature")

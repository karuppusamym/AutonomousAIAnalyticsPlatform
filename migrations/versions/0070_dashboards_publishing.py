"""Dashboards and publishing (spec S08): an artifact version records the typed ops that produced it
(`diff`), and a publication records the dashboard artifact version it published plus what the reconcile
job last found (`drift`, `reconciled_at`). No new tables, so no new tenancy objects: both tables are
already tenant-filled and row-level secured.

Revision ID: 0070_dashboards_publishing
Revises: 0069_claims_evidence
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0070_dashboards_publishing"
down_revision = "0069_claims_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact_version", sa.Column("diff", JSONB(), nullable=True))
    op.add_column("publication", sa.Column("artifact_id", sa.String(40), nullable=True))
    op.add_column("publication", sa.Column("artifact_version", sa.Integer(), nullable=True))
    op.add_column("publication", sa.Column("drift", JSONB(), nullable=True))
    op.add_column("publication", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_publication_artifact_id", "publication", ["artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_publication_artifact_id", table_name="publication")
    for column in ("reconciled_at", "drift", "artifact_version", "artifact_id"):
        op.drop_column("publication", column)
    op.drop_column("artifact_version", "diff")

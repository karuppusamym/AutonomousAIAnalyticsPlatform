"""Claim labels and publication banners (spec S07-T3, T7): insight.claim_label (observed |
supported_association | possible_contributor | rejected | unknown) and artifact.banner (set while a finding
a published artifact presents is VOID). Both tables already carry tenant isolation; no new table.

Revision ID: 0069_claims_evidence
Revises: 0068_desk_daily
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_claims_evidence"
down_revision = "0068_desk_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("insight", sa.Column("claim_label", sa.String(30), nullable=False, server_default="unknown"))
    # Findings written before labels: what their status already says (see evidence.claims.label_of).
    op.execute("UPDATE insight SET claim_label = CASE status WHEN 'verified' THEN 'supported_association' "
               "WHEN 'failed_verification' THEN 'rejected' WHEN 'rejected' THEN 'rejected' ELSE 'unknown' END")
    op.add_column("artifact", sa.Column("banner", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifact", "banner")
    op.drop_column("insight", "claim_label")

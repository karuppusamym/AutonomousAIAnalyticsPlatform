"""Pilot assessments of conversational deliverables (VALUE-05): an assessment names what it measured
(`subject_type` + `subject_id`: an investigation run, an Ask conversation, a builder dashboard, a report document or
a scheduled digest), so `run_id` becomes optional; plus `review_minutes` and `interventions` (evaluation plan §3).

Expand-only: new nullable/defaulted columns; existing rows are investigations (subject_id = run_id). The previous
release ignores the new columns and never writes a row without a run_id.

Revision ID: 0075_pilot_review_subjects
Revises: 0074_context_federation
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075_pilot_review_subjects"
down_revision = "0074_context_federation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pilot_review", sa.Column("subject_type", sa.String(20), nullable=False, server_default="investigation"))
    op.add_column("pilot_review", sa.Column("subject_id", sa.String(40), nullable=True))
    op.add_column("pilot_review", sa.Column("review_minutes", sa.Float(), nullable=True))
    op.add_column("pilot_review", sa.Column("interventions", sa.Integer(), nullable=False, server_default="0"))
    op.execute("UPDATE pilot_review SET subject_id = run_id WHERE subject_id IS NULL")
    op.alter_column("pilot_review", "run_id", existing_type=sa.String(40), nullable=True)
    op.create_index("ix_pilot_review_subject_id", "pilot_review", ["subject_id"])


def downgrade() -> None:
    op.execute("DELETE FROM pilot_review WHERE run_id IS NULL")
    op.alter_column("pilot_review", "run_id", existing_type=sa.String(40), nullable=False)
    op.drop_index("ix_pilot_review_subject_id", table_name="pilot_review")
    for column in ("interventions", "review_minutes", "subject_id", "subject_type"):
        op.drop_column("pilot_review", column)

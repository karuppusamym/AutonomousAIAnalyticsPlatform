"""Investigation follow-up actions and pilot measurements."""
import sqlalchemy as sa
from alembic import op

revision = "0055_investigation_value"
down_revision = "0054_n7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("investigation_action",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("insight_id", sa.String(40), sa.ForeignKey("insight.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False), sa.Column("next_step", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.String(40)), sa.Column("due_date", sa.String(10)),
        sa.Column("impact_assessment", sa.Text(), nullable=False), sa.Column("success_measure", sa.Text(), nullable=False),
        sa.Column("open_questions", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("pilot_review",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("analysis_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_id", sa.String(40), nullable=False), sa.Column("evidence_kind", sa.String(20), nullable=False),
        sa.Column("task", sa.String(500), nullable=False), sa.Column("baseline_minutes", sa.Float(), nullable=False),
        sa.Column("assisted_minutes", sa.Float(), nullable=False), sa.Column("accuracy", sa.String(20), nullable=False),
        sa.Column("usefulness", sa.Integer(), nullable=False), sa.Column("would_use_again", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "reviewer_id", "evidence_kind", name="uq_pilot_review_run_user_kind"))
    for table, columns in {"investigation_action": ["workspace_id", "run_id", "insight_id"],
                           "pilot_review": ["workspace_id", "run_id"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    op.drop_table("pilot_review")
    op.drop_table("investigation_action")

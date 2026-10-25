"""Reviewed context bindings and golden grounding evaluations."""
import sqlalchemy as sa
from alembic import op

revision = "0056_context_grounding"
down_revision = "0055_investigation_value"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hypothesis", sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("context_binding", sa.Column("id", sa.String(40), primary_key=True),
                    sa.Column("workspace_id", sa.String(40), nullable=False), sa.Column("entry_id", sa.String(40)),
                    sa.Column("term", sa.String(200), nullable=False), sa.Column("state", sa.String(20), nullable=False),
                    sa.Column("definition", sa.JSON(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
                    sa.Column("created_by", sa.String(40), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
                    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_context_binding_workspace_id", "context_binding", ["workspace_id"])
    op.create_table("golden_question", sa.Column("id", sa.String(40), primary_key=True),
                    sa.Column("workspace_id", sa.String(40), nullable=False), sa.Column("definition", sa.JSON(), nullable=False),
                    sa.Column("created_by", sa.String(40), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_golden_question_workspace_id", "golden_question", ["workspace_id"])
    op.create_table("grounding_evaluation", sa.Column("id", sa.String(40), primary_key=True),
                    sa.Column("workspace_id", sa.String(40), nullable=False), sa.Column("reviewer_id", sa.String(40), nullable=False),
                    sa.Column("result", sa.JSON(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_grounding_evaluation_workspace_id", "grounding_evaluation", ["workspace_id"])


def downgrade():
    for table in ("grounding_evaluation", "golden_question", "context_binding"):
        op.drop_table(table)
    op.drop_column("hypothesis", "provenance")

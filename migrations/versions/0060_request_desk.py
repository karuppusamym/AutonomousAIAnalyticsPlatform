"""Analyst Request Desk (spec S13, ADR-011): request, request_event, class_baseline. Each is tenant-owned
through its workspace; tenant isolation (column, fill trigger, row-level security) is installed for them here.

Revision ID: 0060_request_desk
Revises: 0059_model_config_layers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0060_request_desk"
down_revision = "0059_model_config_layers"
branch_labels = None
depends_on = None

TABLES = ("request", "request_event", "class_baseline")


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "request",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("requester_id", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_intent", JSONB(), nullable=False, server_default="{}"),
        sa.Column("request_class", sa.String(4), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),
        _ts("sla_due_at", True),
        _ts("sla_breached_at", True),
        sa.Column("assignee_id", sa.String(40), nullable=True),
        sa.Column("conversation_id", sa.String(40), nullable=True),
        sa.Column("run_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("triage", JSONB(), nullable=False, server_default="{}"),
        sa.Column("answer_ref", JSONB(), nullable=True),
        sa.Column("data_pack", JSONB(), nullable=True),
        sa.Column("resolution", sa.String(12), nullable=True),
        sa.Column("human_minutes", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duplicate_of", sa.String(40), nullable=True),
        sa.Column("followers", JSONB(), nullable=False, server_default="[]"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("first_answer_at", True),
        _ts("closed_at", True),
    )
    for col in ("workspace_id", "requester_id", "assignee_id", "duplicate_of"):
        op.create_index(f"ix_request_{col}", "request", [col])
    op.create_index("ix_request_ws_state", "request", ["workspace_id", "state"])
    op.create_index("ix_request_ws_created", "request", ["workspace_id", "created_at"])

    op.create_table(
        "request_event",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("request_id", sa.String(40), sa.ForeignKey("request.id", ondelete="CASCADE"), nullable=False),
        _ws(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True),
        sa.Column("to_state", sa.String(24), nullable=True),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        _ts("created_at"),
    )
    op.create_index("ix_request_event_request_id", "request_event", ["request_id"])
    op.create_index("ix_request_event_workspace_id", "request_event", ["workspace_id"])

    op.create_table(
        "class_baseline",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("request_class", sa.String(4), nullable=False),
        sa.Column("baseline_minutes", sa.Float(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        _ts("measured_at"),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "request_class", name="uq_class_baseline_ws_class"),
    )
    op.create_index("ix_class_baseline_workspace_id", "class_baseline", ["workspace_id"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

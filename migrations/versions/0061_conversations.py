"""Conversations and memory (spec S04): channel, Desk request, branch and legal-hold columns on ask_thread, plus
conversation_state, conversation_summary, conversation_correction and conversation_memory_entry. Each new table is
tenant-owned through its workspace; tenant isolation (column, fill trigger, row-level security) is installed here.

Revision ID: 0061_conversations
Revises: 0060_request_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_conversations"
down_revision = "0060_request_desk"
branch_labels = None
depends_on = None

TABLES = ("conversation_state", "conversation_summary", "conversation_correction", "conversation_memory_entry")


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def _ws() -> sa.Column:
    return sa.Column("workspace_id", sa.String(40), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False)


def _conv(ondelete: str = "CASCADE", nullable: bool = False) -> sa.Column:
    return sa.Column("conversation_id", sa.String(40), sa.ForeignKey("ask_thread.id", ondelete=ondelete), nullable=nullable)


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.db import vectors
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.add_column("ask_thread", sa.Column("channel", sa.String(20), nullable=False, server_default="ui"))
    op.add_column("ask_thread", sa.Column("request_id", sa.String(40), nullable=True))
    op.add_column("ask_thread", sa.Column("parent_conversation_id", sa.String(40),
                                          sa.ForeignKey("ask_thread.id", ondelete="SET NULL",
                                                        name="fk_ask_thread_parent_conversation"), nullable=True))
    op.add_column("ask_thread", sa.Column("branch_from_message_id", sa.String(40), nullable=True))
    op.add_column("ask_thread", sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("ask_thread", _ts("closed_at", True))
    op.add_column("ask_thread", _ts("summarized_at", True))
    op.create_index("ix_ask_thread_request_id", "ask_thread", ["request_id"])
    op.create_index("ix_ask_thread_parent_conversation_id", "ask_thread", ["parent_conversation_id"])

    op.create_table(
        "conversation_state",
        sa.Column("id", sa.String(40), primary_key=True),
        _conv(), _ws(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("at_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_id", sa.String(40), nullable=True),
        sa.Column("cause", sa.String(20), nullable=False),
        sa.Column("state", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(80), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("conversation_id", "version", name="uq_conversation_state_version"),
    )
    op.create_table(
        "conversation_summary",
        sa.Column("id", sa.String(40), primary_key=True),
        _conv(), _ws(),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("through_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("trust", sa.String(20), nullable=False),
        sa.Column("summary", JSONB(), nullable=False, server_default="{}"),
        sa.Column("guard", JSONB(), nullable=False, server_default="{}"),
        sa.Column("produced_by", sa.String(120), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("conversation_id", "version", name="uq_conversation_summary_version"),
    )
    op.create_table(
        "conversation_correction",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(), _conv("SET NULL", nullable=True),
        sa.Column("message_id", sa.String(40), nullable=True),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("term", sa.String(200), nullable=False),
        sa.Column("term_key", sa.String(200), nullable=False),
        sa.Column("meaning", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("promoted_from", sa.String(40), nullable=True),
        sa.Column("suggestion_id", sa.String(40), nullable=True),
        _ts("expires_at", True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_conversation_correction_memory", "conversation_correction", ["workspace_id", "scope", "status"])
    op.create_index("ix_conversation_correction_user_id", "conversation_correction", ["user_id"])
    op.create_table(
        "conversation_memory_entry",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("conversation_id", sa.String(40), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("ref_table", sa.String(60), nullable=False),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("trust", sa.String(20), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("owner_id", sa.String(40), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        _ts("valid_from"),
        _ts("valid_to", True),
        sa.Column("embedding", vectors.column_type(256), nullable=True),
        sa.Column("embedding_model", sa.String(60), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("ref_table", "ref_id", "version", name="uq_conversation_memory_ref"),
    )
    op.create_index("ix_conversation_memory_lookup", "conversation_memory_entry", ["workspace_id", "kind", "trust"])
    for table in TABLES:
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
        if table != "conversation_correction":
            op.create_index(f"ix_{table}_conversation_id", table, ["conversation_id"])
    op.create_index("ix_conversation_correction_conversation_id", "conversation_correction", ["conversation_id"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it
    op.drop_index("ix_ask_thread_parent_conversation_id", table_name="ask_thread")
    op.drop_index("ix_ask_thread_request_id", table_name="ask_thread")
    op.drop_constraint("fk_ask_thread_parent_conversation", "ask_thread", type_="foreignkey")
    for col in ("summarized_at", "closed_at", "legal_hold", "branch_from_message_id", "parent_conversation_id",
                "request_id", "channel"):
        op.drop_column("ask_thread", col)

"""Proactive monitoring, triggers and recurring work (spec S09): trigger_rule (signed webhook / Kafka
ingress per monitor), trigger_event (durable queue + 7-day replay cache), alert group (correlated alerts),
channel_binding and channel_message (inbound Slack / Teams / email to the Request Desk); monitor gains
anti-noise state and its rule approval; alert gains alert group, quiet-hours deferral and notification time.
Tenant isolation (column, fill trigger, row-level security) is installed for the new tables here.

Revision ID: 0071_triggers
Revises: 0070_dashboards_publishing
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0071_triggers"
down_revision = "0070_dashboards_publishing"
branch_labels = None
depends_on = None

TABLES = ("trigger_rule", "trigger_event", "alert_group", "channel_binding", "channel_message")


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
        "trigger_rule",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("monitor_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("action", sa.String(20), nullable=False, server_default="evaluate"),
        sa.Column("secret_ref", sa.String(200), nullable=True),
        sa.Column("payload_schema", JSONB(), nullable=False, server_default="{}"),
        sa.Column("topic", sa.String(250), nullable=True),
        sa.Column("rule_set", sa.String(80), nullable=False, server_default="default"),
        sa.Column("transform", JSONB(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_trigger_rule_name"),
    )
    op.create_index("ix_trigger_rule_workspace_id", "trigger_rule", ["workspace_id"])
    op.create_index("ix_trigger_rule_monitor_id", "trigger_rule", ["monitor_id"])

    op.create_table(
        "trigger_event",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("rule_id", sa.String(40), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("locked_until", True),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("received_at"),
        _ts("processed_at", True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("rule_id", "event_id", name="uq_trigger_event"),
    )
    op.create_index("ix_trigger_event_workspace_id", "trigger_event", ["workspace_id"])
    op.create_index("ix_trigger_event_rule_id", "trigger_event", ["rule_id"])
    op.create_index("ix_trigger_event_due", "trigger_event", ["status", "received_at"])
    op.create_index("ix_trigger_event_expires", "trigger_event", ["expires_at"])

    op.create_table(
        "alert_group",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("correlation_key", sa.String(64), nullable=False),
        sa.Column("key_values", JSONB(), nullable=False, server_default="{}"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("severity", sa.String(10), nullable=False, server_default="warning"),
        sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suppressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_alert_id", sa.String(40), nullable=True),
        sa.Column("monitor_ids", JSONB(), nullable=False, server_default="[]"),
        _ts("started_at"),
        _ts("last_alert_at", True),
        _ts("ended_at", True),
    )
    op.create_index("ix_alert_group_workspace_id", "alert_group", ["workspace_id"])
    op.create_index("ix_alert_group_ws_key_status", "alert_group", ["workspace_id", "correlation_key", "status"])

    op.create_table(
        "channel_binding",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("external_key", sa.String(300), nullable=True),
        sa.Column("secret_ref", sa.String(200), nullable=False),
        sa.Column("default_requester_id", sa.String(40), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(40), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_channel_binding_name"),
    )
    op.create_index("ix_channel_binding_workspace_id", "channel_binding", ["workspace_id"])

    op.create_table(
        "channel_message",
        sa.Column("id", sa.String(40), primary_key=True),
        _ws(),
        sa.Column("binding_id", sa.String(40), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("sender", sa.String(320), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(40), nullable=True),
        _ts("received_at"),
        sa.UniqueConstraint("binding_id", "external_id", name="uq_channel_message"),
    )
    op.create_index("ix_channel_message_workspace_id", "channel_message", ["workspace_id"])
    op.create_index("ix_channel_message_binding_id", "channel_message", ["binding_id"])

    op.add_column("monitor", sa.Column("noise_state", JSONB(), nullable=False, server_default="{}"))
    op.add_column("monitor", sa.Column("approval_id", sa.String(40), nullable=True))
    op.add_column("monitor", sa.Column("rule_hash", sa.String(64), nullable=True))
    op.add_column("alert", sa.Column("group_id", sa.String(40), nullable=True))
    op.add_column("alert", sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("alert", sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_alert_group_id", "alert", ["group_id"])
    op.create_index("ix_alert_deferred_until", "alert", ["deferred_until"])

    sources = {t: TenantSource("workspace", "workspace_id", nullable=False) for t in TABLES}
    install(op.get_bind(), sources, role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    op.drop_index("ix_alert_deferred_until", "alert")
    op.drop_index("ix_alert_group_id", "alert")
    for column in ("notified_at", "deferred_until", "group_id"):
        op.drop_column("alert", column)
    for column in ("rule_hash", "approval_id", "noise_state"):
        op.drop_column("monitor", column)
    for table in reversed(TABLES):
        op.drop_table(table)  # drops its policy and fill trigger with it

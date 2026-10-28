"""Trigger, alert group and channel tables (spec S09). Each carries ``workspace_id`` with a foreign key, so the
tenancy trigger fills ``tenant_id`` from the workspace and row-level security applies (ADR-009)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TriggerRule(Base):
    """How an external event reaches a monitor: a signed webhook or a Kafka topic, with the typed payload
    schema every event must satisfy. The rule never widens what the monitor may do: the monitor's own
    approval (scope, recipients, playbook, limits) still governs every side effect."""

    __tablename__ = "trigger_rule"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_trigger_rule_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    monitor_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # webhook | kafka
    action: Mapped[str] = mapped_column(String(20), default="evaluate")  # evaluate | observe
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)  # webhook: env:ANALYSTOS_TRIGGER_<NAME>
    payload_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # JSON Schema (object)
    topic: Mapped[str | None] = mapped_column(String(250), nullable=True)  # kafka
    rule_set: Mapped[str] = mapped_column(String(80), default="default")  # kafka: one consumer group per rule set
    transform: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {field: "/json/pointer"}; event_id pointer
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TriggerEvent(Base):
    """One accepted event: the durable queue entry and, through ``uq_trigger_event``, the replay cache
    (a second delivery of the same ``event_id`` for the same rule is recognised, never processed twice)."""

    __tablename__ = "trigger_event"
    __table_args__ = (UniqueConstraint("rule_id", "event_id", name="uq_trigger_event"),
                      Index("ix_trigger_event_due", "status", "received_at"),
                      Index("ix_trigger_event_expires", "expires_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[str] = mapped_column(String(40), index=True)
    event_id: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20))  # webhook | kafka
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    # queued | processing | processed | suppressed | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = _ts()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # replay window end (7 days)


class AlertGroup(Base):
    """Alerts that share a correlation key (for example metric + segment) merged into one thing people
    see and are notified about once."""

    __tablename__ = "alert_group"
    __table_args__ = (Index("ix_alert_group_ws_key_status", "workspace_id", "correlation_key", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    correlation_key: Mapped[str] = mapped_column(String(64))
    key_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved
    severity: Mapped[str] = mapped_column(String(10), default="warning")
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, default=0)
    first_alert_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    monitor_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = _ts()
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelBinding(Base):
    """An inbound conversation channel (Slack, Teams, email) bound to one workspace. Each binding has its own
    URL and signing secret; a message it accepts becomes a Request Desk request (S13), nothing more."""

    __tablename__ = "channel_binding"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_channel_binding_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # slack | teams | email
    name: Mapped[str] = mapped_column(String(200))
    external_key: Mapped[str | None] = mapped_column(String(300), nullable=True)  # slack team id, teams tenant, mailbox
    secret_ref: Mapped[str] = mapped_column(String(200))  # env:ANALYSTOS_CHANNEL_<NAME>
    default_requester_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class ChannelMessage(Base):
    """One inbound message: its outcome, and (``uq_channel_message``) the replay guard for its platform id."""

    __tablename__ = "channel_message"
    __table_args__ = (UniqueConstraint("binding_id", "external_id", name="uq_channel_message"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    binding_id: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    sender: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20))  # accepted | refused
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    received_at: Mapped[datetime] = _ts()

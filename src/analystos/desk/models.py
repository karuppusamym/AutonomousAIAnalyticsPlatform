"""Request Desk tables (spec S13, ADR-011). Each carries ``workspace_id`` with a foreign key, so the tenancy
trigger fills ``tenant_id`` from the workspace and row-level security applies (ADR-009)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Request(Base):
    """One piece of analyst work asked for by a person or a client, from intake to close."""

    __tablename__ = "request"
    __table_args__ = (Index("ix_request_ws_state", "workspace_id", "state"),
                      Index("ix_request_ws_created", "workspace_id", "created_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    requester_id: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # ui | api | mcp | teams | slack | email
    text: Mapped[str] = mapped_column(Text)
    normalized_intent: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # metrics, dims, period, outcome, key
    # R1..R9 (product spec §4). Spec S13 calls the column `class`; a keyword in Python and awkward in SQL.
    request_class: Mapped[str | None] = mapped_column(String(4), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="new")
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high | urgent
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # ask_thread id
    run_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    triage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # class, answered_by, confidence, flags, missing
    answer_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)  # turn/evidence/artifact refs, via
    data_pack: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)  # prepared for the analyst
    resolution: Mapped[str | None] = mapped_column(String(12), nullable=True)  # auto | human | declined | duplicate
    human_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_of: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    followers: Mapped[list[str]] = mapped_column(JSONB, default=list)  # requesters of linked duplicates
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    first_answer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequestEvent(Base):
    """The request's typed timeline. Every state change writes one; the UI stream gets a run_event too."""

    __tablename__ = "request_event"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("request.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    actor: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _ts()


class ClassBaseline(Base):
    """Minutes an analyst takes for one request of a class without the platform (practitioner protocol,
    docs/60-delivery/06-practitioner-baseline-protocol.md). The Value screen's minutes-saved estimate rests on it."""

    __tablename__ = "class_baseline"
    __table_args__ = (UniqueConstraint("workspace_id", "request_class", name="uq_class_baseline_ws_class"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    request_class: Mapped[str] = mapped_column(String(4))
    baseline_minutes: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40))  # practitioner_protocol | pilot_review | assumed
    measured_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts()

"""Autonomy engine tables (spec S06). Each carries ``workspace_id`` with a foreign key, so the tenancy
trigger fills ``tenant_id`` from the workspace and row-level security applies (ADR-009)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Goal(Base):
    """A Goal API v2 request (S06 §3.1): what was asked, how the complexity router classified it,
    the limits its run executes under, and the run that carries it out."""

    __tablename__ = "goal"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    question: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20))  # goal_autonomous | goal_supervised
    data_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    requested_outputs: Mapped[list[str]] = mapped_column(JSONB, default=list)
    playbook: Mapped[str | None] = mapped_column(String(120), nullable=True)  # as requested
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # effective (router defaults ∧ request)
    approval_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    complexity: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # ComplexityDecision
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class RunPlanVersion(Base):
    """An immutable record of a superseded plan version (S06 §3.5): the plan, its hash, why it was
    replaced and what its tasks had produced. `GET /v2/runs/{id}/plan?version=` reads it."""

    __tablename__ = "run_plan_version"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_run_plan_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tasks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # key, status, attempts, output, error
    reason_code: Mapped[str] = mapped_column(String(30))  # why the NEXT version replaced this one
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    superseded_at: Mapped[datetime] = _ts()

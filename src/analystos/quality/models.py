"""Declared quality rules and their results (P1-7). Both tables carry `workspace_id` with a foreign key, so the
tenancy trigger fills `tenant_id` from the workspace and row-level security applies (ADR-009, migration 0077).
A result holds counts and one value, never rows (ADR-014)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base


class QualityRuleRow(Base):
    """One declared rule on one asset (or column). `definition` is the validated `contracts.quality.QualityRule`;
    `content_hash` its identity, so declaring the same rule twice returns the first. Only `active` rules run:
    a recommendation is a knowledge suggestion until an owner approves it, which creates the row."""

    __tablename__ = "quality_rule"
    __table_args__ = (UniqueConstraint("workspace_id", "content_hash", name="uq_quality_rule_content"),
                      Index("ix_quality_rule_ws_asset", "workspace_id", "asset", "status"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # catalog asset at declaration time
    asset: Mapped[str] = mapped_column(String(330))  # schema.table
    column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30))
    dimension: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(10), default="warning")
    threshold: Mapped[float] = mapped_column(Float, default=1.0)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | disabled
    origin: Mapped[str] = mapped_column(String(30))  # owner | recommended | import:okf
    suggestion_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # passed | failed | error
    last_pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QualityRuleResult(Base):
    """One evaluation of one rule: counts, pass rate or value, the gateway query id. Never result rows."""

    __tablename__ = "quality_rule_result"
    __table_args__ = (Index("ix_quality_rule_result_rule_at", "rule_id", "run_at"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("quality_rule.id", ondelete="CASCADE"))
    rule_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10))  # passed | failed | error
    pass_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    passed_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    query_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(80))  # manual:<user> | monitor:<id>
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

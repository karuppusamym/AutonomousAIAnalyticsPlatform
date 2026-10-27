"""Audit hash chain (spec S01-T5): ``audit_chain`` seals ``audit_event`` rows into one SHA-256 chain per
tenant. The table is tenant-owned (tenant_id from its event), row-level secured, append-only, and only
the sealer in system scope may insert into it. Existing events are sealed by the scheduler (or
``analystos audit seal``) after the upgrade, not here: the migration stays fast on large audit logs.

Revision ID: 0058_audit_chain
Revises: 0057_tenancy
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_audit_chain"
down_revision = "0057_tenancy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import TenantSource

    op.create_table(
        "audit_chain",
        sa.Column("event_id", sa.BigInteger(), sa.ForeignKey("audit_event.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("chain", sa.String(40), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("chain", "seq", name="uq_audit_chain_seq"),
    )
    # The sealer finds a tenant's unsealed events by (tenant, time window).
    op.create_index("ix_audit_event_tenant_created", "audit_event", ["tenant_id", "created_at"])
    install(op.get_bind(), {"audit_chain": TenantSource("audit_event", "event_id", True)},
            role=get_settings().tenancy_app_role, backfill=False)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TRIGGER IF EXISTS audit_event_immutable ON audit_chain"))
    conn.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON audit_chain"))
    op.drop_index("ix_audit_event_tenant_created", table_name="audit_event")
    op.drop_table("audit_chain")
    conn.execute(sa.text("DROP FUNCTION IF EXISTS tenancy_fill__audit_event__event_id() CASCADE"))

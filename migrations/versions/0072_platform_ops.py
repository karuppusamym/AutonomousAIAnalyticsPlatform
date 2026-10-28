"""Platform operations (spec S12): query_execution gains scan_bytes and credits (cost ledger inputs); the
model_matrix_result table holds the per-model evaluation matrix (a platform table: no tenant data); the
`cost_ledger` view lists one unpriced line per model call, gateway statement, compute task and artifact write
for SQL/BI readers. The view is security_invoker, so the reader's row-level security applies to every base
table (analystos.ops.cost_ledger prices the same lines with config/cost_rates.yaml).

Expand-only (S12 §1): new nullable columns, a new table and a view; the previous release keeps working.

Revision ID: 0072_platform_ops
Revises: 0071_triggers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0072_platform_ops"
down_revision = "0071_triggers"
branch_labels = None
depends_on = None

COST_LEDGER_VIEW = """
CREATE OR REPLACE VIEW cost_ledger WITH (security_invoker = true) AS
SELECT 'model'::text AS category, mc.tenant_id, mc.workspace_id, mc.run_id, mc.task_id AS request_id,
       t.thread_id AS conversation_id, COALESCE(t.user_id, r.requested_by) AS user_id,
       (mc.created_at AT TIME ZONE 'UTC')::date AS day, mc.model AS item,
       (mc.input_tokens + mc.output_tokens)::float8 AS quantity, 'tokens'::text AS unit,
       NULL::float8 AS credits, mc.cost_usd::float8 AS cost_usd
  FROM model_call mc
  LEFT JOIN ask_turn t ON t.id = mc.task_id
  LEFT JOIN analysis_run r ON r.id = mc.run_id
 WHERE mc.status IN ('ok', 'error')
UNION ALL
SELECT 'query', q.tenant_id, q.workspace_id, q.run_id, q.task_id, t.thread_id,
       COALESCE(t.user_id, r.requested_by, CASE WHEN q.actor LIKE 'user:%' THEN substr(q.actor, 6) END),
       (q.created_at AT TIME ZONE 'UTC')::date, COALESCE(q.source_id, 'staged'),
       COALESCE(q.scan_bytes, 0)::float8, 'scan_bytes', q.credits::float8, NULL::float8
  FROM query_execution q
  LEFT JOIN ask_turn t ON t.id = q.task_id
  LEFT JOIN analysis_run r ON r.id = q.run_id
 WHERE q.status = 'ok' AND NOT q.cache_hit
UNION ALL
SELECT 'compute', w.tenant_id, w.workspace_id, w.run_id, w.work_order_id, NULL, r.requested_by,
       (w.created_at AT TIME ZONE 'UTC')::date, w.pool,
       COALESCE((w.usage ->> 'cpu_seconds')::float8, 0), 'cpu_seconds', NULL::float8, NULL::float8
  FROM worker_task w
  LEFT JOIN analysis_run r ON r.id = w.run_id
UNION ALL
SELECT 'storage', a.tenant_id, a.workspace_id, a.run_id, NULL, NULL, COALESCE(a.creator_user, r.requested_by),
       (a.created_at AT TIME ZONE 'UTC')::date, a.type,
       octet_length(a.content::text)::float8, 'bytes', NULL::float8, NULL::float8
  FROM artifact a
  LEFT JOIN analysis_run r ON r.id = a.run_id
"""


def upgrade() -> None:
    op.add_column("query_execution", sa.Column("scan_bytes", sa.BigInteger(), nullable=True))
    op.add_column("query_execution", sa.Column("credits", sa.Float(), nullable=True))
    op.create_table(
        "model_matrix_result",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("batch_id", sa.String(40), nullable=False, index=True),
        sa.Column("suite", sa.String(60), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("profile", sa.String(60), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metrics", JSONB(), nullable=False, server_default="{}"),
        sa.Column("failures", JSONB(), nullable=False, server_default="[]"),
        sa.Column("gates_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gates_sha256", sa.String(64), nullable=True),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_matrix_result_profile_model_created", "model_matrix_result", ["profile", "model", "created_at"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(COST_LEDGER_VIEW)
        from analystos.core.config import get_settings

        from analystos.tenancy.ddl import _q

        bind = op.get_bind()
        role = get_settings().tenancy_app_role
        if bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar():
            bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON model_matrix_result TO {_q(role)}"))
            bind.execute(sa.text(f"GRANT SELECT ON cost_ledger TO {_q(role)}"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP VIEW IF EXISTS cost_ledger")
    op.drop_index("ix_model_matrix_result_profile_model_created", table_name="model_matrix_result")
    op.drop_table("model_matrix_result")
    op.drop_column("query_execution", "credits")
    op.drop_column("query_execution", "scan_bytes")

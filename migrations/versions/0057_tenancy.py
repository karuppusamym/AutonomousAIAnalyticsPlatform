"""Tenant isolation (ADR-009, spec S01): tenant + tenant_member, tenant_id on every tenant-owned
table, fill triggers, row-level security for the restricted application role, append-only audit.

The table list is frozen here; later migrations that add tenant-owned tables call
``analystos.tenancy.ddl.install`` with their own tables.

Revision ID: 0057_tenancy
Revises: 0056_context_grounding
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_tenancy"
down_revision = "0056_context_grounding"
branch_labels = None
depends_on = None


def _sources():
    from analystos.tenancy.schema import TenantSource as S

    roots = {"workspace": S(None, None, False, True), "app_user": S(None, None, False, True),
             "tenant_member": S(None, None, False)}
    return {**roots, **FROZEN(S)}


def FROZEN(S):  # noqa: N802,N803
    return {
        "workspace_member": S('workspace', 'workspace_id', False, False, False),
        "workspace_policy": S('workspace', 'workspace_id', False, False, False),
        "source": S('workspace', 'workspace_id', False, False, False),
        "source_asset": S('workspace', 'workspace_id', True, False, False),
        "relationship": S('workspace', 'workspace_id', True, False, False),
        "context_entry": S('workspace', 'workspace_id', True, False, False),
        "knowledge_pack": S('workspace', 'workspace_id', True, False, True),
        "knowledge_document": S('workspace', 'workspace_id', True, False, True),
        "knowledge_section": S('workspace', 'workspace_id', True, False, True),
        "knowledge_link": S('workspace', 'workspace_id', True, False, True),
        "knowledge_suggestion": S('workspace', 'workspace_id', False, False, False),
        "workspace_capability": S('workspace', 'workspace_id', False, False, False),
        "analysis_run": S('workspace', 'workspace_id', False, False, False),
        "run_event": S('workspace', 'workspace_id', True, False, False),
        "model_call": S('workspace', 'workspace_id', True, False, False),
        "tool_execution": S('workspace', 'workspace_id', True, False, False),
        "query_execution": S('workspace', 'workspace_id', True, False, False),
        "hypothesis": S('workspace', 'workspace_id', True, False, False),
        "experiment": S('workspace', 'workspace_id', True, False, False),
        "insight": S('workspace', 'workspace_id', True, False, False),
        "verification_record": S('workspace', 'workspace_id', True, False, False),
        "evidence_citation_set": S('workspace', 'workspace_id', True, False, False),
        "artifact": S('workspace', 'workspace_id', True, False, False),
        "lineage_edge": S('workspace', 'workspace_id', True, False, False),
        "approval": S('workspace', 'workspace_id', True, False, False),
        "semantic_model": S('workspace', 'workspace_id', False, False, False),
        "semantic_relationship_candidate": S('workspace', 'workspace_id', False, False, False),
        "semantic_metric": S('workspace', 'workspace_id', False, False, False),
        "publication": S('workspace', 'workspace_id', True, False, False),
        "bi_dashboard_import": S('workspace', 'workspace_id', False, False, False),
        "feedback": S('workspace', 'workspace_id', True, False, False),
        "audit_event": S('workspace', 'workspace_id', True, False, False),
        "schedule": S('workspace', 'workspace_id', False, False, False),
        "schedule_run": S('workspace', 'workspace_id', True, False, False),
        "monitor": S('workspace', 'workspace_id', False, False, False),
        "alert": S('workspace', 'workspace_id', True, False, False),
        "notification": S('workspace', 'workspace_id', True, False, False),
        "delivery_destination": S('workspace', 'workspace_id', False, False, False),
        "delivery": S('workspace', 'workspace_id', True, False, False),
        "crawl_run": S('workspace', 'workspace_id', True, False, False),
        "decision": S('workspace', 'workspace_id', True, False, False),
        "decision_outcome": S('workspace', 'workspace_id', True, False, False),
        "mcp_server": S('workspace', 'workspace_id', False, False, False),
        "mcp_grant": S('workspace', 'workspace_id', False, False, False),
        "mcp_usage": S('workspace', 'workspace_id', True, False, False),
        "verified_query": S('workspace', 'workspace_id', False, False, False),
        "registered_hypothesis": S('workspace', 'workspace_id', False, False, False),
        "build_target": S('workspace', 'workspace_id', False, False, False),
        "build_job": S('workspace', 'workspace_id', False, False, False),
        "ask_thread": S('workspace', 'workspace_id', False, False, False),
        "ask_turn": S('workspace', 'workspace_id', True, False, False),
        "definition": S('workspace', 'workspace_id', False, False, False),
        "idempotency_record": S('workspace', 'workspace_id', True, False, False),
        "dispatch_outbox": S('workspace', 'workspace_id', True, False, False),
        "work_order": S('workspace', 'workspace_id', False, False, False),
        "recipe": S('workspace', 'workspace_id', False, False, False),
        "recipe_run": S('workspace', 'workspace_id', False, False, False),
        "workspace_brief": S('workspace', 'workspace_id', False, False, False),
        "readiness_assessment": S('workspace', 'workspace_id', False, False, False),
        "step_branch": S('workspace', 'workspace_id', False, False, False),
        "analysis_step": S('workspace', 'workspace_id', False, False, False),
        "analysis_step_version": S('workspace', 'workspace_id', True, False, False),
        "step_pin": S('workspace', 'workspace_id', False, False, False),
        "notebook": S('workspace', 'workspace_id', False, False, False),
        "pipeline": S('workspace', 'workspace_id', False, False, False),
        "pipeline_run": S('workspace', 'workspace_id', False, False, False),
        "writer_destination": S('workspace', 'workspace_id', False, False, False),
        "materialization": S('workspace', 'workspace_id', False, False, False),
        "worker_task": S('workspace', 'workspace_id', False, False, False),
        "ml_split": S('workspace', 'workspace_id', False, False, False),
        "ml_experiment": S('workspace', 'workspace_id', False, False, False),
        "ml_model_version": S('workspace', 'workspace_id', False, False, False),
        "ml_scoring_run": S('workspace', 'workspace_id', False, False, False),
        "what_if_scenario": S('workspace', 'workspace_id', False, False, False),
        "index_advice": S('workspace', 'workspace_id', False, False, False),
        "entity_match_run": S('workspace', 'workspace_id', False, False, False),
        "entity_match_pair": S('workspace', 'workspace_id', False, False, False),
        "investigation_action": S('workspace', 'workspace_id', False, False, False),
        "pilot_review": S('workspace', 'workspace_id', False, False, False),
        "context_binding": S('workspace', 'workspace_id', True, False, False),
        "golden_question": S('workspace', 'workspace_id', True, False, False),
        "grounding_evaluation": S('workspace', 'workspace_id', True, False, False),
        "agent_message": S('analysis_run', 'run_id', False, False, False),
        "run_task": S('analysis_run', 'run_id', False, False, False),
        "artifact_version": S('artifact', 'artifact_id', True, False, False),
        "verification_dependency": S('verification_record', 'record_id', True, False, False),
        "knowledge_object": S('knowledge_pack', 'pack_id', True, False, True),
        "knowledge_revision": S('knowledge_pack', 'pack_id', True, False, True),
        "worker_task_event": S('worker_task', 'task_id', False, False, False),
        "source_column": S('source_asset', 'asset_id', True, False, False),
        "user_identity": S('app_user', 'user_id', False, False, False),
        "mcp_client": S('app_user', 'service_user_id', False, False, False),
    }


def upgrade() -> None:
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install

    op.create_table(
        "tenant",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("data_residency", sa.String(20), nullable=True),
        sa.Column("settings", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "tenant_member",
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(30), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_member_user_id", "tenant_member", ["user_id"])
    conn = op.get_bind()
    install(conn, _sources(), role=get_settings().tenancy_app_role, backfill=True)
    for table in ("workspace", "app_user"):
        op.create_foreign_key(f"fk_{table}_tenant_id", table, "tenant", ["tenant_id"], ["id"])


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("workspace", "app_user"):
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
    conn.execute(sa.text("DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS audit_event_immutable()"))
    for table in _sources():
        if table == "tenant_member":
            continue
        conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        conn.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
        conn.execute(sa.text(f'DROP TRIGGER IF EXISTS tenancy_fill ON "{table}"'))
        conn.execute(sa.text(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS tenant_id'))
    op.drop_table("tenant_member")
    conn.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON tenant"))
    op.drop_table("tenant")
    conn.execute(sa.text(
        "DO $do$ DECLARE f record; BEGIN FOR f IN SELECT proname FROM pg_proc WHERE proname LIKE 'tenancy_fill__%' "
        "LOOP EXECUTE format('DROP FUNCTION IF EXISTS %I() CASCADE', f.proname); END LOOP; END $do$"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS analystos_current_tenant() CASCADE"))

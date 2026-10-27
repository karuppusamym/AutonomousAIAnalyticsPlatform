"""Which tables are tenant-owned, and where each one's tenant comes from.

Every table in the metadata is either listed in ``GLOBAL_TABLES`` (with the reason) or tenant-owned.
A tenant-owned table gets a ``tenant_id`` column filled by trigger from, in order: the row's own value,
its parent row (``TenantSource.parent`` via ``fk``), the transaction's bound tenant, and, only for
roots that may be created in system scope (``default_tenant``), the default tenant.
``tests/unit/test_tenancy_rls_coverage.py`` fails when a new table is in neither list.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Column, Index, MetaData, String

# table -> why it is shared across tenants
GLOBAL_TABLES: dict[str, str] = {
    "tenant": "the tenant registry itself (RLS limits a tenant to its own row)",
    "agent_definition": "platform capability catalogue; per-workspace enablement lives in workspace_capability",
    "skill_definition": "platform capability catalogue",
    "tool_definition": "platform capability catalogue",
    "platform_setting": "platform administrator settings (admin-only API)",
    "decision_calibration": "per-purpose model calibration statistics, no tenant data",
    "knowledge_index_state": "index bookkeeping keys, no tenant data",
    "model_payload": "content-addressed redacted payloads, reachable only through tenant-scoped model_call rows",
    "spend_counter": "budget counters keyed by scope string (workspace/run keys carry their ids)",
    "verification_sweep": "platform sweep bookkeeping (counts only)",
}


@dataclass(frozen=True)
class TenantSource:
    parent: str | None  # table whose tenant this row inherits; None = only the bound tenant
    fk: str | None  # column on this table that references parent.id
    nullable: bool  # rows without a tenant may exist (platform rows, or rows naming a pseudo workspace)
    default_tenant: bool = False  # a root created in system scope falls into the default tenant
    shared: bool = False  # tenant-less rows are a platform catalogue every tenant may read (never write)


# Tables whose tenant-less rows (failed logins, platform model calls) stay private to the platform even
# though workspace_id is optional. Other optional-workspace tables (knowledge packs shipped with the
# product) share tenant-less rows read-only. A workspace_id without a foreign key may name a pseudo
# workspace ("platform" budget events): those rows get no tenant and are visible only to system scope.
PLATFORM_PRIVATE_TABLES = frozenset({"audit_event", "model_call", "decision", "decision_outcome"})

# Children whose tenant comes from a parent other than workspace.
CHILD_PARENTS: dict[str, tuple[str, str]] = {
    "agent_message": ("analysis_run", "run_id"),
    "run_task": ("analysis_run", "run_id"),
    "artifact_version": ("artifact", "artifact_id"),
    "verification_dependency": ("verification_record", "record_id"),
    "knowledge_object": ("knowledge_pack", "pack_id"),
    "knowledge_revision": ("knowledge_pack", "pack_id"),
    "worker_task_event": ("worker_task", "task_id"),
    "source_column": ("source_asset", "asset_id"),
    "user_identity": ("app_user", "user_id"),
    "mcp_client": ("app_user", "service_user_id"),
    "audit_chain": ("audit_event", "event_id"),
}
ROOTS = ("workspace", "app_user")  # carry tenant_id as a mapped column; created in system scope -> default tenant
DIRECT = ("tenant_member",)  # tenant_id is part of the row's identity, always given explicitly


def tenant_sources(metadata: MetaData) -> dict[str, TenantSource]:
    """Derive the tenant source of every tenant-owned table from the metadata (parents before children)."""
    tables = metadata.tables
    out: dict[str, TenantSource] = {}
    for name in ROOTS:
        out[name] = TenantSource(None, None, nullable=False, default_tenant=True)
    for name in DIRECT:
        if name in tables:
            out[name] = TenantSource(None, None, nullable=False)
    for name, table in tables.items():
        if name in out or name in GLOBAL_TABLES or name in CHILD_PARENTS:
            continue
        if "workspace_id" in table.c:
            col = table.c.workspace_id
            optional = bool(col.nullable)
            pseudo_ok = not col.foreign_keys  # no FK: the id may name no real workspace
            out[name] = TenantSource("workspace", "workspace_id", nullable=optional or pseudo_ok,
                                     shared=optional and name not in PLATFORM_PRIVATE_TABLES)
    pending = dict(CHILD_PARENTS)
    while pending:
        progressed = False
        for name, (parent, fk) in list(pending.items()):
            if parent in out and name in tables:
                fk_nullable = bool(tables[name].c[fk].nullable)
                out[name] = TenantSource(parent, fk, nullable=out[parent].nullable or fk_nullable,
                                         shared=out[parent].shared)
                del pending[name]
                progressed = True
            elif name not in tables:
                del pending[name]
                progressed = True
        if not progressed:
            raise RuntimeError(f"tenancy: unresolved parents for {sorted(pending)}")
    return out


def unclassified(metadata: MetaData) -> list[str]:
    """Tables that are neither global nor tenant-owned: each one is an isolation hole."""
    owned = tenant_sources(metadata)
    return sorted(n for n in metadata.tables if n not in owned and n not in GLOBAL_TABLES)


def attach_tenant_columns(metadata: MetaData) -> None:
    """Add an (unmapped) ``tenant_id`` column to every tenant-owned table that does not declare one.
    Unmapped on purpose: ORM inserts omit it and the database trigger fills it, so no model changes."""
    for name in tenant_sources(metadata):
        table = metadata.tables[name]
        if "tenant_id" in table.c:
            continue
        # Nullable in the metadata: Postgres gets NOT NULL from tenancy.ddl.install (where the trigger fills
        # the value); SQLite unit fixtures have no trigger.
        table.append_column(Column("tenant_id", String(40), nullable=True))
        Index(f"ix_{name}_tenant_id", table.c.tenant_id)

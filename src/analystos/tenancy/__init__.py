"""Tenant isolation (ADR-009, spec S01).

A tenant owns workspaces; every tenant-owned row carries ``tenant_id``, filled by a database trigger
from the row's workspace (or parent row), and Postgres row-level security restricts the
``analystos_app`` role to the tenant bound to the current transaction. Code never filters by tenant
itself: it binds a tenant (``tenant_scope``) or declares cross-tenant work (``system_scope``), and the
database does the rest.
"""
from analystos.tenancy.context import (
    DEFAULT_TENANT_ID,
    current_tenant,
    is_system,
    set_current_tenant,
    system_scope,
    tenant_scope,
)

__all__ = ["DEFAULT_TENANT_ID", "current_tenant", "is_system", "set_current_tenant", "system_scope", "tenant_scope"]

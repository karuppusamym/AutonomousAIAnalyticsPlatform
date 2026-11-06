"""ADR-009 / spec S01: every table is either tenant-owned (tenant_id + trigger + RLS) or deliberately
global. A new table that is neither fails here before it can leak across tenants."""
from __future__ import annotations

import pytest

from analystos.db import models  # noqa: F401
from analystos.db.base import Base
from analystos.tenancy import DEFAULT_TENANT_ID, current_tenant, is_system, system_scope, tenant_scope
from analystos.tenancy.ddl import _fill_function_sql, _policy_sql, _q
from analystos.tenancy.schema import (
    CHILD_PARENTS,
    GLOBAL_TABLES,
    PLATFORM_PRIVATE_TABLES,
    TenantSource,
    tenant_sources,
    unclassified,
)


def test_every_table_is_tenant_owned_or_declared_global():
    assert unclassified(Base.metadata) == []


def test_global_allowlist_names_real_tables():
    assert sorted(set(GLOBAL_TABLES) - set(Base.metadata.tables)) == []


def test_tenant_owned_tables_have_a_tenant_column():
    missing = [n for n in tenant_sources(Base.metadata) if "tenant_id" not in Base.metadata.tables[n].c]
    assert missing == []


def test_children_inherit_from_tenant_owned_parents():
    sources = tenant_sources(Base.metadata)
    for child, (parent, fk) in CHILD_PARENTS.items():
        assert parent in sources, child
        assert fk in Base.metadata.tables[child].c, (child, fk)


def test_workspace_scoped_tables_derive_from_workspace():
    sources = tenant_sources(Base.metadata)
    for name, table in Base.metadata.tables.items():
        if "workspace_id" in table.c and name not in GLOBAL_TABLES and name not in CHILD_PARENTS:
            assert sources[name].parent == "workspace", name


def test_platform_private_tables_are_nullable_but_not_shared():
    sources = tenant_sources(Base.metadata)
    for name in PLATFORM_PRIVATE_TABLES:
        assert name in sources and sources[name].nullable and not sources[name].shared, name


def test_only_optional_workspace_catalogues_share_tenantless_rows():
    sources = tenant_sources(Base.metadata)
    assert sources["knowledge_pack"].shared and sources["knowledge_object"].shared
    assert sources["run_event"].nullable and not sources["run_event"].shared  # pseudo "platform" workspace
    assert not sources["analysis_run"].nullable  # FK to workspace: always a tenant


def test_context_federation_tables_are_owned_by_the_subscriber_workspace():
    """S03-T7: a subscription and its copies belong to the subscriber workspace's tenant, never shared or nullable;
    the source workspace (maybe another tenant) is a plain id, so it cannot decide the row's tenant."""
    sources = tenant_sources(Base.metadata)
    for name in ("context_subscription", "context_federated_entry"):
        src = sources[name]
        assert (src.parent, src.fk, src.nullable, src.shared) == ("workspace", "workspace_id", False, False), name
    assert not Base.metadata.tables["context_subscription"].c.source_workspace_id.foreign_keys


def test_policy_reads_platform_rows_only_where_shared():
    shared = _policy_sql("knowledge_pack", TenantSource("workspace", "workspace_id", True, shared=True), "analystos_app")
    private = _policy_sql("audit_event", TenantSource("workspace", "workspace_id", True), "analystos_app")
    strict = _policy_sql("analysis_run", TenantSource("workspace", "workspace_id", False), "analystos_app")
    assert "tenant_id IS NULL OR" in shared
    assert "IS NULL" not in private and "IS NULL" not in strict
    for sql in (shared, private, strict):  # a tenant never creates a platform row
        assert sql.endswith("WITH CHECK (tenant_id = analystos_current_tenant())")


def test_fill_function_prefers_parent_then_bound_tenant():
    child = _fill_function_sql(TenantSource("analysis_run", "run_id", False))
    root = _fill_function_sql(TenantSource(None, None, False, default_tenant=True))
    assert child.index('FROM "analysis_run"') < child.index("analystos_current_tenant()")
    assert "SECURITY DEFINER" in child
    assert DEFAULT_TENANT_ID in root and DEFAULT_TENANT_ID not in child


@pytest.mark.parametrize("bad", ["x; DROP TABLE t", "Robert'); --", "a-b", ""])
def test_identifiers_are_validated(bad):
    with pytest.raises(ValueError):
        _q(bad)


def test_scopes_nest_and_restore():
    assert current_tenant() is None and not is_system()
    with tenant_scope("tnt_a"):
        assert current_tenant() == "tnt_a"
        with system_scope():
            assert is_system() and current_tenant() is None
        assert current_tenant() == "tnt_a" and not is_system()
    assert current_tenant() is None


def test_session_hook_restricts_only_bound_transactions(monkeypatch):
    from analystos.tenancy import session as hook

    calls: list[tuple[str, object]] = []

    class Conn:
        class dialect:  # noqa: N801
            name = "postgresql"

        def exec_driver_sql(self, sql):
            calls.append(("sql", sql))

        def execute(self, stmt, params=None):
            calls.append(("set", params))

    class S:
        info: dict = {}

    def settings(mode):
        return type("Cfg", (), {"tenancy_mode": mode, "tenancy_app_role": "analystos_app"})()

    monkeypatch.setattr(hook, "_settings", lambda: settings("bound"))
    hook.on_after_begin(S(), None, Conn())
    assert calls == []  # unbound in `bound` mode: owner identity, as before
    with tenant_scope("tnt_a"):
        hook.on_after_begin(S(), None, Conn())
    assert calls == [("set", {"r": "analystos_app", "t": "tnt_a"})]  # role and tenant in one statement

    calls.clear()
    monkeypatch.setattr(hook, "_settings", lambda: settings("strict"))
    hook.on_after_begin(S(), None, Conn())
    assert calls == [("set", {"r": "analystos_app", "t": ""})]  # fail closed
    calls.clear()
    with system_scope():
        hook.on_after_begin(S(), None, Conn())
    assert calls == []


def test_context_webhook_tables_are_owned_by_their_workspace():
    """P1-6: a push subscription and its delivery log belong to the workspace's tenant, never shared or nullable."""
    sources = tenant_sources(Base.metadata)
    for name in ("context_webhook", "context_webhook_delivery"):
        src = sources[name]
        assert (src.parent, src.fk, src.nullable, src.shared) == ("workspace", "workspace_id", False, False), name

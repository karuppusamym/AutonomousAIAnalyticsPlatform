"""Install tenant isolation in a Postgres control database. Idempotent: safe to run on every migrate.

What it installs (ADR-009):
* the restricted role (``analystos_app`` by default) the application switches to per transaction;
* a ``tenant_id`` column on every tenant-owned table, backfilled, NOT NULL where the source is;
* BEFORE INSERT triggers that fill ``tenant_id`` from the parent row or the bound tenant;
* row-level security policies for the restricted role (the owner identity stays unrestricted and is
  used only inside ``system_scope``);
* an append-only ``audit_event`` (no UPDATE/DELETE for anyone without the maintenance switch).

Run by migration ``0057_tenancy`` (with a frozen table list) and by the test fixture after
``create_all`` (with the live metadata).
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from analystos.tenancy.context import DEFAULT_TENANT_ID
from analystos.tenancy.schema import PLATFORM_PRIVATE_TABLES, TenantSource

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
CURRENT = "analystos_current_tenant()"


def _q(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def _fill_function(src: TenantSource) -> str:
    if src.parent:
        return f"tenancy_fill__{src.parent}__{src.fk}"
    return "tenancy_fill__root_default" if src.default_tenant else "tenancy_fill__bound"


def _fill_function_sql(src: TenantSource) -> str:
    lookup = ""
    if src.parent:
        lookup = (f"  IF NEW.tenant_id IS NULL AND NEW.{_q(src.fk)} IS NOT NULL THEN\n"
                  f"    SELECT p.tenant_id INTO NEW.tenant_id FROM {_q(src.parent)} p WHERE p.id = NEW.{_q(src.fk)};\n"
                  f"  END IF;\n")
    default = f"  IF NEW.tenant_id IS NULL THEN NEW.tenant_id := '{DEFAULT_TENANT_ID}'; END IF;\n" if src.default_tenant else ""
    # SECURITY DEFINER: the parent lookup sees the parent's true tenant even when RLS hides it from the
    # caller; a mismatch with the bound tenant is then refused by the policy's WITH CHECK.
    return (f"CREATE OR REPLACE FUNCTION {_fill_function(src)}() RETURNS trigger LANGUAGE plpgsql "
            f"SECURITY DEFINER SET search_path = public AS $fn$\nBEGIN\n{lookup}"
            f"  IF NEW.tenant_id IS NULL THEN NEW.tenant_id := {CURRENT}; END IF;\n{default}"
            f"  RETURN NEW;\nEND $fn$")


def _policy_sql(table: str, src: TenantSource | None, role: str) -> str:
    if table == "tenant":
        expr = check = f"id = {CURRENT}"
    elif table == "app_user":
        expr = (f"tenant_id = {CURRENT} OR EXISTS (SELECT 1 FROM tenant_member m "
                f"WHERE m.user_id = app_user.id AND m.tenant_id = {CURRENT})")
        check = f"tenant_id = {CURRENT}"
    else:
        assert src is not None
        shared = src.nullable and table not in PLATFORM_PRIVATE_TABLES
        expr = f"tenant_id IS NULL OR tenant_id = {CURRENT}" if shared else f"tenant_id = {CURRENT}"
        check = f"tenant_id = {CURRENT}"  # a tenant may read platform rows but never create one
    return (f"CREATE POLICY tenant_isolation ON {_q(table)} TO {_q(role)} "
            f"USING ({expr}) WITH CHECK ({check})")


def ensure_role(conn: Connection, role: str) -> None:
    conn.execute(text(
        f"DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {_q(role)} NOLOGIN NOBYPASSRLS; END IF; END $do$"))
    # The login identity switches into the role with SET LOCAL ROLE, so it must be a member.
    conn.execute(text(
        f"DO $do$ BEGIN IF NOT pg_has_role(current_user, '{role}', 'MEMBER') THEN "
        f"EXECUTE format('GRANT %I TO %I', '{role}', current_user); END IF; END $do$"))


def install(conn: Connection, sources: Mapping[str, TenantSource], role: str = "analystos_app",
            backfill: bool = True) -> None:
    """Install or refresh tenant isolation for ``sources`` (table -> TenantSource) on ``conn``."""
    if conn.dialect.name != "postgresql":
        return
    _q(role)
    ensure_role(conn, role)
    conn.execute(text(f"CREATE OR REPLACE FUNCTION {CURRENT} RETURNS varchar LANGUAGE sql STABLE AS "
                      "$fn$ SELECT NULLIF(current_setting('app.tenant_id', true), '') $fn$"))
    conn.execute(text("INSERT INTO tenant (id, slug, name, status) VALUES (:id, 'default', 'Default tenant', 'active') "
                      "ON CONFLICT (id) DO NOTHING"), {"id": DEFAULT_TENANT_ID})

    for table, src in sources.items():
        t = _q(table)
        conn.execute(text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS tenant_id varchar(40)"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {_q(f'ix_{table}_tenant_id')} ON {t} (tenant_id)"))
        if backfill:
            if src.parent:
                conn.execute(text(f"UPDATE {t} c SET tenant_id = p.tenant_id FROM {_q(src.parent)} p "
                                  f"WHERE c.tenant_id IS NULL AND p.id = c.{_q(src.fk)}"))
            elif src.default_tenant:
                conn.execute(text(f"UPDATE {t} SET tenant_id = :d WHERE tenant_id IS NULL"), {"d": DEFAULT_TENANT_ID})
        if not src.nullable:
            conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN tenant_id SET NOT NULL"))
        conn.execute(text(_fill_function_sql(src)))
        conn.execute(text(f"DROP TRIGGER IF EXISTS tenancy_fill ON {t}"))
        conn.execute(text(f"CREATE TRIGGER tenancy_fill BEFORE INSERT ON {t} FOR EACH ROW "
                          f"EXECUTE FUNCTION {_fill_function(src)}()"))

    for table in [*sources, "tenant"]:
        t = _q(table)
        conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
        conn.execute(text(_policy_sql(table, sources.get(table), role)))

    r = _q(role)
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {r}"))
    conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}"))
    conn.execute(text(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {r}"))
    conn.execute(text(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {r}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {r}"))
    conn.execute(text(f"REVOKE INSERT, UPDATE, DELETE ON tenant FROM {r}"))
    install_audit_immutability(conn, role)


def install_audit_immutability(conn: Connection, role: str) -> None:
    """audit_event is append-only. Retention jobs set ``app.audit_maintenance = 'on'`` in their own
    transaction (owner identity) and are themselves audited."""
    conn.execute(text(
        "CREATE OR REPLACE FUNCTION audit_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $fn$\n"
        "BEGIN\n"
        "  IF current_setting('app.audit_maintenance', true) = 'on' THEN RETURN COALESCE(NEW, OLD); END IF;\n"
        "  RAISE EXCEPTION 'audit_event is append-only (% refused)', TG_OP USING ERRCODE = 'insufficient_privilege';\n"
        "END $fn$"))
    conn.execute(text("DROP TRIGGER IF EXISTS audit_event_immutable ON audit_event"))
    conn.execute(text("CREATE TRIGGER audit_event_immutable BEFORE UPDATE OR DELETE ON audit_event "
                      "FOR EACH ROW EXECUTE FUNCTION audit_event_immutable()"))
    conn.execute(text(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM {_q(role)}"))


def rls_status(conn: Connection, tables: list[str]) -> dict[str, dict[str, bool]]:
    """Per table: row security on, isolation policy present, fill trigger present (for checks and tests)."""
    rows = conn.execute(text(
        "SELECT c.relname, c.relrowsecurity, "
        "EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation'), "
        "EXISTS (SELECT 1 FROM pg_trigger g WHERE g.tgrelid = c.oid AND g.tgname = 'tenancy_fill') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = ANY(:t)"), {"t": tables})
    return {r[0]: {"rls": bool(r[1]), "policy": bool(r[2]), "trigger": bool(r[3])} for r in rows}

"""Bind each control-plane transaction to its tenant.

Modes (``ANALYSTOS_TENANCY_MODE``):
* ``off``    — no role switch (single-tenant installs that have not run the tenancy migration);
* ``bound``  — a transaction with a bound tenant runs as the restricted role; an unbound one (workers,
               scheduler, CLI) runs as the owner, as before. The default while worker propagation
               (S01-T4) is completed;
* ``strict`` — every transaction outside ``system_scope`` runs as the restricted role; an unbound one
               sees no tenant rows and cannot insert any (fail closed).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from analystos.tenancy.context import current_tenant, is_system

_SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


def _settings() -> Any:
    from analystos.core.config import get_settings

    return get_settings()


def _restrict(connection: Any, tenant: str | None, role: str) -> None:
    connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
    connection.execute(_SET_TENANT, {"t": tenant or ""})


def on_after_begin(session: Session, transaction: Any, connection: Any) -> None:
    """``after_begin`` listener on the control-plane sessionmaker (db/base.py)."""
    settings = _settings()
    mode = settings.tenancy_mode
    if mode == "off" or connection.dialect.name != "postgresql":
        return
    if session.info.get("system") or is_system():
        return
    tenant = session.info.get("tenant_id") or current_tenant()
    if tenant is None and mode == "bound":
        return
    _restrict(connection, tenant, settings.tenancy_app_role)


def bind_session(session: Session, tenant_id: str) -> None:
    """Bind an already-open session (its current and later transactions) to ``tenant_id``."""
    session.info["tenant_id"] = tenant_id
    session.info.pop("system", None)
    settings = _settings()
    if settings.tenancy_mode == "off" or not session.in_transaction():
        return
    conn = session.connection()
    if conn.dialect.name == "postgresql":
        _restrict(conn, tenant_id, settings.tenancy_app_role)

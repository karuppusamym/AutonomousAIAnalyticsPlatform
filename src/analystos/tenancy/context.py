"""Which tenant the current unit of work acts for.

Context variables, not thread-locals: FastAPI and anyio copy the context into worker threads, so a
tenant bound by the request middleware reaches every session the request opens, including sync
dependencies and streams.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_TENANT_ID = "tnt_default"

_tenant: ContextVar[str | None] = ContextVar("analystos_tenant", default=None)
_system: ContextVar[bool] = ContextVar("analystos_system", default=False)


def current_tenant() -> str | None:
    return _tenant.get()


def is_system() -> bool:
    return _system.get()


def set_current_tenant(tenant_id: str | None) -> None:
    """Bind for the rest of this context (a request task, an MCP handler). Prefer ``tenant_scope``."""
    _tenant.set(tenant_id)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    """Sessions opened inside act for ``tenant_id`` only (RLS-restricted role)."""
    if not tenant_id:
        raise ValueError("tenant_scope needs a tenant id")
    t, s = _tenant.set(tenant_id), _system.set(False)
    try:
        yield
    finally:
        _system.reset(s)
        _tenant.reset(t)


@contextmanager
def system_scope() -> Iterator[None]:
    """Deliberate cross-tenant work (scheduler claims, tenant resolution, migrations, seeding).
    Sessions opened inside use the owner identity, which row-level security does not restrict."""
    t, s = _tenant.set(None), _system.set(True)
    try:
        yield
    finally:
        _system.reset(s)
        _tenant.reset(t)

"""Background work runs as the tenant that owns it (spec S01-T4).

Two kinds of background code exist, and each says which it is:

* **Platform loops** pick work across tenants: the scheduler's claim, the dispatch relay, sweeps,
  housekeeping. They run inside ``system_scope()`` and only select items and update bookkeeping.
* **Tenant work** is what an item triggers: a run step, a schedule run, a crawl, a delivery. It runs as
  the tenant that owns the item: ``as_owner_of(table, id)`` or the ``owned_by`` decorator. Temporal
  activities also receive the tenant in a workflow header (``workflows/tenant_propagation.py``); the
  owner check then confirms it matches the row.

In ``strict`` mode anything that is neither sees no tenant rows (fail closed).
"""
from __future__ import annotations

import functools
import inspect
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from analystos.tenancy.context import current_tenant, is_system, system_scope, tenant_scope

F = TypeVar("F", bound=Callable[..., Any])
_MISSING = object()
_MAX_ENTRIES = 50_000  # ADR-014: bounded; a row's tenant never changes, so entries never go stale
_cache: OrderedDict[tuple[str, str], str | None] = OrderedDict()
_lock = threading.Lock()


class TenantMismatch(Exception):
    """Work for one tenant's row was asked for while another tenant is bound."""


def _enabled() -> bool:
    from analystos.core.config import get_settings

    return get_settings().tenancy_mode != "off"


def owner_tenant(table: str, row_id: str) -> str | None | object:
    """Tenant of ``table`` row ``row_id``: a tenant id, None for a platform row, or ``_MISSING``."""
    key = (table, row_id)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    from sqlalchemy import text

    from analystos.db.base import session_scope
    from analystos.tenancy.ddl import _q

    with system_scope(), session_scope() as s:
        row = s.execute(text(f"SELECT tenant_id FROM {_q(table)} WHERE id = :id"), {"id": row_id}).first()
    if row is None:
        return _MISSING  # not cached: the row may be created later
    with _lock:
        _cache[key] = row[0]
        if len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)
    return row[0]


def workspace_tenant(workspace_id: str | None) -> str | None:
    """Tenant of a workspace (None when unknown or tenancy is off)."""
    if not workspace_id or not _enabled():
        return None
    owner = owner_tenant("workspace", workspace_id)
    return None if owner is _MISSING else owner  # type: ignore[return-value]


def forget() -> None:
    with _lock:
        _cache.clear()


@contextmanager
def as_owner_of(table: str, row_id: str | None) -> Iterator[str | None]:
    """Run the body as the tenant owning ``table`` row ``row_id``.

    Inside ``system_scope`` (a platform loop) this narrows to the owner. With a tenant already bound it
    only checks that tenant owns the row (``TenantMismatch`` otherwise). A platform row (NULL tenant)
    runs in system scope unless a tenant is bound. A missing row changes nothing: the body finds
    nothing either. Yields the owner tenant (None for a platform or missing row)."""
    if not row_id or not _enabled():
        yield current_tenant()
        return
    owner = owner_tenant(table, row_id)
    bound = current_tenant()
    if owner is _MISSING:
        yield bound
        return
    if bound is not None and bound != owner:
        raise TenantMismatch(f"{table} {row_id} belongs to another tenant")
    if owner is None:
        with system_scope():
            yield None
        return
    if bound == owner and not is_system():
        yield owner
        return
    with tenant_scope(owner):  # type: ignore[arg-type]
        yield owner  # type: ignore[misc]


def owned_by(table: str, param: str) -> Callable[[F], F]:
    """Decorator: the function runs as the tenant owning ``table`` row named by argument ``param``."""

    def wrap(fn: F) -> F:
        sig = inspect.signature(fn)
        if param not in sig.parameters:  # a decorator left on the wrong function would bind nothing, silently
            raise TypeError(f"@owned_by({table!r}, {param!r}) on {fn.__qualname__}, which has no parameter {param!r}")

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            row_id = sig.bind_partial(*args, **kwargs).arguments.get(param)
            with as_owner_of(table, row_id):
                return fn(*args, **kwargs)

        inner.__owned_by__ = (table, param)  # type: ignore[attr-defined]
        return inner  # type: ignore[return-value]

    return wrap


def platform_loop(fn: F) -> F:
    """Decorator for cross-tenant platform loops: the body runs in ``system_scope``."""

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        with system_scope():
            return fn(*args, **kwargs)

    return inner  # type: ignore[return-value]


def carry_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Bind ``fn`` to the caller's tenant before handing it to a thread pool or a new thread, which do
    not copy context variables themselves."""
    import contextvars

    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)

    return inner

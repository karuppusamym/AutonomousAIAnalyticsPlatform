"""Which tenants a user may act for, and which one a request acts for."""
from __future__ import annotations

import threading
import time

from sqlalchemy import select

from analystos.core.errors import Forbidden
from analystos.tenancy.context import system_scope

_TTL_SECONDS = 30.0
_MAX_ENTRIES = 10_000
_cache: dict[str, tuple[float, str | None, frozenset[str]]] = {}
_lock = threading.Lock()


def user_tenants(user_id: str) -> tuple[str | None, frozenset[str]]:
    """(home tenant, every tenant the user may act for). Cached briefly; membership changes apply within the TTL."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(user_id)
        if hit and hit[0] > now:
            return hit[1], hit[2]
    from analystos.db.base import session_scope
    from analystos.db.models import TenantMember, User

    with system_scope(), session_scope() as s:
        home = s.scalar(select(User.tenant_id).where(User.id == user_id))
        extra = set(s.scalars(select(TenantMember.tenant_id).where(TenantMember.user_id == user_id)))
    allowed = frozenset({home, *extra} - {None})
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            _cache.clear()  # bounded (ADR-014); a refill is one indexed query per user
        _cache[user_id] = (now + _TTL_SECONDS, home, allowed)
    return home, allowed


def resolve_tenant(user_id: str, requested: str | None = None) -> str | None:
    """The tenant a request from ``user_id`` acts for: the requested one if the user belongs to it,
    else the home tenant. Asking for a tenant the user does not belong to is refused."""
    home, allowed = user_tenants(user_id)
    if requested:
        if requested not in allowed:
            raise Forbidden("not a member of the requested tenant")
        return requested
    return home


def invalidate(user_id: str | None = None) -> None:
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)

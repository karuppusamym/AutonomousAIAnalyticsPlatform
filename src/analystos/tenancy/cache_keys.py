"""The one key prefix every tenant-data cache uses (spec S01-T7): ``tenant:{t}:scope:{scope_hash}:policy:{v}``.

An entry is only ever found by a caller acting for the same tenant, with the same authorized scope, under the
same policy version. The tenant segment is the tenant bound to the current unit of work; when nothing is bound
(system scope, unit tests, ``bound``/``off`` modes) it is the owner tenant the caller passes (the workspace's),
else ``-``. Entries written with ``-`` are therefore never read by a tenant-bound caller. When the bound tenant
and the owner tenant disagree the key is ``None``: the caller must neither read nor write the cache.

Caches that hold no tenant data (pure functions of their input such as SQL parsing or text screening, platform
catalogues, and the system lookups keyed by globally unique ids in ``tenancy/``) do not use this prefix; the
inventory is in the S01 spec, "As built" T7.
"""
from __future__ import annotations

from analystos.tenancy.context import current_tenant

UNBOUND = "-"


def _segment(value: object) -> str:
    text = UNBOUND if value is None or value == "" else str(value)
    return text.replace(":", "_")  # a segment never contains the separator, so keys parse unambiguously


def cache_tenant(owner_tenant: str | None = None) -> str | None:
    """Tenant segment for a cache key, or None when the bound tenant is not the owner (no cache then)."""
    bound = current_tenant()
    if bound and owner_tenant and bound != owner_tenant:
        return None
    return bound or owner_tenant or UNBOUND


def prefix(*, tenant: str | None, scope_hash: str | None, policy_version: int | str | None) -> str:
    return f"tenant:{_segment(tenant)}:scope:{_segment(scope_hash)}:policy:{_segment(policy_version)}"


def scoped_prefix(*, owner_tenant: str | None = None, scope_hash: str | None = None,
                  policy_version: int | str | None = None) -> str | None:
    """The full prefix for the current context, or None when the cache must not be used."""
    tenant = cache_tenant(owner_tenant)
    if tenant is None:
        return None
    return prefix(tenant=tenant, scope_hash=scope_hash, policy_version=policy_version)


PREFIX_PARTS = 6  # tenant, t, scope, s, policy, p


def parse(segments: list[str]) -> dict[str, str] | None:
    """{tenant, scope, policy} from the six prefix segments of a split key, else None."""
    if len(segments) < PREFIX_PARTS or segments[0] != "tenant" or segments[2] != "scope" or segments[4] != "policy":
        return None
    return {"tenant": segments[1], "scope": segments[3], "policy": segments[5]}

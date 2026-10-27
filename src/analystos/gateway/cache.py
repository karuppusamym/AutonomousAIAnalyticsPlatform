"""Redis result cache for the query gateway.

Key = `analystos:qcache:tenant:{t}:scope:{scope hash}:policy:{policy version}:` + sha256(fingerprint, scope
hash, source version, max_rows) (S01-T7, `tenancy/cache_keys.py`): a result is only reused by the same tenant,
for the same normalized SQL, the same authorized scope (so a narrower scope never reads a wider one's cached
rows), the same policy version, the same source data version and the same row cap. A key of None (the bound
tenant is not the workspace's) is never read or written. Values are JSON. Any Redis failure behaves as a miss
and is logged as a warning; the cache never fails a query.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from analystos.core.logging import get_logger
from analystos.tenancy.cache_keys import scoped_prefix

KEY_PREFIX = "analystos:qcache:"

_log = get_logger(__name__)


def cache_key(fingerprint: str, scope_hash: str, source_version: str, max_rows: int, *,
              tenant: str | None = None, policy_version: int | None = None) -> str | None:
    """`tenant` is the owner (workspace) tenant; the bound tenant, when there is one, must equal it."""
    head = scoped_prefix(owner_tenant=tenant, scope_hash=scope_hash, policy_version=policy_version)
    if head is None:
        return None
    material = "\x1f".join([fingerprint, scope_hash, source_version, str(int(max_rows))])
    return f"{KEY_PREFIX}{head}:{hashlib.sha256(material.encode()).hexdigest()}"


class QueryCache:
    def __init__(self, settings: Any, *, client: Any | None = None, ttl_seconds: int | None = None) -> None:
        self.ttl_seconds = int(ttl_seconds if ttl_seconds is not None else getattr(settings, "query_cache_ttl_seconds", 3600))
        self._url = getattr(settings, "redis_url", None)
        self._client = client

    def _redis(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._url, socket_timeout=0.5, socket_connect_timeout=0.5, health_check_interval=30
            )
        return self._client

    @staticmethod
    def key(fingerprint: str, scope_hash: str, source_version: str, max_rows: int, *,
            tenant: str | None = None, policy_version: int | None = None) -> str | None:
        return cache_key(fingerprint, scope_hash, source_version, max_rows, tenant=tenant,
                         policy_version=policy_version)

    @property
    def enabled(self) -> bool:
        """No Redis URL (lite) = no shared query cache: every lookup is a quiet miss."""
        return self._client is not None or bool(self._url)

    def get(self, key: str | None) -> dict[str, Any] | None:
        if not self.enabled or key is None:
            return None
        try:
            raw = self._redis().get(key)
        except Exception as exc:  # noqa: BLE001 - any cache failure is a miss
            _log.warning("query cache get failed (%s); treating as miss", exc.__class__.__name__)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            _log.warning("query cache entry is not valid JSON; treating as miss")
            return None
        return value if isinstance(value, dict) else None

    def set(self, key: str | None, value: dict[str, Any]) -> bool:
        if self.ttl_seconds <= 0 or not self.enabled or key is None:
            return False
        try:
            payload = json.dumps(value, separators=(",", ":"), default=str)
            self._redis().set(key, payload, ex=self.ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            _log.warning("query cache set failed (%s); continuing without cache", exc.__class__.__name__)
            return False
        return True

    def delete(self, key: str | None) -> None:
        if not self.enabled or key is None:
            return
        try:
            self._redis().delete(key)
        except Exception as exc:  # noqa: BLE001
            _log.warning("query cache delete failed (%s)", exc.__class__.__name__)

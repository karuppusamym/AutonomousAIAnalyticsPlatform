"""Response cache for model calls whose output depends only on the (redacted) request, and the
store the context cache shares (`context/cache.py`).

Keyed by tenant, caller scope and policy version (S01-T7), then purpose + the model that answered + request
payload hash; stored in Redis with a TTL and an
in-process LRU fallback that honours the same TTL. Only purposes listed in
settings.llm.cacheable_purposes are cached."""
from __future__ import annotations

import contextlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any

from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.tenancy.cache_keys import scoped_prefix

log = get_logger(__name__)


class ResponseCache:
    def __init__(self, redis_url: str | None = None, *, max_local: int = 2000,
                 clock: Callable[[], float] = time.monotonic, client: Any = None) -> None:
        self._local: OrderedDict[str, tuple[float, str]] = OrderedDict()  # key -> (expires at, raw)
        self._counters: dict[str, dict[str, float]] = {}
        self._lock = Lock()
        self._max = max_local
        self._clock = clock
        self._redis = client
        if redis_url and client is None:
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
                self._redis.ping()
            except Exception as exc:
                log.warning("llm cache: redis unavailable, using in-process cache (%s)", exc)
                self._redis = None

    @property
    def shared(self) -> bool:
        """True when entries are visible to every worker (Redis), not only this process."""
        return self._redis is not None

    @staticmethod
    def key(purpose: str, model: str | list[str], payload: Any, workspace_id: str | None = None,
            knowledge_version: str | None = None, *, tenant: str | None = None, scope_hash: str | None = None,
            policy_version: int | None = None) -> str | None:
        """`aos:llm:tenant:{t}:scope:{s}:policy:{v}:{workspace}:{purpose}:{hash}` (S01-T7): only the same
        tenant, caller scope and policy version find an entry; None (bound tenant is not `tenant`, the
        workspace's) means no cache. Scoped per workspace so its entries can be found and purged
        (retention, deletion). Keyed on the model that answers (a budget downgrade to another tier then
        finds that tier's answers, not a key made of a different candidate list). The workspace knowledge
        version (P4-T06) is part of the hash, so a knowledge edit misses."""
        head = scoped_prefix(owner_tenant=tenant, scope_hash=scope_hash, policy_version=policy_version)
        if head is None:
            return None
        material = {'models': model if isinstance(model, list) else [model], 'payload': payload}
        if knowledge_version:
            material['knowledge'] = knowledge_version
        return f"aos:llm:{head}:{workspace_id or '-'}:{purpose}:{stable_hash(material)}"

    def get(self, key: str | None) -> dict | None:
        if key is None:
            return None
        raw = None
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
            except Exception:
                raw = None
        if raw is None:
            with self._lock:
                item = self._local.get(key)
                if item is not None and item[0] <= self._clock():
                    self._local.pop(key, None)  # expired: the local fallback honours the TTL like Redis
                    item = None
                if item is not None:
                    self._local.move_to_end(key)
                    raw = item[1]
        return json.loads(raw) if raw else None

    def set(self, key: str | None, value: Any, ttl_seconds: int | float) -> None:
        if ttl_seconds <= 0 or key is None:
            return
        raw = json.dumps(value, default=str)
        if self._redis is not None:
            with contextlib.suppress(Exception):
                self._redis.setex(key, int(max(1, ttl_seconds)), raw)
        with self._lock:
            self._local[key] = (self._clock() + ttl_seconds, raw)
            self._local.move_to_end(key)
            while len(self._local) > self._max:
                self._local.popitem(last=False)

    def clear_local(self) -> None:
        with self._lock:
            self._local.clear()
            self._counters.clear()

    # ------------------------------------------------------------------ counters (visibility only)
    def incr(self, key: str, field: str, amount: float = 1, ttl_seconds: int = 30 * 86400) -> None:
        """Add to a counter hash (Redis HINCRBYFLOAT when shared, else this process)."""
        if self._redis is not None:
            try:
                self._redis.hincrbyfloat(key, field, amount)
                self._redis.expire(key, ttl_seconds)
                return
            except Exception:
                pass
        with self._lock:
            row = self._counters.setdefault(key, {})
            row[field] = row.get(field, 0) + amount

    def counters(self, key: str) -> dict[str, float]:
        if self._redis is not None:
            try:
                raw = self._redis.hgetall(key) or {}
                return {(k.decode() if isinstance(k, bytes) else str(k)): float(v) for k, v in raw.items()}
            except Exception:
                pass
        with self._lock:
            return dict(self._counters.get(key, {}))


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent estimate (~3.6 chars/token for English + JSON)."""
    return max(1, int(len(text) / 3.6))

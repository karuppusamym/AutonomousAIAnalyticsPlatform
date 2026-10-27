"""Per-source step semaphore (S06 §3.6): independent steps run in parallel up to
`min(max_parallel, free slots of every source they query)`.

A slot is a lease `holder -> expiry` in a per-source set. Acquire is atomic (one Lua script on Redis:
drop expired leases, renew the holder's own lease, else add it when the set has room); release removes
the holder. A crashed worker's lease expires after the claim TTL, so a slot is never lost for good.

Redis when it is configured (shared by every API process and Temporal worker); otherwise, and while
Redis is unreachable, an in-process table (the lite profile runs every step in one process, ADR-0025).
Throttling protects the source, it is not an authorization: with Redis down the in-process table still
bounds this process's steps and the run carries on.
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Any

from analystos.core.logging import get_logger

log = get_logger(__name__)

# KEYS[1] = the source's lease set; ARGV = now, expiry, limit, holder, ttl seconds
_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[4]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
  return 1
end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


class _Local:
    def __init__(self) -> None:
        self._leases: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def _live(self, source: str, now: float) -> dict[str, float]:
        leases = self._leases.setdefault(source, {})
        for holder in [h for h, exp in leases.items() if exp <= now]:
            del leases[holder]
        return leases

    def acquire(self, source: str, holder: str, limit: int, ttl: float, now: float) -> bool:
        with self._lock:
            leases = self._live(source, now)
            if holder in leases or len(leases) < limit:
                leases[holder] = now + ttl
                return True
            return False

    def release(self, source: str, holder: str) -> None:
        with self._lock:
            self._leases.get(source, {}).pop(holder, None)

    def holders(self, source: str, now: float) -> int:
        with self._lock:
            return len(self._live(source, now))


class SourceSlots:
    def __init__(self, redis_url: str | None = None, prefix: str = "aos:slots:", *, client: Any = None,
                 clock: Any = time.time) -> None:
        self.prefix, self.clock = prefix, clock
        self.local = _Local()
        self._redis = client
        self._script = None
        if client is None and redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
                self._redis.ping()
            except Exception as exc:
                log.warning("source slots: Redis unavailable (%s); slots are kept per process", exc)
                self._redis = None

    def _key(self, source: str) -> str:
        return f"{self.prefix}{source}"

    def acquire(self, source: str, holder: str, limit: int, ttl: float) -> bool:
        now = self.clock()
        if self._redis is not None:
            try:
                if self._script is None:
                    self._script = self._redis.register_script(_ACQUIRE)
                return bool(int(self._script(keys=[self._key(source)], args=[now, now + ttl, int(limit), holder, int(ttl) + 60])))
            except Exception as exc:
                log.warning("source slots: Redis error (%s); using this process's slots", exc)
        return self.local.acquire(source, holder, int(limit), ttl, now)

    def release(self, source: str, holder: str) -> None:
        if self._redis is not None:
            try:
                self._redis.zrem(self._key(source), holder)
            except Exception as exc:
                log.warning("source slots: Redis error on release (%s); the lease expires on its own", exc)
        self.local.release(source, holder)

    def holders(self, source: str) -> int:
        """Leases held now (for the orchestrator's dispatch cap; acquire is what enforces)."""
        now = self.clock()
        if self._redis is not None:
            try:
                return int(self._redis.zcount(self._key(source), f"({now}", "+inf"))
            except Exception as exc:
                log.warning("source slots: Redis error on count (%s)", exc)
        return self.local.holders(source, now)


@lru_cache(maxsize=1)
def default_slots() -> SourceSlots:
    from analystos.core.config import get_settings

    s = get_settings()
    return SourceSlots(s.redis_url or None, s.budget_counter_prefix.replace(":budget:", ":slots:") or "aos:slots:")


# --------------------------------------------------------------------------- which steps hold slots
QUERY_COST_CLASSES = ("query", "compute")


def run_sources(run: Any) -> list[str]:
    return sorted(set(((run.scope or {}).get("asset_sources") or {}).values()))


def source_limits(run: Any) -> dict[str, int]:
    """The per-source slot limits a goal run was created with (`limits.source_slots`); none = unbounded."""
    return {k: int(v) for k, v in ((run.limits or {}).get("source_slots") or {}).items() if v}


def queries_sources(run: Any, key: str) -> bool:
    """A step holds source slots when its agent's cost class is query or compute (it runs statements)."""
    from analystos.capabilities.binding import resolve, run_playbook

    playbook = run_playbook(run)
    step = playbook.step(key)
    use = step.use if step is not None else getattr(playbook.expansion(key), "use", None)
    if not use:
        return False
    try:
        return resolve(run, use).cost_class in QUERY_COST_CLASSES
    except Exception:  # an unresolvable agent fails in dispatch with its own error; it holds no slot here
        return False


def holder(run_id: str, key: str) -> str:
    return f"{run_id}:{key}"

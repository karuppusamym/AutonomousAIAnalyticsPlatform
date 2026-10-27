from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any


def new_id(prefix: str) -> str:
    """Readable, sortable-enough ids: ws_1f3a9c0b2d4e."""
    return f"{prefix}_{secrets.token_hex(6)}"


def utcnow() -> datetime:
    return datetime.now(UTC)


_last_stamp: datetime | None = None
_stamp_lock = threading.Lock()


def ordered_utcnow() -> datetime:
    """A per-row write stamp that strictly increases within this process. The wall clock can repeat a value
    for several milliseconds (Windows), and rows written back to back must still read back in write order."""
    global _last_stamp
    with _stamp_lock:
        now = datetime.now(UTC)
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + timedelta(microseconds=1)
        _last_stamp = now
        return now


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    """SHA-256 over canonical JSON. Used for plan hashes, approval payload binding and query fingerprints."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()

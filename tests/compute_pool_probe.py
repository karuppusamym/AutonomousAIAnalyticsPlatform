"""Tasks for tests/unit/test_compute_pool.py. Module level so the pool's spawned children can import them."""
from __future__ import annotations

import os
import time


def pid(_: int = 0) -> int:
    time.sleep(0.02)
    return os.getpid()


def sleep(seconds: float) -> int:
    time.sleep(seconds)
    return os.getpid()


def die(_: int = 0) -> int:
    """A child that ends abruptly (a native crash or an OOM kill looks the same to the pool)."""
    os._exit(3)

"""Dedicated run-driver processes with Postgres run leases (ORC-T7, ADR-015 phase 2).

With `ANALYSTOS_RUN_DRIVER=process`, no API process drives `local` or `langgraph` runs. The API records that a run
wants driving (`wake`: start, approval, control, feedback) and any number of `analystos driver` processes share
the work. Each one:

1. **claims** due runs with `SELECT ... FOR UPDATE SKIP LOCKED` on `run_lease` (a run is due when it was woken,
   has never parked, was parked longer than the sweep interval, or its lease expired), bumping the lease `epoch`;
2. **drives** each one with the configured backend's runtime (`LocalRuntime` or the LangGraph driver), exactly as
   the API would, until it is terminal or parks;
3. **renews** every held lease by heartbeat, and **releases** it when the run parks or ends.

Two drivers never advance the same run: a claim takes only a free or expired lease, renewals and releases are
fenced by `epoch`, and a drive checks a local deadline before every engine call that decides. The deadline is the
last successful renewal plus 80 % of the lease, so a driver that stops heartbeating (frozen, partitioned) stops
deciding before its lease can expire in the database and be taken over. A task already executing when that
happens is fenced by the engine itself: the new holder releases the run's RUNNING claims, so a late result of
the old attempt is `superseded` (engine.release_orphaned_claims, claim_version).

All run state stays in Postgres; the lease row only says who may drive. Schedules and monitors are unchanged."""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from analystos.contracts.events import RUN_TERMINAL
from analystos.core.config import get_settings
from analystos.core.logging import get_logger
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
LEASE_MARGIN = 0.2  # a driver stops deciding this fraction of the lease before it would expire
NEVER = 10 * 365 * 86400.0  # "no sweep": a parked run is only re-driven when woken
_TERMINAL = sorted(RUN_TERMINAL)


def _session():
    from analystos.db.base import session_scope

    return session_scope()


# ------------------------------------------------------------------------------------------ the API side
@owned_by("analysis_run", "run_id")
def wake(run_id: str) -> None:
    """Ask a driver to drive the run (start, approval, control, feedback). Idempotent; a wake that lands while a
    driver holds the run keeps it due, so the driver that parks it takes it up again."""
    with _session() as s:
        s.execute(text("INSERT INTO run_lease (run_id, wake_at, updated_at) VALUES (:id, now(), now()) "
                       "ON CONFLICT (run_id) DO UPDATE SET wake_at = now(), updated_at = now()"), {"id": run_id})


@platform_loop
def live_drivers(max_age_seconds: float | None = None) -> list[dict[str, Any]]:
    """Driver processes that heartbeated within a lease (for /api/health)."""
    age = max_age_seconds if max_age_seconds is not None else get_settings().driver_lease_seconds
    with _session() as s:
        rows = s.execute(text("SELECT id, backend, host, pid, runs, heartbeat_at FROM run_driver "
                              "WHERE heartbeat_at > now() - make_interval(secs => :age) ORDER BY id"), {"age": age})
        return [{"id": r.id, "backend": r.backend, "host": r.host, "pid": r.pid, "runs": r.runs,
                 "heartbeat_at": r.heartbeat_at.isoformat()} for r in rows]


# ------------------------------------------------------------------------------------------ the lease store
@dataclass(frozen=True)
class Claim:
    run_id: str
    epoch: int
    took_over: bool  # the lease was held by a driver that stopped renewing it


class LeaseStore:
    """The lease statements (Postgres). Every method is a platform loop: it selects runs across tenants and
    touches only lease bookkeeping; the drive itself runs as each run's tenant."""

    @platform_loop
    def backfill(self) -> int:
        """Lease rows for non-terminal runs that have none (runs started before driver mode, or by a release
        that predates it), so every run a driver should see is due."""
        with _session() as s:
            return s.execute(text("INSERT INTO run_lease (run_id, wake_at, updated_at) "
                                  "SELECT id, now(), now() FROM analysis_run WHERE NOT (status = ANY(:t)) "
                                  "ON CONFLICT (run_id) DO NOTHING"), {"t": _TERMINAL}).rowcount or 0

    @platform_loop
    def claim(self, driver_id: str, limit: int, lease_seconds: float, sweep_seconds: float) -> list[Claim]:
        if limit <= 0:
            return []
        sweep = sweep_seconds if sweep_seconds > 0 else NEVER
        with _session() as s:
            rows = s.execute(text(
                "WITH due AS ("
                "  SELECT l.run_id, l.holder AS old_holder FROM run_lease l JOIN analysis_run r ON r.id = l.run_id"
                "   WHERE NOT (r.status = ANY(:t))"
                "     AND (l.holder IS NULL OR l.expires_at < now())"
                "     AND (l.holder IS NOT NULL OR l.wake_at IS NOT NULL OR l.parked_at IS NULL"
                "          OR l.parked_at < now() - make_interval(secs => :sweep))"
                "   ORDER BY l.wake_at NULLS LAST, l.run_id LIMIT :n FOR UPDATE OF l SKIP LOCKED) "
                "UPDATE run_lease l SET holder = :me, epoch = l.epoch + 1, wake_at = NULL, updated_at = now(),"
                "       expires_at = now() + make_interval(secs => :lease) "
                "  FROM due WHERE l.run_id = due.run_id RETURNING l.run_id, l.epoch, due.old_holder"),
                {"t": _TERMINAL, "sweep": sweep, "n": limit, "me": driver_id, "lease": lease_seconds}).all()
        return [Claim(r.run_id, r.epoch, r.old_holder is not None and r.old_holder != driver_id) for r in rows]

    @platform_loop
    def renew(self, driver_id: str, held: dict[str, int], lease_seconds: float) -> set[str]:
        """Extend every lease this driver still holds at the epoch it claimed; returns the runs still held."""
        if not held:
            return set()
        with _session() as s:
            rows = s.execute(text("UPDATE run_lease SET expires_at = now() + make_interval(secs => :lease), "
                                  "updated_at = now() WHERE holder = :me AND run_id = ANY(:ids) RETURNING run_id, epoch"),
                             {"lease": lease_seconds, "me": driver_id, "ids": list(held)}).all()
        return {r.run_id for r in rows if held.get(r.run_id) == r.epoch}

    @platform_loop
    def release(self, driver_id: str, run_id: str, epoch: int, *, parked: bool, terminal: bool) -> bool:
        with _session() as s:
            if terminal:
                n = s.execute(text("DELETE FROM run_lease WHERE run_id = :id AND holder = :me AND epoch = :e"),
                              {"id": run_id, "me": driver_id, "e": epoch}).rowcount
            else:
                n = s.execute(text("UPDATE run_lease SET holder = NULL, expires_at = NULL, updated_at = now(), "
                                   "parked_at = CASE WHEN :parked THEN now() ELSE parked_at END "
                                   "WHERE run_id = :id AND holder = :me AND epoch = :e"),
                              {"id": run_id, "me": driver_id, "e": epoch, "parked": parked}).rowcount
        return bool(n)

    @platform_loop
    def heartbeat(self, driver_id: str, backend: str, runs: int) -> None:
        with _session() as s:
            s.execute(text("INSERT INTO run_driver (id, backend, host, pid, runs, started_at, heartbeat_at) "
                           "VALUES (:id, :b, :h, :p, :n, now(), now()) "
                           "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now(), runs = excluded.runs"),
                      {"id": driver_id, "b": backend, "h": socket.gethostname()[:200], "p": os.getpid(), "n": runs})
            s.execute(text("DELETE FROM run_driver WHERE heartbeat_at < now() - interval '1 day'"))

    @platform_loop
    def retire(self, driver_id: str) -> None:
        """Clean shutdown: hand back every lease (runs are due again at once) and drop the heartbeat row."""
        with _session() as s:
            s.execute(text("UPDATE run_lease SET holder = NULL, expires_at = NULL, wake_at = now(), updated_at = now() "
                           "WHERE holder = :me"), {"me": driver_id})
            s.execute(text("DELETE FROM run_driver WHERE id = :me"), {"me": driver_id})


# ------------------------------------------------------------------------------------------ the driver process
class _Held:
    def __init__(self, epoch: int, valid_until: float) -> None:
        self.epoch, self.valid_until, self.lost = epoch, valid_until, False


class RunDriver:
    """One `analystos driver` process: claims due runs, drives each with the backend runtime, renews and
    releases their leases. `drive` defaults to the configured runtime's `drive(run_id, park=True, keep_going=...)`."""

    def __init__(self, runtime: Any = None, store: LeaseStore | None = None, *, driver_id: str | None = None,
                 lease_seconds: float | None = None, max_runs: int | None = None, poll: float | None = None,
                 sweep_seconds: float | None = None, backend: str | None = None,
                 drive: Callable[..., str] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        settings = get_settings()
        self.driver_id = driver_id or f"{socket.gethostname()[:60]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_seconds = float(lease_seconds or settings.driver_lease_seconds)
        self.max_runs = int(max_runs or settings.driver_max_runs)
        self.poll = float(poll or settings.driver_poll_seconds)
        self.sweep_seconds = settings.local_sweep_seconds if sweep_seconds is None else sweep_seconds
        self.backend = backend or settings.orchestrator
        self.store = store or LeaseStore()
        self.clock = clock
        if drive is None:
            if runtime is None:
                from analystos.workflows.orchestrator import new_runtime

                runtime = new_runtime(settings)
            drive = runtime.drive
        self.runtime, self._drive_fn = runtime, drive
        self._lock = threading.Lock()
        self._held: dict[str, _Held] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._last_backfill = -NEVER
        self.claimed: deque[Claim] = deque(maxlen=1000)  # the latest claims this process made (bounded, ADR-014)

    # ---------------------------------------------------------------- leases
    def _valid_until(self, started: float) -> float:
        return started + self.lease_seconds * (1 - LEASE_MARGIN)

    def holds(self, run_id: str) -> bool:
        with self._lock:
            held = self._held.get(run_id)
            return held is not None and not held.lost and self.clock() < held.valid_until

    def renew(self) -> None:
        with self._lock:
            held = {r: h.epoch for r, h in self._held.items() if not h.lost}
        if not held:
            return
        started = self.clock()
        try:
            kept = self.store.renew(self.driver_id, held, self.lease_seconds)
        except Exception as exc:  # the database may be briefly unavailable: the local deadline still runs out
            log.warning("driver %s: lease renewal failed: %s", self.driver_id, exc)
            return
        with self._lock:
            for run_id, h in self._held.items():
                if run_id in kept:
                    h.valid_until = self._valid_until(started)
                elif run_id in held:
                    h.lost = True
                    log.warning("driver %s lost the lease of %s; it stops driving it", self.driver_id, run_id)

    # ---------------------------------------------------------------- claiming and driving
    def tick(self) -> list[Claim]:
        """One round: heartbeat, renew, claim up to the free capacity and start a drive per claim."""
        self._reap()
        try:
            self.store.heartbeat(self.driver_id, self.backend, len(self._threads))
        except Exception as exc:
            log.warning("driver %s: heartbeat failed: %s", self.driver_id, exc)
        self.renew()
        now = self.clock()
        if now - self._last_backfill >= max(self.lease_seconds, 5.0):
            try:
                self.store.backfill()
                self._last_backfill = now
            except Exception as exc:
                log.warning("driver %s: lease backfill failed: %s", self.driver_id, exc)
        with self._lock:
            free = self.max_runs - len(self._threads)
        if free <= 0 or self._stop.is_set():
            return []
        started = self.clock()
        claims = self.store.claim(self.driver_id, free, self.lease_seconds, self.sweep_seconds)
        for c in claims:
            with self._lock:
                self._held[c.run_id] = _Held(c.epoch, self._valid_until(started))
                thread = threading.Thread(target=self._drive, args=(c.run_id, c.epoch, c.took_over), daemon=True,
                                          name=f"lease-{c.run_id}")
                self._threads[c.run_id] = thread
            self.claimed.append(c)
            thread.start()
        return claims

    def _reap(self) -> None:
        with self._lock:
            for run_id in [r for r, t in self._threads.items() if not t.is_alive()]:
                del self._threads[run_id]

    @owned_by("analysis_run", "run_id")
    def _drive(self, run_id: str, epoch: int, took_over: bool) -> None:
        from analystos.runtime import engine
        from analystos.workflows.orchestrator import LEASE_LOST, PARKED

        outcome, parked, terminal = None, True, False
        try:
            if took_over:  # the previous holder stopped renewing: its RUNNING claims are orphans
                engine.release_orphaned_claims([run_id])
                log.warning("driver %s took over run %s (epoch %s)", self.driver_id, run_id, epoch)
            outcome = self._drive_fn(run_id, park=True, keep_going=lambda: self.holds(run_id) and not self._stop.is_set())
            terminal = outcome in RUN_TERMINAL
            parked = outcome in PARKED
        except Exception:
            log.exception("driver %s: drive of %s crashed; the sweep re-drives it", self.driver_id, run_id)
        finally:
            with self._lock:
                self._held.pop(run_id, None)
            if outcome != LEASE_LOST:
                try:
                    self.store.release(self.driver_id, run_id, epoch, parked=parked, terminal=terminal)
                except Exception as exc:  # the lease expires on its own; another driver takes the run over
                    log.warning("driver %s: release of %s failed: %s", self.driver_id, run_id, exc)

    def driving(self) -> list[str]:
        with self._lock:
            return [r for r, t in self._threads.items() if t.is_alive()]

    # ---------------------------------------------------------------- the process loop
    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or self._stop
        renewer = threading.Thread(target=self._renew_loop, args=(stop,), daemon=True, name="lease-renewer")
        renewer.start()
        log.info("run driver %s started (%s backend, %d runs, lease %.0f s)", self.driver_id, self.backend,
                 self.max_runs, self.lease_seconds)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # the database may be briefly unavailable
                log.warning("driver %s tick failed: %s", self.driver_id, exc)
            stop.wait(self.poll)
        self.shutdown()

    def _renew_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.lease_seconds / 3):
            self.renew()

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop deciding, let drives return (each stops at its next check), then hand every lease back."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in list(self._threads.values()):
            thread.join(max(0.0, deadline - time.monotonic()))
        try:
            self.store.retire(self.driver_id)
        except Exception as exc:
            log.warning("driver %s: retire failed (leases expire on their own): %s", self.driver_id, exc)
        if self.runtime is not None and hasattr(self.runtime, "shutdown"):
            self.runtime.shutdown()


def serve(max_runs: int | None = None) -> None:
    """`analystos driver`: drive local/langgraph runs under Postgres run leases until interrupted."""
    from analystos.workflows.backends import capabilities, validate

    settings = get_settings()
    caps = capabilities(settings)
    if not caps.parks:
        raise SystemExit(f"`analystos driver` drives local and langgraph runs; ANALYSTOS_ORCHESTRATOR={caps.name} "
                         "is driven by `analystos worker`")
    validate(settings)
    if settings.run_driver != "process":
        log.warning("ANALYSTOS_RUN_DRIVER is %r: API processes also drive runs. Set it to `process` on the API "
                    "and every driver so only drivers do", settings.run_driver)
    driver = RunDriver(max_runs=max_runs)
    stop = threading.Event()
    import contextlib
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):  # not the main thread, or a signal this platform lacks
            signal.signal(sig, lambda *_: stop.set())
    driver.run_forever(stop)

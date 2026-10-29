"""Shared harness for the P4-S05 load tests (spec v3 §8 targets).

Every load run gets its own control-plane database, its own analytics database and its own
loader/reader/builder logins and role prefix (the same isolation the integration suite uses), its
own Temporal task-queue prefix and its own Redis budget prefix, so a load run never touches the dev
stack or a parallel session. Nothing here calls a model: OPENROUTER_API_KEY is removed and every
LLM purpose runs on its deterministic path.

Import `configure(suffix)` before anything from `analystos`: settings are read from the environment.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "60-delivery" / "evidence"
PG_HOSTPORT = os.environ.get("ANALYSTOS_LOAD_PG", "localhost:5432")  # direct: admin, provisioning, the builder
# Where the platform's control/loader/reader URLs point: a transaction-mode PgBouncer (compose
# `--profile pooled`, localhost:6432) or Postgres itself. The builder stays direct (db/pools.py).
APP_HOSTPORT = os.environ.get("ANALYSTOS_LOAD_PG_APP", PG_HOSTPORT)
PG_USER = os.environ.get("ANALYSTOS_LOAD_PG_USER", "analystos:analystos")


@dataclass
class Plane:
    suffix: str
    control_db: str
    analytics_db: str
    role_prefix: str
    queue_prefix: str
    env: dict[str, str] = field(default_factory=dict)

    @property
    def control_url(self) -> str:
        return f"postgresql+psycopg://{PG_USER}@{APP_HOSTPORT}/{self.control_db}"


def configure(suffix: str) -> Plane:
    """Point this process (and the child processes started with `plane.env`) at an isolated plane."""
    if not suffix.replace("_", "").isalnum():
        raise SystemExit("suffix must be alphanumeric")
    prefix = f"aosload_{suffix}_"
    plane = Plane(suffix=suffix, control_db=f"analystos_load_{suffix}", analytics_db=f"analystos_load_{suffix}_analytics",
                  role_prefix=prefix, queue_prefix=f"aosload-{suffix}")
    env = {
        "ANALYSTOS_DATABASE_URL": plane.control_url,
        "ANALYSTOS_ANALYTICS_LOADER_URL": f"postgresql+psycopg://{prefix}loader:loader@{APP_HOSTPORT}/{plane.analytics_db}",
        "ANALYSTOS_ANALYTICS_READER_URL": f"postgresql+psycopg://{prefix}reader:reader@{APP_HOSTPORT}/{plane.analytics_db}",
        "ANALYSTOS_ANALYTICS_BUILDER_URL": f"postgresql+psycopg://{prefix}builder:builder@{PG_HOSTPORT}/{plane.analytics_db}",
        "ANALYSTOS_ANALYTICS_WORKSPACE_ROLE_PREFIX": f"{prefix}r_",
        "ANALYSTOS_ANALYTICS_BUILD_ROLE_PREFIX": f"{prefix}b_",
        "ANALYSTOS_BUDGET_COUNTER_PREFIX": f"aosload:{suffix}:budget:",
        "ANALYSTOS_TEMPORAL_QUEUE_PREFIX": plane.queue_prefix,
        "ANALYSTOS_JWT_SECRET": "load-test-secret-load-test-secret-123456",
        "ANALYSTOS_ENV": os.environ.get("ANALYSTOS_ENV", "dev"),
        "OPENROUTER_API_KEY": "",
        "ANALYSTOS_SOURCE_SECRET_SERVICENOW": "admin",
        "PYTHONPATH": str(ROOT / "src"),
    }
    os.environ.update(env)
    plane.env = {**os.environ}
    return plane


def _admin():
    from sqlalchemy import create_engine

    return create_engine(f"postgresql+psycopg://{PG_USER}@{PG_HOSTPORT}/postgres", isolation_level="AUTOCOMMIT")


def teardown(plane: Plane) -> None:
    from sqlalchemy import text

    admin = _admin()
    with admin.connect() as c:
        for db in (plane.control_db, plane.analytics_db):
            c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        like = plane.role_prefix.replace("_", "\\_") + "%"
        roles = [r[0] for r in c.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE :p"), {"p": like})]
        logins = {f"{plane.role_prefix}{n}" for n in ("loader", "reader", "builder")}
        for role in sorted(roles, key=lambda r: (r in logins, r)):
            c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
    admin.dispose()


def provision(plane: Plane) -> None:
    """Fresh control DB (schema + seed) and analytics plane, mirroring tests/conftest.py."""
    from sqlalchemy import create_engine, text

    teardown(plane)
    p = plane.role_prefix
    admin = _admin()
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{plane.control_db}"'))
        c.execute(text(f"CREATE ROLE {p}loader LOGIN CREATEROLE PASSWORD 'loader'"))
        c.execute(text(f"CREATE ROLE {p}reader LOGIN NOINHERIT PASSWORD 'reader'"))
        c.execute(text(f"CREATE ROLE {p}builder LOGIN NOINHERIT PASSWORD 'builder'"))
        c.execute(text(f"ALTER ROLE {p}reader SET default_transaction_read_only = on"))
        c.execute(text(f"ALTER ROLE {p}reader SET statement_timeout = '60s'"))
        c.execute(text(f'CREATE DATABASE "{plane.analytics_db}" OWNER {p}loader'))
        c.execute(text(f'REVOKE CONNECT ON DATABASE "{plane.analytics_db}" FROM PUBLIC'))
        c.execute(text(f'GRANT CONNECT ON DATABASE "{plane.analytics_db}" TO {p}loader, {p}reader, {p}builder'))
    admin.dispose()
    engine = create_engine(plane.control_url)
    for attempt in range(10):  # a pooler that saw the previous plane's database dropped backs off a while
        try:
            with engine.begin() as c:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            break
        except Exception:
            if attempt == 9:
                raise
            time.sleep(5)
    from analystos.db import models  # noqa: F401
    from analystos.db.base import Base

    Base.metadata.create_all(engine)
    engine.dispose()
    from analystos.cli import seed

    seed()


# ---------------------------------------------------------------------------------------- processes
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_api(plane: Plane, *, workers: int = 1) -> tuple[subprocess.Popen, str]:
    """A private uvicorn on a free port against the plane (never the running API)."""
    import httpx

    port = free_port()
    proc = spawn([sys.executable, "-m", "uvicorn", "analystos.api.app:app", "--host", "127.0.0.1", "--port", str(port),
                  "--log-level", "warning", "--workers", str(workers)], plane)
    base = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            # 10 s: /api/health probes each dependency (an absent Superset alone takes its 2 s timeout)
            if httpx.get(f"{base}/api/health", timeout=10).status_code < 500:
                return proc, base
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise SystemExit("API exited during start-up")
        time.sleep(0.2)
    proc.terminate()
    raise SystemExit("API did not start")


def start_mock_servicenow() -> tuple[object, str]:
    import uvicorn

    from analystos.connectors.servicenow_mock import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"


def spawn(args: list[str], plane: Plane, **kwargs) -> subprocess.Popen:
    """A child in its own process group, so `stop` also reaches its process-pool children (a worker's
    compute pool outlived its parent before, and the orphans held gigabytes on the next run)."""
    return subprocess.Popen(args, env=plane.env, cwd=ROOT, start_new_session=True, **kwargs)


def _signal_group(p: subprocess.Popen, sig: int) -> None:
    import contextlib

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(p.pid, sig)


def stop(procs: list[subprocess.Popen]) -> None:
    import signal

    for p in procs:
        _signal_group(p, signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
    time.sleep(1)
    for p in procs:
        _signal_group(p, signal.SIGKILL)  # whatever of the group is still there (pool children)


# ---------------------------------------------------------------------------------------- measuring
def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))]


def summary(values: list[float], scale: float = 1000.0, digits: int = 1) -> dict[str, float | int | None]:
    """p50/p95/p99/max of seconds, reported in ms by default."""
    def r(v):
        return None if v is None else round(v * scale, digits)
    return {"n": len(values), "p50": r(pct(values, 50)), "p95": r(pct(values, 95)), "p99": r(pct(values, 99)),
            "max": r(max(values) if values else None)}


class Sampler:
    """Samples machine load, the RSS/CPU of given processes and Postgres connections every `every` s.

    No psutil in the image: /proc is read directly (Linux only)."""

    def __init__(self, pids: list[int] | None = None, every: float = 1.0, database: str | None = None):
        self.pids = list(pids or [])
        self.every = every
        self.database = database
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ticks = os.sysconf("SC_CLK_TCK")
        self._last_cpu: dict[int, tuple[float, float]] = {}

    def _proc(self, pid: int) -> tuple[float, float] | None:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            rss_kb = next(int(line.split()[1]) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("VmRSS"))
        except (OSError, StopIteration, IndexError):
            return None
        cpu_s = (int(stat[11]) + int(stat[12])) / self._ticks
        return cpu_s, rss_kb / 1024

    def _children(self, pid: int) -> list[int]:
        try:
            out = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            return []
        kids = [int(x) for x in out]
        return kids + [g for k in kids for g in self._children(k)]

    def _pg_connections(self) -> tuple[int | None, int | None]:
        """(server connections to this plane's databases, all client backends on the server), read directly."""
        if not self.database:
            return None, None
        from sqlalchemy import text

        try:
            if not hasattr(self, "_engine"):
                self._engine = _admin()
            with self._engine.connect() as c:
                row = c.execute(text("SELECT count(*) FILTER (WHERE datname LIKE :d), count(*) FROM pg_stat_activity "
                                     "WHERE backend_type = 'client backend'"), {"d": f"{self.database}%"}).one()
                return int(row[0]), int(row[1])
        except Exception:
            return None, None

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            load1 = float(Path("/proc/loadavg").read_text().split()[0])
            rss, cpu_pct = 0.0, 0.0
            for root in self.pids:
                for pid in [root, *self._children(root)]:
                    got = self._proc(pid)
                    if got is None:
                        continue
                    cpu_s, mb = got
                    rss += mb
                    last = self._last_cpu.get(pid)
                    if last:
                        cpu_pct += 100 * (cpu_s - last[1]) / max(1e-6, now - last[0])
                    self._last_cpu[pid] = (now, cpu_s)
            plane_conns, server_conns = self._pg_connections()
            mem_kb = next((int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines()
                           if ln.startswith("MemAvailable")), 0)
            self.samples.append({"t": now, "load1": load1, "rss_mb": round(rss, 1), "cpu_pct": round(cpu_pct, 1),
                                 "pg_connections": plane_conns, "pg_connections_server": server_conns,
                                 "mem_available_mb": mem_kb // 1024})
            self._stop.wait(self.every)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        if hasattr(self, "_engine"):
            self._engine.dispose()

    def report(self) -> dict:
        s = self.samples
        if not s:
            return {}
        conns = [x["pg_connections"] for x in s if x["pg_connections"] is not None]
        server = [x["pg_connections_server"] for x in s if x.get("pg_connections_server") is not None]
        return {"samples": len(s), "load1_max": max(x["load1"] for x in s), "load1_mean": round(sum(x["load1"] for x in s) / len(s), 2),
                "platform_rss_mb_max": max(x["rss_mb"] for x in s), "platform_cpu_pct_mean": round(sum(x["cpu_pct"] for x in s) / len(s), 1),
                "platform_cpu_pct_max": max(x["cpu_pct"] for x in s), "pg_connections_max": max(conns) if conns else None,
                "pg_server_connections_max": max(server) if server else None,
                "mem_available_mb_min": min(x.get("mem_available_mb", 0) for x in s),
                "cores": os.cpu_count()}


def write_json(name: str, data: dict, out: Path = EVIDENCE) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(data, indent=2, default=str))
    return path

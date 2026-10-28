"""What an isolated worker enforces in its own process (ADR-0022 decision 1, P7-06).

* **Environment.** A worker refuses to start when it was given a database, Redis, graph or provider
  credential (a deployment mistake that would put secrets next to untrusted-size work), then drops
  every variable that is not on a short allowlist. It never builds the platform `Settings`, which
  would read `.env`.
* **Egress.** Python sockets may reach only the artifact store (and, for the Temporal transport, the
  Temporal frontend, which the SDK reaches from native code anyway). Every other connect, datagram,
  name lookup and Unix socket is refused. Jobs run in a child with the guard allowing nothing and,
  where the kernel allows it, in a fresh network namespace with no interface but a down loopback.
* **Resources.** The job child gets rlimits from the task budget (CPU, address space, file size,
  open files, no core files) and is killed on its wall clock by the supervisor.

In-process controls are the inner layer. The outer layer is the deployment: a compose network with
no route to Postgres or Redis, and the Helm NetworkPolicy that lets isolated pods reach DNS, the API
(artifact store) and Temporal only (docs/30-runbooks/04-lite-and-profiles.md, "Isolated compute pools").
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from analystos.core.errors import AnalystOSError

try:
    import resource
except ImportError:  # Windows: the module imports (API, tests), but no job child starts without rlimits
    resource = None  # type: ignore[assignment]

# Exactly these survive into an isolated worker; everything else is dropped.
ALLOWED_ENV = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "PYTHONPATH", "PYTHONHASHSEED", "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "ANALYSTOS_ENV", "ANALYSTOS_WORKER_QUEUES", "ANALYSTOS_WORKER_ARTIFACT_URL",
    "ANALYSTOS_TEMPORAL_ADDRESS", "ANALYSTOS_TEMPORAL_NAMESPACE", "ANALYSTOS_TEMPORAL_QUEUE_PREFIX",
    "ANALYSTOS_WORKER_REQUIRE_RUNTIME",
})
# A worker given any of these refuses to start (names only are reported, never values).
_FORBIDDEN = [re.compile(p) for p in (
    r"^ANALYSTOS_DATABASE_URL$", r"^ANALYSTOS_ANALYTICS_.*_URL$", r"^ANALYSTOS_REDIS_URL$", r"^ANALYSTOS_NEO4J_",
    r"^ANALYSTOS_SUPERSET_(URL|USERNAME|PASSWORD|ANALYTICS_SQLALCHEMY_URI)$", r"^ANALYSTOS_JWT_SECRET$",
    r"^ANALYSTOS_WORKER_TOKEN_SECRET$", r"^ANALYSTOS_ANALYTICS_BI_SECRET$", r"^ANALYSTOS_OIDC_CLIENT_SECRET$",
    r"^ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD$", r"^OPENROUTER_API_KEY$", r"^DATABASE_URL$", r"^REDIS_URL$",
    r"^PG(HOST|PORT|USER|PASSWORD|DATABASE|SERVICE|PASSFILE)$", r"^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)$",
    r"^GOOGLE_APPLICATION_CREDENTIALS$", r"^AZURE_(CLIENT_SECRET|STORAGE_KEY|STORAGE_CONNECTION_STRING)$",
    r"_API_KEY$", r"_PASSWORD$", r"_SECRET$", r"_SECRET_KEY$",
)]
DEFAULT_ARTIFACT_URL = "http://localhost:8000"
EX_CONFIG = 78  # sysexits: configuration error (a worker refusing its environment exits with this)


class WorkerMisconfigured(AnalystOSError):
    code, http_status = "worker_misconfigured", 500


class EgressRefused(PermissionError):
    """A connection an isolated worker may not open. An OSError, so client libraries surface it as a
    connection failure; `analystos.workers.child` reports it as `egress_blocked`."""


def forbidden_names(env: Mapping[str, str]) -> list[str]:
    return sorted(k for k, v in env.items() if v and any(p.search(k) for p in _FORBIDDEN))


def scrubbed(env: Mapping[str, str], extra: Iterable[str] = ()) -> dict[str, str]:
    keep = ALLOWED_ENV | set(extra)
    return {k: v for k, v in env.items() if k in keep}


def check_and_scrub_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Refuse a credential-bearing environment, then reduce it to the allowlist (in place for os.environ)."""
    env = os.environ if env is None else env
    bad = forbidden_names(env)
    if bad:
        raise WorkerMisconfigured("an isolated worker must not be given database, cache or provider credentials; "
                                  f"remove {', '.join(bad)} from its environment", details={"variables": bad})
    kept = scrubbed(env)
    env.clear()
    env.update(kept)
    return kept


# ------------------------------------------------------------------------------------ container runtime
# What the sandboxed kernel reports (S07-T1, ADR-012). gVisor's Sentry answers uname/proc with a fixed
# fake kernel and logs its own banner; Kata runs a real guest kernel, seen as a hypervisor CPU flag.
RUNTIMES = ("gvisor", "kata")
_GVISOR_VERSION = re.compile(r"#1 SMP Sun Jan 10 15:06:54 PST 2016")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(65536)
    except OSError:
        return ""


def detect_runtime(read: Any = None) -> str:
    """`gvisor`, `kata` or `runc` (an ordinary container or host kernel); `unknown` off Linux."""
    read = read or _read
    version = read("/proc/version")
    if not version:
        return "unknown"
    if _GVISOR_VERSION.search(version) or "gvisor" in read("/proc/self/mountinfo").lower():
        return "gvisor"
    flags = next((line for line in read("/proc/cpuinfo").splitlines() if line.startswith("flags")), "")
    if " hypervisor" in flags and "kata" in read("/proc/cmdline").lower():
        return "kata"
    return "runc"


def check_runtime(env: Mapping[str, str] | None = None, *, detect: Any = None) -> str | None:
    """Refuse to start when the deployment asked for a sandboxed runtime (`ANALYSTOS_WORKER_REQUIRE_RUNTIME`,
    set by the Helm chart from the pool's runtimeClassName) and the kernel says otherwise. Returns what ran."""
    env = os.environ if env is None else env
    wanted = (env.get("ANALYSTOS_WORKER_REQUIRE_RUNTIME") or "").strip().lower()
    if not wanted:
        return None
    if wanted not in RUNTIMES:
        raise WorkerMisconfigured(f"ANALYSTOS_WORKER_REQUIRE_RUNTIME must be one of {', '.join(RUNTIMES)} (got {wanted!r})")
    found = (detect or detect_runtime)()
    if found != wanted:
        raise WorkerMisconfigured(f"this isolated worker must run under {wanted} but the kernel reports {found}; "
                                  "check the pod's runtimeClassName and the node's RuntimeClass handler",
                                  details={"required": wanted, "found": found})
    return found


# ------------------------------------------------------------------------------------ egress
_installed: dict[str, Any] = {}


def _endpoint(url_or_address: str) -> tuple[str, int]:
    if "://" in url_or_address:
        parts = urlsplit(url_or_address)
        return parts.hostname or "localhost", parts.port or (443 if parts.scheme == "https" else 80)
    host, _, port = url_or_address.rpartition(":")
    return host or "localhost", int(port)


def resolve_allowed(endpoints: Iterable[str]) -> tuple[set[str], set[tuple[str, int]]]:
    """(host names that may be looked up, (ip, port) pairs that may be connected to)."""
    names: set[str] = set()
    pairs: set[tuple[str, int]] = set()
    for item in endpoints:
        host, port = _endpoint(item)
        names.add(host.lower())
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        for info in infos:
            pairs.add((str(ipaddress.ip_address(info[4][0].split("%")[0])), port))
    return names, pairs


def install_egress_guard(endpoints: Iterable[str] = ()) -> None:
    """Refuse every Python-level connection except to `endpoints` (URLs or host:port). A second call replaces
    the allowlist; the guard itself cannot be removed once installed. Allowed names are re-resolved (at most
    every 5 s) before a refusal, so a store whose address was not resolvable at start-up, or moved, still works."""
    import time

    endpoints = list(endpoints)
    names, pairs = resolve_allowed(endpoints)
    _installed.update(names=names, pairs=pairs, endpoints=endpoints, looked_up=time.monotonic())
    if _installed.get("patched"):
        return

    def refresh() -> None:
        if _installed["endpoints"] and time.monotonic() - _installed["looked_up"] >= 5:
            _installed["names"], _installed["pairs"] = resolve_allowed(_installed["endpoints"])
            _installed["looked_up"] = time.monotonic()

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_sendto, real_getaddrinfo = socket.socket.sendto, socket.getaddrinfo

    def allowed(sock: socket.socket, address: Any) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            raise EgressRefused(f"isolated worker: {sock.family.name} sockets are refused")
        host, port = address[0], int(address[1])
        try:
            ip = str(ipaddress.ip_address(str(host).split("%")[0]))
        except ValueError:
            ip = None
        if ip is not None and (ip, port) not in _installed["pairs"]:
            refresh()
        if ip is None or (ip, port) not in _installed["pairs"]:
            raise EgressRefused(f"isolated worker: egress to {host}:{port} is refused (only the artifact store)")

    def connect(self, address):  # noqa: ANN001, ANN202
        allowed(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):  # noqa: ANN001, ANN202
        allowed(self, address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):  # noqa: ANN001, ANN002, ANN202
        allowed(self, args[-1])
        return real_sendto(self, data, *args)

    def getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        text = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(text.split("%")[0])
        except ValueError:
            if text.lower() not in _installed["names"]:
                raise EgressRefused(f"isolated worker: name lookup of {text} is refused") from None
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.socket.sendto, socket.getaddrinfo = sendto, getaddrinfo
    _installed["patched"] = True


# ------------------------------------------------------------------------------------ job child limits
def job_rlimits(budget: Mapping[str, Any]) -> list[tuple[int, tuple[int, int]]]:
    if resource is None:
        raise WorkerMisconfigured("an isolated worker needs POSIX resource limits; run it on Linux (or in its container)")
    cpu = int(budget["cpu_seconds"])
    memory = int(budget["memory_mb"]) * 1024 * 1024
    out = [(resource.RLIMIT_CPU, (cpu, cpu + 2)), (resource.RLIMIT_AS, (memory, memory)),
           (resource.RLIMIT_CORE, (0, 0)), (resource.RLIMIT_NOFILE, (256, 256))]
    fsize = int(budget["max_output_bytes"]) + 1
    out.append((resource.RLIMIT_FSIZE, (fsize, fsize)))
    return out


def network_namespace_preexec(limits: list[tuple[int, tuple[int, int]]], try_netns: bool):  # noqa: ANN201
    """preexec_fn of a job child: own session (group kill), optional empty network namespace, rlimits.
    Nothing here imports or allocates much: it runs between fork and exec of a threaded process."""
    from analystos.sandbox.isolation import CLONE_NEWNET, CLONE_NEWUSER, load_libc

    libc = load_libc() if try_netns else None
    uid, gid = os.getuid(), os.getgid()

    def preexec() -> None:
        os.setsid()
        if libc is not None and libc.unshare(CLONE_NEWNET) != 0 and libc.unshare(CLONE_NEWUSER | CLONE_NEWNET) == 0:
            for path, text in (("/proc/self/setgroups", "deny"), ("/proc/self/uid_map", f"{uid} {uid} 1"),
                               ("/proc/self/gid_map", f"{gid} {gid} 1")):
                try:
                    with open(path, "w") as f:
                        f.write(text)
                except OSError:
                    pass
        for which, value in limits:
            try:  # noqa: SIM105 - plain try in a post-fork hook: nothing to import or allocate
                resource.setrlimit(which, value)
            except (ValueError, OSError):
                pass
    return preexec

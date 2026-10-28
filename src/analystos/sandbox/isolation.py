"""Isolation backends for the Python sandbox (P4-02: DEX-003..005) and the gate that refuses to run
without one.

Modes (``ANALYSTOS_SANDBOX_ISOLATION``):

* ``container`` - every execution is a fresh ``docker run`` of ``sandbox_container_image``:
  ``--network none``, ``--read-only`` root with a size-limited ``/scratch`` tmpfs, ``--user 65534``,
  ``--cap-drop ALL``, ``no-new-privileges``, ``--memory``/``--memory-swap``, ``--cpus``,
  ``--pids-limit`` (cgroups) plus the in-child rlimits. Needs a docker CLI and socket; used on a
  single host / development machine where the worker runs as a process next to Docker.
* ``process`` - the worker's own child in fresh mount, network, PID, IPC and UTS namespaces (with a
  user namespace when the worker is not root): empty network (down loopback only), a private /proc
  (no other process and so no other process's environment is visible), the whole filesystem
  read-only except a size-limited tmpfs scratch directory, secret paths masked, rlimits for memory,
  CPU, files, file size and processes, and every capability dropped (a one-off uid when the worker is
  real root). Used inside a Kubernetes worker pod, where the pod is the outer container.
* ``auto`` (default) - ``container`` when its image is present, else ``process`` when the host
  allows it, else unavailable.
* ``off`` - rlimits and the Python-level policy only (the in-process sandbox). Refused when
  ``ANALYSTOS_ENV`` is production, and reported as unisolated everywhere it is shown. Outside a
  development host (``ANALYSTOS_ENV`` dev/test/local) it runs only for ``profile=lite`` on synthetic data
  (ADR-012, S07-T1: `in_process_allowed`); anything else needs a real boundary.

When no backend can be established the gate refuses to run sandboxed code (fail closed) with the
reason; ``/api/health`` reports the same status.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["auto", "container", "process", "off"]
Backend = Literal["container", "process", "none"]
MODES = ("auto", "container", "process", "off")
PRODUCTION_ENVS = frozenset({"prod", "production"})
DEVELOPMENT_ENVS = frozenset({"dev", "development", "test", "local"})
DATA_CLASSES = ("synthetic", "unclassified")  # what the caller says the inputs are; unknown = unclassified

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
PROCESS_NAMESPACES = CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWUTS


class IsolationStatus(BaseModel):
    """What the gate would do right now. ``available`` False means sandboxed code is refused."""

    mode: str
    backend: Backend
    available: bool
    isolated: bool
    enforced: dict[str, bool] = Field(default_factory=dict)
    detail: str = ""
    checked_at: float = 0.0


_CONTROLS = ("network", "filesystem", "processes", "memory", "cpu", "wall_clock", "secrets")
_NONE = dict.fromkeys(_CONTROLS, False) | {"memory": True, "cpu": True, "wall_clock": True}  # rlimits + timeout


def _settings() -> Any:
    from analystos.core.config import get_settings

    return get_settings()


def configured_mode(settings: Any | None = None) -> str:
    mode = getattr(settings or _settings(), "sandbox_isolation", "auto")
    if mode not in MODES:
        raise ValueError(f"sandbox isolation mode must be one of {', '.join(MODES)} (got {mode!r})")
    return mode


# -- process backend ----------------------------------------------------------------------------
_LIBC: ctypes.CDLL | None = None


def load_libc() -> ctypes.CDLL:
    """Resolved in the parent: the preexec_fn runs between fork and exec of a threaded worker, where an
    import (and its lock) could deadlock."""
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    return _LIBC


def _unshare(flags: int) -> None:
    libc = _LIBC or load_libc()
    if libc.unshare(flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"unshare({flags:#x}) failed: {os.strerror(err)}")


def enter_process_namespaces() -> None:
    """preexec_fn of a process-mode child: new namespaces, privileged first, else inside a user
    namespace that maps only the caller's uid/gid (as root of that namespace, with no host power)."""
    try:
        _unshare(PROCESS_NAMESPACES)
        return
    except OSError:
        pass
    uid, gid = os.getuid(), os.getgid()
    _unshare(CLONE_NEWUSER | PROCESS_NAMESPACES)
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny")
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"0 {uid} 1")
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"0 {gid} 1")


PROBE_CODE = """import os, socket
report = {"pid": os.getpid(), "interfaces": [n for _, n in socket.if_nameindex()],
          "pids": sorted(int(p) for p in os.listdir("/proc") if p.isdigit()), "cwd": os.getcwd()}
try:
    open("/aos-isolation-probe", "w").close(); report["root_writable"] = True
except OSError:
    report["root_writable"] = False
try:
    open("probe", "w").write("ok"); report["scratch_writable"] = True
except OSError:
    report["scratch_writable"] = False
result = report
"""


def _probe_process() -> tuple[bool, str]:
    """Run the real process backend once and check what it established (not just that it started)."""
    if not sys.platform.startswith("linux"):
        return False, "process isolation needs Linux namespaces"
    from analystos.sandbox.runner import _execute

    r = _execute(PROBE_CODE, backend="process", timeout_s=20, memory_mb=256, allowed_imports=("os", "socket"),
                 enforce_policy=False)
    if not r.ok:
        return False, (r.error or "probe failed")[:400]
    rep = r.result or {}
    problems = []
    if rep.get("pid") != 1 or rep.get("pids") != [1]:
        problems.append("no private PID namespace")
    if rep.get("interfaces") != ["lo"]:
        problems.append("network namespace is not empty")
    if rep.get("root_writable") or not rep.get("scratch_writable"):
        problems.append("filesystem is not read-only-with-scratch")
    return (not problems), ("; ".join(problems) or "namespaces, read-only root, scratch tmpfs, capabilities dropped")


# -- container backend --------------------------------------------------------------------------
def _docker() -> str | None:
    return shutil.which("docker")


def _probe_container(settings: Any) -> tuple[bool, str]:
    docker = _docker()
    if not docker:
        return False, "no docker CLI on this host"
    image = settings.sandbox_container_image
    try:
        r = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", image],  # noqa: S603
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker unavailable: {exc}"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return False, f"sandbox image {image!r} not available: {msg[-1] if msg else 'docker error'}"
    return True, f"docker run {image} (--network none, read-only root, cgroup limits)"


def container_argv(settings: Any, name: str, *, memory_mb: int, scratch_mb: int, env: dict[str, str],
                   harness_source: str) -> list[str]:
    argv = [
        _docker() or "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none", "--ipc", "none", "--read-only",
        "--tmpfs", f"/scratch:rw,nosuid,nodev,noexec,size={int(scratch_mb)}m,mode=1777",
        "--workdir", "/scratch", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", f"{int(memory_mb)}m", "--memory-swap", f"{int(memory_mb)}m",
        "--cpus", str(settings.sandbox_cpus), "--pids-limit", str(int(settings.sandbox_pids_limit)),
        "--label", "analystos.sandbox=1",
    ]
    if settings.sandbox_container_runtime:
        argv += ["--runtime", settings.sandbox_container_runtime]
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    argv += ["--entrypoint", settings.sandbox_container_python, settings.sandbox_container_image,
             "-I", "-B", "-c", harness_source]
    return argv


def docker_kill(name: str) -> None:
    docker = _docker()
    if docker:
        subprocess.run([docker, "kill", name], capture_output=True, timeout=15, check=False)  # noqa: S603


# -- gate ---------------------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[tuple[str, str], IsolationStatus] = {}


def _enforced(backend: Backend) -> dict[str, bool]:
    if backend == "none":
        return dict(_NONE)
    return dict.fromkeys(_CONTROLS, True)


def status(settings: Any | None = None, *, refresh: bool = False) -> IsolationStatus:
    """Resolve the configured mode to a backend. Probed once per process (and on ``refresh``)."""
    settings = settings or _settings()
    try:
        mode = configured_mode(settings)
    except ValueError as exc:
        return IsolationStatus(mode=str(getattr(settings, "sandbox_isolation", "?")), backend="none",
                               available=False, isolated=False, detail=str(exc), checked_at=time.time())
    key = (mode, str(getattr(settings, "sandbox_container_image", "")), str(getattr(settings, "env", "")))
    with _lock:
        if not refresh and key in _cache:
            return _cache[key]
    st = _resolve(settings, mode)
    with _lock:
        _cache[key] = st
    return st


def _resolve(settings: Any, mode: str) -> IsolationStatus:
    now = time.time()
    if mode == "off":
        if str(settings.env).lower() in PRODUCTION_ENVS:
            return IsolationStatus(mode=mode, backend="none", available=False, isolated=False, enforced=_enforced("none"),
                                   detail="sandbox isolation 'off' is development only and is refused when "
                                          f"ANALYSTOS_ENV={settings.env}", checked_at=now)
        return IsolationStatus(mode=mode, backend="none", available=True, isolated=False, enforced=_enforced("none"),
                               detail="DEVELOPMENT ONLY: no network, filesystem or process isolation "
                                      "(ANALYSTOS_SANDBOX_ISOLATION=off)", checked_at=now)
    reasons = []
    if mode in ("container", "auto"):
        ok, why = _probe_container(settings)
        if ok:
            return IsolationStatus(mode=mode, backend="container", available=True, isolated=True,
                                   enforced=_enforced("container"), detail=why, checked_at=now)
        reasons.append(f"container: {why}")
    if mode in ("process", "auto"):
        ok, why = _probe_process()
        if ok:
            return IsolationStatus(mode=mode, backend="process", available=True, isolated=True,
                                   enforced=_enforced("process"), detail=why, checked_at=now)
        reasons.append(f"process: {why}")
    return IsolationStatus(mode=mode, backend="none", available=False, isolated=False, enforced={},
                           detail="sandbox isolation unavailable, sandboxed code is refused: " + "; ".join(reasons),
                           checked_at=now)


def in_process_allowed(settings: Any | None = None, data_class: str = "unclassified") -> tuple[bool, str]:
    """ADR-012: the in-process rlimit sandbox (backend ``none``) is no boundary, so it may run only
    ``profile=lite AND data_class=synthetic``. A development host (ANALYSTOS_ENV dev/test/local) keeps the
    explicit ``off`` opt-in for its own data; production never reaches here (`_resolve` refuses ``off``)."""
    settings = settings or _settings()
    if getattr(settings, "profile", None) == "lite" and data_class == "synthetic":
        return True, "lite profile, synthetic data"
    if str(getattr(settings, "env", "")).lower() in DEVELOPMENT_ENVS:
        return True, f"development host (ANALYSTOS_ENV={settings.env})"
    return False, ("the in-process sandbox runs only with profile=lite on synthetic data (ADR-012); "
                   f"profile={getattr(settings, 'profile', None)}, data_class={data_class}, ANALYSTOS_ENV={settings.env}: "
                   "configure ANALYSTOS_SANDBOX_ISOLATION=process|container or an isolated compute pool")


def reset_cache() -> None:
    with _lock:
        _cache.clear()

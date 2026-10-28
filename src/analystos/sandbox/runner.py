"""Run allow-listed numeric Python in an isolated, resource-limited child (spec §47, P4-02).

Layers, outermost first:

* isolation (P4-02, `sandbox/isolation.py`): the child runs in a sandbox container (`docker run
  --network none --read-only`, cgroup memory/CPU/pids limits, uid 65534, no capabilities) or in
  fresh mount/network/PID/IPC/UTS namespaces (read-only root, tmpfs scratch, private /proc, masked
  secret paths, rlimits incl. processes, no capabilities). `ANALYSTOS_SANDBOX_ISOLATION`:
  `auto` (default) | `container` | `process` | `off` (development only). When no backend can be
  established the code is refused (fail closed) and the result says why.
* static AST check before anything runs: imports must be in `allowed_imports` (top-level module,
  no relative imports); names such as open / exec / eval / compile / __import__ / getattr /
  globals and module names os / sys / subprocess / socket / importlib / ctypes / builtins are
  rejected; every dunder attribute (`__subclasses__`, `__globals__`, `__builtins__`, `__class__`...)
  and every string literal containing "__" is rejected; well-known file / process attributes
  (`read_csv`, `to_csv`, `load`, `save`, `fromfile`, `system`, `popen`...) are rejected;
* the child runs `python -I` (isolated mode) in an empty scratch directory that is deleted afterwards;
* the child environment is rebuilt from scratch: only PATH, HOME/TMPDIR (the scratch dir), LANG and
  single-thread BLAS settings - no proxies, API keys (e.g. OPENROUTER_API_KEY) or database URLs;
* rlimits in the child: RLIMIT_AS (memory), RLIMIT_CPU, RLIMIT_NOFILE, RLIMIT_FSIZE, RLIMIT_CORE = 0
  and (process backend) RLIMIT_NPROC;
* wall-clock timeout; on expiry the process group (and the container) is SIGKILLed;
* inside the child: builtins without the dangerous entries and an import hook that re-checks the
  allowlist at runtime;
* inputs go in as JSON on stdin; the result is the JSON-serialised value of the variable `result`.

What it does NOT do: syscall filtering (seccomp) or protection from kernel exploits. In production
the process backend runs inside the Helm chart's worker pods (`readOnlyRootFilesystem`, capabilities
dropped, non-root, seccomp RuntimeDefault, NetworkPolicy; gVisor where available) as the outer layer.
"""
from __future__ import annotations

import ast
import contextlib
import json
import logging
import math
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

try:
    import resource
except ImportError:  # Windows: the module imports, but the `none` backend refuses to run without rlimits
    resource = None  # type: ignore[assignment]

DEFAULT_ALLOWED_IMPORTS = ("math", "statistics", "json", "numpy", "pandas", "polars", "scipy", "statsmodels", "sklearn")
HARNESS = Path(__file__).with_name("_harness.py")
MARKER = "\x1e__AOS_SANDBOX_RESULT__\x1e"
MAX_CODE_CHARS = 100_000

DENIED_BUILTINS = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "input", "help", "exit", "quit", "memoryview", "__loader__", "__spec__", "copyright", "credits",
    "license",
})
DENIED_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "importlib", "ctypes", "builtins", "shutil", "pathlib", "signal",
    "multiprocessing", "threading", "pickle", "marshal", "io", "posix", "pty", "resource", "gc", "inspect",
    "code", "codeop", "urllib", "http", "requests", "httpx", "asyncio", "tempfile", "glob", "fcntl", "mmap",
})
DENIED_ATTRS = frozenset({
    # process / module escape hatches that can hang off allowed packages
    "os", "sys", "subprocess", "socket", "importlib", "ctypes", "ctypeslib", "builtins", "system", "popen", "spawn",
    "fork", "execv", "execve", "environ", "getenv", "putenv", "f_globals", "f_locals", "f_back", "gi_frame",
    "cr_frame", "tb_frame", "func_globals", "mro", "_getframe", "_current_frames",
    # file I/O on numpy / pandas / polars / scipy
    "load", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "fromfile", "tofile",
    "memmap", "fromregex", "DataSource", "open_memmap", "read_csv", "read_table", "read_fwf", "read_json", "read_html",
    "read_xml", "read_excel", "read_parquet", "read_feather", "read_orc", "read_pickle", "read_sql", "read_sql_query",
    "read_sql_table", "read_hdf", "read_sas", "read_spss", "read_stata", "read_clipboard", "read_ipc", "read_database",
    "read_avro", "read_ndjson", "scan_csv", "scan_parquet", "scan_ipc", "scan_ndjson", "to_csv", "to_json", "to_html",
    "to_xml", "to_excel", "to_parquet", "to_feather", "to_orc", "to_pickle", "to_sql", "to_hdf", "to_stata",
    "to_clipboard", "to_latex", "to_markdown", "write_csv", "write_parquet", "write_json", "write_ipc", "write_ndjson",
    "write_database", "write_excel", "write_avro", "sink_csv", "sink_parquet", "sink_ipc", "loadmat", "savemat",
    "mmread", "mmwrite", "wavfile", "netcdf_file", "readsav", "load_npz", "save_npz", "fetch_openml", "load_svmlight_file",
    "dump_svmlight_file", "get_data_home",
})

class SandboxResult(BaseModel):
    ok: bool
    stdout: str = ""
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    isolation: str | None = None  # container | process | none (`off`) | unavailable (refused by the gate)
    network_isolated: bool | None = None  # the child had no network




class _Checker(ast.NodeVisitor):
    def __init__(self, allowed: set[str]):
        self.allowed = allowed
        self.denied_names = (DENIED_BUILTINS | DENIED_MODULES) - allowed
        self.problems: list[str] = []

    def _bad(self, node: ast.AST, msg: str) -> None:
        self.problems.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name.split(".")[0] not in self.allowed:
                self._bad(node, f"import of '{a.name}' is not allowed")
            if a.asname and a.asname in self.denied_names:
                self._bad(node, f"alias '{a.asname}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._bad(node, "relative imports are not allowed")
        elif not node.module or node.module.split(".")[0] not in self.allowed:
            self._bad(node, f"import from '{node.module}' is not allowed")
        for a in node.names:
            if a.name == "*":
                self._bad(node, "star imports are not allowed")
            elif a.name in DENIED_ATTRS or a.name in self.denied_names or a.name.startswith("__"):
                self._bad(node, f"importing '{a.name}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.denied_names:
            self._bad(node, f"use of '{node.id}' is not allowed")
        elif node.id.startswith("__") and node.id.endswith("__"):
            self._bad(node, f"dunder name '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            self._bad(node, f"dunder attribute '{node.attr}' is not allowed")
        elif node.attr in DENIED_ATTRS and node.attr not in self.allowed:
            self._bad(node, f"attribute '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "__" in node.value:
            self._bad(node, "string literals containing '__' are not allowed")
        self.generic_visit(node)

    def _args(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        for a in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs,
                  *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            if a.arg in self.denied_names:
                self._bad(node, f"parameter name '{a.arg}' is not allowed")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._args(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._bad(node, "async code is not allowed")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._args(node)
        self.generic_visit(node)


def check_code(code: str, allowed_imports: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_IMPORTS) -> list[str]:
    """Static policy check. Returns a list of violations (empty = accepted)."""
    if not isinstance(code, str):
        return ["code must be a string"]
    if len(code) > MAX_CODE_CHARS:
        return [f"code longer than {MAX_CODE_CHARS} characters"]
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]
    c = _Checker(set(allowed_imports))
    c.visit(tree)
    return c.problems


def sandbox_env(workdir: str) -> dict[str, str]:
    """The complete environment the child sees (nothing inherited)."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
    }

_log = logging.getLogger(__name__)


def _json_default(o: Any) -> Any:
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def _host_limits(memory_mb: int, cpu_s: int, nofile: int, fsize_mb: int):
    """preexec_fn of the `off` backend: rlimits only (development)."""
    if resource is None:
        from analystos.core.errors import UnsupportedCapability
        raise UnsupportedCapability("the Python sandbox needs POSIX resource limits; run the API on Linux or in its container")

    def apply() -> None:
        mem = int(memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        fs = int(fsize_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.umask(0o077)

    return apply


def _settings() -> Any:
    from analystos.core.config import get_settings

    return get_settings()


def masked_paths(settings: Any) -> list[str]:
    """Secret-bearing paths hidden from a process-mode child (configured + service-account tokens),
    except any that holds the interpreter or its libraries (the child still has to import them)."""
    needed = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.executable)}
    out = set()
    for p in {*(str(p) for p in settings.sandbox_masked_paths or []), "/run/secrets", "/var/run/secrets"}:
        real = os.path.realpath(p)
        if not any(n == real or n.startswith(real.rstrip("/") + "/") for n in needed):
            out.add(real)
    return sorted(out)


def run_python(code: str, *, inputs: dict[str, list[dict]] | None = None, timeout_s: float = 30, memory_mb: int = 1024,
               allowed_imports: tuple[str, ...] = DEFAULT_ALLOWED_IMPORTS, max_output_bytes: int = 1_000_000,
               max_result_bytes: int = 10_000_000, nofile: int = 256, fsize_mb: int = 16,
               data_class: str = "unclassified") -> SandboxResult:
    """Execute `code` in an isolated, resource-limited child interpreter; returns the value of `result`.

    `inputs` is visible to the code as the dict `inputs` (e.g. `pd.DataFrame(inputs["rows"])`). The
    backend comes from the isolation gate (`sandbox.isolation.status`); without one nothing runs.
    `data_class` says what the inputs are: the unisolated backend takes only `synthetic` outside a
    development host (`isolation.in_process_allowed`, ADR-012)."""
    from analystos.sandbox import isolation

    t0 = time.monotonic()
    gate = isolation.status()
    if not gate.available:
        _log.error("sandboxed code refused: %s", gate.detail)
        return SandboxResult(ok=False, error=gate.detail, isolation="unavailable", network_isolated=False,
                             duration_ms=int((time.monotonic() - t0) * 1000))
    if gate.backend == "none":
        allowed, why = isolation.in_process_allowed(data_class=data_class)
        if not allowed:
            _log.error("sandboxed code refused: %s", why)
            return SandboxResult(ok=False, error=why, isolation="unavailable", network_isolated=False,
                                 duration_ms=int((time.monotonic() - t0) * 1000))
    problems = check_code(code, allowed_imports)
    if problems:
        return SandboxResult(ok=False, error="rejected by sandbox policy: " + "; ".join(problems[:10]),
                             isolation=gate.backend, duration_ms=int((time.monotonic() - t0) * 1000))
    return _execute(code, backend=gate.backend, inputs=inputs, timeout_s=timeout_s, memory_mb=memory_mb,
                    allowed_imports=allowed_imports, max_output_bytes=max_output_bytes,
                    max_result_bytes=max_result_bytes, nofile=nofile, fsize_mb=fsize_mb)


def _execute(code: str, *, backend: str, inputs: dict | None = None, timeout_s: float = 30, memory_mb: int = 1024,
             allowed_imports: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_IMPORTS, max_output_bytes: int = 1_000_000,
             max_result_bytes: int = 10_000_000, nofile: int = 256, fsize_mb: int = 16,
             enforce_policy: bool = True, settings: Any | None = None) -> SandboxResult:
    """Run on one backend (container | process | none). ``enforce_policy=False`` drops the in-child
    Python policy so the isolation probes test the OS boundary alone; no skill or tool reaches it."""
    from analystos.sandbox import isolation

    settings = settings or _settings()
    t0 = time.monotonic()
    cpu_s = max(1, math.ceil(timeout_s)) + 1
    scratch_mb = max(int(fsize_mb), int(settings.sandbox_scratch_mb))
    rlimits = {"memory_mb": memory_mb, "cpu_s": cpu_s, "nofile": nofile, "fsize_mb": fsize_mb}
    body: dict[str, Any] = {"code": code, "inputs": inputs or {}, "allowed": list(allowed_imports),
                            "denied_builtins": sorted(DENIED_BUILTINS), "max_output": max_output_bytes,
                            "max_result": max_result_bytes, "enforce_policy": enforce_policy}
    workdir = tempfile.mkdtemp(prefix="aos-sbx-")
    container = None
    wait_s = float(timeout_s)
    if backend == "container":
        container = f"aos-sbx-{secrets.token_hex(8)}"
        body["rlimits"] = rlimits
        body["env"] = sandbox_env("/scratch")
        argv = isolation.container_argv(settings, container, memory_mb=memory_mb, scratch_mb=scratch_mb,
                                        env=sandbox_env("/scratch"), harness_source=HARNESS.read_text(encoding="utf-8"))
        # the docker client itself is not limited; the container has cgroup limits plus the harness rlimits
        popen: dict[str, Any] = {"cwd": workdir,
                                 "env": {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST")}}
        wait_s += float(settings.sandbox_container_startup_seconds)
    elif backend == "process":
        body["rlimits"] = {**rlimits, "nproc": int(settings.sandbox_pids_limit)}
        body["isolation"] = {"scratch": workdir, "scratch_mb": scratch_mb, "mask": masked_paths(settings),
                             "uid": 200_000 + secrets.randbelow(100_000)}  # one-off uid: RLIMIT_NPROC is per uid
        argv = [sys.executable, "-I", "-B", str(HARNESS)]
        isolation.load_libc()
        popen = {"env": sandbox_env(workdir), "cwd": workdir, "preexec_fn": isolation.enter_process_namespaces}
    elif backend == "none":
        argv = [sys.executable, "-I", "-B", str(HARNESS)]
        popen = {"env": sandbox_env(workdir), "cwd": workdir,
                 "preexec_fn": _host_limits(memory_mb, cpu_s, nofile, fsize_mb)}
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        raise ValueError(f"unknown sandbox backend {backend!r}")
    try:
        payload = json.dumps(body, default=_json_default)
    except (TypeError, ValueError) as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return SandboxResult(ok=False, error=f"inputs are not JSON-serializable: {e}", isolation=backend)
    isolated = backend in ("container", "process")
    timed_out = False
    try:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,  # noqa: S603 - fixed argv
                                    stderr=subprocess.PIPE, close_fds=True, start_new_session=True, **popen)
        except (OSError, subprocess.SubprocessError) as e:  # e.g. namespaces refused: never run unisolated
            return SandboxResult(ok=False, error=f"sandbox could not start: {e}", isolation=backend,
                                 network_isolated=False, duration_ms=int((time.monotonic() - t0) * 1000))
        try:
            out, err = proc.communicate(payload.encode(), timeout=wait_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            if container:
                isolation.docker_kill(container)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        code_rc = proc.returncode
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    dur = int((time.monotonic() - t0) * 1000)
    text = out.decode("utf-8", "replace")
    common: dict[str, Any] = {"duration_ms": dur, "exit_code": code_rc, "isolation": backend, "network_isolated": isolated}
    if timed_out:
        return SandboxResult(ok=False, error=f"timeout: exceeded {timeout_s}s wall clock; sandbox killed",
                             timed_out=True, **common)
    idx = text.rfind(MARKER)
    if idx < 0:
        why = f"child exited with code {code_rc}"
        sig = -code_rc if code_rc is not None and code_rc < 0 else None
        if container and code_rc is not None and code_rc > 128:  # docker reports a signal as 128 + n
            sig = code_rc - 128
        if sig:
            name = signal.Signals(sig).name if sig in signal.Signals._value2member_map_ else str(sig)
            why += f" (signal {name}" + {"SIGXCPU": ": CPU limit", "SIGKILL": ": CPU hard limit or memory limit"}.get(name, "") + ")"
        tail = err.decode("utf-8", "replace")[-2000:]
        if "MemoryError" in tail:
            why = "MemoryError: memory limit exceeded; " + why
        return SandboxResult(ok=False, error=f"{why}; stderr: {tail.strip()}"[:4000], **common)
    try:
        env = json.loads(text[idx + len(MARKER):])
    except json.JSONDecodeError as e:
        return SandboxResult(ok=False, error=f"malformed sandbox output: {e}", **common)
    if env.get("setup_failed"):
        common["network_isolated"] = False
    return SandboxResult(ok=bool(env.get("ok")), stdout=env.get("stdout", ""), result=env.get("result"),
                         error=env.get("error"), **common)

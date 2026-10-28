"""S07-T1 (ADR-012): compute isolation is a release gate. The isolated worker refuses to start outside the
sandboxed runtime it was given (`ANALYSTOS_WORKER_REQUIRE_RUNTIME`; the Helm runtimeClass that sets it is S12's,
branch claude/competent-curran-ad80fc); the Docker dev pools and the container sandbox carry the no-network /
read-only / no-capability flags; and the in-process sandbox runs only for `profile=lite` on synthetic data
(a development host keeps its explicit `off` opt-in)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from analystos.core.config import Settings
from analystos.sandbox import isolation as sandbox_isolation
from analystos.workers import isolation as worker_isolation

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "analystos"


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


def test_isolated_pool_budgets_are_time_cpu_and_memory_limited():
    queues = yaml.safe_load((ROOT / "config" / "task_queues.yaml").read_text(encoding="utf-8"))["queues"]
    pools = _values()["workers"]
    for name in ("compute-py", "compute-ml"):
        budget = queues[name]["budget"]
        assert budget["cpu_seconds"] > 0 and budget["memory_mb"] > 0 and budget["wall_seconds"] > 0
        assert queues[name]["start_to_close_seconds"] >= budget["wall_seconds"]
        assert {"cpu", "memory"} <= set(pools[name]["resources"]["limits"])


def _fake(files: dict[str, str]):
    return lambda path: files.get(path, "")


def test_detect_runtime_reads_the_sandboxed_kernel():
    gvisor = {"/proc/version": "Linux version 4.4.0 #1 SMP Sun Jan 10 15:06:54 PST 2016"}
    assert worker_isolation.detect_runtime(_fake(gvisor)) == "gvisor"
    kata = {"/proc/version": "Linux version 6.1.38 (kata)", "/proc/cpuinfo": "flags\t: fpu vme hypervisor",
            "/proc/cmdline": "console=hvc0 agent.log=debug kata"}
    assert worker_isolation.detect_runtime(_fake(kata)) == "kata"
    assert worker_isolation.detect_runtime(_fake({"/proc/version": "Linux version 6.8.0-45-generic"})) == "runc"
    assert worker_isolation.detect_runtime(_fake({})) == "unknown"


def test_worker_refuses_to_start_outside_the_required_runtime():
    assert worker_isolation.check_runtime({}) is None  # nothing required (compose dev pools)
    assert worker_isolation.check_runtime({"ANALYSTOS_WORKER_REQUIRE_RUNTIME": "gvisor"}, detect=lambda: "gvisor") == "gvisor"
    with pytest.raises(worker_isolation.WorkerMisconfigured, match="must run under gvisor but the kernel reports runc"):
        worker_isolation.check_runtime({"ANALYSTOS_WORKER_REQUIRE_RUNTIME": "gvisor"}, detect=lambda: "runc")
    with pytest.raises(worker_isolation.WorkerMisconfigured, match="must be one of"):
        worker_isolation.check_runtime({"ANALYSTOS_WORKER_REQUIRE_RUNTIME": "chroot"})
    # the requirement survives the environment scrub (it is on the allowlist)
    env = {"ANALYSTOS_WORKER_REQUIRE_RUNTIME": "gvisor", "SOMETHING_ELSE": "x"}
    assert worker_isolation.check_and_scrub_environment(env) == {"ANALYSTOS_WORKER_REQUIRE_RUNTIME": "gvisor"}


def test_docker_dev_pools_have_no_route_out_no_root_and_no_capabilities():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["networks"]["isolated"]["internal"] is True
    for name in ("worker-compute-py", "worker-compute-ml"):
        svc = compose["services"][name]
        assert svc["networks"] == ["isolated"] and svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"] and svc["user"] == "10001:10001"
        assert svc["pids_limit"] > 0 and svc["mem_limit"] and svc["cpus"]
        assert "COMPUTE_RUNTIME" in svc["runtime"]  # runsc where gVisor is installed


def test_container_sandbox_runs_with_no_network_read_only_and_no_capabilities():
    settings = Settings(_env_file=None, sandbox_container_runtime="runsc")
    argv = sandbox_isolation.container_argv(settings, "aos-sbx-test", memory_mb=256, scratch_mb=16, env={},
                                            harness_source="pass")
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--runtime runsc"):
        assert flag in joined, flag
    assert "--user 65534:65534" in joined and "--pids-limit" in joined


@pytest.mark.parametrize(("profile", "env", "data_class", "allowed"), [
    ("lite", "production", "synthetic", True),
    ("lite", "staging", "unclassified", False),
    ("standard", "staging", "synthetic", False),
    ("standard", "dev", "unclassified", True),  # a developer's explicit `off` on their own machine
    ("scale", "test", "unclassified", True),
])
def test_in_process_sandbox_only_for_lite_and_synthetic(profile, env, data_class, allowed):
    settings = Settings(_env_file=None, profile=profile, env=env)
    ok, why = sandbox_isolation.in_process_allowed(settings, data_class)
    assert ok is allowed, why
    if not ok:
        assert "profile=lite on synthetic data" in why


def test_run_python_refuses_the_unisolated_backend_on_real_data(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.sandbox.runner import run_python

    monkeypatch.setenv("ANALYSTOS_SANDBOX_ISOLATION", "off")
    monkeypatch.setenv("ANALYSTOS_ENV", "staging")
    monkeypatch.setenv("ANALYSTOS_PROFILE", "standard")
    get_settings.cache_clear()
    sandbox_isolation.reset_cache()
    try:
        r = run_python("result = 1")
        assert not r.ok and r.isolation == "unavailable" and "ADR-012" in (r.error or "")
    finally:
        get_settings.cache_clear()
        sandbox_isolation.reset_cache()

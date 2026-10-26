"""Static checks of compose.yaml (demo readiness 2026-09-27; the demo owner runs `docker compose` on Windows and
there is no Docker daemon in CI): the file parses, every referenced file exists, host ports do not clash,
`service_healthy` dependencies have a healthcheck, every ANALYSTOS_ variable is a real setting, and the default
(`lite`) services match runbook 04."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from analystos.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
SERVICES: dict = COMPOSE["services"]
# Variables the app reads outside Settings (worker entry points, source secret_refs, the provider key).
NOT_SETTINGS = {"ANALYSTOS_WORKER_QUEUES", "ANALYSTOS_PG_POOLER", "ANALYSTOS_API_EXTRAS"}


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(e.split("=", 1) for e in env)


def test_every_build_context_dockerfile_and_mounted_path_exists():
    for name, svc in SERVICES.items():
        build = svc.get("build")
        if build:
            context = ROOT / (build if isinstance(build, str) else build.get("context", "."))
            assert context.is_dir(), f"{name}: build context {context} missing"
            dockerfile = context / (build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile")
            assert dockerfile.is_file(), f"{name}: {dockerfile} missing"
        for vol in svc.get("volumes") or []:
            source = vol.split(":", 1)[0]
            if source.startswith("."):
                assert (ROOT / source).exists(), f"{name}: mounted {source} missing"
        for arg in svc.get("entrypoint") or []:
            if arg.startswith("/app/analystos/"):
                assert (ROOT / "deploy/superset" / Path(arg).name).is_file(), f"{name}: entrypoint {arg} missing"


def test_host_ports_do_not_clash():
    seen: dict[str, str] = {}
    for name, svc in SERVICES.items():
        for port in svc.get("ports") or []:
            host = str(port).rsplit(":", 1)[0]
            assert host not in seen, f"host port {host} used by {seen.get(host)} and {name}"
            seen[host] = name


def test_healthy_dependencies_have_healthchecks():
    for name, svc in SERVICES.items():
        deps = svc.get("depends_on") or {}
        for dep, cond in (deps.items() if isinstance(deps, dict) else []):
            assert dep in SERVICES, f"{name} depends on unknown service {dep}"
            if (cond or {}).get("condition") == "service_healthy":
                has = "healthcheck" in SERVICES[dep] or dep == "web"
                assert has, f"{name} waits for {dep} to be healthy but {dep} has no healthcheck"


def test_every_analystos_variable_is_a_setting():
    fields = {f"ANALYSTOS_{k.upper()}" for k in Settings.model_fields}
    names = set()
    for svc in SERVICES.values():
        names |= {k for k in _env(svc) if k.startswith("ANALYSTOS_")}
    for env_file in (ROOT / "deploy/compose").glob("*.env"):
        names |= {line.split("=", 1)[0] for line in env_file.read_text(encoding="utf-8").splitlines() if line.startswith("ANALYSTOS_")}
    unknown = sorted(names - fields - NOT_SETTINGS)
    assert not unknown, f"compose sets variables no setting reads: {unknown}"


def test_profile_env_files_name_real_profiles_and_services():
    profiles = {p for svc in SERVICES.values() for p in svc.get("profiles") or []}
    for env_file in (ROOT / "deploy/compose").glob("*.env"):
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("COMPOSE_PROFILES="):
                assert set(line.split("=", 1)[1].split(",")) <= profiles, env_file.name
            m = re.match(r"ANALYSTOS_\w+_URL=https?://([\w-]+):", line)
            if m:
                assert m.group(1) in SERVICES, f"{env_file.name} points at unknown service {m.group(1)}"


def test_lite_is_postgres_api_web_and_the_runbook_profiles_exist():
    default = {name for name, svc in SERVICES.items() if not svc.get("profiles")}
    assert default == {"postgres", "api", "web"}
    assert _env(SERVICES["api"])["ANALYSTOS_PROFILE"] == "${ANALYSTOS_PROFILE:-lite}"
    runbook = (ROOT / "docs/30-runbooks/04-lite-and-profiles.md").read_text(encoding="utf-8")
    profiles = {p for svc in SERVICES.values() for p in svc.get("profiles") or []}
    for profile in ("standard", "scale", "bi", "graph", "demo", "sandbox", "pooled", "isolated"):
        assert profile in profiles and f"`{profile}`" in runbook, profile


def test_uploads_and_artifacts_survive_recreation_and_are_shared_with_workers():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert "runtime-data" in config["volumes"]
    for name in ("api", "worker", "scheduler", "worker-analysis", "worker-compute", "worker-publish", "worker-crawl"):
        assert "runtime-data:/app/var" in SERVICES[name].get("volumes", []), name


def test_isolated_compute_does_not_mount_platform_runtime_data():
    for name in ("worker-compute-py", "worker-compute-ml"):
        assert not any("runtime-data" in mount for mount in SERVICES[name].get("volumes", [])), name


@pytest.mark.parametrize("script", ["deploy/superset/bootstrap.sh", "deploy/postgres/02-extensions.sh", "scripts/demo.sh"])
def test_shell_scripts_run_in_linux_containers_from_a_windows_checkout(script):
    """A Windows checkout with core.autocrlf would give `bash\\r: not found`: .gitattributes pins LF."""
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.sh\s+text\s+eol=lf", attrs, re.M)
    assert b"\r\n" not in (ROOT / script).read_bytes()

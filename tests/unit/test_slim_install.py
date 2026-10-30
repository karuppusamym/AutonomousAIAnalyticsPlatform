"""P7-17 without Docker: a core install (no extras) imports the API, CLI and local orchestrator without
loading any extra's library; with nothing configured it runs as `lite` (the `standard` default needs
the `temporal` extra); and the image extras named by compose, the env files, the Dockerfile and the
Helm small overlay agree with `pyproject.toml`."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from analystos.core import config
from analystos.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
EXTRA_MODULES = ("sklearn", "statsmodels", "matplotlib", "fpdf", "openpyxl", "temporalio", "neo4j", "mcp", "langgraph",
                 "langchain_core", "langsmith")

# A child interpreter in which every extra's top-level module is missing, as in `pip install analystos`.
_CORE_ONLY = """
import importlib.abc, json, sys
BLOCKED = set(sys.argv[1].split(","))
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None
sys.meta_path.insert(0, Missing())
import analystos.api.app, analystos.cli, analystos.workflows.orchestrator, analystos.services.schedules
from analystos.core import profiles
from analystos.core.config import Settings
s = Settings(_env_file=None)
print(json.dumps({"loaded": sorted(m for m in BLOCKED | {"polars"} if m in sys.modules),
                  "extras": profiles.summary(s)["extras"], "profile": s.profile, "orchestrator": s.orchestrator}))
"""


def _core_only(tmp_path: Path) -> dict:
    env_clear = {k: v for k, v in os.environ.items() if not k.startswith("ANALYSTOS_")}
    script = tmp_path / "core_only.py"
    script.write_text(_CORE_ONLY, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script), ",".join(EXTRA_MODULES)], capture_output=True, text=True, cwd=tmp_path,
                         env={**env_clear, "PYTHONPATH": str(ROOT / "src")}, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_core_install_starts_without_any_extra_and_defaults_to_lite(tmp_path):
    got = _core_only(tmp_path)
    assert got["loaded"] == []  # no extra's library and no polars at API start (file ingest loads it on first use)
    assert got["extras"] == {"ml": False, "reports": False, "temporal": False, "graph": False, "langgraph": False}
    assert (got["profile"], got["orchestrator"]) == ("lite", "local")


def test_the_temporal_default_needs_the_extra_or_an_explicit_choice(monkeypatch):
    for var in ("ANALYSTOS_PROFILE", "ANALYSTOS_ORCHESTRATOR", "ANALYSTOS_REDIS_URL", "ANALYSTOS_SUPERSET_URL"):
        monkeypatch.delenv(var, raising=False)
    assert Settings(_env_file=None).profile == "standard"  # the extra is installed (dev): unchanged
    monkeypatch.setattr(config, "_installed", lambda m: m != "temporalio")
    lite = Settings(_env_file=None)
    assert (lite.profile, lite.orchestrator, lite.redis_url, lite.superset_url) == ("lite", "local", "", "")
    assert Settings(_env_file=None, profile="standard").profile == "standard"  # an explicit choice wins
    assert Settings(_env_file=None, orchestrator="temporal").profile == "standard"


# ------------------------------------------------------------------------------------ deploy consistency
def _extras() -> dict[str, list[str]]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]


def _names(spec: str) -> set[str]:
    return {e.strip() for e in spec.split(",") if e.strip()}


def _expand(extras: dict[str, list[str]], names: set[str]) -> set[str]:
    """`standard` -> its member extras (`analystos[ml,reports,...]`)."""
    out: set[str] = set()
    for n in names:
        out.add(n)
        for dep in extras[n]:
            m = re.fullmatch(r"analystos\[([^\]]+)\]", dep)
            if m:
                out |= _expand(extras, _names(m.group(1)))
    return out


def _env_file(name: str) -> dict[str, str]:
    lines = (ROOT / "deploy" / "compose" / name).read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def test_image_extras_agree_across_compose_dockerfile_and_helm():
    extras = _extras()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    default = re.fullmatch(r"\$\{ANALYSTOS_API_EXTRAS:-([^}]*)\}", services["api"]["build"]["args"]["EXTRAS"]).group(1)
    dockerfile = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG EXTRAS=(\S*)$", dockerfile, re.M).group(1) == default == "reports,ml"

    named = {"api(lite)": _names(default)}
    for name, svc in services.items():
        args = svc["build"].get("args") or {} if isinstance(svc.get("build"), dict) else {}
        if "EXTRAS" in args and name != "api":
            named[name] = _names(args["EXTRAS"])
    for env in sorted((ROOT / "deploy" / "compose").glob("*.env")):
        values = _env_file(env.name)
        if "ANALYSTOS_API_EXTRAS" in values:
            named[f"api({env.stem})"] = _names(values["ANALYSTOS_API_EXTRAS"])
    for where, names in named.items():
        assert names <= set(extras), f"{where} names an extra pyproject does not define: {names - set(extras)}"

    # lite: the API image has no Temporal or graph library (it carries ml since dc2e4e4: experiments train in the API),
    # and compose defaults to the lite profile.
    assert not _expand(extras, named["api(lite)"]) & {"temporal", "graph"}
    assert compose["x-app-env"]["ANALYSTOS_PROFILE"] == "${ANALYSTOS_PROFILE:-lite}"
    # Every env file that selects Temporal builds its API with the temporal extra.
    for env in sorted((ROOT / "deploy" / "compose").glob("*.env")):
        values = _env_file(env.name)
        if values.get("ANALYSTOS_ORCHESTRATOR") == "temporal":
            assert "temporal" in _expand(extras, _names(values.get("ANALYSTOS_API_EXTRAS", default))), env.name
        if values.get("ANALYSTOS_ORCHESTRATOR") == "langgraph":  # ADR-015: the backend needs its extra
            assert "langgraph" in _expand(extras, _names(values.get("ANALYSTOS_API_EXTRAS", default))), env.name
    # langgraph is never a profile default: no image carries it unless asked for (ADR-015 §1).
    assert not any("langgraph" in _expand(extras, names) for names in named.values())
    # Workers and the scheduler carry every extra; the isolated pools only what they run.
    assert _expand(extras, named["worker"]) >= {"ml", "reports", "temporal", "graph"}
    assert named["worker"] == named["scheduler"] == named["worker-compute"]
    assert _expand(extras, named["worker-compute-ml"]) >= {"ml", "temporal"}
    assert "ml" not in _expand(extras, named["worker-compute-py"])

    # Helm small: one image for every pod, Temporal in the API and the compute queue in the `all` worker,
    # so the overlay documents the standard image; no isolated pools.
    small_text = (ROOT / "deploy" / "helm" / "analystos" / "values-small.yaml").read_text(encoding="utf-8")
    small = yaml.safe_load(small_text)
    base = yaml.safe_load((ROOT / "deploy" / "helm" / "analystos" / "values.yaml").read_text(encoding="utf-8"))
    assert small["config"]["extra"]["ANALYSTOS_PROFILE"] == "standard"
    assert small["config"].get("orchestrator", base["config"]["orchestrator"]) == "temporal"
    assert "EXTRAS=standard" in small_text
    assert small["workers"]["all"]["queues"] == "all"
    assert small["workers"]["compute-ml"] is None and small["workers"]["compute-py"] is None


@pytest.mark.parametrize("name", ["ml", "reports", "temporal", "graph"])
def test_each_extra_is_reported_by_the_installation_summary(name):
    from analystos.core import profiles

    assert name in profiles.EXTRAS and name in _extras()

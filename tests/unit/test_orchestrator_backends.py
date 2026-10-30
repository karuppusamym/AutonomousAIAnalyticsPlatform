"""ORC-T1 / ORC-T4 (ADR-015): the orchestrator seam, packaging and guards, without services.

* callers ask a backend for a capability instead of comparing the name with "temporal";
* `langgraph` is never a profile default, needs its extra, and startup fails with the remedy without it;
* LangSmith tracing is refused unless an operator opts in (tests/conformance/orchestrator covers the guard);
* /api/health and the profile summary report the backend; with `ANALYSTOS_RUN_DRIVER=process` the API drives
  nothing and health wants a live `analystos driver`;
* no module but `workflows/langgraph_backend.py` imports LangGraph, LangChain or LangSmith."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from analystos.api import app as app_module
from analystos.core import profiles
from analystos.core.config import Settings
from analystos.core.errors import FeatureUnavailable
from analystos.workflows import backends
from analystos.workflows import orchestrator as orch

SRC = Path(__file__).resolve().parents[2] / "src" / "analystos"


def test_capabilities_replace_name_checks():
    t, loc, lg = (backends.capabilities(n) for n in ("temporal", "local", "langgraph"))
    assert (t.durable, t.needs_worker, t.parks, t.isolated_transport, t.remote_compute) == (True, True, False, "temporal", True)
    assert (loc.durable, loc.needs_worker, loc.parks, loc.isolated_transport, loc.extra) == (False, False, True, "subprocess", None)
    assert (lg.durable, lg.needs_worker, lg.parks, lg.isolated_transport, lg.extra) == (False, False, True, "subprocess", "langgraph")
    assert backends.capabilities(Settings(_env_file=None, orchestrator="langgraph")) is lg
    with pytest.raises(ValueError, match="unknown orchestrator"):
        backends.capabilities("airflow")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, orchestrator="airflow")


def test_no_caller_compares_the_orchestrator_name():
    """ADR-015: the ~8 `orchestrator == "temporal"` checks became capability reads; none may come back."""
    hits = []
    for path in SRC.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"orchestrator\s*[!=]=\s*[\"']", line):
                hits.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert hits == [] or all("config.py" in h for h in hits), hits


def test_only_the_backend_imports_langgraph_langchain_or_langsmith():
    """Mirrors the import-linter contract (pyproject [tool.importlinter]) for every langchain_* package too."""
    pattern = re.compile(r"^\s*(from|import)\s+(langgraph|langchain\w*|langsmith)\b", re.M)
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if pattern.search(p.read_text(encoding="utf-8")) and p.name != "langgraph_backend.py"]
    assert offenders == []


def test_langgraph_is_never_a_profile_default(monkeypatch):
    for var in ("ANALYSTOS_PROFILE", "ANALYSTOS_ORCHESTRATOR"):
        monkeypatch.delenv(var, raising=False)
    assert Settings(_env_file=None, profile="lite").orchestrator == "local"
    assert Settings(_env_file=None, profile="standard").orchestrator == "temporal"
    assert Settings(_env_file=None, profile="scale").orchestrator == "temporal"
    lg = Settings(_env_file=None, profile="standard", orchestrator="langgraph")
    assert lg.local_worker_count == 4 and lg.resume_local_runs  # it always fans out on a pool and resumes
    std = Settings(_env_file=None, profile="standard", orchestrator="local")
    assert std.local_worker_count is None and not std.resume_local_runs  # local in standard: unchanged


def test_a_missing_extra_stops_startup_with_the_remedy(monkeypatch):
    settings = Settings(_env_file=None, orchestrator="langgraph")
    monkeypatch.setattr(profiles, "missing_modules", lambda extra: ["langgraph"] if extra == "langgraph" else [])
    monkeypatch.setattr(orch, "get_settings", lambda: settings)
    with pytest.raises(FeatureUnavailable, match=r"analystos\[langgraph\]"):
        orch.start_local_runtime()
    with pytest.raises(FeatureUnavailable):
        orch.new_runtime(settings)
    summary = profiles.orchestrator_summary(settings)
    assert summary["backend"] == "langgraph" and summary["available"] is False and "langgraph" in summary["reason"]


def test_the_profile_summary_names_the_backend():
    s = profiles.summary(Settings(_env_file=None, profile="standard", orchestrator="temporal"))
    assert s["orchestrator"]["backend"] == "temporal" and s["orchestrator"]["durable"] is True
    assert "standard" in s["features"]
    lg = profiles.summary(Settings(_env_file=None, profile="standard", orchestrator="langgraph"))
    assert "standard" not in lg["features"]  # standard = the Temporal orchestrator and its workers
    assert lg["orchestrator"] == {"backend": "langgraph", "durable": False, "needs_worker": False, "parks": True,
                                  "run_driver": "api", "available": True, "reason": None}
    assert "langgraph" in lg["extras"]


def test_health_reports_the_langgraph_backend(monkeypatch):
    settings = Settings(_env_file=None, profile="lite", orchestrator="langgraph")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(orch, "get_settings", lambda: settings)
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["orchestrator"] == "langgraph"
    assert body["checks"]["orchestrator"]["state"] == "up" and body["checks"]["orchestrator"]["backend"] == "langgraph"
    assert body["checks"]["worker"]["state"] == "off" and "in the API process" in body["checks"]["worker"]["status"]
    assert body["installation"]["orchestrator"]["backend"] == "langgraph"


def test_health_reports_a_refused_backend_as_critical(monkeypatch):
    settings = Settings(_env_file=None, profile="lite", orchestrator="langgraph")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["orchestrator"]["state"] == "down" and "LANGSMITH_TRACING" in body["checks"]["orchestrator"]["error"]
    assert any(p["dependency"] == "orchestrator" and p["severity"] == "critical" for p in body["problems"])


def test_with_driver_processes_the_api_only_wakes_runs(monkeypatch):
    settings = Settings(_env_file=None, profile="lite", orchestrator="langgraph", run_driver="process")
    monkeypatch.setattr(orch, "get_settings", lambda: settings)
    woken = []
    from analystos.workflows import driver

    monkeypatch.setattr(driver, "wake", lambda run_id: woken.append(run_id))
    monkeypatch.setattr(orch, "local_runtime", lambda: pytest.fail("the API must not drive runs"))
    assert orch.start_run("r1") == "langgraph-r1"
    orch.signal_run("r1")
    assert woken == ["r1", "r1"]
    assert orch.start_local_runtime() is None  # no resume, no sweeper: drivers take expired leases over


def test_health_wants_a_live_driver_in_process_mode(monkeypatch):
    settings = Settings(_env_file=None, profile="lite", orchestrator="local", run_driver="process")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(orch, "get_settings", lambda: settings)
    from analystos.workflows import driver

    monkeypatch.setattr(driver, "live_drivers", lambda: [])
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["orchestrator"]["state"] == "down" and "analystos driver" in body["checks"]["orchestrator"]["error"]
    monkeypatch.setattr(driver, "live_drivers", lambda: [{"id": "d1", "runs": 2}])
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["orchestrator"]["state"] == "up" and body["checks"]["orchestrator"]["drivers"][0]["id"] == "d1"


def test_the_driver_command_refuses_the_temporal_backend(monkeypatch):
    from analystos.workflows import driver

    monkeypatch.setattr(driver, "get_settings", lambda: Settings(_env_file=None, orchestrator="temporal"))
    with pytest.raises(SystemExit, match="analystos worker"):
        driver.serve()


class _Store:
    """LeaseStore double: renewal keeps only what `kept` says."""

    def __init__(self) -> None:
        self.kept: set[str] = set()

    def renew(self, driver_id, held, lease_seconds):
        return {r for r in held if r in self.kept}


def test_a_lost_or_stale_lease_stops_the_drive():
    from analystos.workflows.driver import RunDriver, _Held

    now = [0.0]
    store = _Store()
    d = RunDriver(store=store, driver_id="d", lease_seconds=10, drive=lambda *a, **k: "COMPLETED", backend="local",
                  clock=lambda: now[0])
    d._held["r"] = _Held(1, d._valid_until(0.0))
    assert d.holds("r")
    now[0] = 7.9
    assert d.holds("r")
    now[0] = 8.1  # 80 % of the lease since the last renewal: stop before it can expire in the database
    assert not d.holds("r")
    store.kept = {"r"}
    d.renew()
    assert d.holds("r")  # a successful renewal extends the local deadline
    store.kept = set()
    d.renew()
    assert not d.holds("r")  # the lease was taken over: fenced at once

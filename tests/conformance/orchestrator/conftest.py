"""Fixtures of the orchestrator backend conformance suite (ADR-015, ORC-T2).

Every in-process backend (`local`, `langgraph`) runs the same behaviours against `ConformanceEngine`, an
in-memory engine that keeps the contract of `runtime/engine.py`: plan_run / get_state / execute_task /
finish_run with optimistic claims, approval gates, controls, replans, required-task failures and budget
limits. No services are needed. `temporal` runs the same scenarios against a Temporal dev server in
`tests/integration/test_orchestrator_conformance_temporal.py`; the decision table every driver must
agree on is `test_next_action_parity.py`.

`ANALYSTOS_CONFORMANCE_BACKENDS` narrows the backends (comma-separated; default: every installed one).
"""
from __future__ import annotations

import pytest
from tests.conformance.orchestrator.harness import OPS, ConformanceEngine, installed_backends

from analystos.workflows import orchestrator as orch


@pytest.fixture
def fake(monkeypatch):
    engine = ConformanceEngine()
    for name in OPS:
        monkeypatch.setattr(orch.engine, name, getattr(engine, name))
    from analystos.tenancy import background

    # Nodes bind the run's tenant with as_owner_of(analysis_run, id): resolve it from the fake rows.
    def owner(table: str, row_id: str):
        if table == "analysis_run" and row_id in engine.runs:
            return engine.runs[row_id]["tenant"]
        return background._MISSING

    monkeypatch.setattr(background, "owner_tenant", owner)
    return engine


@pytest.fixture(params=installed_backends())
def backend(request) -> str:
    return request.param


@pytest.fixture
def make_runtime(backend):
    """`make_runtime(workers, **kw)` builds the backend's runtime; every one is shut down afterwards."""
    made: list[orch.LocalRuntime] = []

    def make(workers: int | None = 2, **kw) -> orch.LocalRuntime:
        if backend == "langgraph":
            from analystos.workflows.langgraph_backend import LangGraphRuntime

            rt: orch.LocalRuntime = LangGraphRuntime(workers, **{"poll": 0.005, **kw})
        else:
            rt = orch.LocalRuntime(workers, **{"poll": 0.005, **kw})
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt._stop.set()
        rt.shutdown()

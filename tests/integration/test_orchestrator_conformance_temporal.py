"""ORC-T2 (ADR-015): the `temporal` leg of the orchestrator conformance suite.

The real AnalysisWorkflow runs on a Temporal server; its four engine activities are served over the same
in-memory `ConformanceEngine` the in-process backends use (`tests/conformance/orchestrator`), so the three
backends are held to one set of outcomes: done, fail, cancel, limited, approval wait + nudge, pause/resume.
Every test uses its own task queue. Skips when Temporal is unreachable (ANALYSTOS_TEMPORAL_ADDRESS)."""
from __future__ import annotations

import asyncio
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.conformance.orchestrator.harness import ConformanceEngine

pytestmark = pytest.mark.integration
temporalio = pytest.importorskip("temporalio")
from temporalio import activity  # noqa: E402

ENGINE = ConformanceEngine()


@activity.defn(name="plan_run")
def plan_run(run_id: str) -> dict:
    return ENGINE.plan_run(run_id)


@activity.defn(name="get_state")
def get_state(run_id: str) -> dict:
    return ENGINE.get_state(run_id)


@activity.defn(name="execute_task")
def execute_task(run_id: str, key: str) -> dict:
    return ENGINE.execute_task(run_id, key)


@activity.defn(name="finish_run")
def finish_run(run_id: str, outcome: str, error: str | None) -> None:
    ENGINE.finish_run(run_id, outcome, error)


def _address() -> str:
    from analystos.core.config import get_settings

    return get_settings().temporal_address


@pytest.fixture()
def temporal():
    host, port = _address().split(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
    except OSError as exc:
        pytest.skip(f"Temporal not reachable at {_address()}: {exc}")
    return _address()


def _opts(queue: str) -> dict:
    from analystos.workflows.queues import WORKLOADS, fallback_options

    opts = fallback_options("conformance")
    for w in WORKLOADS:  # one queue for every workload: the test worker serves them all
        opts["queues"][w] = {**opts["queues"][w], "name": queue}
    return opts


async def _drive(temporal, run_id: str, during=None) -> str:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from analystos.workflows.analysis_workflow import AnalysisWorkflow

    client = await Client.connect(temporal)
    queue = f"orc-conformance-{uuid.uuid4().hex[:8]}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        async with Worker(client, task_queue=queue, workflows=[AnalysisWorkflow],
                          activities=[plan_run, get_state, execute_task, finish_run], activity_executor=pool):
            handle = await client.start_workflow("AnalysisWorkflow", args=[run_id, _opts(queue)], id=f"orc-{run_id}",
                                                 task_queue=queue)
            if during is not None:
                await during(handle)
            return await asyncio.wait_for(handle.result(), timeout=60)


def _run(coro):
    return asyncio.run(coro)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_done(temporal):
    run = _id("done")
    ENGINE.add_run(run, {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
    assert _run(_drive(temporal, run)) == "COMPLETED"
    assert ENGINE.runs[run]["finished"] == [("COMPLETED", None)]


def test_fail(temporal):
    run = _id("fail")
    ENGINE.add_run(run, {"a": [], "b": ["a"]})
    ENGINE.hooks["a"] = lambda *a: {"fail": "policy refused"}
    try:
        assert _run(_drive(temporal, run)) == "FAILED"
    finally:
        ENGINE.hooks.pop("a")
    assert ENGINE.runs[run]["finished"] == [("FAILED", "required task(s) failed: a")]


def test_cancel(temporal):
    run = _id("cancel")
    ENGINE.add_run(run, {"a": []})
    ENGINE.runs[run]["control"] = "cancel"
    assert _run(_drive(temporal, run)) == "CANCELLED"


def test_limited(temporal):
    run = _id("limited")
    ENGINE.add_run(run, {"a": [], "b": ["a"], "c": ["b"]}, max_steps=2)
    assert _run(_drive(temporal, run)) == "COMPLETED_WITH_LIMITATIONS"


def test_approval_wait_and_nudge(temporal):
    run = _id("approval")
    ENGINE.add_run(run, {"plan": [], "gate": ["plan"], "publish": ["gate"]}, gate="gate")

    async def approve(handle):
        for _ in range(200):
            if ENGINE.status(run) == "WAITING_USER":
                break
            await asyncio.sleep(0.05)
        assert ENGINE.status(run) == "WAITING_USER"
        ENGINE.runs[run]["approved"] = True
        await handle.signal("nudge")  # without it the workflow would re-check only after 300 s

    assert _run(_drive(temporal, run, approve)) == "COMPLETED"


def test_pause_and_resume(temporal):
    run = _id("pause")
    ENGINE.add_run(run, {"a": [], "b": ["a"]})
    ENGINE.runs[run]["control"] = "pause"

    async def resume(handle):
        for _ in range(200):
            if ENGINE.status(run) == "PAUSED":
                break
            await asyncio.sleep(0.05)
        assert ENGINE.status(run) == "PAUSED" and not ENGINE.ops("execute_task", run)
        ENGINE.runs[run]["control"] = "run"
        await handle.signal("nudge")

    assert _run(_drive(temporal, run, resume)) == "COMPLETED"

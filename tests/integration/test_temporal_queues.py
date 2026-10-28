"""P4-S01 against the compose Temporal: task queues per workload, a process pool for statistics,
heartbeats, per-queue timeouts and continue-as-new.

The workflow is the real AnalysisWorkflow and the pools are the real `build_workers`; only the
activity bodies are probes (`tests/temporal_probe.py`). Every test uses its own queue prefix, so
the dev worker (`analystos-*` queues) never sees these tasks. Skips when Temporal is unreachable.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import time
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from tests import temporal_probe as probe

from analystos.workflows.queues import queue_options

pytestmark = pytest.mark.integration

# The compute pool runs activities in a process pool (spawned children, killed with SIGKILL on teardown): POSIX
# only, like the worker conformance suite. On Windows the pool child dies and the run never finishes.
process_pool = pytest.mark.skipif(sys.platform == "win32", reason="the compute pool is a POSIX process pool; run on Linux")

PROBE_ACTIVITIES = {
    "analysis": [probe.plan_run, probe.get_state, probe.finish_run, probe.execute_task, probe.queue_ping],
    "compute": [probe.execute_task, probe.queue_ping],
    "publish": [probe.execute_task, probe.queue_ping],
    "crawl": [probe.run_crawl, probe.queue_ping],
    "elt": [probe.queue_ping],
}
HEARTBEAT = 2  # compute heartbeat timeout under test (production: config/task_queues.yaml)


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


@pytest.fixture()
def probe_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AOS_PROBE_DIR", str(tmp_path))  # inherited by the spawned compute processes
    return tmp_path


def _specs():
    from analystos.workflows.queues import DEFAULT_SPECS

    specs = dict(DEFAULT_SPECS)
    specs["compute"] = replace(specs["compute"], start_to_close_seconds=60, heartbeat_seconds=HEARTBEAT, max_concurrent=6)
    return specs


def _kill(pids: set[int]) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


async def _with_pools(workloads, body, probe_dir):
    """Run the pools for `workloads` on a private prefix while `body(client, prefix)` runs."""
    from temporalio.client import Client

    from analystos.workflows.worker import build_workers

    prefix = f"aostest-s1-{uuid.uuid4().hex[:10]}"
    client = await Client.connect(_address())
    with contextlib.ExitStack() as stack:
        workers = build_workers(client, workloads, prefix=prefix, stack=stack, specs=_specs(), activities=PROBE_ACTIVITIES)
        running = [asyncio.create_task(w.run()) for w in workers]
        try:
            return await body(client, prefix)
        finally:
            await _stop_pools(workers, running, probe_dir)


async def _stop_pools(workers, running, probe_dir) -> None:
    """Bounded teardown. A hung pool process never returns: kill it (and any other pool process) first,
    or the pool and the worker wait on it forever; then give each step a deadline."""
    import multiprocessing

    _kill({c["pid"] for c in probe.calls(probe_dir) if c["key"] == "test:hang"}
          | {p.pid for p in multiprocessing.active_children() if p.name.startswith("SpawnProcess")})
    for w in workers:  # one at a time: shutting the pools down concurrently can stall on the broken pool
        with contextlib.suppress(Exception):
            await asyncio.wait_for(w.shutdown(), 30)
    for task in running:
        task.cancel()
    await asyncio.wait(running, timeout=30)


async def _histories(client, workflow_id: str, first_run_id: str) -> list:
    """Histories of every execution in a continue-as-new chain, oldest first."""
    out, run_id = [], first_run_id
    while run_id:
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        out.append(history)
        last = history.events[-1]
        run_id = (last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                  if last.HasField("workflow_execution_continued_as_new_event_attributes") else None)
    return out


def _scheduled(histories) -> dict[str, dict]:
    """execute_task key -> {queue, heartbeat, start_to_close} from ActivityTaskScheduled events."""
    out = {}
    for h in histories:
        for e in h.events:
            if not e.HasField("activity_task_scheduled_event_attributes"):
                continue
            a = e.activity_task_scheduled_event_attributes
            if a.activity_type.name != "execute_task":
                continue
            key = json.loads(a.input.payloads[1].data)
            out[key] = {"queue": a.task_queue.name, "heartbeat": a.heartbeat_timeout.ToSeconds(),
                        "start_to_close": a.start_to_close_timeout.ToSeconds()}
    return out


@process_pool
def test_steps_run_on_their_workload_queue_and_long_runs_continue_as_new(temporal, probe_dir):
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    probe.SCRIPTS[run_id] = [["context", "profile", "quality"], ["test:h1", "test:h2", "publish_request"],
                             ["publish"], ["insights", "finalize"]]

    async def body(client, prefix):
        opts = queue_options(prefix, _specs(), 5)  # continue-as-new after 5 activities
        handle = await client.start_workflow("AnalysisWorkflow", args=[run_id, opts], id=f"{prefix}-{run_id}",
                                             task_queue=f"{prefix}-analysis")
        assert await asyncio.wait_for(handle.result(), 120) == "COMPLETED"
        return prefix, await _histories(client, handle.id, handle.first_execution_run_id)

    prefix, histories = asyncio.run(_with_pools(["analysis", "compute", "publish"], body, probe_dir))
    assert probe.FINISHED[run_id] == "COMPLETED"
    assert len(histories) >= 2, "the run should have continued as new"
    expected = {"context": "analysis", "profile": "compute", "quality": "compute", "test:h1": "compute",
                "test:h2": "compute", "publish_request": "publish", "publish": "publish", "insights": "analysis",
                "finalize": "analysis"}
    scheduled = _scheduled(histories)
    assert {k: v["queue"] for k, v in scheduled.items()} == {k: f"{prefix}-{w}" for k, w in expected.items()}
    assert scheduled["test:h1"]["heartbeat"] == HEARTBEAT and scheduled["test:h1"]["start_to_close"] == 60
    assert scheduled["context"]["heartbeat"] == 120 and scheduled["publish"]["start_to_close"] == 900
    ran = {c["key"]: c for c in probe.calls(probe_dir)}
    assert {k: c["queue"] for k, c in ran.items()} == {k: f"{prefix}-{w}" for k, w in expected.items()}
    # compute steps ran in the process pool, the rest in this process's thread pools
    assert all(ran[k]["pid"] != os.getpid() for k, w in expected.items() if w == "compute")
    assert all(ran[k]["pid"] == os.getpid() for k, w in expected.items() if w != "compute")


@process_pool
def test_a_hung_statistics_task_is_detected_within_its_heartbeat_window(temporal, probe_dir, monkeypatch):
    """test:hang freezes its pool process in GIL-holding native code; test:slow runs longer than the
    heartbeat timeout but keeps heartbeating. Only the hung one may time out, and it must be caught
    by the heartbeat timeout (seconds), not start_to_close (60 s here, 10 min in production)."""
    monkeypatch.setenv("AOS_PROBE_SLOW_SECONDS", str(HEARTBEAT * 3))
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    probe.SCRIPTS[run_id] = [["test:slow", "test:hang"]]

    async def body(client, prefix):
        started = time.time()
        handle = await client.start_workflow("AnalysisWorkflow", args=[run_id, queue_options(prefix, _specs(), 400)],
                                             id=f"{prefix}-{run_id}", task_queue=f"{prefix}-analysis")
        assert await asyncio.wait_for(handle.result(), 90) == "COMPLETED"
        return started, await handle.fetch_history()

    started, history = asyncio.run(_with_pools(["analysis", "compute"], body, probe_dir))
    calls = probe.calls(probe_dir)
    slow = [c for c in calls if c["key"] == "test:slow"]
    hang = sorted((c for c in calls if c["key"] == "test:hang"), key=lambda c: c["attempt"])
    assert [c["attempt"] for c in slow] == [1], "a heartbeating task running past the heartbeat timeout must not be retried"
    assert [c["attempt"] for c in hang] == [1, 2, 3]
    assert all(c["pid"] != os.getpid() for c in hang)
    # Attempt n+1 starts after attempt n is declared hung plus the retry backoff (2 s, then 4 s).
    detected = [hang[i + 1]["at"] - hang[i]["at"] - backoff for i, backoff in enumerate((2, 4))]
    assert all(d < HEARTBEAT + 3 for d in detected), detected
    timed_out = [e.activity_task_timed_out_event_attributes for e in history.events
                 if e.HasField("activity_task_timed_out_event_attributes")]
    assert len(timed_out) == 1
    from temporalio.api.enums.v1 import TimeoutType

    assert timed_out[0].failure.timeout_failure_info.timeout_type == TimeoutType.TIMEOUT_TYPE_HEARTBEAT
    assert time.time() - started < 60


def test_crawls_run_on_the_crawl_pool(temporal, probe_dir):
    async def body(client, prefix):
        result = await client.execute_workflow("CrawlWorkflow", args=["crw_probe", "usr_probe", queue_options(prefix, _specs(), 400)],
                                               id=f"{prefix}-crawl", task_queue=f"{prefix}-crawl",
                                               execution_timeout=timedelta(seconds=60))
        return prefix, result

    prefix, result = asyncio.run(_with_pools(["analysis", "crawl", "elt"], body, probe_dir))
    assert result == {"crawl_id": "crw_probe", "queue": f"{prefix}-crawl"}


def test_a_retry_after_a_timed_out_attempt_releases_the_task_claim(control_db):
    from sqlalchemy import select

    from analystos.core.ids import new_id, utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunTask, User, Workspace
    from analystos.runtime import engine
    from analystos.workflows.activities import release_timed_out_claim

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws = Workspace(id=new_id("ws"), name="claims", created_by=admin.id)
        s.add(ws)
        s.flush()
        run = AnalysisRun(id=new_id("run"), workspace_id=ws.id, objective="claims", requested_by=admin.id, status="RUNNING",
                          plan_version=1)
        s.add(run)
        s.flush()
        s.add(RunTask(id=new_id("tsk"), run_id=run.id, key="test:h1", agent_id="data_scientist", title="t", depends_on=[],
                      input={}, plan_version=1, seq=0, status="RUNNING", attempts=1, started_at=utcnow()))
        run_id = run.id
    # A fresh claim held by a dead attempt: the engine alone reports it in progress until the claim TTL.
    assert engine.execute_task(run_id, "test:h1") == {"status": "in_progress"}
    assert release_timed_out_claim(run_id, "test:h1", 2) is True
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "test:h1"))
        assert task.status == "RUNNING" and task.started_at is None  # the retry's execute_task retakes it
    assert release_timed_out_claim(run_id, "missing", 2) is False


@pytest.fixture()
def servicenow_url():
    import threading

    import uvicorn

    from analystos.connectors.servicenow_mock import app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@process_pool
def test_full_run_on_temporal_worker_pools(temporal, control_db, servicenow_url, probe_dir, monkeypatch):
    """The real engine through the real pools: the run starts on Temporal, its statistics steps run in
    the compute process pool (spawned processes with their own DB connections), publication on the
    publish pool, and it completes with verified findings."""
    import threading

    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router
    from analystos.workflows import orchestrator
    from analystos.workflows.queues import load_config

    prefix = f"aostest-s1-{uuid.uuid4().hex[:10]}"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ANALYSTOS_ORCHESTRATOR", "temporal")
    monkeypatch.setenv("ANALYSTOS_TEMPORAL_QUEUE_PREFIX", prefix)
    get_settings.cache_clear()
    default_router.cache_clear()
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()

    async def pools():
        from temporalio.client import Client

        from analystos.workflows.worker import build_workers

        client = await Client.connect(_address())
        with contextlib.ExitStack() as stack:
            workers = build_workers(client, ["analysis", "compute", "publish"], prefix=prefix, stack=stack, specs=load_config()[0])
            running = [asyncio.create_task(w.run()) for w in workers]
            await stop.wait()
            await _stop_pools(workers, running, probe_dir)

    thread = threading.Thread(target=loop.run_until_complete, args=(pools(),), daemon=True)
    thread.start()
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import AnalysisRun, Approval, Insight, User
        from analystos.governance.approvals import decide
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="temporal pools", objective="Find the drivers of SLA breaches in IT incidents",
                                  policy={"require_approved_metrics": False})  # the gate: test_semantic_layer.py
            s.flush()
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident"])
        run = create_run(admin, ws_id, objective=None)

        def wait(statuses: set[str], timeout: float = 600) -> str:
            started = time.time()
            while time.time() - started < timeout:
                with session_scope() as s:
                    status = s.get(AnalysisRun, run.id).status
                if status in statuses:
                    return status
                time.sleep(0.5)
            raise AssertionError(f"run did not reach {statuses}")

        assert wait({"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
        with session_scope() as s:
            approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            decide(s, approval.id, approver, approve=True)
        orchestrator.signal_run(run.id)
        assert wait({"FAILED", "COMPLETED"}) == "COMPLETED"
        with session_scope() as s:
            assert s.scalar(select(Insight.id).where(Insight.run_id == run.id, Insight.status == "verified").limit(1))

        async def histories():
            from temporalio.client import Client

            client = await Client.connect(_address())
            handle = client.get_workflow_handle(orchestrator.workflow_id(run.id))
            first = (await handle.describe()).raw_description.workflow_execution_info.first_run_id
            return await _histories(client, handle.id, first or (await handle.describe()).run_id)

        used = {k: v["queue"].removeprefix(f"{prefix}-") for k, v in _scheduled(asyncio.run(histories())).items()}
        assert used["profile"] == "compute" and used["publish"] == "publish"
        assert used["context"] == "analysis" and used["verify"] == "analysis"
        assert [k for k in used if k.startswith("test:")], used
        assert all(q == "compute" for k, q in used.items() if k.startswith("test:"))
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(60)
        get_settings.cache_clear()
        default_router.cache_clear()

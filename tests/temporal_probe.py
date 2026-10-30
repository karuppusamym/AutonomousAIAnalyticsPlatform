"""Probe activities for the Temporal task-queue tests (P4-S01). Module level so the compute pool's
spawned processes can import them. They stand in for the engine: the workflow under test is the real
AnalysisWorkflow, the workers are the real `build_workers` pools, only the activity bodies are fakes.

Behaviour per task key:
  test:slow  heartbeats through `heartbeat_pump` for longer than the heartbeat timeout: must complete.
  test:hang  a statistic hung in native code that holds the GIL (catastrophic regex backtracking):
             the pool process freezes, heartbeats stop, Temporal must time the attempt out.
  other      returns at once.
Each call writes `{key, attempt, queue, pid, at}` to its own $AOS_PROBE_DIR/call-<pid>-<id>.json.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from temporalio import activity

from analystos.workflows.activities import heartbeat_pump

_lock = threading.Lock()
SCRIPTS: dict[str, list[list[str]]] = {}  # run_id -> ready batches, consumed by get_state (analysis pool, in-process)
FINISHED: dict[str, str] = {}


def _record(key: str) -> None:
    info = activity.info()
    line = json.dumps({"key": key, "attempt": info.attempt, "queue": info.task_queue, "pid": os.getpid(), "at": time.time()})
    # One file per call: appends to one shared file from several pool processes are not atomic on Windows.
    (Path(os.environ["AOS_PROBE_DIR"]) / f"call-{os.getpid()}-{uuid.uuid4().hex}.json").write_text(line, encoding="utf-8")


def calls(probe_dir: Path) -> list[dict]:
    return sorted((json.loads(p.read_text(encoding="utf-8")) for p in probe_dir.glob("call-*.json")), key=lambda c: c["at"])


@activity.defn(name="plan_run")
def plan_run(run_id: str) -> dict:
    return {"plan_version": 1}


@activity.defn(name="get_state")
def get_state(run_id: str) -> dict:
    with _lock:
        script = SCRIPTS[run_id]
        if not script:
            return {"done": True}
        return {"ready": script.pop(0)}


@activity.defn(name="finish_run")
def finish_run(run_id: str, outcome: str, error: str | None) -> None:
    FINISHED[run_id] = outcome


@activity.defn(name="execute_task")
def execute_task(run_id: str, key: str) -> dict:
    _record(key)
    if key == "test:slow":
        with heartbeat_pump():
            time.sleep(float(os.environ.get("AOS_PROBE_SLOW_SECONDS", "5")))
    elif key == "test:hang":
        with heartbeat_pump():
            re.match(r"(a+)+$", "a" * 48 + "!")  # ~2^48 backtracking steps inside _sre, GIL held throughout
    return {"status": "COMPLETED", "queue": activity.info().task_queue}


@activity.defn(name="run_crawl")
def run_crawl(crawl_id: str, user_id: str) -> dict:
    _record(f"crawl:{crawl_id}")
    return {"crawl_id": crawl_id, "queue": activity.info().task_queue}


@activity.defn(name="queue_ping")
def queue_ping() -> str:
    return activity.info().task_queue

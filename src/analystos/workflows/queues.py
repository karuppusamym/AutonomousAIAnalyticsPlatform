"""Temporal task queues per workload (spec v3 §8, P4-S01).

The one place that says which run step runs on which queue, and how long an attempt may take there.
The workflow gets its queue table as input (`workflow_options`), so a config change never alters
the replay of a workflow already in flight; `FALLBACK_OPTIONS` covers workflows started without it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `query` (S02-T1) is worker:query: the only pool that opens source connections when ANALYSTOS_QUERY_TRANSPORT=temporal.
WORKLOADS = ("analysis", "compute", "publish", "crawl", "elt", "query")
# Isolated pools (ADR-0022, P7-06): no database or provider credentials, egress to the artifact store only.
# Never part of `all`: a worker that serves them runs `analystos.workers.main`, not the engine activities.
ISOLATED_POOLS = ("compute-py", "compute-ml")

# step key (or dynamic prefix) -> workload. Statistics and hypothesis tests are CPU-bound; publishing
# talks to external BI systems and is rate-limited separately; everything else is I/O-bound analysis.
STEP_WORKLOADS: dict[str, str] = {
    "profile": "compute",
    "quality": "compute",
    "test:": "compute",
    "publish_request": "publish",
    "publish": "publish",
    # elt_build.v1 (P4-E04): dbt parse/estimate and the approved dbt build run on the elt pool
    "elt_plan": "elt",
    "elt_run": "elt",
}


def workload_for_step(key: str) -> str:
    """Workload of a run step; dynamic keys (`test:<hypothesis>`) match by prefix."""
    if key in STEP_WORKLOADS:
        return STEP_WORKLOADS[key]
    for prefix, workload in STEP_WORKLOADS.items():
        if prefix.endswith(":") and key.startswith(prefix):
            return workload
    return "analysis"


@dataclass(frozen=True)
class QueueSpec:
    start_to_close_seconds: int
    heartbeat_seconds: int
    max_concurrent: int
    executor: str = "thread"  # thread | process
    max_tasks_per_child: int | None = None
    isolated: bool = False
    budget: dict[str, Any] | None = None  # isolated pools: the default TaskEnvelope budget


DEFAULT_SPECS: dict[str, QueueSpec] = {
    "analysis": QueueSpec(1800, 120, 8),
    "compute": QueueSpec(600, 60, 4, "process", 50),
    "publish": QueueSpec(900, 120, 4),
    "crawl": QueueSpec(1800, 120, 2),
    "elt": QueueSpec(3600, 120, 2),
    "query": QueueSpec(600, 30, 16),
    "compute-py": QueueSpec(1800, 60, 2, isolated=True, budget={"cpu_seconds": 600, "memory_mb": 2048, "wall_seconds": 900,
                                                               "max_output_bytes": 256 * 1024 * 1024}),
    "compute-ml": QueueSpec(3600, 60, 2, isolated=True, budget={"cpu_seconds": 1800, "memory_mb": 4096,
                                                               "wall_seconds": 1800, "max_output_bytes": 512 * 1024 * 1024,
                                                               "max_trials": 50}),
}
DEFAULT_MAX_ACTIVITIES = 400


def queue_name(prefix: str, workload: str) -> str:
    return f"{prefix}-{workload}"


def load_config(path: Path | None = None) -> tuple[dict[str, QueueSpec], int]:
    """Queue specs from `config/task_queues.yaml`, over the defaults above."""
    import yaml

    from analystos.core.config import DATA_ROOT

    path = path or DATA_ROOT / "config" / "task_queues.yaml"
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    specs = dict(DEFAULT_SPECS)
    for name, cfg in (raw.get("queues") or {}).items():
        if name not in WORKLOADS + ISOLATED_POOLS:
            raise ValueError(f"unknown workload '{name}' in {path}; expected one of {WORKLOADS + ISOLATED_POOLS}")
        base = specs[name]
        specs[name] = QueueSpec(**{**base.__dict__, **cfg})
        if specs[name].isolated != (name in ISOLATED_POOLS):
            raise ValueError(f"queue {name}: only {', '.join(ISOLATED_POOLS)} are isolated pools (isolated: true)")
    for name, spec in specs.items():
        if spec.executor not in ("thread", "process"):
            raise ValueError(f"queue {name}: executor must be thread or process")
        if spec.heartbeat_seconds >= spec.start_to_close_seconds:
            raise ValueError(f"queue {name}: heartbeat_seconds must be below start_to_close_seconds")
    return specs, int(raw.get("max_activities_per_run", DEFAULT_MAX_ACTIVITIES))


def queue_options(prefix: str, specs: dict[str, QueueSpec], max_activities: int) -> dict[str, Any]:
    return {"queues": {w: {"name": queue_name(prefix, w), "start_to_close": s.start_to_close_seconds,
                           "heartbeat": s.heartbeat_seconds} for w, s in specs.items()},
            "max_activities": max_activities}


def workflow_options(prefix: str | None = None) -> dict[str, Any]:
    """The queue table passed to AnalysisWorkflow when a run is started (JSON-serialisable)."""
    from analystos.core.config import get_settings

    specs, max_activities = load_config()
    return queue_options(prefix or get_settings().temporal_queue_prefix, specs, max_activities)


def fallback_options(prefix: str) -> dict[str, Any]:
    """Pure (no I/O): used inside the workflow sandbox when a run was started without options."""
    return queue_options(prefix, DEFAULT_SPECS, DEFAULT_MAX_ACTIVITIES)


def parse_workloads(value: str | None) -> list[str]:
    """`analysis,compute` -> ['analysis', 'compute']; empty or `all` -> every workload (never an isolated
    pool: those are named explicitly and served alone, `compute-py,compute-ml`)."""
    if not value or value.strip() in ("all", "*"):
        return list(WORKLOADS)
    out = [w.strip() for w in value.split(",") if w.strip()]
    unknown = [w for w in out if w not in WORKLOADS + ISOLATED_POOLS]
    if unknown:
        raise ValueError(f"unknown queue(s) {unknown}; expected any of {', '.join(WORKLOADS + ISOLATED_POOLS)}")
    isolated = [w for w in out if w in ISOLATED_POOLS]
    if isolated and len(isolated) != len(out):
        raise ValueError(f"isolated pools ({', '.join(isolated)}) run in their own worker without credentials; "
                         "do not combine them with other queues")
    return list(dict.fromkeys(out))

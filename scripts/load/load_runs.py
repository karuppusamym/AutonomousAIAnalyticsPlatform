"""P4-S05 load: N concurrent investigate runs on Temporal (spec v3 §8 target: 50 concurrent runs on a
four-node worker deployment).

    PYTHONPATH=src python scripts/load/load_runs.py [--runs 50] [--workers 4] [--deadline 420] [--suffix s05r] [--keep]

On an isolated plane (scripts/load/harness.py) it stages the ServiceNow mock's `incident` table once,
starts `--workers` worker processes (each serving every queue, as one "node"), runs one investigation
alone as the baseline, then starts `--runs` investigations at once through `services.runs.create_run`
(the path the API, schedules and alerts share; publication skipped so a run ends without an approval
wait) and follows them to a terminal state or the deadline. No model is called: every purpose runs
on its deterministic path. Measured: submit latency, time to complete (p50/p95), completions and
failures, peak concurrently RUNNING, throughput, machine load, worker RSS/CPU, Postgres connections.
Runs still going at the deadline are cancelled and reported as not finished — never as passed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED"}
POOL_ENV = ("ANALYSTOS_DB_", "ANALYSTOS_ANALYTICS_POOL_", "ANALYSTOS_ANALYTICS_MAX_OVERFLOW", "ANALYSTOS_LOADER_")
OBJECTIVE = "Find the drivers of SLA breaches in IT incidents"


def setup_world(snow_url: str):
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    get_settings.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="load runs", objective=OBJECTIVE, autonomy_level=3)
        s.flush()
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": snow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    t0 = time.time()
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    return admin, ws_id, round(time.time() - t0, 1)


def start(admin, ws_id: str) -> tuple[str, float]:
    from analystos.services.runs import create_run

    t0 = time.perf_counter()
    run = create_run(admin, ws_id, objective=OBJECTIVE, autonomy_level=3, origin={"type": "load", "publish": "skip"})
    return run.id, time.perf_counter() - t0


def statuses(run_ids: list[str]) -> dict[str, tuple[str, datetime | None, datetime | None, datetime, str | None]]:
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    with session_scope() as s:
        rows = s.execute(select(AnalysisRun.id, AnalysisRun.status, AnalysisRun.started_at, AnalysisRun.finished_at,
                                AnalysisRun.created_at, AnalysisRun.error).where(AnalysisRun.id.in_(run_ids))).all()
    return {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in rows}


def follow(run_ids: list[str], deadline: float, on_tick=None) -> tuple[dict, int, list[tuple[float, int, int, int]]]:
    """Poll until every run is terminal or the deadline passes. Returns final statuses, the peak number
    of RUNNING runs and a (t, running, done, waiting) timeline."""
    peak, timeline = 0, []
    t0 = time.time()
    while True:
        try:
            st = statuses(run_ids)
        except Exception as exc:  # the observer shares the pooler: a refused poll is a sample lost, not the end
            print(f"  status poll failed: {type(exc).__name__}", flush=True)
            if time.time() > deadline:
                raise
            time.sleep(2.0)
            continue
        running = sum(1 for v in st.values() if v[0] == "RUNNING")
        done = sum(1 for v in st.values() if v[0] in TERMINAL)
        waiting = sum(1 for v in st.values() if v[0] == "WAITING_USER")
        peak = max(peak, running)
        timeline.append((round(time.time() - t0, 1), running, done, waiting))
        if on_tick:
            on_tick(running, done)
        if done + waiting == len(run_ids) or time.time() > deadline:
            return st, peak, timeline
        time.sleep(1.0)


def cancel(run_ids: list[str]) -> None:
    from sqlalchemy import update

    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    with session_scope() as s:
        s.execute(update(AnalysisRun).where(AnalysisRun.id.in_(run_ids), AnalysisRun.status.notin_(TERMINAL))
                  .values(control="cancel"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4, help="worker processes, each serving every queue (one per 'node')")
    ap.add_argument("--deadline", type=float, default=420, help="seconds to wait for the concurrent batch")
    ap.add_argument("--suffix", default="s05r")
    ap.add_argument("--keep", action="store_true", help="keep the plane (databases, roles) afterwards")
    args = ap.parse_args()

    plane = harness.configure(args.suffix)
    import os
    import signal

    # A SIGTERM (e.g. `timeout`) unwinds through `finally`: workers and their pools stopped, plane dropped.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    os.environ["ANALYSTOS_ORCHESTRATOR"] = "temporal"
    plane.env["ANALYSTOS_ORCHESTRATOR"] = "temporal"
    started_at = datetime.now(UTC)
    harness.provision(plane)
    snow, snow_url = harness.start_mock_servicenow()
    workers: list[subprocess.Popen] = []
    result: dict = {"started_at": started_at.isoformat(), "target": "50 concurrent runs on a four-node worker deployment",
                    "runs": args.runs, "workers": args.workers, "deadline_s": args.deadline, "task_queue_prefix": plane.queue_prefix,
                    "pooling": {"app_hostport": harness.APP_HOSTPORT, "direct_hostport": harness.PG_HOSTPORT,
                                "settings": {k: v for k, v in sorted(os.environ.items()) if k.startswith(POOL_ENV)}}}
    try:
        admin, ws_id, staging_s = setup_world(snow_url)
        result["staging_seconds"] = staging_s
        logs = []
        for i in range(args.workers):
            logs.append(tempfile.NamedTemporaryFile(prefix=f"aosload-worker{i}-", suffix=".log", delete=False))  # noqa: SIM115 (the worker writes it)
            workers.append(harness.spawn([sys.executable, "-m", "analystos.cli", "worker"], plane,
                                         stdout=logs[-1], stderr=subprocess.STDOUT))
        result["worker_logs"] = [f.name for f in logs]
        time.sleep(5)
        if any(w.poll() is not None for w in workers):
            raise SystemExit("a worker exited during start-up: " + Path(logs[0].name).read_text()[-2000:])

        # Baseline: one run alone on the same workers.
        base_id, base_submit = start(admin, ws_id)
        st, _, _ = follow([base_id], time.time() + 300)
        s, started, finished, created, error = st[base_id]
        result["baseline"] = {"status": s, "submit_ms": round(base_submit * 1000, 1), "error": error,
                              "seconds": round((finished - created).total_seconds(), 1) if finished else None}
        print(f"baseline run: {result['baseline']}", flush=True)

        # The concurrent batch.
        with harness.Sampler([w.pid for w in workers], every=1.0, database=plane.control_db) as sampler:
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=args.runs) as pool:
                submitted = list(pool.map(lambda _: _try_start(admin, ws_id), range(args.runs)))
            submit_wall = time.time() - t0
            run_ids = [r for r, _, e in submitted if r]
            submit_errors = [e for _, _, e in submitted if e]

            def tick(running, done):
                print(f"  t+{time.time() - t0:5.0f}s running={running:3d} done={done:3d}", flush=True)
            st, peak, timeline = follow(run_ids, t0 + args.deadline, tick)
            wall = time.time() - t0
        unfinished = [r for r, v in st.items() if v[0] not in TERMINAL]
        if unfinished:
            cancel(unfinished)
        durations = [(v[2] - v[3]).total_seconds() for v in st.values() if v[0] == "COMPLETED" and v[2]]
        queue_waits = [(v[1] - v[3]).total_seconds() for v in st.values() if v[1]]
        completed = sum(1 for v in st.values() if v[0] == "COMPLETED")
        result.update({
            "submitted": len(run_ids), "submit_errors": sorted(set(submit_errors))[:5],
            "submit_latency_ms": harness.summary([t for _, t, e in submitted if not e]), "submit_wall_s": round(submit_wall, 1),
            "completed": completed, "failed": sum(1 for v in st.values() if v[0] == "FAILED"),
            "waiting_user": sum(1 for v in st.values() if v[0] == "WAITING_USER"), "unfinished_at_deadline": len(unfinished),
            "errors": sorted({(v[4] or "")[:200] for v in st.values() if v[0] == "FAILED"})[:5],
            "run_seconds": harness.summary(durations, scale=1, digits=1),
            "start_delay_seconds": harness.summary(queue_waits, scale=1, digits=1),
            "peak_running": peak, "batch_wall_seconds": round(wall, 1),
            "throughput_runs_per_min": round(completed / (wall / 60), 2) if wall else None,
            "resources": sampler.report(),
            "timeline": timeline[:: max(1, len(timeline) // 60)],
        })
        result["checks"] = {
            "all_runs_submitted": len(run_ids) == args.runs and not submit_errors,
            "all_runs_completed": completed == args.runs,
            f"{args.runs}_running_at_once": peak >= args.runs,
            "no_failures": result["failed"] == 0,
        }
    finally:
        harness.stop(workers)
        snow.should_exit = True
        result["worker_log_signals"] = log_signals(result.get("worker_logs", []))
        if not args.keep:
            from analystos.db.base import get_engine
            from analystos.gateway.engines import dispose_all

            dispose_all()
            get_engine().dispose()
            harness.teardown(plane)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    path = harness.write_json(f"load-runs-{stamp}.json", result)
    print(f"wrote {path}")
    return 0


SIGNALS = {"pg_too_many_clients": "too many clients already", "pg_connection_failed": "connection failed",
           "activity_failed_after_retries": "task activity failed after retries", "task_crashed": "crashed\\nTraceback",
           "heartbeat_timeout": "heartbeat timeout"}


def log_signals(paths: list[str]) -> dict[str, int]:
    """Resource limits the workers hit, counted from their logs (a failed run's cause, not a guess)."""
    counts = dict.fromkeys(SIGNALS, 0)
    for p in paths:
        try:
            text = Path(p).read_text(errors="replace")
        except OSError:
            continue
        for key, needle in SIGNALS.items():
            counts[key] += text.count(needle)
    return counts


def _try_start(admin, ws_id: str):
    try:
        run_id, t = start(admin, ws_id)
        return run_id, t, None
    except Exception as exc:  # recorded: one refused submission must not hide the rest
        return None, 0.0, f"{type(exc).__name__}: {exc}"[:300]


if __name__ == "__main__":
    sys.exit(main())

"""A run-driver process for tests/integration/test_run_driver_leases.py (ORC-T7).

`python -m tests.driver_probe <log_dir> <name> <lease_seconds>` runs the real `RunDriver` and `LeaseStore`
against ANALYSTOS_DATABASE_URL; only the drive itself is a probe: it logs `start`, then advances the run one
step every 0.1 s (logging each, so overlapping drives would show) and marks the run COMPLETED. A run whose
objective starts with `slow` takes 200 steps, so the test can kill its driver mid-drive. Each process writes
its own JSON-lines log."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


def main() -> None:
    log_dir, name, lease = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
    log_path = log_dir / f"{name}.jsonl"
    lock = threading.Lock()

    def log(event: str, run_id: str, **extra) -> None:
        line = json.dumps({"driver": name, "pid": os.getpid(), "event": event, "run": run_id, "t": time.time(), **extra})
        with lock, log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    from sqlalchemy import text

    from analystos.db.base import session_scope
    from analystos.workflows.driver import RunDriver

    def drive(run_id: str, *, park: bool, keep_going) -> str:
        with session_scope() as s:
            objective = s.execute(text("SELECT objective FROM analysis_run WHERE id = :id"), {"id": run_id}).scalar()
        log("start", run_id)
        steps = 200 if objective.startswith("slow") else 5
        for i in range(steps):
            if not keep_going():
                log("lost", run_id, step=i)
                return "LEASE_LOST"
            log("step", run_id, step=i)
            time.sleep(0.1)
        with session_scope() as s:
            s.execute(text("UPDATE analysis_run SET status = 'COMPLETED' WHERE id = :id"), {"id": run_id})
        log("end", run_id)
        return "COMPLETED"

    driver = RunDriver(drive=drive, driver_id=f"probe-{name}", lease_seconds=lease, max_runs=4, poll=0.1,
                       sweep_seconds=0, backend="local")
    stop = threading.Event()
    (log_dir / f"{name}.ready").write_text(str(os.getpid()), encoding="utf-8")
    driver.run_forever(stop)


if __name__ == "__main__":
    main()

"""Live restart drill for an in-process orchestrator backend (ORC-T6, ADR-015): kill the API mid-run, start it
again, and check the run resumes from Postgres and completes.

    python scripts/e2e_orchestrator_restart.py --workspace WS [--port 28001] [--out docs/60-delivery/evidence]

The script starts the API itself (`python -m uvicorn analystos.api.app:app --port PORT`, with this process's
environment, so set ANALYSTOS_ORCHESTRATOR=langgraph or local and the database first), so no other API may drive
runs on the same database while it runs. In the given workspace (one `scripts/e2e_demo.py` prepared: a ServiceNow
source with incident and change_request selected, members analyst and approver) it:

1. starts a run as the analyst and waits until a step is RUNNING;
2. kills the API process tree hard (no shutdown hooks: what a crash or an OOM kill does);
3. starts a new API process: startup `resume()` releases the dead process's claims and re-drives the run;
4. approves the publication as the approver when it is requested, and waits for a terminal status;
5. writes orc-restart-<timestamp>.md + .json: the timeline, the steps whose claim was released, attempts, the
   backend reported by /api/health before and after, and the final status.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_demo import OBJECTIVE  # noqa: E402  (the workspace e2e_demo prepared holds the definitions it needs)

PASSWORD = os.getenv("ANALYSTOS_DEMO_PASSWORD", "ChangeMe123!")


def login(base: str, email: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=120)
    r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


def start_api(port: int, log: Path) -> subprocess.Popen:
    windows = sys.platform == "win32"
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "analystos.api.app:app", "--port", str(port)],
                            stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
                            start_new_session=not windows)  # its own group: the kill below never reaches this script
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/api/health", timeout=10).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise SystemExit(f"the API exited ({proc.returncode}); see {log}")
        time.sleep(1)
    raise SystemExit("the API did not become healthy")


def _retaken(step: dict, before: dict) -> bool:
    return (step["claim_version"] or 0) > (before[step["key"]]["claim_version"] or 0)


def kill_tree(proc: subprocess.Popen) -> None:
    """A hard kill of the process and its children: no lifespan shutdown, no drained tasks."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        import signal

        os.killpg(proc.pid, signal.SIGKILL)  # the API leads its own session (start_api)
    proc.wait(30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--port", type=int, default=28001)
    ap.add_argument("--out", default="docs/60-delivery/evidence")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()
    base, ws = f"http://127.0.0.1:{args.port}", args.workspace
    started = datetime.now(UTC)
    logs = Path("var") / "orc-restart"
    logs.mkdir(parents=True, exist_ok=True)
    timeline: list[dict] = []

    def mark(event: str, **kw) -> None:
        timeline.append({"t": round(time.time() - started.timestamp(), 1), "event": event, **kw})
        print(f"[{timeline[-1]['t']:>6}s] {event} {kw if kw else ''}", flush=True)

    api = start_api(args.port, logs / "api-before.log")
    health_before = httpx.get(f"{base}/api/health", timeout=30).json()
    mark("api started", orchestrator=health_before["orchestrator"])
    analyst, approver = login(base, "analyst@analystos.local"), login(base, "approver@analystos.local")
    rid = analyst.post(f"/api/workspaces/{ws}/analysis", json={"objective": OBJECTIVE}).json()["id"]
    mark("run started", run=rid)

    def detail() -> dict:
        r = analyst.get(f"/api/workspaces/{ws}/analysis/{rid}")
        r.raise_for_status()
        return r.json()

    deadline = time.monotonic() + 600
    running: list[str] = []
    while time.monotonic() < deadline:
        d = detail()
        # a step past the first ones, so the kill lands inside real work, not the planner
        running = [t["key"] for t in d["tasks"] if t["status"] == "RUNNING"]
        done = sum(1 for t in d["tasks"] if t["status"] == "COMPLETED")
        if running and done >= 2:
            break
        if d["status"] in ("FAILED", "CANCELLED", "COMPLETED"):
            raise SystemExit(f"the run ended {d['status']} before the kill")
        time.sleep(0.5)
    else:
        raise SystemExit("no step was RUNNING within 10 minutes")
    before_kill = {t["key"]: {"status": t["status"], "attempts": t["attempts"], "claim_version": t.get("claim_version")}
                   for t in d["tasks"]}
    kill_tree(api)
    mark("api killed", running=running)

    api = start_api(args.port, logs / "api-after.log")
    health_after = httpx.get(f"{base}/api/health", timeout=30).json()
    mark("api restarted", orchestrator=health_after["orchestrator"])
    analyst, approver = login(base, "analyst@analystos.local"), login(base, "approver@analystos.local")
    approved, last = [], None
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            d = detail()
            if d["status"] != last:
                mark("status", status=d["status"])
                last = d["status"]
            for a in d["approvals"]:
                if a["status"] == "pending" and a["id"] not in approved:
                    r = approver.post(f"/api/approvals/{a['id']}/approve", json={"reason": "ORC-T6 restart drill"})
                    approved.append(a["id"])
                    mark("approved", action=a["action"], code=r.status_code)
            if d["status"] in ("COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
                break
            time.sleep(2)
        final = detail()
    finally:
        kill_tree(api)
    released = [{"key": t["key"], "status": t["status"], "attempts": t["attempts"], "claim_version": t.get("claim_version")}
                for t in final["tasks"] if before_kill.get(t["key"], {}).get("status") == "RUNNING"]
    resumed_log = (logs / "api-after.log").read_text(encoding="utf-8", errors="replace")
    checks = {
        "backend_before_and_after": health_before["orchestrator"] == health_after["orchestrator"],
        "a_step_was_running_at_the_kill": bool(running),
        "startup_released_the_dead_claims": "released" in resumed_log and "claim" in resumed_log,
        "startup_resumed_the_run": rid in resumed_log and "resumed" in resumed_log,
        # A step seen RUNNING just before the kill either was killed (its claim was released and retaken: a higher
        # claim version) or finished in the instant before the kill (its one attempt under the original claim).
        "killed_steps_were_retaken_under_a_new_claim": any(_retaken(r, before_kill) for r in released) and all(
            _retaken(r, before_kill) or (r["attempts"] == 1 and r["status"] == "COMPLETED") for r in released),
        "run_completed": final["status"] in ("COMPLETED", "COMPLETED_WITH_LIMITATIONS"),
        "every_step_terminal": all(t["status"] in ("COMPLETED", "SKIPPED", "FAILED", "CANCELLED", "INVALIDATED")
                                   for t in final["tasks"]),
    }
    ev = {"started_at": started.isoformat(), "workspace": ws, "run": rid, "orchestrator": health_after["orchestrator"],
          "installation": health_after.get("installation", {}).get("orchestrator"), "timeline": timeline,
          "running_at_kill": running, "before_kill": before_kill, "killed_steps_after": released,
          "final_status": final["status"], "final_error": final.get("error"),
          "tasks": [{k: t.get(k) for k in ("key", "status", "attempts", "claim_version", "error")} for t in final["tasks"]],
          "checks": checks}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"orc-restart-{started:%Y%m%d-%H%M%S}"
    (out / f"{stem}.json").write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")
    passed = sum(checks.values())
    lines = [f"# Orchestrator restart drill — {started:%Y-%m-%d %H:%M} UTC", "",
             f"Backend `{ev['orchestrator']}` (ADR-015, ORC-T6). Workspace `{ws}`, run `{rid}`. Raw evidence: `{stem}.json`.",
             "The API process tree was killed hard while a step ran, then started again on the same database.", "",
             f"**{passed}/{len(checks)} checks passed.** Final status: `{final['status']}`.", "",
             "| Check | Result |", "|---|---|", *[f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in checks.items()], "",
             "## Timeline", "", "| t (s) | Event |", "|---|---|",
             *[f"| {e['t']} | {e['event']} {json.dumps({k: v for k, v in e.items() if k not in ('t', 'event')})} |" for e in timeline],
             "", "## Steps running at the kill", "", "| Step | Claim before | After | Attempts | Status |", "|---|---|---|---|---|",
             *[f"| {r['key']} | {before_kill[r['key']]['claim_version']} | {r['claim_version']} | {r['attempts']} | {r['status']} |"
               for r in released], ""]
    (out / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{passed}/{len(checks)} checks; evidence {out / stem}.md")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

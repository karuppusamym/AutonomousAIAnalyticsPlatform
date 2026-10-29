"""S12-T6: load at `load_factor` x the pilot targets (config/slo.yaml) and an SLO report, runnable locally.

    PYTHONPATH=src python scripts/load/slo_report.py [--suffix s12slo] [--duration 120] [--factor 2] [--keep]
    PYTHONPATH=src python scripts/load/slo_report.py --base-url http://host:8000 --token ... --workspace ws_...

Self-hosted (default): an isolated plane (scripts/load/harness.py: its own control and analytics databases, logins,
queue and budget prefixes; set ANALYSTOS_LOAD_PG to a Postgres you may write to), migrated and seeded with
`analystos migrate` / `seed`, the ServiceNow mock staged into one workspace, and a private API (local orchestrator)
on a free port. No model is called (OPENROUTER_API_KEY is cleared): Ask answers from the registry and rules.

Load: `ask_users` x factor people each asking a question, pausing `ask_think_seconds`, for `--duration` seconds;
meanwhile `concurrent_runs` x factor investigations start at once and are followed to a terminal state. The SLIs
are measured twice: client side (every request's latency and status) and from the API's own /metrics before and
after (the same series the burn-rate alerts read). Writes docs/60-delivery/evidence/s12-slo-<date>.{json,md}.
A run still going at the deadline is reported as not finished, never as a success.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

ROOT = harness.ROOT
QUESTIONS = ["How many incidents per priority?", "Number of incidents by priority", "How many incidents are there?",
             "What is the SLA breach rate by assignment group?", "Incidents by state", "Count incidents by category",
             "Which assignment group has the most incidents?", "Average resolution time by priority"]
TERMINAL = {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED", "REJECTED", "EXPIRED"}
# A run started through the API stops at the publish approval (WAITING_USER): every automated step is done and
# a person decides. The load test counts reaching that gate as the run's success (`at_gate`), never approves.
GATE = "WAITING_USER"
SUCCESS = {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", GATE}


def load_slo(path: Path = ROOT / "config" / "slo.yaml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- setup
def provision_migrated(plane: harness.Plane) -> None:
    """The harness plane, but schema by `analystos migrate` (tenancy, RLS, views) instead of create_all."""
    from sqlalchemy import create_engine, text

    harness.teardown(plane)
    p = plane.role_prefix
    admin = harness._admin()
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{plane.control_db}"'))
        c.execute(text(f"CREATE ROLE {p}loader LOGIN CREATEROLE PASSWORD 'loader'"))
        c.execute(text(f"CREATE ROLE {p}reader LOGIN NOINHERIT PASSWORD 'reader'"))
        c.execute(text(f"CREATE ROLE {p}builder LOGIN NOINHERIT PASSWORD 'builder'"))
        c.execute(text(f"ALTER ROLE {p}reader SET default_transaction_read_only = on"))
        c.execute(text(f'CREATE DATABASE "{plane.analytics_db}" OWNER {p}loader'))
        c.execute(text(f'REVOKE CONNECT ON DATABASE "{plane.analytics_db}" FROM PUBLIC'))
        c.execute(text(f'GRANT CONNECT ON DATABASE "{plane.analytics_db}" TO {p}loader, {p}reader, {p}builder'))
    admin.dispose()
    engine = create_engine(plane.control_url)
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    for cmd in ("migrate", "seed"):
        r = subprocess.run([sys.executable, "-m", "analystos.cli", cmd], env=plane.env, cwd=ROOT, capture_output=True,
                           text=True, timeout=900)
        if r.returncode:
            raise SystemExit(f"analystos {cmd} failed:\n{r.stdout[-2000:]}\n{r.stderr[-4000:]}")


def start_temporal(suffix: str, containers: list[str]) -> str:
    """A throwaway Temporal (auto-setup 1.25 on its own Postgres), never the running stack's."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import dr_drill

    net, pg, server = f"aosslo-{suffix}-net", f"aosslo-{suffix}-tpg", f"aosslo-{suffix}-temporal"
    dr_drill.cleanup([server, pg], [], [net])
    containers += [net, pg, server]
    dr_drill.sh("docker", "network", "create", net)
    port = dr_drill.free_port()
    dr_drill._temporal_pair(net, pg, server, port)
    return f"127.0.0.1:{port}"


def login(base: str, email: str | None = None, password: str | None = None) -> str:
    import httpx

    from analystos.core.config import get_settings

    s = get_settings()
    r = httpx.post(f"{base}/api/auth/login", json={"email": email or s.bootstrap_admin_email,
                                                   "password": password or s.bootstrap_admin_password}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


ASKER_PASSWORD = "load-test-asker-1"  # local load plane only (harness.configure), like its JWT secret


def add_askers(ws: str, n: int) -> list[str]:
    """One analyst login per simulated person. Ask budgets are per person per hour (governance/budgets.py), so
    20 people sharing one login would measure that login's budget refusals, not Ask latency (2026-10-28 run:
    207 of 269 asks refused `budget_exceeded`)."""
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security.auth import hash_password
    from analystos.services.workspaces import add_member
    from analystos.tenancy import system_scope

    emails = [f"load-asker-{i}@analystos.local" for i in range(n)]
    digest = hash_password(ASKER_PASSWORD)
    with system_scope(), session_scope() as s:
        owner = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        for i, email in enumerate(emails):
            if s.scalar(select(User).where(User.email == email)) is None:
                s.add(User(id=new_id("usr"), email=email, name=f"Load asker {i}", password_hash=digest, attributes={}))
        s.flush()
        for email in emails:
            add_member(s, owner, ws, email, "analyst")
    return emails


# ------------------------------------------------------------------------------------------- load
class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def add(self, kind: str, seconds: float, status: int, extra: dict | None = None) -> None:
        with self.lock:
            self.calls.append({"kind": kind, "seconds": seconds, "status": status, **(extra or {})})

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind]


def ask_user(base: str, token: str, ws: str, until: float, think: float, rec: Recorder, seed: int) -> None:
    """One person: their own login (`token`), one thread, a question every `think` seconds (+-50%)."""
    import httpx

    rng = random.Random(seed)
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base, headers=h, timeout=120) as c:
        t0 = time.perf_counter()
        r = c.post(f"/api/workspaces/{ws}/ask/threads", json={"title": f"load {seed}"})
        rec.add("thread", time.perf_counter() - t0, r.status_code)
        if r.status_code >= 400:
            return
        thread = r.json()["id"]
        while time.monotonic() < until:
            t0 = time.perf_counter()
            try:
                r = c.post(f"/api/ask/threads/{thread}/turns", json={"question": rng.choice(QUESTIONS)})
                status, body = r.status_code, (r.json() if r.status_code < 500 else {})
            except httpx.HTTPError as exc:
                status, body = 599, {"error": type(exc).__name__}
            outcome = body.get("status") if isinstance(body, dict) else None
            if outcome == "refused":  # which governance step refused it (budget, scope, no model, ...)
                outcome = f"refused:{(body.get('refusal') or {}).get('kind')}"
            rec.add("ask", time.perf_counter() - t0, status, {"outcome": outcome})
            time.sleep(rng.uniform(0.5, 1.5) * think)


def run_batch(base: str, token: str, ws: str, n: int, deadline: float, rec: Recorder) -> dict:
    import httpx

    if n <= 0:  # --runs 0: Ask alone (how fast Ask is when nothing else uses the host)
        return {"started": 0, "requested": 0, "finished": 0, "not_finished": 0, "not_finished_last_status": {},
                "outcomes": {}, "seconds": harness.summary([], scale=1.0, digits=1)}
    h = {"Authorization": f"Bearer {token}"}
    ids: dict[str, float] = {}
    with httpx.Client(base_url=base, headers=h, timeout=60) as c:
        def start(i: int) -> None:
            t0 = time.perf_counter()
            r = c.post(f"/api/workspaces/{ws}/analysis", json={"objective": "Find the drivers of SLA breaches in IT incidents"})
            rec.add("run_start", time.perf_counter() - t0, r.status_code)
            if r.status_code < 400:
                ids[r.json()["id"]] = time.monotonic()

        with ThreadPoolExecutor(max_workers=min(n, 16)) as pool:
            list(pool.map(start, range(n)))
        done: dict[str, tuple[str, float]] = {}
        last: dict[str, str | None] = {}
        while len(done) < len(ids) and time.monotonic() < deadline:
            for rid, started in ids.items():
                if rid in done:
                    continue
                t0 = time.perf_counter()
                r = c.get(f"/api/workspaces/{ws}/analysis/{rid}")
                rec.add("run_poll", time.perf_counter() - t0, r.status_code)
                if r.status_code < 400:
                    last[rid] = r.json().get("status")
                    if last[rid] in TERMINAL or last[rid] == GATE:
                        done[rid] = (last[rid], time.monotonic() - started)
            time.sleep(2)
        for rid in set(ids) - set(done):
            c.post(f"/api/workspaces/{ws}/analysis/{rid}/cancel")
    outcomes: dict[str, int] = {}
    for status, _ in done.values():
        outcomes[status] = outcomes.get(status, 0) + 1
    unfinished: dict[str, int] = {}
    for rid in set(ids) - set(done):
        unfinished[str(last.get(rid))] = unfinished.get(str(last.get(rid)), 0) + 1
    return {"started": len(ids), "requested": n, "finished": len(done), "not_finished": len(ids) - len(done),
            "not_finished_last_status": unfinished,
            "outcomes": outcomes, "seconds": harness.summary([s for _, s in done.values()], scale=1.0, digits=1)}


# ------------------------------------------------------------------------------------------- host load
def _cpu_times() -> tuple[float, float] | None:
    """(busy, total) CPU time of the whole host: GetSystemTimes on Windows, /proc/stat on Linux."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        val = [(t.dwHighDateTime << 32) | t.dwLowDateTime for t in (idle, kernel, user)]
        return float(val[1] + val[2] - val[0]), float(val[1] + val[2])  # kernel time includes idle time
    try:
        fields = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    except OSError:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
    return sum(fields) - idle, sum(fields)


class HostLoad:
    """Host CPU busy share every `every` seconds while the load runs (the evidence says how loaded the host was:
    other sessions' work on a shared dev host moves the latencies)."""

    def __init__(self, every: float = 2.0) -> None:
        self.every, self.samples, self._stop = every, [], threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        last = _cpu_times()
        while not self._stop.wait(self.every):
            now = _cpu_times()
            if last and now and now[1] > last[1]:
                self.samples.append((now[0] - last[0]) / (now[1] - last[1]))
            last = now

    def __enter__(self) -> HostLoad:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def summary(self) -> dict:
        xs = sorted(self.samples)
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "mean_busy": round(sum(xs) / len(xs), 3), "p95_busy": round(xs[int(0.95 * (len(xs) - 1))], 3),
                "max_busy": round(xs[-1], 3)}


# ------------------------------------------------------------------------------------------- SLIs
_LINE = re.compile(r'^([a-z_]+)(\{[^}]*\})? ([0-9.e+-]+|\+Inf)$')


def scrape(base: str, token: str | None = None) -> dict[tuple, float]:
    import httpx

    r = httpx.get(f"{base}/metrics", timeout=30, headers={"Authorization": f"Bearer {token}"} if token else {})
    out: dict[tuple, float] = {}
    for line in r.text.splitlines():
        m = _LINE.match(line)
        if m:
            labels = tuple(sorted(re.findall(r'(\w+)="([^"]*)"', m.group(2) or "")))
            out[(m.group(1), labels)] = float(m.group(3))
    return out


def delta(after: dict, before: dict, name: str, **match: str) -> float:
    total = 0.0
    for (n, labels), v in after.items():
        d = dict(labels)
        if n == name and all(d.get(k) == val for k, val in match.items()):
            total += v - before.get((n, labels), 0.0)
    return total


def server_slis(before: dict, after: dict, slo: dict) -> dict:
    le = repr(float(slo["slos"]["ask_latency"]["p95_seconds"])).rstrip("0").rstrip(".")
    asks = delta(after, before, "analystos_http_request_duration_seconds_count", **{"class": "ask", "method": "POST"})
    fast = delta(after, before, "analystos_http_request_duration_seconds_bucket", **{"class": "ask", "method": "POST", "le": le})
    reqs = sum(delta(after, before, "analystos_http_requests_total", **{"class": c}) for c in
               {dict(k[1]).get("class") for k in after if k[0] == "analystos_http_requests_total"} - {"health", "metrics", None})
    errors = sum(v - before.get(k, 0.0) for k, v in after.items() if k[0] == "analystos_http_requests_total"
                 and dict(k[1]).get("status", "").startswith("5") and dict(k[1]).get("class") not in ("health", "metrics"))
    runs = {dict(k[1])["outcome"]: v - before.get(k, 0.0) for k, v in after.items() if k[0] == "analystos_runs_total"}
    finished = sum(v for o, v in runs.items() if o != "CANCELLED")
    return {"ask_requests": asks, "ask_within_target_share": round(fast / asks, 4) if asks else None,
            "requests": reqs, "availability": round(1 - errors / reqs, 5) if reqs else None,
            "runs_by_outcome": runs, "run_success": round(sum(runs.get(o, 0) for o in SUCCESS) / finished, 4) if finished else None}


def evaluate(client: dict, server: dict, runs: dict, slo: dict) -> dict:
    s = slo["slos"]
    asks = client["ask"]
    a_lat = s["ask_latency"]
    within = [c for c in asks["raw"] if c["seconds"] <= a_lat["p95_seconds"] and c["status"] < 500]
    ask_share = round(len(within) / len(asks["raw"]), 4) if asks["raw"] else None
    finished = sum(v for k, v in runs["outcomes"].items() if k != "CANCELLED") + runs["not_finished"]
    run_ok = sum(runs["outcomes"].get(o, 0) for o in SUCCESS)  # COMPLETED (+ limitations) or at the approval gate
    run_share = round(run_ok / finished, 4) if finished else None
    avail = client["availability"]

    def verdict(value, target):
        return None if value is None else value >= target
    return {
        "ask_latency": {"target": f">= {a_lat['target']:.0%} within {a_lat['p95_seconds']} s", "client": ask_share,
                        "server": server.get("ask_within_target_share"), "p95_ms": asks["latency_ms"]["p95"],
                        "met": verdict(ask_share, a_lat["target"])},
        "run_success": {"target": f">= {s['run_success']['target']:.0%} completed or at the approval gate, "
                                  f"p95 <= {s['run_success']['max_p95_seconds']} s",
                        "client": run_share, "server": server.get("run_success"), "p95_s": runs["seconds"]["p95"],
                        "met": verdict(run_share, s["run_success"]["target"]) and
                        (runs["seconds"]["p95"] or 0) <= s["run_success"]["max_p95_seconds"] if run_share is not None else None},
        "availability": {"target": f">= {s['availability']['target']:.1%}", "client": avail,
                         "server": server.get("availability"), "met": verdict(avail, s["availability"]["target"])},
    }


def client_slis(rec: Recorder) -> dict:
    out = {}
    for kind in ("ask", "thread", "run_start", "run_poll"):
        calls = rec.of(kind)
        out[kind] = {"n": len(calls), "errors_5xx": sum(c["status"] >= 500 for c in calls),
                     "latency_ms": harness.summary([c["seconds"] for c in calls]), "raw": calls if kind == "ask" else []}
    everything = rec.calls
    out["availability"] = round(1 - sum(c["status"] >= 500 for c in everything) / len(everything), 5) if everything else None
    out["ask"]["outcomes"] = {}
    for c in rec.of("ask"):
        out["ask"]["outcomes"][c.get("outcome") or "none"] = out["ask"]["outcomes"].get(c.get("outcome") or "none", 0) + 1
    return out


def _load_text(load: dict) -> str:
    if not load.get("n"):
        return "not sampled"
    return f"mean {load['mean_busy']:.0%}, p95 {load['p95_busy']:.0%}, max {load['max_busy']:.0%} ({load['n']} samples)"


def markdown(report: dict) -> str:
    slo, ev = report["slo"], report["verdict"]
    lines = [f"# S12 load test and SLO report - {report['date']}", "",
             f"Load: {report['load']['ask_users']} people asking (think {report['load']['think_seconds']} s) for "
             f"{report['load']['duration_seconds']} s and {report['load']['runs']} concurrent investigations: "
             f"{slo['load_factor']}x the pilot targets of config/slo.yaml v{slo['version']}. Host: {report['host']}; "
             f"host CPU busy during the load: {_load_text(report.get('host_load') or {})}.", "",
             "| SLO | Target | Client side | Server (/metrics) | Detail | Met |", "|---|---|---|---|---|---|"]
    detail = {"ask_latency": f"p95 {ev['ask_latency']['p95_ms']} ms", "run_success": f"p95 {ev['run_success']['p95_s']} s",
              "availability": ""}
    for name, v in ev.items():
        met = {True: "yes", False: "**no**", None: "not measured"}[v["met"]]
        lines.append(f"| {name} | {v['target']} | {v['client']} | {v['server']} | {detail[name]} | {met} |")
    c = report["client"]
    lines += ["", "## Requests", "", "| Kind | n | 5xx | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|---|"]
    for kind in ("ask", "thread", "run_start", "run_poll"):
        k = c[kind]
        lines.append(f"| {kind} | {k['n']} | {k['errors_5xx']} | {k['latency_ms']['p50']} | {k['latency_ms']['p95']} | {k['latency_ms']['p99']} |")
    lines += ["", f"Ask outcomes: {c['ask']['outcomes']}", "", "## Runs", "", "```json", json.dumps(report["runs"], indent=1), "```",
              "", "## Notes", "", *[f"* {n}" for n in report["notes"]], ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suffix", default="s12slo")
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--factor", type=float, default=None, help="default: config/slo.yaml load_factor")
    ap.add_argument("--run-deadline", type=float, default=900)
    ap.add_argument("--runs", type=int, default=None,
                    help="concurrent investigations (default: concurrent_runs x factor); 0 = Ask alone")
    ap.add_argument("--base-url")
    ap.add_argument("--token")
    ap.add_argument("--workspace")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--workers", type=int, default=2,
                    help="worker processes on a throwaway Temporal (docker; scripts/dr_drill.py helpers), the standard "
                         "topology; 0 = the lite topology (runs inside the API process, local orchestrator)")
    ap.add_argument("--out", type=Path, default=harness.EVIDENCE)
    args = ap.parse_args(argv)
    slo = load_slo()
    factor = args.factor or float(slo["load_factor"])
    users = max(1, round(slo["pilot"]["ask_users"] * factor))
    nruns = max(1, round(slo["pilot"]["concurrent_runs"] * factor)) if args.runs is None else max(0, args.runs)
    procs, plane, notes, containers = [], None, [], []
    try:
        if args.base_url:
            base, token, ws = args.base_url.rstrip("/"), args.token, args.workspace
            if not (token and ws):
                ap.error("--base-url needs --token and --workspace")
            tokens = [token] * users
            notes.append("every asker used the one --token login: its per-person Ask budget applies to all of them")
        else:
            plane = harness.configure(args.suffix)
            plane.env.update({"ANALYSTOS_ORCHESTRATOR": "local", "ANALYSTOS_INPROCESS_SCHEDULER": "false",
                              "ANALYSTOS_METRICS_DIR": str(Path(harness.ROOT / "var" / f"metrics-{args.suffix}"))})
            if args.workers:
                address = start_temporal(args.suffix, containers)
                plane.env.update({"ANALYSTOS_ORCHESTRATOR": "temporal", "ANALYSTOS_TEMPORAL_ADDRESS": address})
                notes.append(f"topology: API + {args.workers} worker process(es) (every queue) on a throwaway Temporal 1.25")
            else:
                notes.append("topology: lite (runs execute inside the API process, local orchestrator)")
            os.environ.update({k: plane.env[k] for k in ("ANALYSTOS_ORCHESTRATOR",)})
            provision_migrated(plane)
            from load_runs import setup_world

            from analystos.tenancy import system_scope

            _, snow = harness.start_mock_servicenow()
            with system_scope():  # set-up is platform work (strict tenancy: an unbound session sees nothing)
                _, ws, staged = setup_world(snow)
            notes.append(f"world: ServiceNow mock `incident` staged into one workspace in {staged} s")
            with system_scope():
                askers = add_askers(ws, users)
            notes.append(f"{users} people asking, each with their own analyst login (Ask budgets are per person)")
            api, base = harness.start_api(plane)
            procs.append(api)
            for _ in range(args.workers):
                procs.append(harness.spawn([sys.executable, "-m", "analystos.cli", "worker"], plane))
            token = login(base)
            tokens = [login(base, email, ASKER_PASSWORD) for email in askers]
        before = scrape(base)
        rec = Recorder()
        until = time.monotonic() + args.duration
        started = datetime.now(UTC)
        runs_box: dict = {}
        runner = threading.Thread(target=lambda: runs_box.update(run_batch(base, token, ws, nruns,
                                                                            time.monotonic() + args.run_deadline, rec)))
        runner.start()
        think = float(slo["pilot"]["ask_think_seconds"])
        with HostLoad() as host_load:
            with ThreadPoolExecutor(max_workers=users) as pool:
                for i in range(users):
                    pool.submit(ask_user, base, tokens[i], ws, until, think, rec, i)
            runner.join()
        after = scrape(base)
        client = client_slis(rec)
        server = server_slis(before, after, slo)
        verdict = evaluate(client, server, runs_box, slo)
        report = {"date": started.strftime("%Y-%m-%d"), "started_at": started.isoformat(), "slo": slo,
                  "load": {"ask_users": users, "think_seconds": think, "duration_seconds": args.duration, "runs": nruns,
                           "factor": factor}, "host": f"{sys.platform}, {os.cpu_count()} cores",
                  "host_load": host_load.summary(),
                  "client": {k: ({**v, "raw": []} if isinstance(v, dict) else v) for k, v in client.items()},
                  "server": server, "runs": runs_box, "verdict": verdict, "notes": notes}
        notes.append("no model was called (OPENROUTER_API_KEY cleared): Ask answered from the registry and rules")
        stem = f"s12-slo-{report['date']}"
        harness.write_json(f"{stem}.json", report, args.out)
        (args.out / f"{stem}.md").write_text(markdown(report), encoding="utf-8")
        print(markdown(report))
        return 0 if all(v["met"] for v in verdict.values()) else 1
    finally:
        if hasattr(os, "killpg"):
            harness.stop(procs)
        else:  # Windows: no process groups; the API has no pool children here (local orchestrator, threads)
            for p in procs:
                p.terminate()
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
        if plane is not None and not args.keep:
            harness.teardown(plane)
        if containers and not args.keep:
            import dr_drill

            dr_drill.cleanup(containers[1:], [], containers[:1])


if __name__ == "__main__":
    sys.exit(main())

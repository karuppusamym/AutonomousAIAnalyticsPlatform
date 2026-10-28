"""Disaster-recovery drill (spec S12-T7): point-in-time recovery of the control-plane Postgres and a Temporal restore,
in throwaway containers (docker), timed, with a dated evidence report. Never touches a running stack.

    python scripts/dr_drill.py [--part pitr|temporal|all] [--suffix s12] [--report auto|PATH] [--keep]

PITR (RPO <= 15 min, S12 §1):
  1. a fresh Postgres 16 (pgvector) with WAL archiving (archive_timeout = RPO bound) gets the platform schema
     (`analystos migrate` + `seed`: tenancy, RLS, audit chain) and a base backup (`pg_basebackup`, tar + gzip);
  2. phase A: a workspace with audit events (sealed into the audit chain) - the data that must survive;
  3. the target time is taken from the database clock; then phase B, the incident: an operator deletes every
     workspace (cascading to runs, approvals, ...) and creates one that must not exist after recovery;
  4. the primary is destroyed; a new container restores the base backup and replays the archived WAL to the target
     time (`recovery_target_time`, promote) - timed: the recovery time;
  5. verified on the recovered database: phase A present, the deleted rows back, the incident's row absent, the Alembic
     head unchanged, `analystos ops rls-check` (RLS on every tenant table) and `analystos audit verify` (chain intact).

Temporal (AT-09 "no lost accepted goal" after a restore):
  1. Temporal (auto-setup 1.25) on its own Postgres; a worker takes N probe workflows to mid-flight (each waits for a
     signal) and stops; the workflow histories are recorded;
  2. `pg_dump` of `temporal` and `temporal_visibility`; Temporal and its database are destroyed;
  3. a new Postgres restores the dumps and a new Temporal starts on it - timed;
  4. verified: every workflow still RUNNING with the same history length; a new worker receives the signals and
     every workflow COMPLETES (accepted work resumed, nothing lost or run twice).

Exit 1 when a check fails. The report: docs/60-delivery/evidence/<date>-s12-dr-drill.md (`--report auto`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
PG_IMAGE = os.environ.get("ANALYSTOS_DR_PG_IMAGE", "pgvector/pgvector:pg16")
TEMPORAL_IMAGE = os.environ.get("ANALYSTOS_DR_TEMPORAL_IMAGE", "temporalio/auto-setup:1.25")
PW = "drill-only"  # throwaway containers bound to 127.0.0.1; never a real credential
ARCHIVE_TIMEOUT_S = 60


try:  # the Temporal drill's probe workflow (module level: the SDK refuses local workflow classes)
    from temporalio import workflow as _workflow
except ImportError:  # pragma: no cover - the temporal extra is optional
    _workflow = None

if _workflow is not None:
    @_workflow.defn(name="AosDrProbe")
    class Probe:
        """Waits for the `finish` signal: in flight across the backup, completes after the restore."""

        def __init__(self) -> None:
            self.done = False

        @_workflow.run
        async def run(self, payload: str) -> str:
            await _workflow.wait_condition(lambda: self.done)
            return f"ok:{payload}"

        @_workflow.signal
        def finish(self) -> None:
            self.done = True
else:  # pragma: no cover
    Probe = None


# ----------------------------------------------------------------------------------- docker helpers
def sh(*args: str, check: bool = True, timeout: int = 600, input_: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, input=input_)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args[:6])}... failed ({r.returncode}): {r.stderr[-1500:] or r.stdout[-1500:]}")
    return r


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def psql(container: str, sql: str, db: str = "analystos") -> str:
    return sh("docker", "exec", "-u", "postgres", container, "psql", "-U", "analystos", "-d", db, "-At", "-v",
              "ON_ERROR_STOP=1", "-c", sql).stdout.strip()


def wait_pg(container: str, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = sh("docker", "exec", container, "pg_isready", "-U", "analystos", check=False)
        if r.returncode == 0 and sh("docker", "exec", "-u", "postgres", container, "psql", "-U", "analystos", "-d",
                                    "analystos", "-Atc", "SELECT 1", check=False).returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"{container} did not become ready")


def cleanup(names: list[str], volumes: list[str], networks: list[str]) -> None:
    for n in names:
        sh("docker", "rm", "-f", "-v", n, check=False)
    for v in volumes:
        sh("docker", "volume", "rm", "-f", v, check=False)
    for n in networks:
        sh("docker", "network", "rm", n, check=False)


def platform_env(url: str) -> dict[str, str]:
    env = {**os.environ, "ANALYSTOS_DATABASE_URL": url, "ANALYSTOS_ENV": "dev", "OPENROUTER_API_KEY": "",
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]), "ANALYSTOS_REDIS_URL": ""}
    return env


def analystos(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "-m", "analystos.cli", *args], env=platform_env(url), cwd=ROOT,
                       capture_output=True, text=True, timeout=900)
    if check and r.returncode:
        raise RuntimeError(f"analystos {' '.join(args)} failed ({r.returncode}): {r.stderr[-2000:] or r.stdout[-2000:]}")
    return r


# ----------------------------------------------------------------------------------- PITR
def pitr(suffix: str, keep: bool) -> dict[str, Any]:
    name, restored = f"aosdr-{suffix}-pg", f"aosdr-{suffix}-pg-restored"
    vols = [f"aosdr-{suffix}-archive", f"aosdr-{suffix}-backup"]
    out: dict[str, Any] = {"image": PG_IMAGE, "archive_timeout_s": ARCHIVE_TIMEOUT_S, "steps": {}, "checks": {}}
    cleanup([name, restored], vols, [])
    try:
        for v in vols:
            sh("docker", "volume", "create", v)
        port = free_port()
        t0 = time.monotonic()
        sh("docker", "run", "-d", "--name", name, "-e", "POSTGRES_USER=analystos", "-e", f"POSTGRES_PASSWORD={PW}",
           "-e", "POSTGRES_DB=analystos", "-v", f"{vols[0]}:/archive", "-v", f"{vols[1]}:/backup",
           "-p", f"127.0.0.1:{port}:5432", PG_IMAGE, "-c", "wal_level=replica", "-c", "archive_mode=on",
           "-c", "archive_command=test ! -f /archive/%f && cp %p /archive/%f", "-c", f"archive_timeout={ARCHIVE_TIMEOUT_S}")
        sh("docker", "exec", "-u", "root", name, "sh", "-c", "chown postgres /archive /backup")
        wait_pg(name)
        url = f"postgresql+psycopg://analystos:{PW}@127.0.0.1:{port}/analystos"
        analystos(url, "migrate")
        analystos(url, "seed")
        head = psql(name, "SELECT version_num FROM alembic_version")
        out["steps"]["primary_ready_s"] = round(time.monotonic() - t0, 1)
        out["alembic_head"] = head

        t0 = time.monotonic()
        sh("docker", "exec", "-u", "postgres", name, "pg_basebackup", "-U", "analystos", "-D", "/backup/base", "-Ft", "-z",
           "-X", "stream", "-c", "fast")
        out["steps"]["base_backup_s"] = round(time.monotonic() - t0, 1)
        out["base_backup_bytes"] = int(sh("docker", "exec", name, "sh", "-c", "du -sb /backup/base | cut -f1").stdout.strip())

        # phase A: must survive
        analystos(url, "ops", "spend", "--days", "1")  # a platform read through the CLI (tenancy, RLS) before the incident
        phase_a = _workload(url, "dr-before")
        psql(name, "SELECT pg_switch_wal()")
        time.sleep(1.5)
        target = psql(name, "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
        time.sleep(1.5)
        before_counts = _counts(name)
        # phase B: the incident
        deleted = int(psql(name, "WITH d AS (DELETE FROM workspace RETURNING 1) SELECT count(*) FROM d"))
        _workload(url, "dr-after")
        incident_at = psql(name, "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
        last_wal = psql(name, "SELECT pg_walfile_name(pg_switch_wal())")
        end = time.monotonic() + 120
        while time.monotonic() < end and psql(name, "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver") < last_wal:
            time.sleep(0.5)
        archived = psql(name, "SELECT last_archived_wal, last_archived_time FROM pg_stat_archiver")
        out.update(target_time=target, incident_at=incident_at, workspaces_deleted=deleted, phase_a=phase_a,
                   last_archived=archived)

        sh("docker", "rm", "-f", name)  # the primary is gone
        t0 = time.monotonic()
        port2 = free_port()
        boot = ("set -e; mkdir -p \"$PGDATA\"; tar xzf /backup/base/base.tar.gz -C \"$PGDATA\"; "
                "tar xzf /backup/base/pg_wal.tar.gz -C \"$PGDATA/pg_wal\"; touch \"$PGDATA/recovery.signal\"; "
                "chown -R postgres:postgres \"$PGDATA\"; chmod 700 \"$PGDATA\"; "
                "exec docker-entrypoint.sh postgres -c 'restore_command=cp /archive/%f %p' "
                f"-c \"recovery_target_time={target}\" -c recovery_target_action=promote -c archive_mode=off")
        sh("docker", "run", "-d", "--name", restored, "-e", f"POSTGRES_PASSWORD={PW}", "-v", f"{vols[0]}:/archive",
           "-v", f"{vols[1]}:/backup", "-p", f"127.0.0.1:{port2}:5432", "--entrypoint", "bash", PG_IMAGE, "-c", boot)
        end = time.monotonic() + 300
        promoted = False
        while time.monotonic() < end:
            r = sh("docker", "exec", "-u", "postgres", restored, "psql", "-U", "analystos", "-d", "analystos", "-Atc",
                   "SELECT pg_is_in_recovery()", check=False)
            if r.returncode == 0 and r.stdout.strip() == "f":
                promoted = True
                break
            time.sleep(0.5)
        out["steps"]["recovery_s"] = round(time.monotonic() - t0, 1)
        if not promoted:
            out["checks"]["promoted"] = False
            out["restore_log"] = sh("docker", "logs", "--tail", "40", restored, check=False).stderr[-3000:]
            return out
        url2 = f"postgresql+psycopg://analystos:{PW}@127.0.0.1:{port2}/analystos"
        after_counts = _counts(restored)
        names = set(filter(None, psql(restored, "SELECT string_agg(name, ',') FROM workspace").split(",")))
        out["checks"] = {
            "promoted": True,
            "phase_a_present": "dr-before" in names,
            "incident_row_absent": "dr-after" not in names,
            "deleted_rows_restored": after_counts == before_counts,
            "alembic_head_unchanged": psql(restored, "SELECT version_num FROM alembic_version") == head,
            "rls_check": analystos(url2, "ops", "rls-check", check=False).returncode == 0,
            "audit_chain_verify": analystos(url2, "audit", "verify", check=False).returncode == 0,
        }
        out["counts_at_target"] = before_counts
        out["rpo_bound_s"] = ARCHIVE_TIMEOUT_S
        return out
    finally:
        if not keep:
            cleanup([name, restored], vols, [])


def _workload(url: str, workspace_name: str) -> dict[str, Any]:
    code = (
        "import json\n"
        "from sqlalchemy import select\n"
        "from analystos.core.config import get_settings\n"
        "from analystos.db.base import session_scope\n"
        "from analystos.db.models import User\n"
        "from analystos.governance.audit import audit\n"
        "from analystos.services.workspaces import create_workspace\n"
        "from analystos.tenancy import system_scope\n"
        "with system_scope(), session_scope() as s:\n"
        "    admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))\n"
        f"    ws = create_workspace(s, admin, name={workspace_name!r}, objective='dr drill', autonomy_level=3)\n"
        "    s.flush()\n"
        "    for i in range(5):\n"
        "        audit(f'user:{admin.id}', 'dr.drill', workspace_id=ws.id, target=ws.id, decision='allow', reasons=[str(i)], session=s)\n"
        "    print(json.dumps({'workspace_id': ws.id}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=platform_env(url), cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    if r.returncode:
        raise RuntimeError(f"workload failed: {r.stderr[-2000:]}")
    sealed = analystos(url, "audit", "seal", "--full", check=False)
    return {**json.loads(r.stdout.strip().splitlines()[-1]), "audit_sealed": sealed.returncode == 0}


def _counts(container: str) -> dict[str, int]:
    tables = ("workspace", "workspace_member", "audit_event", "audit_chain", "app_user", "tenant")
    return {t: int(psql(container, f"SELECT count(*) FROM {t}")) for t in tables}


# ----------------------------------------------------------------------------------- Temporal
def temporal(suffix: str, keep: bool, workflows: int = 5) -> dict[str, Any]:
    net = f"aosdr-{suffix}-net"
    pg1, pg2, t1, t2 = (f"aosdr-{suffix}-tpg", f"aosdr-{suffix}-tpg-restored", f"aosdr-{suffix}-temporal",
                        f"aosdr-{suffix}-temporal-restored")
    out: dict[str, Any] = {"image": TEMPORAL_IMAGE, "workflows": workflows, "steps": {}, "checks": {}}
    cleanup([t1, t2, pg1, pg2], [], [net])
    tmp = Path(tempfile.mkdtemp(prefix="aosdr-"))
    try:
        sh("docker", "network", "create", net)
        port = free_port()
        t0 = time.monotonic()
        _temporal_pair(net, pg1, t1, port)
        out["steps"]["temporal_ready_s"] = round(time.monotonic() - t0, 1)
        queue = f"aosdr-{suffix}"
        ids = [f"aosdr-probe-{suffix}-{i}" for i in range(workflows)]
        before = asyncio.run(_start_mid_flight(f"127.0.0.1:{port}", queue, ids))
        out["before"] = before
        t0 = time.monotonic()
        for db in ("temporal", "temporal_visibility"):
            sh("docker", "exec", pg1, "pg_dump", "-U", "analystos", "-Fc", "-f", f"/tmp/{db}.dump", db)
            sh("docker", "cp", f"{pg1}:/tmp/{db}.dump", str(tmp / f"{db}.dump"))
        out["steps"]["backup_s"] = round(time.monotonic() - t0, 1)
        out["dump_bytes"] = {p.name: p.stat().st_size for p in tmp.glob("*.dump")}
        sh("docker", "rm", "-f", t1, pg1)  # Temporal and its database are gone

        t0 = time.monotonic()
        sh("docker", "run", "-d", "--name", pg2, "--network", net, "-e", "POSTGRES_USER=analystos", "-e",
           f"POSTGRES_PASSWORD={PW}", "-e", "POSTGRES_DB=analystos", PG_IMAGE)
        wait_pg(pg2)
        for db in ("temporal", "temporal_visibility"):
            sh("docker", "cp", str(tmp / f"{db}.dump"), f"{pg2}:/tmp/{db}.dump")
            psql(pg2, f"CREATE DATABASE {db}")
            sh("docker", "exec", pg2, "pg_restore", "-U", "analystos", "--no-owner", "--exit-on-error", "-d", db,
               f"/tmp/{db}.dump")
        port2 = free_port()
        _temporal_server(net, pg2, t2, port2, skip_db_create=True)
        out["steps"]["restore_s"] = round(time.monotonic() - t0, 1)
        after = asyncio.run(_resume(f"127.0.0.1:{port2}", queue, ids))
        out["after"] = after
        out["checks"] = {
            "all_running_after_restore": all(s == "RUNNING" for s in after["status_before_signal"].values()),
            "histories_preserved": all(after["history_before_signal"][i] == before["history"][i] for i in ids),
            "all_completed": all(r == f"ok:{i}" for i, r in after["results"].items()) and len(after["results"]) == len(ids),
            "each_ran_once": all(n == 1 for n in after["completions"].values()),
        }
        return out
    finally:
        if not keep:
            cleanup([t1, t2, pg1, pg2], [], [net])


def _temporal_pair(net: str, pg: str, server: str, port: int) -> None:
    sh("docker", "run", "-d", "--name", pg, "--network", net, "-e", "POSTGRES_USER=analystos", "-e",
       f"POSTGRES_PASSWORD={PW}", "-e", "POSTGRES_DB=analystos", PG_IMAGE)
    wait_pg(pg)
    _temporal_server(net, pg, server, port)


def _temporal_server(net: str, pg: str, server: str, port: int, *, skip_db_create: bool = False) -> None:
    env = ["-e", "DB=postgres12", "-e", "DB_PORT=5432", "-e", f"POSTGRES_SEEDS={pg}", "-e", "POSTGRES_USER=analystos",
           "-e", f"POSTGRES_PWD={PW}"]
    if skip_db_create:
        env += ["-e", "SKIP_DB_CREATE=true"]
    sh("docker", "run", "-d", "--name", server, "--network", net, *env, "-p", f"127.0.0.1:{port}:7233", TEMPORAL_IMAGE)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        r = sh("docker", "exec", server, "temporal", "operator", "namespace", "describe", "default", "--address",
               f"{server}:7233", check=False)
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{server} did not become ready: {sh('docker', 'logs', '--tail', '30', server, check=False).stderr[-2000:]}")


def _probe_workflow() -> Any:
    if Probe is None:
        raise RuntimeError("the temporal extra (temporalio) is not installed")
    return Probe


async def _history_length(client: Any, wid: str) -> int:
    n = 0
    async for _ in client.get_workflow_handle(wid).fetch_history_events():
        n += 1
    return n


async def _start_mid_flight(address: str, queue: str, ids: list[str]) -> dict[str, Any]:
    from temporalio.client import Client
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    client = await Client.connect(address)
    probe = _probe_workflow()
    async with Worker(client, task_queue=queue, workflows=[probe], workflow_runner=UnsandboxedWorkflowRunner()):
        for wid in ids:
            await client.start_workflow("AosDrProbe", wid, id=wid, task_queue=queue)
        end = time.monotonic() + 60
        while time.monotonic() < end:  # every workflow has run its first task and waits for the signal
            lengths = {wid: await _history_length(client, wid) for wid in ids}
            if all(n >= 4 for n in lengths.values()):
                break
            await asyncio.sleep(0.5)
    return {"history": {wid: await _history_length(client, wid) for wid in ids}}


async def _resume(address: str, queue: str, ids: list[str]) -> dict[str, Any]:
    from temporalio.client import Client, WorkflowExecutionStatus
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    client = await Client.connect(address)
    status = {wid: WorkflowExecutionStatus((await client.get_workflow_handle(wid).describe()).status).name for wid in ids}
    history = {wid: await _history_length(client, wid) for wid in ids}
    probe = _probe_workflow()
    results: dict[str, str] = {}
    completions: dict[str, int] = {}
    async with Worker(client, task_queue=queue, workflows=[probe], workflow_runner=UnsandboxedWorkflowRunner()):
        for wid in ids:
            await client.get_workflow_handle(wid).signal("finish")
        for wid in ids:
            results[wid] = await asyncio.wait_for(client.get_workflow_handle(wid).result(), timeout=60)
    for wid in ids:
        events = [e async for e in client.get_workflow_handle(wid).fetch_history_events()]
        completions[wid] = sum(e.HasField("workflow_execution_completed_event_attributes") for e in events)
    return {"status_before_signal": status, "history_before_signal": history, "results": results, "completions": completions}


# ----------------------------------------------------------------------------------- report
def report_md(result: dict[str, Any]) -> str:
    lines = [f"# S12-T7 disaster-recovery drill - {result['date']}", "",
             f"Host: {sys.platform}. Throwaway containers ({PG_IMAGE}, {TEMPORAL_IMAGE}); `scripts/dr_drill.py`. "
             "Nothing ran against a live stack.", ""]
    for part in ("pitr", "temporal"):
        r = result.get(part)
        if not r:
            continue
        ok = r.get("checks") and all(r["checks"].values())
        lines += [f"## {part.upper()} - {'passed' if ok else 'FAILED'}", "", "| Check | Result |", "|---|---|"]
        lines += [f"| {k} | {'yes' if v else '**no**'} |" for k, v in r.get("checks", {}).items()]
        lines += ["", "Timings (s): " + ", ".join(f"{k} {v}" for k, v in r.get("steps", {}).items()), ""]
        if part == "pitr":
            lines += [f"Target time {r.get('target_time')}; incident at {r.get('incident_at')} deleted "
                      f"{r.get('workspaces_deleted')} workspace(s). RPO bound: archive_timeout {r.get('rpo_bound_s')} s "
                      f"(spec: <= 15 min). Last archived WAL: {r.get('last_archived')}. Base backup "
                      f"{r.get('base_backup_bytes')} bytes; Alembic head {r.get('alembic_head')}.", "",
                      f"Row counts at the target and after recovery: `{json.dumps(r.get('counts_at_target'))}`", ""]
        else:
            lines += [f"{r['workflows']} probe workflows taken mid-flight before the backup; dumps "
                      f"`{json.dumps(r.get('dump_bytes'))}`.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", choices=["pitr", "temporal", "all"], default="all")
    ap.add_argument("--suffix", default="s12")
    ap.add_argument("--report", default=None, help="auto = docs/60-delivery/evidence/<date>-s12-dr-drill.md, or a path")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    result: dict[str, Any] = {"date": datetime.now(UTC).strftime("%Y-%m-%d")}
    for part, fn in (("pitr", pitr), ("temporal", temporal)):
        if args.part in (part, "all"):
            try:
                result[part] = fn(args.suffix, args.keep)
            except Exception as exc:  # a part that cannot finish is a failed drill, still reported
                result[part] = {"error": f"{type(exc).__name__}: {exc}"[:3000], "checks": {"completed": False}}
    print(json.dumps(result, indent=1, default=str))
    if args.report:
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{result['date']}-s12-dr-drill.md"
                if args.report == "auto" else Path(args.report))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_md(result), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    ok = all(all(r.get("checks", {}).values()) and r.get("checks") for k, r in result.items() if k in ("pitr", "temporal"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

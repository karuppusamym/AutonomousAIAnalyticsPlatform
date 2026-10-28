"""P7-06 with the control plane's database: the real API serves the token-authenticated artifact routes,
`dispatch_isolated` records the task, its events (live, as the local pool streams them) and its result,
bus events of the task.* types and lineage edges; a recipe run's snapshot job goes to compute-py when the
pool is configured; migration 0038 goes up, down and up."""
from __future__ import annotations

import socket
import sys
import threading
import time

import httpx
import pytest
from sqlalchemy import select

from analystos.db import vectors

pytestmark = pytest.mark.integration
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="worker pools need POSIX select() on pipes; "
                                "run on Linux")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served_app(control_db, tmp_path_factory):
    import uvicorn

    from analystos.api.app import app
    from analystos.core.config import get_settings

    settings = get_settings()
    old_dir = settings.artifact_dir
    settings.artifact_dir = tmp_path_factory.mktemp("artifacts")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    settings.artifact_dir = old_dir


@pytest.fixture()
def workspace(control_db):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User, Workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws = new_id("ws")
        s.add(Workspace(id=ws, name="compute pools", created_by=admin.id))
    return ws


@pytest.fixture(scope="module")
def local_pool(served_app):
    from analystos.workers.dispatch import SubprocessTransport

    t = SubprocessTransport(served_app, conformance=True)
    yield t
    t.close()


@posix_only
def test_a_task_is_recorded_with_its_events_result_and_lineage(served_app, local_pool, workspace):
    from analystos.contracts.worker import Budget, ProbeSpec, TaskEnvelope
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import LineageEdge, RunEvent, WorkerTask, WorkerTaskEvent
    from analystos.workers.dispatch import capability_ref, dispatch_isolated, raise_for_result
    from analystos.workers.store import control_store

    settings = get_settings()
    source = control_store(settings).put(workspace, b"input bytes", kind="blob")
    env = TaskEnvelope(task_id=new_id("wtask"), workspace_id=workspace, capability=capability_ref("conformance.probe"),
                       spec=ProbeSpec(action="write", args={"outputs": {"out": "hello"}}), input_artifacts=[source],
                       budget=Budget(cpu_seconds=20, memory_mb=512, wall_seconds=30, max_output_bytes=10_000),
                       required_outputs=["out"], idempotency_key=new_id("idem"), context_ref="recipe_run:rrun_x")
    result = raise_for_result(dispatch_isolated("compute-py", env, transport=local_pool, require_configured=False))
    ref = result.outputs["out"]
    assert control_store(settings).read(ref.artifact_id)[1] == b"hello"
    with session_scope() as s:
        task = s.get(WorkerTask, env.task_id)
        assert task.status == "completed" and task.outputs["out"]["artifact_id"] == ref.artifact_id
        assert task.envelope_hash == env.envelope_hash and task.finished_at is not None and task.usage["wall_seconds"] > 0
        types = [e.type for e in s.scalars(select(WorkerTaskEvent).where(WorkerTaskEvent.task_id == env.task_id)
                                            .order_by(WorkerTaskEvent.id))]
        assert types == ["task.started", "task.completed"]
        bus = [e.type for e in s.scalars(select(RunEvent).where(RunEvent.workspace_id == workspace).order_by(RunEvent.id))]
        assert bus == ["task.started", "task.completed"]
        edges = {(e.from_type, e.relation, e.to_type) for e in s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == workspace))}
        assert {("worker_artifact", "input_to", "worker_task"), ("worker_task", "produced", "worker_artifact"),
                ("recipe_run", "dispatched", "worker_task")} <= edges


@posix_only
def test_a_failed_task_is_recorded_with_its_structured_error(local_pool, workspace):
    from analystos.contracts.worker import Budget, ProbeSpec, TaskEnvelope
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import WorkerTask
    from analystos.workers.dispatch import capability_ref, dispatch_isolated

    env = TaskEnvelope(task_id=new_id("wtask"), workspace_id=workspace, capability=capability_ref("conformance.probe"),
                       spec=ProbeSpec(action="egress", args={"targets": [["127.0.0.1", 5432]]}),
                       budget=Budget(cpu_seconds=10, wall_seconds=20), idempotency_key=new_id("idem"))
    result = dispatch_isolated("compute-ml", env, transport=local_pool, require_configured=False)
    assert result.error.code == "egress_blocked"  # the control plane's own Postgres is unreachable from a job
    with session_scope() as s:
        task = s.get(WorkerTask, env.task_id)
        assert task.status == "failed" and task.error["code"] == "egress_blocked"


def test_the_api_serves_worker_routes_to_task_tokens_only(served_app, workspace):
    from analystos.core.config import get_settings
    from analystos.security.auth import issue_token
    from analystos.workers.store import control_store
    from analystos.workers.tokens import issue, secret_from_settings

    ref = control_store().put(workspace, b"abc", kind="blob")
    url = f"{served_app}/api/worker/artifacts/{ref.artifact_id}"
    token = issue(secret_from_settings(get_settings()), task_id="t", workspace_id=workspace, idempotency_key="idem-api-1",
                  reads=[ref.artifact_id])
    assert httpx.get(url, headers={"Authorization": f"Bearer {token}"}).content == b"abc"
    session = issue_token("u_x", "admin@analystos.local")
    assert httpx.get(url, headers={"Authorization": f"Bearer {session}"}).status_code == 401
    assert httpx.get(url).status_code == 401
    pools = httpx.get(f"{served_app}/api/health").json()["checks"]["isolated_pools"]
    assert pools["ok"] and pools["pools"] == sorted(get_settings().isolated_pool_set)


@posix_only
def test_a_recipe_snapshot_job_runs_on_compute_py_when_configured(served_app, local_pool, workspace, tmp_path,
                                                                   monkeypatch):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import WorkerTask
    from analystos.recipes.execute import SnapshotStore, run_snapshot_job
    from analystos.workers import dispatch
    from analystos.workflows.orchestrator import run_recipe_compute

    store = SnapshotStore(tmp_path / "recipe_snapshots")
    t = store.put(["k", "v"], [[1, 2.5], [2, 4.0], [1, 1.5]])
    job = {"sql": 'SELECT "k", SUM("v") AS "s" FROM "a"."t" GROUP BY "k" ORDER BY "k"', "tables": {"a.t": t},
           "max_rows": 10, "timeout_seconds": 10, "artifact_dir": str(tmp_path), "workspace_id": workspace}
    inline = run_snapshot_job(dict(job))
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-py")
    monkeypatch.setattr(dispatch, "default_transport", lambda settings=None: local_pool)
    assert run_recipe_compute(dict(job)) == inline
    with session_scope() as s:
        rows = s.scalars(select(WorkerTask).where(WorkerTask.workspace_id == workspace, WorkerTask.kind == "recipe.snapshot")).all()
        assert len(rows) == 1 and rows[0].status == "completed" and rows[0].pool == "compute-py"


def test_migration_0038_up_down_up(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig38"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)

        def tables() -> set[str]:
            return set(inspect(engine).get_table_names())

        command.upgrade(cfg, "0038")
        assert {"worker_task", "worker_task_event"} <= tables()
        command.downgrade(cfg, "0035")
        assert not {"worker_task", "worker_task_event"} & tables()
        command.upgrade(cfg, "0038")
        cols = {c["name"] for c in inspect(engine).get_columns("worker_task")}
        from analystos.db.models import WorkerTask

        assert cols == {c.name for c in WorkerTask.__table__.columns} - {"tenant_id"}  # tenant_id arrives with 0057
        ecols = {c["name"] for c in inspect(engine).get_columns("worker_task_event")}
        from analystos.db.models import WorkerTaskEvent

        assert ecols == {c.name for c in WorkerTaskEvent.__table__.columns} - {"tenant_id"}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()

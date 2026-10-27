"""Fixtures of the worker conformance suite (ADR-0022 decision 6, P7-06).

The suite runs against any pool implementation: `ANALYSTOS_CONFORMANCE_IMPL` picks it.

* `subprocess` (default): the local worker processes of `SubprocessTransport`, as the lite profile runs them;
* `temporal`: `IsolatedTaskWorkflow` + `analystos worker --queues <pool>` processes on a Temporal dev server
  (`temporalio.testing.WorkflowEnvironment.start_local`), see `test_worker_conformance_temporal.py`;
* `module:factory`: an out-of-tree implementation. `factory(artifact_url)` returns an object with
  `run(dispatch, on_event) -> TaskResult`, `records_events` and optionally `close()`; the worker behind
  it must serve the conformance probe (`--conformance`) and reach the store at `artifact_url`.

The artifact store is the real token-authenticated router (`api/routers/worker.py`) over a temporary
directory, served by uvicorn on a free local port. No database, Redis or Temporal is needed for the
default implementation.
"""
from __future__ import annotations

import importlib
import os
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from analystos.contracts.worker import ISOLATED_POOLS, Budget, ProbeSpec, TaskEnvelope
from analystos.core.ids import new_id
from analystos.workers.dispatch import ListSink, SubprocessTransport, capability_ref, dispatch_isolated
from analystos.workers.store import ArtifactStore


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """The subprocess transport reads worker pipes with select(), which Windows supports only for
    sockets. Pools run on Linux (containers, ADR-012), so the suite is POSIX-only like the sandbox tests."""
    if sys.platform != "win32":
        return
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason="worker pools need POSIX select() on pipes; run this suite on Linux")
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(here.resolve()):
            item.add_marker(skip)


class FakeRouter:
    """Stands in for the control plane's model router in the callback test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def complete(self, purpose, messages, *, ctx=None, max_tokens=None):  # noqa: ANN001, ANN201
        self.calls.append((purpose, ctx))
        return type("R", (), {"text": f"ok:{purpose}", "model": "fake/model"})()


@dataclass
class StoreServer:
    url: str
    store: ArtifactStore
    secret: bytes
    router: FakeRouter
    port: int


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def store_server(tmp_path_factory) -> StoreServer:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from analystos.api.routers import worker as worker_routes
    from analystos.core.errors import AnalystOSError
    from analystos.llm.router import CallContext

    root = tmp_path_factory.mktemp("worker-store")
    store, secret, fake = ArtifactStore(root), secrets.token_bytes(32), FakeRouter()
    app = FastAPI()
    app.include_router(worker_routes.router)

    @app.exception_handler(AnalystOSError)
    async def _err(_, exc: AnalystOSError):  # noqa: ANN001, ANN202
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    app.dependency_overrides[worker_routes.worker_store] = lambda: store
    app.dependency_overrides[worker_routes.worker_secret] = lambda: secret
    app.dependency_overrides[worker_routes.model_router] = lambda: fake
    app.dependency_overrides[worker_routes.call_context] = lambda: (
        lambda claims: CallContext(workspace_id=claims.workspace_id, task_id=claims.task_id, agent_id="worker"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "conformance artifact store did not start"
    yield StoreServer(url=f"http://127.0.0.1:{port}", store=store, secret=secret, router=fake, port=port)
    server.should_exit = True
    thread.join(timeout=10)


def _make_transport(url: str) -> Any:
    impl = os.environ.get("ANALYSTOS_CONFORMANCE_IMPL", "subprocess")
    if impl == "subprocess":
        return SubprocessTransport(url, conformance=True)
    if impl == "temporal":
        from tests.conformance.worker.temporal_impl import factory

        try:
            return factory(url)
        except Exception as exc:  # noqa: BLE001 - no dev server binary here
            pytest.skip(f"Temporal dev server unavailable: {type(exc).__name__}: {exc}")
    if ":" in impl:
        module, _, attr = impl.partition(":")
        return getattr(importlib.import_module(module), attr)(url)
    pytest.skip(f"conformance implementation {impl!r} is driven by its own test module")


@pytest.fixture(scope="session")
def transport(store_server):
    t = _make_transport(store_server.url)
    yield t
    if hasattr(t, "close"):
        t.close()


@pytest.fixture(params=ISOLATED_POOLS)
def pool(request) -> str:
    return request.param


@pytest.fixture()
def workspace() -> str:
    return new_id("ws")


class Runner:
    def __init__(self, server: StoreServer, transport: Any, pool: str, workspace: str) -> None:
        self.server, self.transport, self.pool, self.workspace = server, transport, pool, workspace

    def envelope(self, action: str, args: dict[str, Any] | None = None, *, outputs: list[str] | None = None,
                 inputs: list | None = None, budget: dict[str, Any] | None = None, key: str | None = None,
                 **extra: Any) -> TaskEnvelope:
        return TaskEnvelope(task_id=new_id("wtask"), workspace_id=self.workspace,
                            capability=capability_ref("conformance.probe"),
                            spec=ProbeSpec(action=action, args=args or {}), input_artifacts=inputs or [],
                            budget=Budget(**{"cpu_seconds": 20, "memory_mb": 1024, "wall_seconds": 30,
                                             "max_output_bytes": 1_000_000, **(budget or {})}),
                            required_outputs=outputs or [], idempotency_key=key or new_id("idem"), **extra)

    def run(self, envelope: TaskEnvelope, *, sink: ListSink | None = None, attempts: int = 1, **kw: Any):
        return dispatch_isolated(self.pool, envelope, transport=self.transport, sink=sink or ListSink(),
                                 secret=self.server.secret, require_configured=False, attempts=attempts, **kw)

    def put(self, data: bytes, *, workspace: str | None = None, kind: str = "blob"):
        return self.server.store.put(workspace or self.workspace, data, kind=kind)


@pytest.fixture()
def runner(store_server, transport, pool, workspace) -> Runner:
    return Runner(store_server, transport, pool, workspace)


@pytest.fixture()
def worker_python() -> str:
    import sys

    return sys.executable


@pytest.fixture()
def src_root() -> str:
    import analystos

    return str(Path(analystos.__file__).resolve().parents[1])

from __future__ import annotations

import gc
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from analystos.api.request_id import RequestIdMiddleware, current, envelope
from analystos.api.routers import admin, analysis, artifacts, auth, capabilities, catalog, continuous, workspaces
from analystos.api.routers import ask as ask_router
from analystos.api.routers import bi_dashboards as bi_dashboards_router
from analystos.api.routers import builds as builds_router
from analystos.api.routers import context_export as context_export_router
from analystos.api.routers import context_grounding as context_grounding_router
from analystos.api.routers import context_v2 as context_v2_router
from analystos.api.routers import conversations as conversations_router
from analystos.api.routers import dashboards_v2 as dashboards_v2_router
from analystos.api.routers import data_shape as data_shape_router
from analystos.api.routers import decisions as decisions_router
from analystos.api.routers import definitions as definitions_router
from analystos.api.routers import deliveries as deliveries_router
from analystos.api.routers import desk as desk_router
from analystos.api.routers import entity_matching as entity_matching_router
from analystos.api.routers import evidence as evidence_router
from analystos.api.routers import goals_v2 as goals_v2_router
from analystos.api.routers import index_advice as index_advice_router
from analystos.api.routers import investigation_value as investigation_value_router
from analystos.api.routers import knowledge as knowledge_router
from analystos.api.routers import mcp as mcp_router
from analystos.api.routers import ml as ml_router
from analystos.api.routers import models_admin as models_admin_router
from analystos.api.routers import ops_v2 as ops_v2_router
from analystos.api.routers import pilot as pilot_router
from analystos.api.routers import pipelines as pipelines_router
from analystos.api.routers import playbook_editor as playbook_editor_router
from analystos.api.routers import process as process_router
from analystos.api.routers import quality as quality_router
from analystos.api.routers import recipes as recipes_router
from analystos.api.routers import registries as registries_router
from analystos.api.routers import report_docs as report_docs_router
from analystos.api.routers import scenarios as scenarios_router
from analystos.api.routers import semantic as semantic_router
from analystos.api.routers import steps as steps_router
from analystos.api.routers import tenants as tenants_router
from analystos.api.routers import tools_v2 as tools_v2_router
from analystos.api.routers import triggers_v2 as triggers_v2_router
from analystos.api.routers import work_orders as work_orders_router
from analystos.api.routers import worker as worker_router
from analystos.api.routers import workspace_brief as brief_router
from analystos.api.security_headers import SecurityHeadersMiddleware
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError
from analystos.core.logging import configure_logging, get_logger
from analystos.mcp import server as mcp_server
from analystos.observability.asgi import MetricsMiddleware
from analystos.tenancy.middleware import TenantMiddleware

configure_logging()
log = get_logger("analystos.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The import graph (statistics, ML, reporting stacks) is hundreds of thousands of long-lived
    # objects. Freezing them keeps full GC collections from rescanning them: each one otherwise
    # pauses the event loop for 100-150 ms, which every open SSE stream feels (P4-C04 load test).
    gc.collect()
    gc.freeze()
    # Lite profile (ADR-0025): the API process is also the orchestrator and the scheduler. Resume
    # re-drives every non-terminal run left by a previous process; the scheduler loop claims before it
    # fires, so it is safe next to a separate `analystos scheduler`.
    from analystos.workflows.orchestrator import start_local_runtime

    settings = get_settings()
    from analystos.observability import metrics as _metrics
    from analystos.observability.telemetry import setup_tracing

    setup_tracing("api")
    _metrics.set_gauge("analystos_build_info", 1, version=app.version, component="api")
    runtime = start_local_runtime()
    from analystos.services.stale_work import sweep_quietly

    sweep_quietly()  # Ask turns and ML experiments a previous process left "running" end as failed
    scheduler_stop = None
    if settings.run_inprocess_scheduler:
        from analystos.services.schedules import start_inprocess_scheduler

        scheduler_stop = start_inprocess_scheduler()
    try:
        yield
    finally:
        if scheduler_stop is not None:
            scheduler_stop.set()
        from analystos.workers.dispatch import reset_default_transport

        reset_default_transport()  # local isolated worker processes (P7-06), if any were started
        if runtime is not None:
            runtime.shutdown()
            from analystos.workflows.orchestrator import reset_local_runtime

            reset_local_runtime()


app = FastAPI(title="Context2AI AnalystOS", version="0.1.0", lifespan=lifespan,
              description="Autonomous, governed data & analytics agent operating system (Phase 1 MVP).")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["ETag", "Idempotent-Replayed", "Location", "X-Request-ID"])  # P4-06
app.add_middleware(MetricsMiddleware)  # S12: request latency per route class + the request span (inside the tenant)
app.add_middleware(TenantMiddleware)  # binds the caller's tenant before any route code (ADR-009)
app.add_middleware(SecurityHeadersMiddleware)  # nosniff, no framing, CSP and no-store on every response
app.add_middleware(RequestIdMiddleware)  # outermost: every response, errors included, names its request
for r in (auth.router, workspaces.router, analysis.router, artifacts.router, admin.router, continuous.router, catalog.router,
          capabilities.router, registries_router.router, semantic_router.router):
    app.include_router(r)
app.include_router(mcp_router.router)
app.include_router(tools_v2_router.router)  # tool, agent and data-product registry (S10)
# The web client reaches only /api (nginx and the dev proxy forward nothing else): the same routes again under /api,
# kept out of the schema so the generated client keeps one typed path per route. External callers keep /v2.
app.include_router(tools_v2_router.router, prefix="/api", include_in_schema=False)
app.include_router(quality_router.router)  # declared quality rules (P1-7)
app.include_router(quality_router.products)  # data-product contracts and access requests (P2-9)
app.include_router(quality_router.products, prefix="/api", include_in_schema=False)
app.include_router(triggers_v2_router.router)  # signed triggers, incidents, conversation channels (S09)
app.include_router(models_admin_router.router)  # model gateway admin (S05)
app.include_router(ops_v2_router.router)  # S12: model evaluation matrix, spend (cost ledger) and its export
app.include_router(decisions_router.router)
app.include_router(builds_router.router)
app.include_router(ask_router.router)
app.include_router(tenants_router.router)
app.include_router(scenarios_router.router)  # what-if scenarios (N-9)
app.include_router(knowledge_router.router)
app.include_router(context_export_router.router)  # download and inspect the workspace context (Stream D)
app.include_router(context_v2_router.router)  # Context Service over the ledger (S03): search/get/compile/changes, OKF
app.include_router(evidence_router.router)
app.include_router(definitions_router.router)
app.include_router(playbook_editor_router.router)  # S06-T6: YAML playbook drafts, simulation, signing
app.include_router(work_orders_router.router)
app.include_router(recipes_router.router)
app.include_router(brief_router.router)
app.include_router(steps_router.router)
app.include_router(pipelines_router.router)
app.include_router(worker_router.router)  # token-authenticated routes for isolated compute workers (P7-06)
app.include_router(ml_router.router)
app.include_router(entity_matching_router.router)  # general entity matching (INT-004, N-7)
app.include_router(pilot_router.router)  # named owners and pilot readiness (P4-09)
app.include_router(investigation_value_router.router)
app.include_router(context_grounding_router.router)
app.include_router(process_router.router)  # process and task mining over event logs
app.include_router(desk_router.router)  # Analyst Request Desk (S13)
app.include_router(conversations_router.router)  # API v2 conversations and memory (S04)
app.include_router(goals_v2_router.router)  # Goal API v2 (S06): goals, signals, run state, SSE, evidence, plans
app.include_router(goals_v2_router.router, prefix="/api")  # the same, for the web client (/api/v2/...)
# before S08's /schemas/{kind}: /schemas/report-document is this router's
app.include_router(report_docs_router.router)  # CONV-04/05: report documents by conversation, dashboard stories
app.include_router(dashboards_v2_router.router)  # S08 dashboard builder: typed ops, versions, publish, embed, exports
app.include_router(deliveries_router.router)  # approved external email/webhook delivery (N-3)
app.include_router(data_shape_router.router)  # what the selected tables are good for (patterns, next steps)
app.include_router(index_advice_router.router)  # index / partitioning / clustering advice, never applied (N-11)
app.include_router(bi_dashboards_router.router)  # existing-dashboard mode (N-2, BI-011/012)
MCP_GATE = mcp_server.mount(app)  # MCP protocol endpoint at /mcp (P4-X06); None without the `mcp` extra


@app.exception_handler(AnalystOSError)
async def domain_error(request: Request, exc: AnalystOSError):
    return JSONResponse(status_code=exc.http_status, content=envelope(exc.to_dict(), request))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    import json

    errors = json.loads(json.dumps(exc.errors(), default=str))  # a validator's ValueError in `ctx` is not JSON
    return JSONResponse(status_code=422, content=envelope({"code": "invalid_input", "message": "request validation failed",
                                                           "details": {"errors": errors}, "retryable": False}, request))


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    """An unexpected failure is still the one envelope, with the request id to find it in the logs."""
    from analystos.observability.metrics import rls_denied

    rls_denied(exc, "api")
    return JSONResponse(status_code=500, content=envelope({"code": "internal_error", "message": "internal error",
                                                           "details": {}, "retryable": False}, request),
                        headers={"X-Request-ID": current(request) or ""})


# The dependency checks answer within this, below the container probe's 3 s: a stopped optional
# service (a DNS lookup of a removed compose host alone takes ~4 s) must read as "down", never make
# the API itself look unhealthy. The worker check (it reads the Temporal result) gets what is left.
HEALTH_DEADLINE_S = 2.0
HEALTH_BUDGET_S = 2.5


def _run_checks(checks: dict[str, dict], fns: list[tuple[str, object]], deadline_s: float | None = None) -> None:
    """Run the checks concurrently; one with no answer by the deadline is reported down."""
    from concurrent.futures import ThreadPoolExecutor

    deadline_s = HEALTH_DEADLINE_S if deadline_s is None else deadline_s

    def timed(fn):
        started = time.perf_counter()
        try:
            return {"ok": True, "ms": round((time.perf_counter() - started) * 1000), **(fn() or {})}
        except Exception as exc:  # noqa: BLE001 - health never raises
            return {"ok": False, "error": str(exc)[:200]}

    pool = ThreadPoolExecutor(max_workers=len(fns), thread_name_prefix="health")
    futures = {name: pool.submit(timed, fn) for name, fn in fns}
    end = time.perf_counter() + deadline_s
    for name, fut in futures.items():
        try:
            checks[name] = fut.result(timeout=max(0.0, end - time.perf_counter()))
        except TimeoutError:
            checks[name] = {"ok": False, "error": f"no answer within {deadline_s:g} s"}
    pool.shutdown(wait=False)  # a check stuck in a DNS lookup finishes on its own; the answer does not wait


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request):
    """Prometheus exposition (S12 §2). Optional bearer token ANALYSTOS_METRICS_TOKEN; the chart's NetworkPolicy
    limits the port to the monitoring namespace either way."""
    import hmac
    import os

    from fastapi.responses import PlainTextResponse

    from analystos.observability.metrics import render

    token = os.environ.get("ANALYSTOS_METRICS_TOKEN", "")
    if token:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            return PlainTextResponse("unauthorized", status_code=401)
    return PlainTextResponse(render(), media_type="text/plain; version=0.0.4")


def _drives_runs(settings) -> str:
    return "in `analystos driver` processes" if getattr(settings, "run_driver", "api") == "process" else "in the API process"


@app.get("/api/health")
def health():
    """Dependency health. Reports what is reachable; never claims readiness it cannot observe."""
    from sqlalchemy import text

    from analystos.db.base import get_engine

    checks: dict[str, dict] = {}
    started = time.perf_counter()
    settings = get_settings()

    def pg():
        with get_engine().connect() as c:
            c.execute(text("select 1"))

    def redis_():
        if not settings.redis_url:  # lite: counters and caches live in Postgres and in process
            return {"enabled": False, "status": "not configured (lite profile)", "spend_counters": settings.spend_store}
        import redis

        redis.Redis.from_url(settings.redis_url, socket_timeout=1).ping()
        return {"spend_counters": settings.spend_store}

    def neo():
        if not settings.graph_enabled:  # optional projection; lineage and neighbourhood come from Postgres
            return {"enabled": False, "status": "disabled"}
        from analystos.graph.projection import _driver

        _driver().verify_connectivity()
        return {"enabled": True}

    from analystos.workflows.backends import capabilities

    caps = capabilities(settings)

    def temporal():
        if not caps.needs_worker:
            return {"enabled": False, "status": f"{caps.name} orchestrator ({_drives_runs(settings)})"}
        import socket

        host, port = settings.temporal_address.split(":")
        socket.create_connection((host, int(port)), timeout=1).close()

    def superset():
        if not settings.superset_url:
            return {"enabled": False, "status": "not configured: publishing uses the preview destination (add the bi profile)"}
        import httpx

        httpx.get(f"{settings.superset_url}/health", timeout=2).raise_for_status()

    def models():
        from analystos.runtime.context import default_router

        r = default_router()
        return {"chat": r.available("planning"), "jev": r.available("risk_check")}

    def orchestrator():
        """The selected backend can run here (its extra, its guards) and, with driver processes, one is alive."""
        from analystos.core.profiles import orchestrator_summary
        from analystos.workflows.backends import get_backend, validate

        validate(settings)
        out = {**orchestrator_summary(settings), **get_backend(settings).health()}
        if caps.parks and getattr(settings, "run_driver", "api") == "process":
            from analystos.workflows.driver import live_drivers

            drivers = live_drivers()
            if not drivers:
                raise RuntimeError("no `analystos driver` process holds a heartbeat: runs wait until one starts")
            out["drivers"] = drivers
        return out

    def worker():
        if not caps.needs_worker:
            return {"enabled": False, "status": f"{caps.name} orchestrator: runs execute {_drives_runs(settings)}"}
        if not checks.get("temporal", {}).get("ok"):
            raise RuntimeError("unknown: Temporal is not reachable")
        from analystos.workflows.orchestrator import worker_status

        st = worker_status()
        if not st["ok"]:
            raise RuntimeError(f"no worker is polling {', '.join(st['missing'])}")
        return {"queues": st["queues"]}

    _run_checks(checks, [("postgres", pg), ("redis", redis_), ("neo4j", neo), ("temporal", temporal),
                         ("superset", superset), ("models", models), ("orchestrator", orchestrator)])
    _run_checks(checks, [("worker", worker)], max(0.3, started + HEALTH_BUDGET_S - time.perf_counter()))

    # P4-02: the Python sandbox's isolation, as the gate sees it. Not ok = sandboxed code is refused
    # (or, in `off` mode, runs unisolated: development only).
    try:
        from analystos.sandbox.isolation import status

        st = status()
        checks["sandbox"] = {"ok": st.available and st.isolated, "mode": st.mode, "backend": st.backend,
                             "available": st.available, "isolated": st.isolated, "enforced": st.enforced,
                             "detail": st.detail}
    except Exception as exc:  # noqa: BLE001 - health never raises
        checks["sandbox"] = {"ok": False, "available": False, "error": str(exc)[:200]}
    # P7-06: isolated compute pools this installation dispatches to (opt-in) and how it reaches them.
    pools = sorted(settings.isolated_pool_set)
    transport = settings.isolated_transport
    if transport == "auto":
        transport = caps.isolated_transport
    checks["isolated_pools"] = {"ok": True, "enabled": bool(pools), "pools": pools,
                                "transport": transport if pools else None}
    # Informational: a deployment without the `mcp` extra serves no /mcp, so MCP clients cannot connect.
    checks["mcp"] = {"ok": True, "enabled": MCP_GATE is not None, "path": mcp_server.MCP_PATH}
    from analystos.core.profiles import summary

    problems = health_problems(checks)
    return {"ok": checks["postgres"]["ok"], "degraded": bool(problems), "problems": problems,
            "orchestrator": settings.orchestrator, "installation": summary(settings), "checks": checks}


# What a dependency being down means for someone using the product, and how loudly to say it: the UI's
# status banner shows `critical` and `warning`; `info` (optional features with a fallback) stays in the details.
HEALTH_IMPACT = {
    "postgres": ("critical", "The platform database is unreachable: nothing can be loaded or saved until it is back."),
    "temporal": ("critical", "Temporal is unreachable: new runs cannot start and running ones wait until it is back."),
    "worker": ("critical", "No worker is running: runs stay queued until `analystos worker` is started."),
    "orchestrator": ("critical", "The selected orchestrator cannot drive runs here: new runs wait until it can."),
    "redis": ("warning", "Redis is unreachable: model calls are refused (spend caps fail closed) and live updates "
                         "fall back to polling."),
    "superset": ("warning", "Superset is not reachable: new publications go to the in-platform preview, and an approved "
                            "Superset publication fails until it is back."),
    "neo4j": ("info", "Neo4j is unreachable: lineage is served from Postgres."),
    "sandbox": ("info", "The Python sandbox is unavailable here: Python code steps are refused; analysis runs are unaffected."),
}


def health_problems(checks: dict[str, dict]) -> list[dict]:
    """Label every check up / down / off (in place) and list the down ones with their plain-language impact."""
    problems = []
    for name, c in checks.items():
        c["state"] = "off" if c.get("enabled") is False else "up" if c.get("ok") else "down"
        if c["state"] == "down":
            severity, message = HEALTH_IMPACT.get(name, ("warning", f"{name} is not healthy."))
            problems.append({"dependency": name, "severity": severity, "message": message,
                             "detail": c.get("error") or c.get("detail")})
    return problems

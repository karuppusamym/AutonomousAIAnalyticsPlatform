"""CTX-005 measurement: compiled-context reuse on the standard deterministic local flow (ServiceNow
mock, one full run, a redirect, then an Ask thread where the question is asked again and refreshed),
with every chat purpose forced to call the model (`always`) against a counting fake transport (no
network, no real model). JEV decisions and the L0 response cache are off, so every prompt is built.

Set ANALYSTOS_CONTEXT_REUSE_OUT=<file.json> to write the per-purpose counts (evidence input for
docs/60-delivery/evidence/ctx-005-context-reuse-*.md)."""
from __future__ import annotations

import json
import os
import time

import pytest
from sqlalchemy import select
from tests.integration.test_context_tokens import (  # noqa: F401
    CALLS,
    CountingTransport,
    _purpose,
    _wait,
    servicenow_url,
)

pytestmark = pytest.mark.integration


def test_compiled_contexts_are_reused_within_a_run_and_a_thread(control_db, servicenow_url, monkeypatch):  # noqa: F811
    from analystos.agents import common
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, PlatformSetting, User
    from analystos.llm import router as router_mod
    from analystos.llm.config import load_models_config
    from analystos.runtime.context import default_router
    from analystos.services import platform_settings

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-000000000000000000000000")
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(router_mod, "HttpTransport", CountingTransport)
    original = router_mod.ModelRouter.complete

    def complete(self, purpose, messages, **kw):
        _purpose.value = purpose
        return original(self, purpose, messages, **kw)

    monkeypatch.setattr(router_mod.ModelRouter, "complete", complete)
    timings: dict[str, list[float]] = {}
    compile_ = common._compile

    def timed(ctx, purpose, *a, **kw):
        started = time.perf_counter()
        try:
            return compile_(ctx, purpose, *a, **kw)
        finally:
            timings.setdefault(purpose, []).append((time.perf_counter() - started) * 1000)

    monkeypatch.setattr(common, "_compile", timed)
    chat_always = {p: "always" for p, profile in load_models_config().routing.items() if profile != "decision"}
    CALLS.clear()
    common._COMPILED.clear()
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin_id = s.scalar(select(User.id).where(User.email == get_settings().bootstrap_admin_email))
        last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
        previous_doc, version = (dict(last.document), last.version) if last else ({}, 0)
        s.add(PlatformSetting(version=version + 1, created_by=admin_id, note="context reuse measurement",
                              document={**previous_doc, "llm": {**previous_doc.get("llm", {}), "purpose_modes": chat_always,
                                                                "cache_enabled": False},
                                        "features": {**previous_doc.get("features", {}), "jev_decisions": False}}))
    platform_settings.invalidate()
    try:
        from analystos.services.ask import ask_in_thread, create_thread
        from analystos.services.runs import _interpret_redirect, create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.id == admin_id))
            ws = create_workspace(s, admin, name="context reuse", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin",
                                          "tables": ["incident", "change_request"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
        assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"
        run_stats = common.compiled_context_stats()
        with session_scope() as s:
            finished = s.get(AnalysisRun, run.id)
            s.expunge(finished)
        _interpret_redirect(finished, "focus on priority 1 incidents in the network group")
        with session_scope() as s:
            thread = create_thread(s, s.merge(admin), ws_id)["id"]
        question = "Which incidents breached SLA after a change, by priority?"  # not a rules shape: generation
        turns = [ask_in_thread(admin, thread, question) for _ in range(3)]  # asked, asked again, refreshed
        stats = common.compiled_context_stats()
        report = {"run_id": run.id, "thread_id": thread, "provider_calls": len(CALLS), "run": run_stats, "total": stats,
                  "turns": [{"status": t["status"], "refusal": (t.get("refusal") or {}).get("kind")} for t in turns],
                  "compile_ms": {p: {"n": len(v), "mean": round(sum(v) / len(v), 2), "max": round(max(v), 2)}
                                 for p, v in timings.items()}}
        out = os.environ.get("ANALYSTOS_CONTEXT_REUSE_OUT")
        if out:
            with open(out, "w") as f:
                json.dump(report, f, indent=2)
        # The fake answers {} so generation has no SQL: each turn is refused as invalid output, and
        # the second and third turns reuse the thread's compiled context instead of recompiling.
        assert [t["refusal"]["kind"] for t in turns] == ["invalid_output"] * 3
        assert stats["sql_generation"]["compiled"] == 1 and stats["sql_generation"]["reused"] == 2, stats
        compiled = sum(v["compiled"] for v in stats.values())
        assert compiled == sum(len(v) for v in timings.values())  # every miss compiled once, every hit compiled nothing
    finally:
        with session_scope() as s:
            last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
            s.add(PlatformSetting(version=last.version + 1, created_by=admin_id, note="restore after context reuse measurement",
                                  document=previous_doc))
        platform_settings.invalidate()
        get_settings.cache_clear()
        default_router.cache_clear()
        common._COMPILED.clear()

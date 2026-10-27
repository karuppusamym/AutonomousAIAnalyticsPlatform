"""P4-T03 measurement: prompt tokens per run on the deterministic local flow with every chat
purpose forced to call the model (`always`), against a counting fake transport (no network, no
real model). JEV decisions and the L0 response cache are off so every prompt is built and counted.

Set ANALYSTOS_TOKEN_MEASURE_OUT=<file.json> to write the per-purpose counts (evidence input for
docs/60-delivery/evidence/context-compiler-*.md). Token estimate: characters / 3.6 of the exact
message text sent (`llm.cache.estimate_tokens`), the platform's own estimator."""
from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servicenow_url():
    from analystos.connectors.servicenow_mock import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


_purpose = threading.local()
CALLS: list[dict] = []


def _text(content) -> str:
    return "".join(b.get("text", "") for b in content) if isinstance(content, list) else str(content)


def _prefix_chars(messages: list[dict]) -> int:
    """Characters of the stable prefix the request marks for caching (cache_control blocks), or the
    system message when the request carries no marks (the pre-P4-T04 layout)."""
    marked, total = 0, 0
    for m in messages:
        for block in (m["content"] if isinstance(m["content"], list) else [{"text": m["content"]}]):
            total += len(block.get("text", ""))
            if block.get("cache_control"):
                marked = total
    return marked or len(_text(messages[0]["content"]))


class CountingTransport:
    def __init__(self, **_: object) -> None:  # the router passes allowed_hosts (egress guard)
        pass

    def chat(self, *, base_url, api_key, payload, timeout):
        text = "".join(_text(m["content"]) for m in payload["messages"])
        CALLS.append({"purpose": getattr(_purpose, "value", "?"), "model": payload["model"], "chars": len(text),
                      "prefix_chars": _prefix_chars(payload["messages"]),
                      "blocks": any(isinstance(m["content"], list) for m in payload["messages"])})
        return {"model": payload["model"], "choices": [{"message": {"content": "{}"}}], "usage": {}}

    def decide(self, *, base_url, api_key, payload, timeout):  # pragma: no cover - JEV is switched off below
        raise AssertionError("decision calls are disabled for this measurement")


def _wait(run_id: str, statuses: set[str], timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in statuses:
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def test_prompt_tokens_per_run_with_every_chat_purpose_on(control_db, servicenow_url, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall, PlatformSetting, User
    from analystos.llm import router as router_mod
    from analystos.llm.cache import estimate_tokens
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
    # Every chat purpose model-first (P4-T02 made `auto` the default); decision purposes stay off.
    # The pinned model table keeps a prompt-cache family (Claude) so cache breakpoints are measured; the shipped
    # models.yaml blocks that family for cost (blocked_families).
    from tests.conftest import PINNED_MODELS

    monkeypatch.setenv("ANALYSTOS_MODELS_CONFIG", str(PINNED_MODELS))
    CALLS.clear()
    get_settings.cache_clear()
    load_models_config.cache_clear()
    CHAT_ALWAYS = {p: "always" for p, profile in load_models_config().routing.items() if profile != "decision"}
    default_router.cache_clear()
    with session_scope() as s:
        admin_id = s.scalar(select(User.id).where(User.email == get_settings().bootstrap_admin_email))
        last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
        previous_doc, version = (dict(last.document), last.version) if last else ({}, 0)
        s.add(PlatformSetting(version=version + 1, created_by=admin_id, note="token measurement",
                              document={**previous_doc, "llm": {**previous_doc.get("llm", {}), "purpose_modes": CHAT_ALWAYS,
                                                                "cache_enabled": False},
                                        "features": {**previous_doc.get("features", {}), "jev_decisions": False}}))
    platform_settings.invalidate()
    try:
        from analystos.agents.sql_agent import ask as sql_ask
        from analystos.api.routers.analysis import _adhoc
        from analystos.core.errors import AnalystOSError
        from analystos.db.models import AnalysisRun
        from analystos.services.runs import _interpret_redirect, create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.id == admin_id))
            ws = create_workspace(s, admin, name="token measurement", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident", "change_request"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
        assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"
        with session_scope() as s:
            finished = s.get(AnalysisRun, run.id)
            s.expunge(finished)
        _interpret_redirect(finished, "focus on priority 1 incidents in the network group")
        with session_scope() as s:
            ctx = _adhoc(s, admin, ws_id)
        with pytest.raises(AnalystOSError):  # the fake answers {}: no SQL, so Ask declines after one call
            sql_ask(ctx, "How many incidents breached SLA by priority?")

        per_purpose: dict[str, dict] = {}
        for c in CALLS:
            p = per_purpose.setdefault(c["purpose"], {"calls": 0, "chars": 0, "prefix_chars": 0})
            p["calls"] += 1
            p["chars"] += c["chars"]
            p["prefix_chars"] += c["prefix_chars"]
        for p in per_purpose.values():
            p["est_tokens"] = estimate_tokens("x" * p["chars"])
        total_chars = sum(c["chars"] for c in CALLS)
        report = {"run_id": run.id, "calls": len(CALLS), "chars": total_chars, "est_tokens": estimate_tokens("x" * total_chars),
                  "prefix_chars": sum(c["prefix_chars"] for c in CALLS), "per_purpose": per_purpose,
                  "cache_control_calls": sum(1 for c in CALLS if c["blocks"])}
        out = os.environ.get("ANALYSTOS_TOKEN_MEASURE_OUT")
        if out:
            with open(out, "w") as f:
                json.dump(report, f, indent=2)
        assert {"planning", "hypothesis_generation", "sql_generation", "feedback_interpretation"} <= set(per_purpose)
        # P4-T03/T04: compiled calls carry receipts, and the stable prefix is marked for provider caching
        with session_scope() as s:
            rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run.id, ModelCall.status == "ok",
                                                          ModelCall.purpose == "hypothesis_generation")))
        assert rows and all(r.context_receipts for r in rows)
        anthropic = [c for c in CALLS if c["model"].startswith("anthropic/")]  # prompt_cache: true in the pinned table
        assert anthropic and all(c["blocks"] for c in anthropic)
        assert not any(c["blocks"] for c in CALLS if not c["model"].startswith("anthropic/"))
    finally:
        with session_scope() as s:
            last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
            s.add(PlatformSetting(version=last.version + 1, created_by=admin_id, note="restore after token measurement",
                                  document=previous_doc))
        platform_settings.invalidate()
        get_settings.cache_clear()
        default_router.cache_clear()
        load_models_config.cache_clear()  # the pinned table must not leak into later tests

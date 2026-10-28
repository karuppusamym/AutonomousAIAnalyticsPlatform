"""Token economy measurement (P4-T04 / Stream B): what the standard deterministic local flow sends to
models, and how much of it sits in the stable, cache-flagged prefix.

Flow (the context-compiler evidence flow, `test_context_tokens.py`): ServiceNow mock with `incident`
and `change_request`, one full analysis run on the local orchestrator (publication skipped), one
redirect interpretation, then an Ask thread where a generation question is asked, asked again and
refreshed. Every chat purpose is forced to `always`, JEV decisions and the L0 response cache are off,
so every prompt is built and reaches a counting fake transport (no network, no real model). The
fake answers every call with a well-formed but empty JSON object, so each agent takes its
deterministic path identically before and after a change.

Per provider request it records the characters sent, the characters of the cache-flagged prefix
(the leading messages marked `cache: True`, i.e. what carries a `cache_control` breakpoint on a
cache-capable model) and the longest prefix shared with any earlier request of the flow (an upper
bound on what automatic prefix caching could reuse). Compiled-context reuse comes from
`agents.common.compiled_context_stats()`.

Set ANALYSTOS_ECONOMY_OUT=<file.json> to write the report; `scripts/measure_context_economy.py`
runs this test and renders the before/after table."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest
from sqlalchemy import select
from tests.integration.test_context_tokens import _wait, servicenow_url  # noqa: F401

pytestmark = pytest.mark.integration

_state = threading.local()
REQUESTS: list[dict] = []
ANSWER = {"findings": [], "reviews": []}  # well-formed and empty: every purpose falls back to its rules


def _content(content) -> str:
    return "".join(b.get("text", "") for b in content) if isinstance(content, list) else str(content or "")


def _flagged_prefix(messages: list[dict], json_output: bool) -> int:
    """Characters of the leading messages up to the last one marked `cache: True` (the router appends
    the JSON instruction to the first message, which is part of that prefix when it is flagged)."""
    from analystos.llm.router import JSON_INSTRUCTION

    upto, total = 0, 0
    for i, m in enumerate(messages):
        total += len(str(m.get("content") or "")) + (len(JSON_INSTRUCTION) if i == 0 and json_output else 0)
        if m.get("cache"):
            upto = total
    return upto


class EconomyTransport:
    def __init__(self, **_: object) -> None:  # the router passes allowed_hosts (egress guard)
        pass

    def chat(self, *, base_url, api_key, payload, timeout):
        text = "\n".join(f"{m['role']}:{_content(m['content'])}" for m in payload["messages"])
        shared = max((len(os.path.commonprefix([text, r["_text"]])) for r in REQUESTS), default=0)
        logical = getattr(_state, "logical", None) or {}
        REQUESTS.append({"purpose": getattr(_state, "purpose", "?"), "model": payload["model"],
                         "chars": sum(len(_content(m["content"])) for m in payload["messages"]),
                         "flagged_prefix_chars": logical.get("flagged", 0), "shared_prefix_chars": shared,
                         "breakpoints": sum(1 for m in payload["messages"] if isinstance(m["content"], list)
                                            for b in m["content"] if b.get("cache_control")),
                         "_text": text})
        return {"model": payload["model"], "choices": [{"message": {"content": json.dumps(ANSWER)}}], "usage": {}}

    def decide(self, *, base_url, api_key, payload, timeout):  # pragma: no cover - JEV is switched off below
        raise AssertionError("decision calls are disabled for this measurement")


def summarize(requests: list[dict]) -> dict:
    from analystos.llm.cache import estimate_tokens

    per: dict[str, dict] = {}
    for r in requests:
        p = per.setdefault(r["purpose"], {"calls": 0, "chars": 0, "flagged_prefix_chars": 0, "shared_prefix_chars": 0})
        p["calls"] += 1
        for k in ("chars", "flagged_prefix_chars", "shared_prefix_chars"):
            p[k] += r[k]
    for p in per.values():
        p["est_tokens"] = estimate_tokens("x" * p["chars"])
        p["flagged_share"] = round(p["flagged_prefix_chars"] / p["chars"], 4) if p["chars"] else 0.0
        p["shared_share"] = round(p["shared_prefix_chars"] / p["chars"], 4) if p["chars"] else 0.0
    chars = sum(r["chars"] for r in requests)
    flagged = sum(r["flagged_prefix_chars"] for r in requests)
    shared = sum(r["shared_prefix_chars"] for r in requests)
    return {"calls": len(requests), "chars": chars, "est_tokens": estimate_tokens("x" * chars) if chars else 0,
            "flagged_prefix_chars": flagged, "flagged_share": round(flagged / chars, 4) if chars else 0.0,
            "shared_prefix_chars": shared, "shared_share": round(shared / chars, 4) if chars else 0.0,
            "breakpoint_requests": sum(1 for r in requests if r["breakpoints"]), "per_purpose": per}


def _shared_stats() -> dict | None:
    """Hits and misses of the shared context cache (compiled contexts, retrieval), where the tree has one."""
    try:
        from analystos.context import cache
    except ImportError:  # the tree before Stream B
        return None
    return cache.stats()


def test_context_economy_of_the_standard_flow(control_db, servicenow_url, monkeypatch):  # noqa: F811
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
    monkeypatch.setattr(router_mod, "HttpTransport", EconomyTransport)
    original = router_mod.ModelRouter.complete

    def complete(self, purpose, messages, **kw):
        _state.purpose = purpose
        _state.logical = {"flagged": _flagged_prefix(messages, bool(kw.get("json_output")))}
        return original(self, purpose, messages, **kw)

    monkeypatch.setattr(router_mod.ModelRouter, "complete", complete)
    try:  # the shared context-cache counters of this measurement never mix with a dev stack's on the same Redis
        from analystos.context import cache as context_cache

        monkeypatch.setattr(context_cache, "STATS_KEY", f"aos:ctx:stats:measure:{os.getpid()}:{time.time_ns()}")
    except ImportError:
        pass
    chat_always ={p: "always" for p, profile in load_models_config().routing.items() if profile != "decision"}
    REQUESTS.clear()
    clear = getattr(common, "clear_context_caches", None) or common._COMPILED.clear
    clear()
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin_id = s.scalar(select(User.id).where(User.email == get_settings().bootstrap_admin_email))
        last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
        previous_doc, version = (dict(last.document), last.version) if last else ({}, 0)
        s.add(PlatformSetting(version=version + 1, created_by=admin_id, note="token economy measurement",
                              document={**previous_doc, "llm": {**previous_doc.get("llm", {}), "purpose_modes": chat_always,
                                                                "cache_enabled": False},
                                        "features": {**previous_doc.get("features", {}), "jev_decisions": False}}))
    platform_settings.invalidate()
    started = time.perf_counter()
    try:
        from analystos.services.ask import ask_in_thread, create_thread
        from analystos.services.runs import _interpret_redirect, create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace, set_policy

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.id == admin_id))
            ws = create_workspace(s, admin, name="token economy", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin",
                                          "tables": ["incident", "change_request"]},
                                  secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
            s.flush()
            # The independent-model review is opt-in per workspace; on here so verification prompts are measured.
            set_policy(s, admin, ws.id, {"independent_model_verification": True})
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
        assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"
        run_requests = len(REQUESTS)
        with session_scope() as s:
            finished = s.get(AnalysisRun, run.id)
            s.expunge(finished)
        _interpret_redirect(finished, "focus on priority 1 incidents in the network group")
        with session_scope() as s:
            thread = create_thread(s, s.merge(admin), ws_id)["id"]
        question = "Which incidents breached SLA after a change, by priority?"  # not a rules shape: generation
        turns = [ask_in_thread(admin, thread, question) for _ in range(3)]  # asked, asked again, refreshed
        report = {"run_id": run.id, "seconds": round(time.perf_counter() - started, 1),
                  "flow": summarize(REQUESTS), "run_only": summarize(REQUESTS[:run_requests]),
                  "context_cache": common.compiled_context_stats(),
                  "context_cache_shared": _shared_stats(),
                  "turns": [(t.get("refusal") or {}).get("kind") or t.get("status") for t in turns]}
        out = os.environ.get("ANALYSTOS_ECONOMY_OUT")
        if out:
            with open(out, "w") as f:
                json.dump(report, f, indent=2)
        dump = os.environ.get("ANALYSTOS_ECONOMY_DUMP")  # every request's text, for reading what dominates
        if dump:
            with open(dump, "w", encoding="utf-8") as f:
                for i, r in enumerate(REQUESTS):
                    f.write(f"===== {i} {r['purpose']} {r['model']} chars={r['chars']} flagged={r['flagged_prefix_chars']}\n")
                    f.write(r["_text"] + "\n")
        assert report["flow"]["calls"] > 0 and "hypothesis_generation" in report["flow"]["per_purpose"]
    finally:
        with session_scope() as s:
            last = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
            s.add(PlatformSetting(version=last.version + 1, created_by=admin_id, note="restore after token economy measurement",
                                  document=previous_doc))
        platform_settings.invalidate()
        get_settings.cache_clear()
        default_router.cache_clear()
        clear()

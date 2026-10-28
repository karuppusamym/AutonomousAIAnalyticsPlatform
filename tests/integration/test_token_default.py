"""P4-T02 measurement: the standard run (spec v1 §62 flow, local orchestrator) under the DEFAULT
platform settings, with a model provider "available" through a counting fake transport.

Not live models: the transport answers every chat call with `{}` and every decision with no
answers, so model output never changes a finding. What is measured is how many calls the platform
decides to make. Set ANALYSTOS_TOKEN_EVIDENCE_OUT=<file.json> to write the measurement (used for the
evidence file and to re-record the cost-gate fixture, see scripts/cost_gate.py)."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select

pytestmark = pytest.mark.integration

MAX_CHAT_CALLS_PER_RUN = 10  # spec v3 §4.2 target, P4-T02 acceptance


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


class CountingTransport:
    """Fake provider: every chat call answers `{}`, every decision no answers (callers degrade)."""

    def __init__(self) -> None:
        self.chat_calls: list[str] = []
        self.decide_calls: list[str] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        self.chat_calls.append(payload["model"])
        return {"model": payload["model"], "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.001}}

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls.append(payload["model"])
        return {"model": payload["model"], "answers": {}, "usage": {"input_tokens": 200, "output_tokens": 5, "cost": 0.00002}}


def measure_standard_run(servicenow_url: str, monkeypatch) -> dict:
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # preview destination
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Approval, Hypothesis, Insight, ModelCall, User
    from analystos.governance.approvals import decide
    from analystos.llm.cache import ResponseCache
    from analystos.llm.router import ModelRouter
    from analystos.runtime import context as runtime_context
    from analystos.runtime import engine
    from analystos.runtime.usage import DbUsageSink
    from analystos.services import runs as runs_svc
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace
    from analystos.workflows.orchestrator import run_local

    get_settings.cache_clear()
    transport = CountingTransport()
    # Default platform settings (read from the settings store, which holds no version in the test DB).
    router = ModelRouter(transport=transport, api_key_lookup=lambda _env: "fake", sink=DbUsageSink(), cache=ResponseCache(None),
                         max_retries=0)
    monkeypatch.setattr(runtime_context, "default_router", lambda: router)
    monkeypatch.setattr(runs_svc, "default_router", lambda: router)
    monkeypatch.setattr(engine, "default_services", lambda: runtime_context.Services(router=router,
                                                                                      gateway=runtime_context.default_gateway()))
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"token default {time.time_ns()}", objective="Find the drivers of SLA breaches in IT incidents",
                              policy={"require_approved_metrics": False})  # the gate: test_semantic_layer.py
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident", "change_request"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident", "change_request"])
    run = runs_svc.create_run(admin, ws_id, objective=None)
    assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
    with session_scope() as s:
        approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        decide(s, approval.id, approver, approve=True)
    assert run_local(run.id) == "COMPLETED"
    with session_scope() as s:
        pairs = s.execute(select(Insight, Hypothesis).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                          .where(Insight.run_id == run.id, Insight.status == "verified")).all()
        # The staged asset's schema embeds the source id, which differs per workspace: compare by table.
        verified = sorted((i.title, i.finding, json.dumps({**h.spec, "asset": str(h.spec.get("asset")).split(".")[-1]},
                                                           sort_keys=True)) for i, h in pairs)
        confidence = sorted((i.title, i.confidence) for i, _ in pairs)
        rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run.id)))
        calls = [r for r in rows if r.status in ("ok", "error")]
        out = {
            "run_id": run.id,
            "chat_calls": sum(1 for r in calls if r.provider != "typesafe"),
            "decision_calls": sum(1 for r in calls if r.provider == "typesafe"),
            "transport_chat_calls": len(transport.chat_calls),
            "transport_decision_calls": len(transport.decide_calls),
            "tokens_used": sum(r.input_tokens + r.output_tokens for r in calls),
            "tokens_saved": sum(r.tokens_saved for r in rows),
            "cost_usd": round(sum(r.cost_usd for r in rows), 6),
            "calls_by_purpose": dict(sorted(Counter(r.purpose for r in calls).items())),
            "rows_by_purpose_status": dict(sorted(Counter(f"{r.purpose}:{r.status}" for r in rows).items())),
            "rows_by_rung": dict(sorted(Counter(str(getattr(r, "answered_by", None)) for r in rows).items())),
            "verified_findings": [t for t, _, _ in verified],
            "verified_findings_hash": hashlib.sha256(json.dumps(verified).encode()).hexdigest()[:16],  # title + finding + spec
            "verified_confidence": confidence,
        }
    get_settings.cache_clear()
    return out


def test_standard_run_default_preset_chat_calls(control_db, servicenow_url, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = measure_standard_run(servicenow_url, monkeypatch)
    target = os.environ.get("ANALYSTOS_TOKEN_EVIDENCE_OUT")
    if target:
        Path(target).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    from scripts import cost_gate

    if os.environ.get("ANALYSTOS_COST_FIXTURE_RECORD"):  # refresh the cost-gate recording from this run
        cost_gate.write_fixture("standard_run", cost_gate.points_from_run(result["run_id"]),
                                description="Standard run (v1 §62 flow, ServiceNow incident + change_request) recorded by "
                                            "tests/integration/test_token_default.py under the default settings. Fake "
                                            "transport, not live models: chat answers '{}', decisions answer nothing.")
    assert len(result["verified_findings"]) >= 3, result
    assert result["chat_calls"] <= MAX_CHAT_CALLS_PER_RUN, result
    # P4-T10 on the live pipeline: a new model call site in agent code shows up here even before the
    # recording is refreshed.
    baseline = json.loads(cost_gate.BASELINE.read_text(encoding="utf-8"))
    live = {"standard_run": {"calls": result["chat_calls"] + result["decision_calls"], "tokens": result["tokens_used"]}}
    assert cost_gate.compare(live, baseline) == [], (live, baseline)

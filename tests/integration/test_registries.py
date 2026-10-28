"""P4-T05 acceptance on the local stack: registries (ladder rung L1).

1. A scheduled re-analysis on unchanged data replays the hypothesis registry with **0 chat calls**
   (and 0 decision calls) although every purpose is routable and set to `always`, and reports 0 new,
   0 resolved and 0 changed findings: everything persists. The opt-in novelty round makes at most
   one call and its findings are "new questions", never "new findings".
2. Ask answers from the verified-query registry with no model call at p50 < 1 s, declines when a
   required parameter is missing, and falls back to the model only on a miss.
The model transport counts every call; no real provider is reached."""
from __future__ import annotations

import json
import socket
import statistics
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
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _wait(run_id: str, timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in ("COMPLETED", "FAILED", "CANCELLED", "REJECTED"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def _wait_schedule_run(srun_id: str, timeout: float = 120) -> None:
    """The run is COMPLETED (and recorded as steps) before complete_from_run finishes the schedule_run."""
    from analystos.db.base import session_scope
    from analystos.db.models import ScheduleRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            if s.get(ScheduleRun, srun_id).status in ("succeeded", "failed", "skipped"):
                return
        time.sleep(0.5)
    raise AssertionError(f"schedule run {srun_id} did not finish")


class CountingTransport:
    """Every chat and decision call is recorded; `chat` answers with the test's callback."""

    def __init__(self, chat=None):
        self._chat = chat or (lambda payload: {})
        self.chat_calls: list[dict] = []
        self.decide_calls: list[dict] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        from tests.fakes import chat_json

        self.chat_calls.append(payload)
        return chat_json(self._chat(payload))

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls.append(payload)
        return {"answers": {}, "usage": {}}


def _services(transport):
    """Every purpose routable (a key is present) and on `always`: only the registry can avoid a call."""
    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.llm.router import ModelRouter
    from analystos.runtime.context import Services, default_gateway
    from analystos.runtime.usage import DbUsageSink

    settings = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    router = ModelRouter(transport=transport, api_key_lookup=lambda env: "k", sink=DbUsageSink(), settings_provider=lambda: settings)
    return Services(router=router, gateway=default_gateway())


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    """A workspace with the ServiceNow incident table and one completed deterministic baseline run."""
    import os

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.runtime.context import default_router
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANALYSTOS_SOURCE_SECRET_SERVICENOW", "ANALYSTOS_SUPERSET_URL")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    os.environ["ANALYSTOS_SUPERSET_URL"] = "http://127.0.0.1:9"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="registries", objective="Find the drivers of SLA breaches in IT incidents")
        s.flush()
        add_member(s, admin, ws.id, "analyst@analystos.local", "analyst")
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["incident"])
    baseline = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(baseline.id) == "COMPLETED"
    yield {"ws": ws_id, "admin": admin, "baseline": baseline.id, "source": src_id}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


def _tested(session, run_id):
    from analystos.db.models import Hypothesis
    from analystos.registries.hypotheses import TESTED

    return list(session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.status.in_(TESTED))))


def test_scheduled_reanalysis_replays_the_registry_with_zero_model_calls(world, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Hypothesis, Insight, ModelCall, RegisteredHypothesis, ScheduleRun
    from analystos.registries.hypotheses import spec_hash
    from analystos.runtime import engine
    from analystos.services import schedules as sch_svc

    ws, admin, baseline = world["ws"], world["admin"], world["baseline"]
    with session_scope() as s:
        tested = _tested(s, baseline)
        registry = list(s.scalars(select(RegisteredHypothesis).where(RegisteredHypothesis.workspace_id == ws)))
        verified_before = s.scalars(select(Insight).where(Insight.run_id == baseline, Insight.status == "verified")).all()
        assert tested and len(verified_before) >= 1
        # finalize registered every tested hypothesis of the baseline, keyed by spec hash, with its claim key
        assert {r.spec_hash for r in registry} == {spec_hash(h.spec) for h in tested}
        assert all(r.last_run_id == baseline and r.times_tested == 1 and r.claim for r in registry)
        assert sum(r.last_outcome == "verified" for r in registry) == len({i.hypothesis_id for i in verified_before})

    transport = CountingTransport()
    monkeypatch.setattr(engine, "default_services", lambda: _services(transport))
    with session_scope() as s:
        sch = sch_svc.create_schedule(s, s.merge(admin), ws, name="weekly replay", kind="reanalysis", cron="0 7 * * 1",
                                      config={"baseline_run_id": baseline, "refresh_first": False,
                                              "report": {"kind": "weekly_summary", "formats": ["html"]}})
        s.flush()
        sch_id = sch.id
    srun_id = sch_svc.run_now(admin, sch_id)
    with session_scope() as s:
        rerun_id = s.get(ScheduleRun, srun_id).result["run_id"]
    assert _wait(rerun_id) == "COMPLETED"
    _wait_schedule_run(srun_id)

    # Acceptance: 0 chat calls (and 0 decision calls) although every purpose was routable on `always`.
    assert transport.chat_calls == [], [c.get("model") for c in transport.chat_calls]
    assert transport.decide_calls == []
    with session_scope() as s:
        rerun = s.get(AnalysisRun, rerun_id)
        changes = rerun.summary["changes"]
        assert changes["previous_run_id"] == baseline
        assert changes["new"] == [] and changes["resolved"] == [], changes
        assert changes["changed"] == [] and changes["not_retested"] == [] and changes["new_questions"] == []
        assert len(changes["persisting"]) == len(verified_before)
        assert s.get(ScheduleRun, srun_id).result["changes"] == {"new": 0, "persisting": len(verified_before), "changed": 0,
                                                                 "resolved": 0, "new_questions": 0}
        # The same questions, from the registry, in one round (no follow-up generation).
        replayed = list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == rerun_id)))
        assert {h.origin for h in replayed} == {"registry"}
        assert {spec_hash(h.spec) for h in replayed} == {spec_hash(h.spec) for h in _tested(s, baseline)}
        calls = list(s.scalars(select(ModelCall).where(ModelCall.run_id == rerun_id)))
        assert calls and all(c.status == "skipped" and c.provider == "deterministic" for c in calls)
        assert all((c.error or "").startswith("registry replay:") for c in calls)
        registry_rung = [c for c in calls if c.purpose == "hypothesis_generation"]
        assert len(registry_rung) == 1 and "L1 registry" in registry_rung[0].error and registry_rung[0].tokens_saved > 0
        entries = list(s.scalars(select(RegisteredHypothesis).where(RegisteredHypothesis.workspace_id == ws)))
        assert all(r.times_tested == 2 and r.last_run_id == rerun_id for r in entries)

    # Opt-in novelty round: its own budget, at most one call, findings labelled "new question".
    with session_scope() as s:
        entry = next(r for r in entries if (r.spec.get("segment") or {}).get("type") == "column")
        seg = entry.spec["segment"]["column"]
    novel_spec = {**entry.spec, "filters": [*(entry.spec.get("filters") or []), {"column": seg, "op": "is not null"}]}

    def propose(payload):
        content = payload["messages"][-1]["content"]  # text blocks when the model takes cache_control (P4-T04)
        body = json.loads(content if isinstance(content, str) else content[-1]["text"])
        assert body["already_testing"] and body["max_new_hypotheses"] == 2
        return {"hypotheses": [{"statement": "Novel: same question on rows with a known segment", "question": "Novel?",
                                "spec": novel_spec, "priority": "high"}]}

    novelty = CountingTransport(chat=propose)
    monkeypatch.setattr(engine, "default_services", lambda: _services(novelty))
    with session_scope() as s:
        sch_svc.update_schedule(s, s.merge(admin), sch_id, {"config": {
            "refresh_first": False, "report": {"kind": "weekly_summary", "formats": ["html"]},
            "novelty": {"enabled": True, "max_hypotheses": 2, "llm_budget_usd": 0.01}}})
    srun2 = sch_svc.run_now(admin, sch_id)
    with session_scope() as s:
        run2 = s.get(ScheduleRun, srun2).result["run_id"]
    assert _wait(run2) == "COMPLETED"
    assert len(novelty.chat_calls) == 1 and novelty.decide_calls == []
    with session_scope() as s:
        r2 = s.get(AnalysisRun, run2)
        assert r2.origin["previous_run_id"] == rerun_id
        hyps = list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == run2)))
        assert sum(h.origin == "novelty" for h in hyps) == 1
        assert {h.origin for h in hyps} == {"registry", "novelty"}
        ch = r2.summary["changes"]
        assert ch["new"] == [] and ch["resolved"] == [] and ch["changed"] == []
        assert all(f["origin"] == "novelty" for f in ch["new_questions"])
        chat = [c for c in s.scalars(select(ModelCall).where(ModelCall.run_id == run2)) if c.status in ("ok", "error")]
        assert [c.purpose for c in chat] == ["hypothesis_generation"]


def _vocab_column(session, ws):
    """A profiled categorical column of the incident table with at least three string values."""
    from analystos.db.models import SourceAsset, SourceColumn

    asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.name == "incident"))
    for col in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)):
        values = [str(t["value"]) for t in (col.profile or {}).get("top_values") or [] if isinstance(t.get("value"), str)]
        if col.semantic_type == "categorical" and len(values) >= 3 and "pii" not in (col.tags or []) \
                and all(any(ch.isalpha() for ch in v) for v in values):
            return f"{asset.schema_name}.{asset.name}", col.name, values
    raise AssertionError("no profiled categorical column")


def test_ask_answers_from_the_verified_query_registry(world, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.base import session_scope
    from analystos.db.models import Insight, ModelCall, QueryExecution, User, VerifiedQuery
    from analystos.runtime import context as runtime_context

    ws = world["ws"]
    with session_scope() as s:
        table, column, values = _vocab_column(s, ws)
    label = column.replace("_", " ")
    sql = f'SELECT COUNT(*) AS n FROM {table} WHERE "{column}" = \'{values[0]}\''
    transport = CountingTransport(chat=lambda payload: {"sql": sql, "explanation": "count"})
    services = _services(transport)
    monkeypatch.setattr(runtime_context, "default_services", lambda: services)
    with TestClient(app) as api:
        r = api.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"})
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 1. A miss: the model writes the SQL (one chat call); the answer is promoted.
        question = f"How many incidents have {label} {values[0]}?"
        first = api.post(f"/api/workspaces/{ws}/ask", headers=headers, json={"question": question})
        assert first.status_code == 200, first.text
        assert first.json()["answered_by"] == "model" and len(transport.chat_calls) == 1
        r = api.post(f"/api/workspaces/{ws}/verified-queries", headers=headers,
                     json={"question": question, "query_id": first.json()["result"]["query_id"],
                           "patterns": [f"Number of incidents where {label} is {{{column}}}"]})
        assert r.status_code == 200, r.text
        vq = r.json()
        assert "{{" + column + "}}" in vq["sql_template"] and values[0] not in vq["sql_template"]
        assert vq["parameters"][0]["type"] == "enum" and vq["parameters"][0]["required"] is True
        assert set(values) <= set(vq["parameters"][0]["values"])

        # 2. Registry hits: no model call, typed value from the profiled vocabulary, p50 < 1 s.
        latencies = []
        for i in range(20):
            value = values[i % len(values)]
            phrasing = f"how many INCIDENTS have {label} {value}" if i % 2 else f"Number of incidents where {label} is {value}"
            t0 = time.perf_counter()
            hit = api.post(f"/api/workspaces/{ws}/ask", headers=headers, json={"question": phrasing})
            latencies.append(time.perf_counter() - t0)
            assert hit.status_code == 200, hit.text
            body = hit.json()
            assert body["answered_by"] == "registry" and body["status"] == "answered", body
            assert body["parameters"] == {column: value} and f"'{value}'" in body["sql"]
            assert body["result"]["row_count"] == 1
        p50 = statistics.median(latencies)
        print(f"\nAsk registry hit latency over {len(latencies)} hits: p50={p50 * 1000:.0f} ms, "
              f"p90={sorted(latencies)[int(0.9 * len(latencies)) - 1] * 1000:.0f} ms, max={max(latencies) * 1000:.0f} ms")
        assert p50 < 1.0, latencies
        assert len(transport.chat_calls) == 1  # still only the first miss

        # 3. Decline: the question matches but names no value; nothing is guessed or executed.
        with session_scope() as s:
            before = s.query(QueryExecution).filter(QueryExecution.workspace_id == ws).count()
        declined = api.post(f"/api/workspaces/{ws}/ask", headers=headers, json={"question": f"How many incidents have {label}?"})
        assert declined.status_code == 200, declined.text
        d = declined.json()
        assert d["status"] == "needs_input" and d["sql"] is None and d["result"] is None
        assert d["missing"][0]["name"] == column and set(values) <= set(d["missing"][0]["values"])
        with session_scope() as s:
            assert s.query(QueryExecution).filter(QueryExecution.workspace_id == ws).count() == before
        answered = api.post(f"/api/workspaces/{ws}/ask", headers=headers,
                            json={"question": f"How many incidents have {label}?", "parameters": {column: values[1].upper()}})
        assert answered.json()["answered_by"] == "registry" and answered.json()["parameters"] == {column: values[1]}
        bad = api.post(f"/api/workspaces/{ws}/ask", headers=headers,
                       json={"question": f"How many incidents have {label}?", "parameters": {column: "x'; DROP TABLE t; --"}})
        assert bad.status_code == 422
        assert len(transport.chat_calls) == 1

        # 4. A different question misses the registry and the rules (a filter and wording they cannot
        # resolve), so it falls back to the model path. Simple shapes are answered by rules since 2026-09-26.
        miss = api.post(f"/api/workspaces/{ws}/ask", headers=headers,
                        json={"question": "Which callers reopened incidents after an escalation last quarter?"})
        assert miss.json()["answered_by"] == "model" and len(transport.chat_calls) == 2

        # 5. A verified finding can be promoted too, and its question then answers from the registry.
        with session_scope() as s:
            insight = s.scalar(select(Insight).where(Insight.run_id == world["baseline"], Insight.status == "verified"))
            insight_id = insight.id
        r = api.post(f"/api/workspaces/{ws}/verified-queries", headers=headers, json={"insight_id": insight_id})
        assert r.status_code == 200, r.text
        finding_vq = r.json()
        assert finding_vq["origin"]["type"] == "finding" and finding_vq["spec"]
        hit = api.post(f"/api/workspaces/{ws}/ask", headers=headers, json={"question": finding_vq["origin"]["question"]})
        assert hit.json()["answered_by"] == "registry" and hit.json()["verified_query"]["id"] == finding_vq["id"]

        # Retired entries no longer answer; viewers cannot promote.
        assert api.patch(f"/api/verified-queries/{finding_vq['id']}", headers=headers, json={"status": "retired"}).status_code == 200
        again = api.post(f"/api/workspaces/{ws}/ask", headers=headers, json={"question": finding_vq["origin"]["question"]})
        assert (again.json().get("verified_query") or {}).get("id") != finding_vq["id"]
        listed = api.get(f"/api/workspaces/{ws}/verified-queries", headers=headers).json()
        assert {v["id"] for v in listed} == {vq["id"], finding_vq["id"]}
        assert api.get(f"/api/workspaces/{ws}/hypothesis-registry", headers=headers).status_code == 200

    with session_scope() as s:
        skips = [c for c in s.scalars(select(ModelCall).where(ModelCall.purpose == "sql_generation", ModelCall.status == "skipped"))
                 if (c.error or "").startswith("L1 registry")]
        assert len(skips) >= 23 and all(c.tokens_saved > 0 for c in skips)  # 21 hits + decline + finding hit
        assert s.get(VerifiedQuery, vq["id"]).hits >= 21
        assert s.scalar(select(User).where(User.email == "analyst@analystos.local"))

"""Process mining on the local stack: the ServiceNow mock's activity log is discovered, selected, loaded and
crawled; the Process candidates detect it (with the ITSM pack's mapping and reference models); analysing a
segment reads it through the gateway (keyset pages, audited queries) and rediscovers the planted patterns
(GROUND_TRUTH); a saved analysis is a versioned artifact with lineage to its queries."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


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
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):
    import os

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.crawler import crawl_source
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    saved = os.environ.get("ANALYSTOS_SOURCE_SECRET_SERVICENOW")
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    get_settings.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="process mining it", objective="How do tasks flow?")
        s.flush()
        src = register_source(s, admin, ws.id, kind="servicenow", name="SN activity",
                              config={"instance_url": servicenow_url, "username": "admin", "page_size": 10_000,
                                      "tables": ["u_task_activity", "sc_task"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    loaded = select_assets(admin, src_id, ["u_task_activity", "sc_task"])
    crawl = crawl_source(admin, src_id, mode="full", profile=True)
    yield {"ws": ws_id, "source": src_id, "loaded": loaded, "crawl": crawl}
    if saved is None:
        os.environ.pop("ANALYSTOS_SOURCE_SECRET_SERVICENOW", None)
    else:
        os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = saved
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def api(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"email": "admin@analystos.local", "password": PASSWORD})
        assert r.status_code == 200, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield client


@pytest.fixture(scope="module")
def activity():
    import polars as pl

    from analystos.connectors.synthetic_servicenow import generate_activity_data

    return pl.from_arrow(generate_activity_data()["u_task_activity"])


@pytest.fixture(scope="module")
def candidate(api, world):
    r = api.get(f"/api/workspaces/{world['ws']}/process/candidates")
    assert r.status_code == 200, r.text
    found = r.json()["candidates"]
    assert found, "no event log detected"
    return found[0]


def _analyze(api, ws: str, candidate: dict, segment: str, **extra) -> dict:
    body = {"asset_id": candidate["asset_id"], **candidate["mapping"],
            "filters": [{"column": "task_type", "op": "=", "value": segment}], **extra}
    r = api.post(f"/api/workspaces/{ws}/process/analyze", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_activity_log_is_detected_with_the_pack_mapping(candidate, world):
    assert candidate["name"] == "u_task_activity"
    assert candidate["declared_by"].startswith("pack.itsm@")
    assert candidate["mapping"] == {"case_column": "task_sys_id", "activity_column": "activity",
                                    "timestamp_column": "activity_at", "resource_column": "assignment_group"}
    seg = candidate["segments"][0]
    assert seg["column"] == "task_type"
    values = {v["value"]: v for v in seg["values"]}
    assert set(values) == {"incident", "change_request", "sc_task"}
    assert values["change_request"]["reference_path"][2] == "Authorized"
    assert values["incident"]["count"] > 50_000


def test_the_crawler_sees_an_event_table(api, world, candidate):
    assert candidate["role"] == "event"
    assert "the crawler classified the table as an event table" in candidate["reasons"]


def test_changes_skip_authorization_and_database_upgrades_cancel_late(api, world, candidate, activity):
    import polars as pl

    from analystos.connectors.synthetic_servicenow import GROUND_TRUTH

    a = _analyze(api, world["ws"], candidate, "change_request", save=True)
    expected_cases = activity.filter(pl.col("task_type") == "change_request")["task_sys_id"].n_unique()
    assert a["summary"]["cases"] == expected_cases
    conf = a["conformance"]
    assert conf["source"].startswith("pack.itsm@") and conf["reference"][2] == "Authorized"
    skipped = next(d for d in conf["deviations"] if d["kind"] == "missing" and d["activity"] == "Authorized")
    gt = GROUND_TRUTH["change_authorization_skipped"]["expected"]
    assert gt["other"] < skipped["share"] < gt["emergency"]
    assert {x["activity"] for x in a["cancellations"]["after"]} == {"Assessed", "Scheduled"}
    assert a["provenance"]["queries"] and a["provenance"]["coverage"]["truncated"] is False
    art = a["artifact"]
    detail = api.get(f"/api/artifacts/{art['id']}").json()
    assert detail["type"] == "process_analysis" and detail["content"]["summary"] == a["summary"]
    assert any(e["relation"] == "derived_from" and e["to"] == ["query", a["provenance"]["queries"][0]]
               for e in detail["lineage"]["edges"])
    listed = api.get(f"/api/workspaces/{world['ws']}/process/analyses").json()
    assert listed[0]["id"] == art["id"] and listed[0]["segment"] == "change_request"


def test_incidents_read_in_pages_show_network_operations_ping_pong(api, world, candidate, activity):
    import polars as pl

    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution

    a = _analyze(api, world["ws"], candidate, "incident")
    inc = activity.filter(pl.col("task_type") == "incident")
    assert a["summary"]["cases"] == inc["task_sys_id"].n_unique()
    assert a["summary"]["events"] == inc.height  # more than one gateway page: keyset paging lost nothing
    assert a["provenance"]["coverage"]["pages"] >= 2
    assert "Network Operations" in (a["handovers"]["pairs"][0]["source"], a["handovers"]["pairs"][0]["target"])
    top_pp = a["handovers"]["ping_pong"][0]
    assert "Network Operations" in (top_pp["a"], top_pp["b"])
    assert a["rework"]["activities"][0]["activity"] == "Reassigned"
    assert a["variants"]["happy_path_rank"] is not None
    with session_scope() as s:
        rows = s.scalars(select(QueryExecution).where(QueryExecution.id.in_(a["provenance"]["queries"]))).all()
        assert len(rows) == len(a["provenance"]["queries"])
        assert all(r.purpose == "process_mining.events" and r.status == "ok" for r in rows)


def test_catalog_tasks_show_cancellations_before_work(api, world, candidate):
    from analystos.connectors.synthetic_servicenow import GROUND_TRUTH

    a = _analyze(api, world["ws"], candidate, "sc_task")
    gt = GROUND_TRUTH["catalog_cancelled_before_work"]
    assert abs(a["cancellations"]["share"] - gt["expected"]["cancelled_share"]) <= gt["tolerance"]["cancelled_share"]
    before = sum(x["cases"] for x in a["cancellations"]["after"] if x["activity"] != "Work started")
    assert before / a["cancellations"]["cases"] >= gt["expected"]["before_work_share"] - gt["tolerance"]["before_work_share"]
    assert any(b["source"] == "On hold" and b["target"] == "Resumed" for b in a["bottlenecks"])


def test_demo_seed_builds_the_process_mining_workspace_through_the_api(api, world, servicenow_url):
    """`analystos demo-seed --only process` against the app: workspace, members, source, selection, brief and
    one saved analysis per task type; a second run changes nothing."""
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.demo import seed

    def as_user(email: str) -> seed.Api:
        client = TestClient(app)
        r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        user = seed.Api.__new__(seed.Api)
        user.c = client
        return user

    admin, analyst = as_user("admin@analystos.local"), as_user("analyst@analystos.local")
    approver = as_user("approver@analystos.local")
    # the investigation on the process tables has its own test (test_an_investigation_runs_on_the_process_tables)
    wid = seed.ensure_process_workspace(admin, analyst, approver, servicenow_url, timeout=60, investigate=False)
    saved = analyst.get(f"/api/workspaces/{wid}/process/analyses")
    wanted = seed.process_demo()["process"]["analyses"]
    assert sorted(a["name"] for a in saved) == sorted(a["name"] for a in wanted)
    by_segment = {a["segment"]: a for a in saved}
    assert by_segment["change_request"]["summary"]["fitness"] < 1.0
    outputs = analyst.get(f"/api/workspaces/{wid}/artifacts", params={"type": "process_analysis"})
    assert len(outputs) == len(wanted)
    tables = next(s for s in analyst.get(f"/api/workspaces/{wid}/sources") if s["name"] == "Process mining tables")
    names = {a["name"] for a in analyst.get(f"/api/workspaces/{wid}/catalog") if a["source_id"] == tables["id"] and a["selected"]}
    assert names == {"u_task_activity_cases", "u_task_activity_transitions"}

    writes: list[str] = []
    for u in (admin, analyst):
        original = u.call

        def spy(method, path, body=None, _original=original, **kw):  # noqa: ANN001, ANN202
            if method != "GET":
                writes.append(f"{method} {path}")
            return _original(method, path, body, **kw)

        u.call = spy  # type: ignore[method-assign]
    assert seed.ensure_process_workspace(admin, analyst, approver, servicenow_url, timeout=60, investigate=False) == wid
    assert writes == []


def test_a_mapping_to_an_unknown_column_is_refused(api, world, candidate):
    body = {"asset_id": candidate["asset_id"], **candidate["mapping"], "activity_column": "no_such_column"}
    r = api.post(f"/api/workspaces/{world['ws']}/process/analyze", json=body)
    assert r.status_code in (400, 422)
    assert "no_such_column" in r.text


# ------------------------------------------------------------------------------------ process data for everything else
@pytest.fixture(scope="module")
def process_tables(api, world, candidate):
    """The event log as the workspace's case and transition tables (one row per case / per move)."""
    body = {"asset_id": candidate["asset_id"], **candidate["mapping"]}
    r = api.post(f"/api/workspaces/{world['ws']}/process/tables", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_event_log_becomes_profiled_described_tables_of_the_workspace(api, world, process_tables):
    t = {x["kind"]: x for x in process_tables["tables"]}
    assert set(t) == {"cases", "transitions"} and t["cases"]["name"] == "u_task_activity_cases"
    assert {s["segment"] for s in process_tables["segments"]} == {"incident", "change_request", "sc_task"}
    assert t["cases"]["rows"] == process_tables["cases"] and t["transitions"]["rows"] == process_tables["transitions"]
    assert process_tables["transitions"] > process_tables["cases"] > 1000 and not process_tables["truncated"]
    catalog = {a["name"]: a for a in api.get(f"/api/workspaces/{world['ws']}/catalog").json()}
    cases = catalog["u_task_activity_cases"]
    assert cases["selected"] and cases["business_name"].startswith("Process cases")
    assert cases["description_origin"] == "source" and "one row per case" in cases["description"]
    cols = {c["name"]: c for c in cases["columns"]}
    assert cols["duration_hours"]["profile"] and cols["follows_expected_path"]["description_origin"] == "source"
    # rebuilding replaces the tables (same names, same source), it does not add new ones
    again = api.post(f"/api/workspaces/{world['ws']}/process/tables",
                     json={"asset_id": process_tables["event_log"]["asset_id"], "case_column": "task_sys_id",
                           "activity_column": "activity", "timestamp_column": "activity_at",
                           "resource_column": "assignment_group"}).json()
    # the derived tables are not offered as event logs themselves
    offered = {c["name"] for c in api.get(f"/api/workspaces/{world['ws']}/process/candidates").json()["candidates"]}
    assert "u_task_activity" in offered and not offered & {"u_task_activity_cases", "u_task_activity_transitions"}
    assert again["source_id"] == process_tables["source_id"] and [x["fq"] for x in again["tables"]] == [x["fq"] for x in process_tables["tables"]]


def test_ask_answers_process_questions_from_the_tables_without_a_model(world, process_tables):
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services import ask as ask_svc

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        thread = ask_svc.create_thread(s, admin, world["ws"])
        s.expunge(admin)
    turn = ask_svc.ask_in_thread(admin, thread["id"], "average duration hours by segment")
    assert turn["status"] == "answered" and turn["answered_by"] == "rules", turn.get("refusal")
    assert "u_task_activity_cases" in turn["sql"] and len(turn["result"]["rows"]) == 3
    step = ask_svc.ask_in_thread(admin, thread["id"], "cases by segment and status", mode="analyst")
    assert step["status"] == "answered" and all(s["answered_by"] == "rules" for s in step["analysis"]["steps"])
    waits = ask_svc.ask_in_thread(admin, thread["id"], "average wait hours by from activity")
    assert waits["status"] == "answered" and "u_task_activity_transitions" in waits["sql"]


def test_an_investigation_runs_on_the_process_tables(world, process_tables):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Hypothesis, User
    from analystos.services.runs import create_run
    from analystos.workflows.orchestrator import run_local

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        s.expunge(admin)
    run = create_run(admin, world["ws"], objective="Why do some cases take much longer than others?",
                     source_ids=[process_tables["source_id"]], origin={"type": "user", "publish": "skip"})
    assert run_local(run.id) == "COMPLETED"
    with session_scope() as s:
        r = s.get(AnalysisRun, run.id)
        tested = list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(
            ["supported", "rejected", "inconclusive"]))))
        assert r.status == "COMPLETED" and tested, r.error
        assert any("u_task_activity_cases" in str(h.spec) or "u_task_activity_transitions" in str(h.spec) for h in tested)

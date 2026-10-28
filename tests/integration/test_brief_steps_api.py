"""P4-04, P7-04, P7-05 and P7-12 through the HTTP API on the local stack, with the real query gateway over a
staged ServiceNow table (no model):

* the workspace brief: suggestions, If-Match revisions, review, history, viewer read-only;
* Start work job kinds with reasons; readiness assessments; a prediction without a label and a
  "predict" work order cannot start;
* an Ask thread ingested as steps (private to its author); notebook SQL cells through the gateway (audited
  with the step id), a table outside the scope refused by the gateway, Python on upstream results; a cell
  edit voids and re-runs its dependents and the old version stays readable;
* a verified step pinned to a tile through an approval, replaying the frozen query after an edit;
* fork, compare and merge with both branches' lineage; cross-workspace ids are 404;
* migration 0040 upgrades and downgrades.
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select, text

from analystos.db import vectors

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
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module", autouse=True)
def sandbox_backend():
    from analystos.core.config import Settings, get_settings
    from analystos.sandbox import isolation

    mp = pytest.MonkeyPatch()
    process = isolation.status(Settings(_env_file=None, sandbox_isolation="process")).available
    mp.setenv("ANALYSTOS_SANDBOX_ISOLATION", "process" if process else "off")
    mp.setenv("ANALYSTOS_ENV", "dev")
    get_settings.cache_clear()
    yield
    mp.undo()
    get_settings.cache_clear()


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(api, servicenow_url):
    import os

    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.services.sources import discover_source, register_source, select_assets

    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    analyst, approver, admin = (_login(api, f"{u}@analystos.local") for u in ("analyst", "approver", "admin"))
    ws = api.post("/api/workspaces", headers=analyst, json={"name": "data thread", "objective": "Why do incidents breach?"}).json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    other = api.post("/api/workspaces", headers=analyst, json={"name": "thread other", "objective": "Another workspace"}).json()["id"]
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, owner, ws, kind="servicenow", name="SN",
                              config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        src_id = src.id
        s.expunge(owner)
    discover_source(owner, src_id)
    select_assets(owner, src_id, ["incident"])
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.name == "incident"))
        table = f"{asset.schema_name}.{asset.name}"
    return {"ws": ws, "other": other, "analyst": analyst, "approver": approver, "admin": admin, "table": table,
            "owner": owner}


# ------------------------------------------------------------------------------------ brief and readiness
def test_brief_versions_review_and_if_match(api, world):
    ws, analyst, approver = world["ws"], world["analyst"], world["approver"]
    r = api.post(f"/api/workspaces/{ws}/brief/suggestions", headers=analyst)
    assert r.status_code == 200, r.text
    brief = r.json()
    assert brief["version"] >= 1 and r.headers["ETag"] == f'"{brief["version"]}"'
    grain_key = f"data_semantics.grain:{world['table']}"
    grain = next(a for a in brief["assertions"] if a["key"] == grain_key)
    assert grain["origin"] == "rule" and grain["review_state"] == "suggested"
    patch = {"ops": [{"op": "review", "key": grain_key}]}
    assert api.patch(f"/api/workspaces/{ws}/brief", headers=analyst, json=patch).status_code == 428
    assert api.patch(f"/api/workspaces/{ws}/brief", headers={**analyst, "If-Match": '"99"'}, json=patch).status_code == 412
    r = api.patch(f"/api/workspaces/{ws}/brief", headers={**analyst, "If-Match": f'"{brief["version"]}"'}, json=patch)
    assert r.status_code == 200 and r.json()["version"] == brief["version"] + 1
    assert next(a for a in r.json()["assertions"] if a["key"] == grain_key)["review_state"] == "reviewed"
    versions = api.get(f"/api/workspaces/{ws}/brief/versions", headers=approver).json()
    assert [v["version"] for v in versions][:2] == [brief["version"] + 1, brief["version"]]
    assert api.patch(f"/api/workspaces/{ws}/brief", headers={**approver, "If-Match": "*"}, json=patch).status_code == 403
    assert api.get(f"/api/workspaces/{world['other']}/brief", headers=approver).status_code in (403, 404)


def test_start_work_and_readiness_refuse_what_cannot_run(api, world):
    ws, analyst = world["ws"], world["analyst"]
    kinds = {k["key"]: k for k in api.get(f"/api/workspaces/{ws}/capabilities", headers=analyst).json()["job_kinds"]}
    assert kinds["explain"]["available"] and not kinds["predict"]["available"]
    assert "no_ml_spec" in {r["code"] for r in kinds["predict"]["reasons"]}
    r = api.post(f"/api/workspaces/{ws}/readiness", headers=analyst, json={"job_kind": "predict", "assets": [world["table"]]})
    assert r.status_code == 201
    out = r.json()
    assert out["status"] == "unsupported" and any(c["check"] == "label_availability" and c["status"] == "fail"
                                                   for c in out["checks"])
    assert api.get(f"/api/workspaces/{ws}/readiness/{out['id']}", headers=analyst).json()["status"] == "unsupported"
    assert api.get(f"/api/workspaces/{world['other']}/readiness/{out['id']}", headers=analyst).status_code == 404
    explain = api.post(f"/api/workspaces/{ws}/readiness", headers=analyst,
                       json={"job_kind": "explain", "assets": [world["table"], "hr.salaries"]}).json()
    assert explain["status"] == "blocked"
    assert next(c for c in explain["checks"] if c["check"] == "scope")["status"] == "fail"
    wo = {"kind": "predict", "objective": "Predict which incidents will breach their SLA",
          "spec": {"type": "analysis", "analyses": [{"method": "rate_by_segment", "asset": world["table"],
                                                      "outcome": {"type": "is_true", "column": "made_sla"},
                                                      "segment": {"type": "column", "column": "priority"}}]}}
    wo_id = api.post(f"/api/workspaces/{ws}/work-orders", headers=analyst, json=wo).json()["id"]
    assessed = api.post(f"/api/workspaces/{ws}/work-orders/{wo_id}/assessments", headers=analyst)
    assert assessed.status_code == 201 and assessed.json()["work_order_id"] == wo_id
    refused = api.post(f"/api/workspaces/{ws}/work-orders/{wo_id}/runs", headers={**analyst, "If-Match": '"1"'})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "unsupported_capability"


# ------------------------------------------------------------------------------------ steps
def test_ask_thread_steps_are_private_to_their_author(api, world):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AskTurn

    ws, analyst, approver = world["ws"], world["analyst"], world["approver"]
    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=analyst, json={"title": "steps"}).json()["id"]
    with session_scope() as s:
        s.add(AskTurn(id=new_id("askt"), thread_id=thread, workspace_id=ws, user_id=world["owner"].id, seq=1,
                      question="Incidents by priority", parameters={}, status="answered",
                      sql=f"SELECT priority, COUNT(*) AS n FROM {world['table']} GROUP BY priority",
                      result={"query_id": "qry_x", "columns": ["priority", "n"], "rows": [["1", 5], ["2", 7]], "row_count": 2,
                              "result_hash": "h"}, attempts=[], stages=[], decisions=[], provenance={}, promotions=[]))
    r = api.post(f"/api/workspaces/{ws}/threads/ask_thread/{thread}/ingest", headers=analyst)
    assert r.status_code == 200, r.text
    steps = r.json()["steps"]
    assert len(steps) == 1 and steps[0]["origin"]["type"] == "ask_turn" and steps[0]["status"] == "ok"
    again = api.post(f"/api/workspaces/{ws}/threads/ask_thread/{thread}/ingest", headers=analyst).json()["steps"]
    assert [s["id"] for s in again] == [steps[0]["id"]]  # idempotent
    assert api.get(f"/api/workspaces/{ws}/threads/ask_thread/{thread}", headers=approver).status_code == 404
    assert api.get(f"/api/workspaces/{ws}/steps/{steps[0]['id']}", headers=approver).status_code == 404


def _notebook(api, world, title):
    return api.post(f"/api/workspaces/{world['ws']}/notebooks", headers=world["analyst"], json={"title": title}).json()


@pytest.mark.skipif(sys.platform == "win32", reason="the Python sandbox needs POSIX resource limits "
                    "(resource/rlimits, preexec_fn); run on Linux")
def test_notebook_cells_run_through_the_gateway_and_an_edit_voids_dependents(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution, VerificationRecord

    ws, analyst = world["ws"], world["analyst"]
    nb = _notebook(api, world, "Breaches")
    base = f"/api/workspaces/{ws}/notebooks/{nb['id']}"
    sql = api.post(f"{base}/cells", headers=analyst, json={"cell": "sql", "source": f"SELECT priority, COUNT(*) AS n FROM "
                                                                                      f"{world['table']} GROUP BY priority"})
    assert sql.status_code == 201, sql.text
    sql = sql.json()
    assert sql["status"] in ("ok", "flagged") and sql["verification_record"]["state"] == "ACTIVE"
    assert (sql["cell"], sql["version"]) == ("sql", 1) and sql["source"].startswith("SELECT priority")  # as the GET shows it
    with session_scope() as s:
        q = s.get(QueryExecution, sql["receipts"][-1]["query_id"])
        assert q is not None and q.task_id == sql["id"] and q.status == "ok"  # audited by the gateway, bound to the step
    py = api.post(f"{base}/cells", headers=analyst, json={
        "cell": "python", "source": "result = {'total': sum(r['n'] for r in inputs['cell1'])}"}).json()
    assert py["status"] == "ok" and py["depends_on"] == [sql["id"]], py
    outside = api.post(f"{base}/cells", headers=analyst, json={"cell": "sql", "source": "SELECT * FROM pg_catalog.pg_user"}).json()
    assert outside["status"] == "failed" and "sql_rejected" in outside["error"]
    stale = api.patch(f"{base}/cells/{sql['id']}", headers={**analyst, "If-Match": '"5"'},
                      json={"source": f"SELECT priority, COUNT(*) AS n FROM {world['table']} WHERE priority = '1' GROUP BY priority"})
    assert stale.status_code == 412
    r = api.patch(f"{base}/cells/{sql['id']}", headers={**analyst, "If-Match": '"1"'},
                  json={"source": f"SELECT priority, COUNT(*) AS n FROM {world['table']} WHERE priority = '1' GROUP BY priority"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["step"]["version"] == 2 and [x["id"] for x in out["rerun"]] == [py["id"]]
    with session_scope() as s:
        assert s.get(VerificationRecord, sql["verification_record"]["record_id"]).state == "VOID"
        assert s.get(VerificationRecord, py["verification_record"]["record_id"]).state == "VOID"
    v1 = api.get(f"/api/workspaces/{ws}/steps/{sql['id']}", headers=analyst, params={"version": 1}).json()
    assert v1["version"] == 1 and "WHERE" not in v1["spec"]["sql"] and v1["verification_record"]["state"] == "VOID"
    versions = api.get(f"/api/workspaces/{ws}/steps/{sql['id']}/versions", headers=analyst).json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    why = api.get(f"/api/workspaces/{ws}/steps/{sql['id']}/why", headers=analyst, params={"version": 1})
    assert why.status_code == 200, why.text
    why = why.json()
    assert why["verification_state"]["state"] == "VOID" and why["numbers"]
    links = {lk["link"]: lk for lk in why["numbers"][0]["links"]}
    assert list(links) == ["fact", "step", "query_receipt", "data_version", "semantic_version", "verdict"]
    assert links["verdict"]["state"] == "void" and links["query_receipt"]["state"] == "ok"  # the gateway's own receipt
    assert api.get(f"/api/workspaces/{world['other']}/steps/{sql['id']}/why", headers=analyst).status_code == 404
    assert api.get(f"/api/workspaces/{world['other']}/steps/{sql['id']}", headers=analyst).status_code == 404


def test_a_pinned_tile_replays_the_frozen_query(api, world):
    ws, analyst, approver = world["ws"], world["analyst"], world["approver"]
    nb = _notebook(api, world, "Pins")
    base = f"/api/workspaces/{ws}/notebooks/{nb['id']}"
    frozen_sql = f"SELECT priority, COUNT(*) AS n FROM {world['table']} GROUP BY priority"
    cell = api.post(f"{base}/cells", headers=analyst, json={"cell": "sql", "source": frozen_sql}).json()
    if cell["status"] != "ok":
        pytest.skip(f"the fixture query was flagged by a self-check: {cell['checks']}")
    req = api.post(f"/api/workspaces/{ws}/steps/{cell['id']}/pins", headers=analyst, json={"target": "tile", "dashboard": "Ops"})
    assert req.status_code == 202, req.text
    approval = req.json()["approval_id"]
    assert api.post(f"/api/approvals/{approval}/approve", headers=approver, json={"reason": "ok"}).status_code == 200
    done = api.post(f"/api/workspaces/{ws}/steps/{cell['id']}/pins", headers=analyst,
                    json={"target": "tile", "dashboard": "Ops", "approval_id": approval})
    assert done.status_code == 201, done.text
    pin = done.json()["pin"]
    api.patch(f"{base}/cells/{cell['id']}", headers={**analyst, "If-Match": '"1"'},
              json={"source": f"SELECT COUNT(*) AS n FROM {world['table']}"})
    replay = api.post(f"/api/workspaces/{ws}/pins/{pin['id']}/refresh", headers=analyst).json()
    assert replay["sql"] == frozen_sql and replay["pin_state"] == "upgrade_available" and replay["columns"] == ["priority", "n"]
    assert api.get(f"/api/workspaces/{world['other']}/pins/{pin['id']}", headers=analyst).status_code == 404


def test_fork_compare_and_merge_through_the_api(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import LineageEdge

    ws, analyst = world["ws"], world["analyst"]
    nb = _notebook(api, world, "Branches")
    base = f"/api/workspaces/{ws}/notebooks/{nb['id']}"
    cell = api.post(f"{base}/cells", headers=analyst, json={
        "cell": "sql", "source": f"SELECT priority, COUNT(*) AS n FROM {world['table']} GROUP BY priority"}).json()
    fork = api.post(f"/api/workspaces/{ws}/branches/{nb['branch_id']}/forks", headers=analyst, json={
        "from_step_id": cell["id"], "name": "only P1",
        "spec": {"cell": "sql", "sql": f"SELECT priority, COUNT(*) AS n FROM {world['table']} WHERE priority = '1' GROUP BY priority"}})
    assert fork.status_code == 201, fork.text
    fork_id = fork.json()["branch"]["id"]
    cmp = api.get(f"/api/workspaces/{ws}/branches/{nb['branch_id']}/compare", headers=analyst, params={"with": fork_id}).json()
    assert cmp["steps"][0]["match"] == "diverged" and cmp["steps"][0]["spec_diff"]
    first = api.post(f"/api/workspaces/{ws}/branches/{nb['branch_id']}/merge", headers=analyst, json={"title": "Breaches"})
    assert first.status_code == 200, first.text
    report = first.json()["report"]["id"]
    second = api.post(f"/api/workspaces/{ws}/branches/{fork_id}/merge", headers=analyst, json={"report_id": report}).json()
    assert {b["branch_id"] for b in second["report"]["content"]["branches"]} == {nb["branch_id"], fork_id}
    with session_scope() as s:
        merged = {e.from_id for e in s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == ws,
                                                                          LineageEdge.relation == "merged_into",
                                                                          LineageEdge.to_id == report))}
    assert merged == {nb["branch_id"], fork_id}
    assert api.get(f"/api/workspaces/{world['other']}/branches/{fork_id}", headers=analyst).status_code == 404


# ------------------------------------------------------------------------------------ migration
def test_migration_0040_upgrades_and_downgrades(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from analystos.core.config import REPO_ROOT

    url = make_url(control_db)
    name = f"{url.database}_m40"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {name}"))
    target = url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(target)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", target)
        command.upgrade(cfg, "0040")
        tables = ("workspace_brief", "readiness_assessment", "step_branch", "analysis_step", "analysis_step_version",
                  "step_pin", "notebook")
        with engine.connect() as c:
            present = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert set(tables) <= present
        command.downgrade(cfg, "0035")
        with engine.connect() as c:
            present = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert not set(tables) & present
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()

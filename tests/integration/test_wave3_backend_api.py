"""Wave 3 backend follow-ups through the HTTP API on Postgres:

* migration 0041 up / down / up, matching the models;
* P4-06: `request_id` in every error envelope; optional If-Match on monitor and Ask-thread PATCH (412 when stale,
  old clients without it keep working);
* P7-11: a query tool's draft -> tested -> published path, with the one-draft index covering a tested draft;
* P7-03: promotion dev -> test by content hash with the connection re-bound by name;
* P4-05: an ownership transfer offered by the owner and accepted by the recipient; the approvals inbox refuses it;
* P7-01 / P7-08: the re-verify and Ask-turn "why" routes answer 404 for an unknown id, with the envelope.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.db import vectors

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(api):
    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    ids = {}
    for env in ("dev", "test"):
        r = api.post("/api/workspaces", headers=analyst, json={"name": f"wave3 {env}", "objective": "Find the drivers of SLA breaches"})
        assert r.status_code == 200, r.text
        ids[env] = r.json()["id"]
        assert api.post(f"/api/workspaces/{ids[env]}/members", headers=analyst,
                        json={"email": "approver@analystos.local", "role": "editor"}).status_code == 200
    from analystos.db.base import session_scope
    from analystos.db.models import Source, User, Workspace

    with session_scope() as s:
        for env, ws in ids.items():
            w = s.get(Workspace, ws)
            w.settings = {**(w.settings or {}), "environment": env}
            s.add(Source(id=f"src_w3_{env}", workspace_id=ws, kind="postgres", name="warehouse"))
        analyst_id = s.scalar(select(User.id).where(User.email == "analyst@analystos.local"))
        approver_id = s.scalar(select(User.id).where(User.email == "approver@analystos.local"))
    return {"dev": ids["dev"], "test": ids["test"], "analyst": analyst, "approver": approver, "analyst_id": analyst_id,
            "approver_id": approver_id}


def test_migration_0041_up_down_up(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT
    from analystos.db.models import AskThread, Definition, Monitor

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig41"
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

        def cols(table: str) -> set[str]:
            return {c["name"] for c in inspect(engine).get_columns(table)}

        command.upgrade(cfg, "0041")
        # columns later migrations add: tenant_id (0057, ADR-009) everywhere; S04 conversations (0062); the S09 noise
        # gate on monitors (0071); the S06-T6 playbook signature (0073)
        later = {"definition": {"signature"}, "monitor": {"noise_state", "approval_id", "rule_hash"},
                 "ask_thread": {"summarized_at", "closed_at", "legal_hold", "branch_from_message_id", "parent_conversation_id",
                                "request_id", "channel"}}
        for table, model in (("definition", Definition), ("monitor", Monitor), ("ask_thread", AskThread)):
            assert cols(table) == {c.name for c in model.__table__.columns} - {"tenant_id"} - later.get(table, set()), table
        idx = {i["name"]: i for i in inspect(engine).get_indexes("definition")}
        assert "tested" in str(idx["uq_definition_one_draft"].get("dialect_options", {}).get("postgresql_where"))
        command.downgrade(cfg, "0037")
        assert "test_evidence" not in cols("definition") and "revision" not in cols("monitor")
        command.upgrade(cfg, "0041")
        assert "revision" in cols("ask_thread")
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))


# ------------------------------------------------------------------------------------ P4-06
def test_error_envelopes_carry_the_request_id(api, world):
    r = api.post("/api/verification/ver_nope/reverify", headers={**world["analyst"], "X-Request-ID": "w3-reverify"})
    assert r.status_code == 404 and r.json()["error"]["request_id"] == "w3-reverify"
    assert r.headers["X-Request-ID"] == "w3-reverify"
    r = api.get("/api/ask/turns/turn_nope/why", headers=world["analyst"])
    assert r.status_code == 404 and r.json()["error"]["request_id"] == r.headers["X-Request-ID"]


def test_monitor_patch_takes_an_optional_if_match(api, world):
    ws, h = world["dev"], world["analyst"]
    r = api.post(f"/api/workspaces/{ws}/monitors", headers=h,
                 json={"name": "breaches", "kind": "metric_threshold",
                       "config": {"sql_expression": "COUNT(*)", "op": ">", "value": 10}})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    r = api.patch(f"/api/monitors/{mid}", headers=h, json={"name": "breaches (old client)"})  # no If-Match: works
    assert r.status_code == 200 and r.headers["ETag"] == '"2"' and r.json()["revision"] == 2
    stale = api.patch(f"/api/monitors/{mid}", headers={**h, "If-Match": '"1"'}, json={"enabled": False})
    assert stale.status_code == 412 and stale.json()["error"]["details"]["current_revision"] == 2
    assert stale.json()["error"]["request_id"]
    r = api.patch(f"/api/monitors/{mid}", headers={**h, "If-Match": '"2"'}, json={"enabled": False})
    assert r.status_code == 200 and r.headers["ETag"] == '"3"' and r.json()["enabled"] is False
    bad = api.patch(f"/api/monitors/{mid}", headers=h, json={"config": {"on_void_baseline": "hide"}})
    assert bad.status_code == 422


def test_ask_thread_patch_takes_an_optional_if_match(api, world):
    ws, h = world["dev"], world["analyst"]
    tid = api.post(f"/api/workspaces/{ws}/ask/threads", headers=h, json={"title": "SLA"}).json()["id"]
    r = api.patch(f"/api/ask/threads/{tid}", headers=h, json={"title": "SLA questions"})
    assert r.status_code == 200 and r.headers["ETag"] == '"2"'
    assert api.patch(f"/api/ask/threads/{tid}", headers={**h, "If-Match": '"1"'}, json={"archived": True}).status_code == 412
    r = api.patch(f"/api/ask/threads/{tid}", headers={**h, "If-Match": '"2"'}, json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True and r.headers["ETag"] == '"3"'


# ------------------------------------------------------------------------------------ P7-11 + P7-03
def test_a_query_tool_is_tested_published_and_promoted(api, world, monkeypatch):
    from analystos.tools import query_tools as Q

    monkeypatch.setattr(Q, "run", lambda *a, **k: {"query_id": "q_w3", "row_count": 3, "result_hash": "rh",
                                                   "columns": ["id"], "rows": [], "truncated": False})
    dev, test_ws, h = world["dev"], world["test"], world["analyst"]
    spec = {"description": "Tickets of a priority", "sql": "SELECT id FROM sn.incident WHERE priority = :p",
            "parameters": {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]},
            "source_id": "src_w3_dev", "test_arguments": {"p": "P1"}}
    r = api.post(f"/api/workspaces/{dev}/definitions", headers=h, json={"kind": "query_tool", "key": "by_priority", "spec": spec})
    assert r.status_code == 201, r.text
    d = r.json()
    early = api.post(f"/api/workspaces/{dev}/definitions/{d['id']}/publish", headers={**h, "If-Match": '"1"'})
    assert early.status_code == 409 and "must pass its test" in early.json()["error"]["message"]
    r = api.post(f"/api/workspaces/{dev}/definitions/{d['id']}/test", headers={**h, "If-Match": '"1"'}, json={})
    assert r.status_code == 200 and r.json()["status"] == "tested" and r.json()["test_evidence"]["query_id"] == "q_w3"
    again = api.post(f"/api/workspaces/{dev}/definitions", headers=h, json={"kind": "query_tool", "key": "by_priority", "spec": spec})
    assert again.status_code == 409  # the tested draft is still the one draft
    r = api.post(f"/api/workspaces/{dev}/definitions/{d['id']}/publish", headers={**h, "If-Match": '"2"'})
    assert r.status_code == 200 and r.json()["status"] == "published"
    r = api.post(f"/api/workspaces/{dev}/definitions/{d['id']}/promote", headers=h, json={"target_workspace_id": test_ws})
    assert r.status_code == 201, r.text
    promoted = r.json()
    assert promoted["content_hash"] == d["content_hash"] and promoted["workspace_id"] == test_ws
    assert promoted["bindings"] == {"environment": "test", "sources": {"src_w3_dev": "src_w3_test"}}
    assert promoted["promoted_from"]["definition_id"] == d["id"]


# ------------------------------------------------------------------------------------ P4-05
def test_ownership_is_offered_and_accepted_by_the_recipient(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import SemanticMetric

    ws = world["dev"]
    with session_scope() as s:
        s.add(SemanticMetric(id="smet_w3", workspace_id=ws, name="breach_rate", version=1, status="approved",
                             definition={"name": "breach_rate", "expressions": [{"dialect": "ANSI_SQL", "expression": "AVG(x)"}]},
                             expression="AVG(x)", normalized_expression="avg(x)", owner_id=world["analyst_id"],
                             proposed_by=world["analyst_id"], proposed_via="user", content_hash="c_w3"))
    r = api.post(f"/api/workspaces/{ws}/semantic/ownership", headers=world["analyst"],
                 json={"subject": "metric", "name": "breach_rate", "to_owner": world["approver_id"]})
    assert r.status_code == 201, r.text
    apr = r.json()["id"]
    inbox = api.post(f"/api/approvals/{apr}/approve", headers=world["approver"])
    assert inbox.status_code == 422 and "recipient" in inbox.json()["error"]["message"]
    assert api.post(f"/api/workspaces/{ws}/semantic/ownership/{apr}/accept", headers=world["analyst"]).status_code == 403
    r = api.post(f"/api/workspaces/{ws}/semantic/ownership/{apr}/accept", headers=world["approver"])
    assert r.status_code == 200 and r.json()["to_owner"] == world["approver_id"]
    with session_scope() as s:
        assert s.get(SemanticMetric, "smet_w3").owner_id == world["approver_id"]

"""S07-T8 number check on Postgres with row-level security, through the HTTP API: a claimed number is confirmed or
contradicted against Ask's answer over staged ServiceNow data (numbers equal an independent reference query), a
labelled row is picked out of a breakdown, an unanswerable question is unverifiable, the claimed value never reaches
the model, and each check leaves an audit row. The model transport is a fake that writes the SQL (as in AT-19)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from tests.integration.test_request_desk import _login, _reference, api, servicenow_url, transport  # noqa: F401

pytestmark = pytest.mark.integration
CLAIM_MARKER = "98765"


@pytest.fixture(scope="module")
def world(control_db, servicenow_url):  # noqa: F811 - the module's own workspace for the imported `api`
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.runtime.context import default_router
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANALYSTOS_SOURCE_SECRET_SERVICENOW")}
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    get_settings.cache_clear()
    default_router.cache_clear()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="number checks", objective="Check numbers quoted by other tools")
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
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws_id, SourceAsset.name == "incident"))
        table = f"{asset.schema_name}.{asset.name}"
    yield {"ws": ws_id, "table": table}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


def _check(api, headers, ws, **body):  # noqa: F811
    r = api.post("/api/v2/number-checks", headers=headers, json={"workspace_id": ws, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_number_checks_confirm_contradict_and_refuse(api, world, transport):  # noqa: F811
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent

    table, ws = world["table"], world["ws"]
    headers = _login(api, "analyst@analystos.local")
    total_q, by_prio_q = "How many incidents are there in total?", "Count of incidents by priority"
    transport.sql_for = {total_q: f"SELECT COUNT(*) AS n FROM {table}",
                         by_prio_q: f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY priority"}
    [(total,)] = _reference(f"SELECT COUNT(*) AS n FROM {table}")
    total = int(total)

    ok = _check(api, headers, ws, question=total_q, claimed=f"{total:,}", source="Other BI copilot")
    assert (ok["verdict"], ok["authority"], ok["computed"]["value"]) == ("confirmed", "ad_hoc", total)
    assert ok["evidence"]["query_id"] and ok["evidence"]["result_hash"] and ok["evidence"]["sql"]
    assert ok["evidence"]["assets"] == [table] and ok["source"] == "Other BI copilot"

    bad = _check(api, headers, ws, question=total_q, claimed=CLAIM_MARKER)
    assert bad["verdict"] == "contradicted" and bad["comparison"]["difference"] == float(CLAIM_MARKER) - total

    prio, n = _reference(f"SELECT priority, COUNT(*) AS n FROM {table} GROUP BY priority")[0]
    label = str(int(prio)) if isinstance(prio, float) and prio.is_integer() else str(prio)
    row = _check(api, headers, ws, question=by_prio_q, claimed=str(int(n)), label=label)
    assert row["verdict"] == "confirmed" and row["computed"]["value"] == n, row["reason"]
    unlabelled = _check(api, headers, ws, question=by_prio_q, claimed=str(int(n)))
    assert unlabelled["verdict"] == "unverifiable" and "label" in unlabelled["reason"]

    none = _check(api, headers, ws, question="How many payroll runs by department this year?", claimed="12")
    assert none["verdict"] == "unverifiable" and none["computed"] is None
    marked = _check(api, headers, ws, question="Number of incidents by state", claimed=CLAIM_MARKER)
    assert marked["verdict"] == "unverifiable"  # the fake model has no SQL for it, so the model rung certainly ran
    assert transport.chat_calls, "the checks ran Ask's model rung"
    assert not any(CLAIM_MARKER in str(call) for call in transport.chat_calls), "the claimed value reached the model"

    r = api.post("/api/v2/number-checks", headers=headers, json={"workspace_id": ws, "question": total_q,
                                                                  "claimed": "about twelve"})
    assert r.status_code == 422

    with session_scope() as s:
        decisions = [a.decision for a in s.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == ws, AuditEvent.action == "number_check.completed").order_by(AuditEvent.id))]
    assert sorted(decisions) == sorted(["confirmed", "contradicted", "confirmed"] + ["unverifiable"] * 3)


def test_number_check_needs_workspace_membership(api, world):  # noqa: F811
    headers = _login(api, "approver@analystos.local")  # not a member of this workspace
    r = api.post("/api/v2/number-checks", headers=headers,
                 json={"workspace_id": world["ws"], "question": "How many incidents?", "claimed": "1"})
    assert r.status_code in (403, 404), r.text


def test_handoff_to_the_desk_and_trust_coverage(api, world, transport):  # noqa: F811
    from analystos.db.base import session_scope
    from analystos.desk.models import Request

    table, ws = world["table"], world["ws"]
    headers = _login(api, "analyst@analystos.local")
    total_q = "How many incidents are there in total?"
    transport.sql_for = {total_q: f"SELECT COUNT(*) AS n FROM {table}"}
    [(total,)] = _reference(f"SELECT COUNT(*) AS n FROM {table}")
    bad = _check(api, headers, ws, question=total_q, claimed=str(int(total) + 5), source="Other BI copilot")
    assert bad["verdict"] == "contradicted"

    # the verdict is recomputed from the stored answer: a caller cannot hand off a different one
    r = api.post("/api/v2/number-checks/handoff", headers=headers, json={
        "workspace_id": ws, "turn_id": bad["evidence"]["turn_id"], "claimed": str(int(total) + 5), "source": "Other BI copilot"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["state"] == "escalated" and out["verdict"] == "contradicted"
    with session_scope() as s:
        req = s.get(Request, out["request_id"])
        assert (req.request_class, req.conversation_id) == ("R7", bad["evidence"]["thread_id"])
        pack = req.data_pack["number_check"]
        assert pack["verdict"] == "contradicted" and pack["computed"]["value"] == float(total)
        assert req.data_pack["evidence"][0]["id"] == bad["evidence"]["turn_id"] and req.sla_due_at is not None
    assert api.get(f"/api/requests/{out['request_id']}", headers=headers).json()["state"] == "escalated"

    other = api.post("/api/v2/number-checks/handoff", headers=_login(api, "approver@analystos.local"), json={
        "workspace_id": ws, "turn_id": bad["evidence"]["turn_id"], "claimed": "1"})
    assert other.status_code in (403, 404), other.text  # not a member of the workspace

    value = api.get("/api/value", headers=headers, params={"workspace": ws})
    assert value.status_code == 200, value.text
    trust = value.json()["trust"]
    assert trust["answered"] >= 1 and trust["certified"] == 0 and trust["certified_share"] == 0
    assert trust["checks"]["contradicted"] >= 1

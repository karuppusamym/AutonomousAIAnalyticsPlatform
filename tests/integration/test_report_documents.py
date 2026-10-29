"""CONV-04/05 on Postgres under strict tenancy, over REST: a report built by conversation from a builder dashboard
(rules ladder, no model call), the numbers guard, versions and revert, the export approval bound to version and
snapshot hash (requester only, single use, invalidated by an edit), another workspace's member denied, the dashboard
story and "turn this into a report", and a schedule fire refreshing a pinned report into a final snapshot."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.ids import new_id  # noqa: E402
from analystos.db.base import session_scope  # noqa: E402
from analystos.db.models import Approval, Artifact, AuditEvent, Schedule, User  # noqa: E402

pytestmark = pytest.mark.integration


def _user(s, email: str) -> User:
    from analystos.security.auth import hash_password

    u = User(id=new_id("usr"), email=email, name=email.split("@")[0], password_hash=hash_password("x"), is_admin=False)
    s.add(u)
    s.flush()
    return u


def _auth(user: User) -> dict[str, str]:
    from analystos.security.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(user.id, user.email)}"}


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        people = {r: _user(s, f"{r}-{new_id('x')}@t") for r in ("approver", "editor", "viewer")}
        outsider = _user(s, f"outsider-{new_id('x')}@t")
        ws = create_workspace(s, owner, name="Report authoring", objective="Shape reports by conversation", autonomy_level=3)
        other = create_workspace(s, outsider, name="Elsewhere", objective="Another workspace", autonomy_level=3)
        s.flush()
        for role, u in people.items():
            add_member(s, owner, ws.id, u.email, role)
        return {"ws": ws.id, "other_ws": other.id, "owner": owner.id, **{r: u.id for r, u in people.items()},
                "headers": {k: _auth(u) for k, u in {"owner": owner, "outsider": outsider, **people}.items()}}


@pytest.fixture(scope="module")
def api(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def dashboard(api, world):
    from tests.unit.test_dashboard_dsl import sample

    raw = copy.deepcopy(sample())
    raw["id"] = f"d_{new_id('x')[-8:].lower()}"
    r = api.post(f"/api/v2/workspaces/{world['ws']}/dashboards", json={"document": raw}, headers=world["headers"]["editor"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _say(api, world, doc_id: str, text: str, version: int | None = None) -> dict:
    r = api.post(f"/api/v2/report-documents/{doc_id}/messages", json={"text": text, "base_version": version},
                 headers=world["headers"]["editor"])
    assert r.status_code == 200, r.text
    return r.json()


def _approve(world, approval_id: str) -> None:
    from analystos.governance.approvals import decide

    with session_scope() as s:
        decide(s, approval_id, s.get(User, world["approver"]), approve=True)


def test_report_by_conversation_with_approval_bound_export(api, world, dashboard):
    h = world["headers"]
    assert api.post(f"/api/v2/workspaces/{world['ws']}/report-documents", json={"title": "x"}, headers=h["viewer"]).status_code == 403
    created = api.post(f"/api/v2/workspaces/{world['ws']}/report-documents", json={"title": "Weekly operations"}, headers=h["editor"])
    assert created.status_code == 201
    doc_id = created.json()["id"]
    content = api.get(f"/api/v2/workspaces/{world['ws']}/report-content", headers=h["editor"]).json()
    assert {"Incidents per week", "Open incidents"} <= {c["title"] for c in content}

    out = _say(api, world, doc_id, "add a section on incidents per week", 1)
    assert out["status"] == "applied" and out["proposal"]["answered_by"] == "rules"
    assert out["report"]["document"]["sections"][1]["blocks"][0]["rows"][1] == ["2026-10-12", 135, "APAC"]
    assert _say(api, world, doc_id, "include the open incidents kpi in the summary section")["status"] == "applied"
    stale = api.post(f"/api/v2/report-documents/{doc_id}/messages", json={"text": "make the summary shorter", "base_version": 1},
                     headers=h["editor"])
    assert stale.status_code == 409  # If-Match: the report moved on
    assert _say(api, world, doc_id, "set the summary to: Incidents rose to 135 in the week of 12 October.")["status"] == "applied"
    refused = _say(api, world, doc_id, "set the summary to: Incidents rose 40% to 189.")
    assert refused["status"] == "refused" and "40%" in refused["refused"][0]["reason"]

    versions = api.get(f"/api/v2/report-documents/{doc_id}/versions", headers=h["viewer"]).json()
    assert [v["version"] for v in versions] == [4, 3, 2, 1] and versions[0]["diff"][0]["op"] == "text.set"
    preview = api.get(f"/api/v2/report-documents/{doc_id}/preview", headers=h["viewer"]).json()
    assert "Incidents rose to 135" in preview["html"] and preview["snapshot_hash"] in preview["html"]

    req = _say(api, world, doc_id, "export it as a web page")
    aid = req["export"]["approval"]["id"]
    with session_scope() as s:
        a = s.get(Approval, aid)
        assert a.payload["snapshot_hash"] == preview["snapshot_hash"] and a.payload["version"] == 4
    pending = api.get(f"/api/v2/report-documents/{doc_id}/exports/html", params={"approval_id": aid}, headers=h["editor"])
    assert pending.status_code != 200
    _approve(world, aid)
    assert api.get(f"/api/v2/report-documents/{doc_id}/exports/html", params={"approval_id": aid},
                   headers=h["owner"]).status_code == 403  # granted to the requester only
    ok = api.get(f"/api/v2/report-documents/{doc_id}/exports/html", params={"approval_id": aid}, headers=h["editor"])
    assert ok.status_code == 200 and b"Incidents rose to 135" in ok.content
    assert preview["snapshot_hash"].encode() in ok.content
    again = api.get(f"/api/v2/report-documents/{doc_id}/exports/html", params={"approval_id": aid}, headers=h["editor"])
    assert again.status_code != 200  # single use
    with session_scope() as s:
        assert s.get(Artifact, doc_id).status == "final"
        assert s.query(AuditEvent).filter(AuditEvent.action == "report_document.exported", AuditEvent.target == doc_id).count() == 1

    forked = _say(api, world, doc_id, "rename the incidents per week section to Weekly trend")
    assert forked["forked_from"] == doc_id and forked["report"]["id"] != doc_id  # the exported report stays as it was

    # another workspace's member sees nothing, as for any workspace child
    assert api.get(f"/api/v2/report-documents/{doc_id}", headers=h["outsider"]).status_code == 404
    assert api.get(f"/api/v2/workspaces/{world['ws']}/report-content", headers=h["outsider"]).status_code == 404


def test_edit_after_request_invalidates_the_export_and_revert_is_a_new_version(api, world, dashboard):
    h = world["headers"]
    doc_id = api.post(f"/api/v2/workspaces/{world['ws']}/report-documents", json={"title": "Ops"}, headers=h["editor"]).json()["id"]
    _say(api, world, doc_id, "add a section on incidents per week")
    req = api.post(f"/api/v2/report-documents/{doc_id}/exports", json={"format": "html"}, headers=h["editor"]).json()
    _approve(world, req["approval"]["id"])
    _say(api, world, doc_id, "rename the incidents per week section to Trend")
    with session_scope() as s:
        assert s.get(Approval, req["approval"]["id"]).status == "invalidated"
    assert api.get(f"/api/v2/report-documents/{doc_id}/exports/html", params={"approval_id": req["approval"]["id"]},
                   headers=h["editor"]).status_code != 200
    back = api.post(f"/api/v2/report-documents/{doc_id}/revert", json={"to_version": 2}, headers=h["editor"]).json()
    assert back["report"]["version"] == 4 and back["report"]["document"]["sections"][1]["title"] == "Incidents per week"


def test_story_to_report_and_a_pinned_report_refreshes_on_a_schedule_fire(api, world, dashboard):
    from analystos.dashboards import service as dashboards
    from analystos.services import report_docs

    h = world["headers"]
    story = api.get(f"/api/v2/dashboards/{dashboard}/story", headers=h["viewer"]).json()
    assert story["shows"].startswith("Operations overview shows") and story["facts"] and story["wording"] == "rules"
    made = api.post(f"/api/v2/dashboards/{dashboard}/story/report", json={}, headers=h["editor"])
    assert made.status_code == 201
    doc_id = made.json()["id"]
    assert made.json()["document"]["origin"]["type"] == "story"

    with session_scope() as s:
        sch = Schedule(id=new_id("sch"), workspace_id=world["ws"], name=f"Weekly {new_id('x')[-4:]}", kind="monitor",
                       cron="0 8 * * 1", timezone="UTC", config={}, owner_id=world["editor"], enabled=True)
        s.add(sch)
        sch_id, sch_name = sch.id, sch.name
    pinned = _say(api, world, doc_id, f"pin it to the {sch_name} schedule")
    assert pinned["status"] == "applied" and pinned["report"]["document"]["schedule_id"] == sch_id

    with session_scope() as s:  # the dashboard's data moves on
        editor = s.get(User, world["editor"])
        art = dashboards.load(s, editor, dashboard, for_update=True)
        content = copy.deepcopy(art.content)
        content["charts"][0]["preview"]["rows"].append(["2026-10-19", 150, "EMEA"])
        dashboards._new_version(s, art, dashboards.document(art).model_validate(content), [{"op": "test"}], editor)
    with session_scope() as s:
        result = report_docs.after_schedule_fire(s, s.get(Schedule, sch_id), type("Run", (), {"id": "srun_test"})())
    info = result["report_documents"][doc_id]
    assert info["changed"] and info["report_artifact_id"]
    with session_scope() as s:
        doc = report_docs.document(s.get(Artifact, doc_id))
        assert "150" in doc.section("summary").blocks[0].markdown
        report = s.get(Artifact, info["report_artifact_id"])
        assert report.type == "report" and report.status == "final" and report.content["source"]["report_document"] == doc_id

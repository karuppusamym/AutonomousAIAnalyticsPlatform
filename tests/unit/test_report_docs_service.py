"""CONV-04/05 service layer on the in-memory control plane: a report built by conversation from a builder dashboard
the user may read, versions with op diffs, revert, the export approval bound to version and snapshot hash (verified
immediately before rendering, single use), fork-on-edit of an exported report, a dashboard story turned into a
report, and a schedule fire that refreshes a pinned report and renders a final snapshot for delivery."""
from __future__ import annotations

import copy

import pytest

from analystos.core.errors import ApprovalRequired, Forbidden
from analystos.db.models import Approval, Artifact, Schedule, User


class _Off:
    """The model rungs turned off: the rules decide (the default router would share the test's SQLite connection)."""

    def mode(self, purpose, ctx=None):
        return "off"

    def record_skip(self, *a, **k):
        pass


OFF = _Off()


def _user(uid: str) -> User:
    return User(id=uid, email=f"{uid}@example.org", name=uid, password_hash="unused", is_admin=False, active=True)


@pytest.fixture
def world(sqlite_db, tmp_path, monkeypatch):
    from tests.unit.test_dashboard_dsl import sample

    from analystos.core.config import get_settings
    from analystos.dashboards import service as dashboards
    from analystos.services.workspaces import add_member, create_workspace

    monkeypatch.setattr(get_settings(), "artifact_dir", str(tmp_path))
    with sqlite_db() as s:
        owner, editor, approver, viewer = (_user(u) for u in ("u_owner", "u_editor", "u_approver", "u_viewer"))
        s.add_all([owner, editor, approver, viewer])
        s.flush()
        ws = create_workspace(s, owner, name="Reports")
        s.flush()
        for u, role in ((editor, "editor"), (approver, "approver"), (viewer, "viewer")):
            add_member(s, owner, ws.id, u.email, role)
        raw = copy.deepcopy(sample())
        raw["kpis"][0]["value"] = 42
        dash = dashboards.create(s, editor, ws.id, raw)
        s.commit()
        return {"ws": ws.id, "dash": dash.id, "factory": sqlite_db}


def U(s, uid: str) -> User:
    return s.get(User, uid)


def test_build_by_conversation_then_export_through_an_approval(world):
    from analystos.governance.approvals import decide
    from analystos.services import report_docs as svc

    with world["factory"]() as s:
        ed = U(s, "u_editor")
        art = svc.create(s, ed, world["ws"], title="Weekly operations")
        cat = svc.catalog(s, ed, world["ws"])
        assert {c.title for c in cat} >= {"Incidents per week", "Open incidents"}  # the preview-less chart is not offered
        assert "Incidents by priority" not in {c.title for c in cat}
        out = svc.converse(s, ed, art.id, "add a section on incidents per week", router=OFF)
        assert out["status"] == "applied" and out["report"]["version"] == 2
        assert [o["op"] for o in out["proposal"]["ops"]] == ["section.add", "block.add"]
        out = svc.converse(s, ed, art.id, "include the open incidents kpi in the summary section", router=OFF)
        assert out["status"] == "applied"
        out = svc.converse(s, ed, art.id, "set the summary to: Incidents rose to 135; 42 are open.", router=OFF)
        assert out["status"] == "applied"
        refused = svc.converse(s, ed, art.id, "set the summary to: 77 are open.", router=OFF)
        assert refused["status"] == "refused" and "77" in refused["refused"][0]["reason"]
        history = svc.history(s, ed, art.id)
        assert history[0]["version"] == 4 and history[0]["diff"][0]["origin"]["answered_by"] == "rules"
        prev = svc.preview(s, ed, art.id)
        assert "Incidents rose to 135" in prev["html"] and prev["snapshot_hash"] in prev["html"]

        with pytest.raises(Forbidden):  # a viewer cannot request an export
            svc.request_export(s, U(s, "u_viewer"), art.id, "html")
        req = svc.converse(s, ed, art.id, "export it as a web page", router=OFF)
        approval_id = req["export"]["approval"]["id"]
        assert req["report"]["status"] == "approval_required"
        with pytest.raises(ApprovalRequired):  # pending: nothing is rendered yet
            svc.export(s, ed, art.id, "html", approval_id=approval_id)
        decide(s, approval_id, U(s, "u_approver"), approve=True)
        with pytest.raises(Forbidden):  # only the requester executes the approval
            svc.export(s, U(s, "u_owner"), art.id, "html", approval_id=approval_id)
        data, mime, name = svc.export(s, ed, art.id, "html", approval_id=approval_id)
        assert mime.startswith("text/html") and b"Incidents rose to 135" in data and name.endswith("-v4.html")
        assert s.get(Approval, approval_id).status == "executed" and s.get(Artifact, art.id).status == "final"
        with pytest.raises(ApprovalRequired):  # single use
            svc.export(s, ed, art.id, "html", approval_id=approval_id)

        forked = svc.converse(s, ed, art.id, "rename the incidents per week section to Weekly trend", router=OFF)
        assert forked["forked_from"] == art.id and forked["report"]["id"] != art.id
        assert s.get(Artifact, art.id).version == 4  # the exported report is untouched


def test_an_edit_after_the_request_invalidates_the_export_approval(world):
    from analystos.governance.approvals import decide
    from analystos.services import report_docs as svc

    with world["factory"]() as s:
        ed = U(s, "u_editor")
        art = svc.create(s, ed, world["ws"], title="Ops")
        svc.converse(s, ed, art.id, "add a section on incidents per week", router=OFF)
        req = svc.request_export(s, ed, art.id, "html")
        decide(s, req["approval"]["id"], U(s, "u_approver"), approve=True)
        svc.converse(s, ed, art.id, "rename the incidents per week section to Trend", router=OFF)
        assert s.get(Approval, req["approval"]["id"]).status == "invalidated"
        with pytest.raises(ApprovalRequired):
            svc.export(s, ed, art.id, "html", approval_id=req["approval"]["id"])
        reverted = svc.revert(s, ed, art.id, 2)
        assert reverted["report"]["version"] == 4 and reverted["report"]["document"]["sections"][1]["title"] == "Incidents per week"


def test_story_becomes_a_report_and_a_pinned_report_refreshes_on_a_schedule_fire(world):
    from analystos.dashboards import service as dashboards
    from analystos.services import report_docs as svc
    from analystos.services import stories

    with world["factory"]() as s:
        ed = U(s, "u_editor")
        st = stories.story(s, ed, world["dash"], router=OFF)
        assert st["shows"].startswith("Operations overview shows") and st["facts"]
        art = stories.to_report(s, ed, world["dash"])
        doc = svc.document(art)
        assert doc.origin["type"] == "story" and [x.id for x in doc.sections][:2] == ["summary", "changes"]
        assert doc.derived and doc.section("details").blocks

        sch = Schedule(id="sch_1", workspace_id=world["ws"], name="Weekly ops", kind="monitor", cron="0 8 * * 1",
                       timezone="UTC", config={}, owner_id="u_editor", enabled=True)
        s.add(sch)
        s.flush()
        pinned = svc.converse(s, ed, art.id, "pin it to the Weekly ops schedule", router=OFF)
        assert pinned["status"] == "applied" and pinned["report"]["document"]["schedule_id"] == "sch_1"

        dash = dashboards.load(s, ed, world["dash"])  # the dashboard's trend moves on
        content = copy.deepcopy(dash.content)
        content["charts"][0]["preview"]["rows"].append(["2026-10-19", 150, "EMEA"])
        dashboards._new_version(s, dash, dashboards.document(dash).model_validate(content), [{"op": "test"}], ed)

        result = svc.after_schedule_fire(s, sch, type("Run", (), {"id": "srun_1"})())
        info = result["report_documents"][art.id]
        assert info["changed"] and info["report_artifact_id"] and info["deliveries"] == []
        refreshed = svc.document(s.get(Artifact, art.id))
        assert "150" in refreshed.section("summary").blocks[0].markdown  # the story text follows the data
        report = s.get(Artifact, info["report_artifact_id"])
        assert report.type == "report" and report.status == "final" and report.content["kind"] == "document"
        assert report.content["source"]["report_document"] == art.id and "html" in report.content["files"]

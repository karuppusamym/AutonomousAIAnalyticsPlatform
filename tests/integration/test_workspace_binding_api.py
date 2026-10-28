"""P4-01 (SEC-002, UI-003) through the real app and Postgres: workspace-bound child routes and SSE
streams that lose their caller.

Two workspaces A and B. `dual` is an analyst of both, `single` an analyst of A only. Every child lives
in B. A path that names A with a child of B is a 404 even for `dual`; a child of B addressed by id
alone is a 404 for `single`; the 404 is byte-for-byte the one an unknown id gets (nothing about B leaks)
and nothing is mutated. An open event stream ends with `revoked` when the membership is removed and
with `expired` when the token does, and carries no event after that."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
SECRET = "B-only objective 7f3a"  # appears in B's objects; must never appear in a denied response


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
    from analystos.core.ids import new_id, utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import (
        Alert,
        AnalysisRun,
        Approval,
        Artifact,
        AskThread,
        AskTurn,
        Feedback,
        Hypothesis,
        Insight,
        Monitor,
        QueryExecution,
        RunEvent,
        RunTask,
        Schedule,
        Source,
    )

    admin = _login(api, "admin@analystos.local")
    owner = _login(api, "analyst@analystos.local")
    users = {}
    for label in ("single", "dual"):
        email = f"{label}-{new_id('u')}@analystos.local"
        r = api.post("/api/users", headers=admin, json={"email": email, "name": label, "password": PASSWORD})
        assert r.status_code == 200, r.text
        users[label] = {"email": email, "id": r.json()["id"], "h": _login(api, email)}
    ws = {}
    for label in ("A", "B"):
        r = api.post("/api/workspaces", headers=owner, json={"name": f"binding {label}", "objective": "Find the drivers of SLA breaches"})
        assert r.status_code == 200, r.text
        ws[label] = r.json()["id"]
    for label, member in (("A", "single"), ("A", "dual"), ("B", "dual")):
        r = api.post(f"/api/workspaces/{ws[label]}/members", headers=owner, json={"email": users[member]["email"], "role": "analyst"})
        assert r.status_code == 200, r.text

    b, ids = ws["B"], {}
    with session_scope() as s:
        def run(status: str) -> AnalysisRun:
            r = AnalysisRun(id=new_id("run"), workspace_id=b, objective=SECRET, status=status, plan={"steps": []}, plan_version=1,
                            scope={}, instructions=[], constraints={}, requested_by=users["dual"]["id"], summary={},
                            origin={"type": "user"}, control="run")
            s.add(r)
            return r
        done, live = run("COMPLETED"), run("RUNNING")
        s.flush()
        s.add(RunEvent(workspace_id=b, run_id=done.id, type="run.status", payload={"secret": SECRET}))
        task = RunTask(id=new_id("task"), run_id=done.id, key="profile", agent_id="profiler", title=SECRET)
        hyp = Hypothesis(id=new_id("hyp"), workspace_id=b, run_id=done.id, code="H1", statement=SECRET)
        ins = Insight(id=new_id("ins"), workspace_id=b, run_id=done.id, code="F1", title=SECRET, finding=SECRET, evidence=[])
        art = Artifact(id=new_id("art"), workspace_id=b, run_id=done.id, type="report", name=SECRET, plan_version=1,
                       status="validated", content={"secret": SECRET}, content_hash="h")
        apr = Approval(id=new_id("apr"), workspace_id=b, run_id=done.id, action="publish_dashboard", risk_tier="high",
                       payload={"secret": SECRET}, payload_hash="0" * 64, policy_version=1, requested_by=users["dual"]["id"],
                       expires_at=utcnow() + timedelta(days=1))
        qe = QueryExecution(id=new_id("qry"), workspace_id=b, run_id=done.id, actor="agent:sql", sql=f"SELECT '{SECRET}'",
                            status="ok")
        mon = Monitor(id=new_id("mon"), workspace_id=b, name=SECRET, kind="metric_threshold", created_by=users["dual"]["id"])
        alert = Alert(id=new_id("alr"), workspace_id=b, severity="warning", title=SECRET, message=SECRET, dedupe_key="k")
        fb = Feedback(id=new_id("fb"), workspace_id=b, run_id=done.id, user_id=users["dual"]["id"], kind="redirect", text=SECRET)
        sch = Schedule(id=new_id("sch"), workspace_id=b, name=SECRET, kind="reanalysis", cron="0 6 * * *",
                       owner_id=users["dual"]["id"], enabled=False)
        src = Source(id=new_id("src"), workspace_id=b, kind="csv", name=SECRET, config={}, status="ready")
        thread = AskThread(id=new_id("ask"), workspace_id=b, user_id=users["dual"]["id"], title=SECRET)
        s.add_all([task, hyp, ins, art, apr, qe, mon, alert, fb, sch, src, thread])
        s.flush()
        turn = AskTurn(id=new_id("askt"), thread_id=thread.id, workspace_id=b, user_id=users["dual"]["id"], question=SECRET,
                       status="answered", result={})
        s.add(turn)
        ids = {"run": done.id, "live": live.id, "task": task.id, "hyp": hyp.id, "ins": ins.id, "art": art.id, "apr": apr.id,
               "qry": qe.id, "mon": mon.id, "alert": alert.id, "fb": fb.id, "sch": sch.id, "src": src.id, "thread": thread.id,
               "turn": turn.id}
    return {"ws": ws, "users": users, "owner": owner, "ids": ids}


# A path with {ws} and a child of B: (method, template, json body)
WS_ROUTES = [
    ("GET", "/api/workspaces/{ws}/analysis/{run}", None),
    ("GET", "/api/workspaces/{ws}/analysis/{run}/console", None),
    ("GET", "/api/workspaces/{ws}/analysis/{run}/events", None),
    ("POST", "/api/workspaces/{ws}/analysis/{live}/pause", None),
    ("POST", "/api/workspaces/{ws}/analysis/{live}/cancel", None),
    ("POST", "/api/workspaces/{ws}/analysis/{live}/resume", None),
    ("POST", "/api/workspaces/{ws}/analysis/{live}/feedback", {"text": "stop", "kind": "redirect"}),
    ("POST", "/api/workspaces/{ws}/analysis/{run}/findings/attest", None),
    ("GET", "/api/workspaces/{ws}/analysis/{run}/openlineage", None),
    ("POST", "/api/workspaces/{ws}/sources/{src}/discover", None),
    ("PUT", "/api/workspaces/{ws}/sources/{src}/selection", {"assets": ["x"]}),
    ("POST", "/api/workspaces/{ws}/sources/{src}/crawl", {}),
]
# A child of B addressed by id alone.
ID_ROUTES = [
    ("GET", "/api/agent-runs/{task}", None),
    ("GET", "/api/runs/{run}/context-receipts", None),
    ("PATCH", "/api/hypotheses/{hyp}", {"priority": "low"}),
    ("GET", "/api/insights/{ins}", None),
    ("GET", "/api/insights/{ins}/attested", None),
    ("POST", "/api/insights/{ins}/outcome", {"signal": "accept"}),
    ("GET", "/api/artifacts/{art}", None),
    ("GET", "/api/artifacts/{art}/download", None),
    ("POST", "/api/artifacts/{art}/publish", None),
    ("POST", "/api/artifacts/{art}/approve", None),
    ("GET", "/api/queries/{qry}", None),
    ("POST", "/api/approvals/{apr}/approve", None),
    ("POST", "/api/approvals/{apr}/reject", None),
    ("PATCH", "/api/monitors/{mon}", {"name": "renamed"}),
    ("GET", "/api/monitors/{mon}/series", None),
    ("POST", "/api/alerts/{alert}/acknowledge", None),
    ("POST", "/api/feedback/{fb}/correct", {"kind": "redirect"}),
    ("PATCH", "/api/schedules/{sch}", {"enabled": True}),
    ("DELETE", "/api/schedules/{sch}", None),
    ("GET", "/api/ask/threads/{thread}", None),
    ("PATCH", "/api/ask/threads/{thread}", {"title": "renamed"}),
    ("POST", "/api/ask/threads/{thread}/turns", {"question": "how many?"}),
    ("GET", "/api/ask/turns/{turn}/inspector", None),
    ("POST", "/api/ask/turns/{turn}/promote", {"target": "investigate"}),
]


def _call(api, method: str, template: str, body, headers, **names):
    return api.request(method, template.format(**names), headers=headers, json=body)


def _assert_denied(r, twin) -> None:
    # 404; or 403 when the caller's role in the path's own workspace is too low (decided before any lookup)
    assert r.status_code == 404 or (r.status_code == 403 and twin.status_code == 403), r.text
    # identical to an unknown id (existence never leaks), apart from the per-request id every envelope carries (P4-06)
    body, twin_body = r.json(), twin.json()
    assert body["error"].pop("request_id") and twin_body["error"].pop("request_id")
    assert body == twin_body, (r.text, twin.text)
    assert SECRET not in r.text and set(r.json()) == {"error"}


def _unknown(ids: dict) -> dict:
    return {k: f"{v[:4]}nope{v[8:]}" for k, v in ids.items()}


def test_dual_member_cannot_reach_a_b_child_through_an_a_path(api, world):
    ws, ids, dual = world["ws"], world["ids"], world["users"]["dual"]["h"]
    for method, template, body in WS_ROUTES:
        r = _call(api, method, template, body, dual, ws=ws["A"], **ids)
        twin = _call(api, method, template, body, dual, ws=ws["A"], **_unknown(ids))
        _assert_denied(r, twin)


def test_single_member_cannot_reach_a_b_child_by_either_path(api, world):
    ws, ids, single = world["ws"], world["ids"], world["users"]["single"]["h"]
    for method, template, body in WS_ROUTES:
        for path_ws in (ws["A"], ws["B"]):
            r = _call(api, method, template, body, single, ws=path_ws, **ids)
            twin = _call(api, method, template, body, single, ws=path_ws, **_unknown(ids))
            _assert_denied(r, twin)
    for method, template, body in ID_ROUTES:
        r = _call(api, method, template, body, single, **ids)
        twin = _call(api, method, template, body, single, **_unknown(ids))
        _assert_denied(r, twin)


def test_denied_calls_changed_nothing(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import Alert, AnalysisRun, Approval, AskThread, Hypothesis, Monitor, Schedule

    ids = world["ids"]
    with session_scope() as s:
        live = s.get(AnalysisRun, ids["live"])
        assert live.control == "run" and live.status == "RUNNING"
        assert s.get(Approval, ids["apr"]).status == "pending"
        assert s.get(Hypothesis, ids["hyp"]).priority != "low"
        assert s.get(Monitor, ids["mon"]).name == SECRET
        assert s.get(Alert, ids["alert"]).status == "open"
        sch = s.get(Schedule, ids["sch"])
        assert sch is not None and sch.enabled is False
        assert s.get(AskThread, ids["thread"]).title == SECRET


def test_the_owning_path_still_works_for_the_dual_member(api, world):
    ws, ids, dual = world["ws"], world["ids"], world["users"]["dual"]["h"]
    r = api.get(f"/api/workspaces/{ws['B']}/analysis/{ids['run']}", headers=dual)
    assert r.status_code == 200 and r.json()["id"] == ids["run"]
    assert api.get(f"/api/insights/{ids['ins']}", headers=dual).status_code == 200
    assert api.get(f"/api/ask/threads/{ids['thread']}", headers=dual).status_code == 200
    with api.stream("GET", f"/api/workspaces/{ws['B']}/analysis/{ids['run']}/events", headers=dual) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "event: run.status" in body and "event: end" in body


# ------------------------------------------------------------------------------------ SSE revocation
def _later(delay: float, fn) -> threading.Thread:
    from analystos.tenancy.background import carry_context  # the delayed write acts for the test's tenant

    def go():
        time.sleep(delay)
        fn()
    t = threading.Thread(target=carry_context(go), daemon=True)
    t.start()
    return t


def _stream(api, url: str, headers: dict) -> str:
    """The whole body of a stream that must end on its own (a revoked or expired one)."""
    with api.stream("GET", url, headers=headers) as r:
        assert r.status_code == 200, r.read()
        return "".join(r.iter_text())


def test_membership_removed_mid_stream_ends_it_with_revoked(api, world):
    from analystos.events.bus import emit

    ws, ids, users = world["ws"], world["ids"], world["users"]
    b, live = ws["B"], ids["live"]
    emit(b, "task.updated", {"n": "before"}, run_id=live)

    def revoke_then_emit():
        r = api.delete(f"/api/workspaces/{b}/members/{users['dual']['id']}", headers=world["owner"])
        assert r.status_code == 200, r.text
        emit(b, "task.updated", {"n": "after-revocation"}, run_id=live)

    worker = _later(1.0, revoke_then_emit)
    started = time.monotonic()
    body = _stream(api, f"/api/workspaces/{b}/analysis/{live}/events", users["dual"]["h"])
    worker.join()
    assert '"before"' in body
    assert body.rstrip().endswith('event: revoked\ndata: {"reason": "access_revoked"}')
    assert "after-revocation" not in body and body.count("event: revoked") == 1
    assert time.monotonic() - started < 15
    # and a reconnect is refused outright
    assert api.get(f"/api/workspaces/{b}/analysis/{live}/events", headers=users["dual"]["h"]).status_code == 404
    r = api.post(f"/api/workspaces/{b}/members", headers=world["owner"], json={"email": users["dual"]["email"], "role": "analyst"})
    assert r.status_code == 200, r.text


def test_token_expiry_mid_stream_ends_it_with_expired(api, world):
    import jwt

    from analystos.core.config import get_settings
    from analystos.events.bus import emit

    ws, ids, dual = world["ws"], world["ids"], world["users"]["dual"]
    now = int(time.time())
    token = jwt.encode({"sub": dual["id"], "email": dual["email"], "iat": now, "exp": now + 3, "iss": "analystos"},
                       get_settings().jwt_secret, algorithm="HS256")
    b, live = ws["B"], ids["live"]
    worker = _later(0.5, lambda: emit(b, "task.updated", {"n": "while-valid"}, run_id=live))
    started = time.monotonic()
    body = _stream(api, f"/api/workspaces/{b}/analysis/{live}/events", {"Authorization": f"Bearer {token}"})
    worker.join()
    assert "while-valid" in body
    assert body.rstrip().endswith('event: expired\ndata: {"reason": "token_expired"}')
    assert time.monotonic() - started < 10

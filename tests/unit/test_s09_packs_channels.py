"""S09-T4 recurring packs (auto-deliver after clean runs, review otherwise) and S09-T5 channels: Teams / Slack
cards and Graph email outbound, signed Slack / Teams / email inbound into the Request Desk. Fake HTTP transports
and a fake Desk submit only: no services, no real workspace credentials."""
from __future__ import annotations

import base64
import ipaddress
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, PolicyDenied, Unauthenticated
from analystos.core.ids import stable_hash
from analystos.db.base import session_scope
from analystos.db.models import Alert, Artifact, Delivery, Schedule, ScheduleRun, User, VerificationRecord
from analystos.governance.approvals import decide
from analystos.services import deliveries
from analystos.services import schedules as sched
from analystos.triggers import channels, packs
from analystos.triggers.models import ChannelMessage

SLACK_SECRET = "slack-signing-secret-0123"  # test values, set in the environment by the fixture
TEAMS_TOKEN = base64.b64encode(b"teams-outgoing-webhook-token-0123").decode()
EMAIL_SECRET = "email-relay-secret-01234"
TABLES = ("delivery_destination", "delivery", "trigger_rule", "trigger_event", "alert_group", "channel_binding",
          "channel_message")


def _public(host: str, port: int) -> list:
    return [ipaddress.ip_address("93.184.216.34")]


class Recorder:
    def __init__(self, status: int = 200):
        self.status, self.requests = status, []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})


@pytest.fixture
def env(world, sqlite_db, monkeypatch, tmp_path):  # noqa: F811
    from analystos.db import models
    from analystos.governance import approvals

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t] for t in TABLES])
    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone
    for name, value in {"ANALYSTOS_CHANNEL_SLACK": SLACK_SECRET, "ANALYSTOS_CHANNEL_TEAMS": TEAMS_TOKEN,
                        "ANALYSTOS_CHANNEL_MAIL": EMAIL_SECRET, "ANALYSTOS_WEBHOOK_SLACK_OPS": "https://hooks.slack.com/services/T/B/x",
                        "ANALYSTOS_WEBHOOK_EVIL": "https://evil.example.org/collect", "GRAPH_TEST_TOKEN": "graph-token-test"}.items():
        monkeypatch.setenv(name, value)
    s = get_settings()
    for key, value in {"outbound_private_hosts": "", "channel_slack_hosts": "hooks.slack.com", "artifact_dir": str(tmp_path),
                       "delivery_email_allowed_domains": "", "trigger_skew_seconds": 300}.items():
        monkeypatch.setattr(s, key, value)
    return SimpleNamespace(tmp=tmp_path)


def _user(s, uid: str) -> User:  # noqa: ANN001
    return s.get(User, uid)


def _approve_destination(dest_id: str) -> None:
    with session_scope() as s:
        dest = s.get(deliveries.DeliveryDestination, dest_id)
        approval = decide(s, dest.approval_id, _user(s, "usr_approver"), approve=True, reason="ok")
        deliveries.apply_decision(s, approval)


def _destination(kind: str, name: str, config: dict, kinds: list[str]) -> str:
    with session_scope() as s:
        dest_id = deliveries.create_destination(s, _user(s, "usr_owner"), WS, name=name, kind=kind, config=config,
                                                content_kinds=kinds).id
    _approve_destination(dest_id)
    return dest_id


# ------------------------------------------------------------------------------------ T4 recurring packs
def _pack_schedule(dest_id: str, n: int = 3) -> str:
    with session_scope() as s:
        return sched.create_schedule(s, _user(s, "usr_owner"), WS, name="weekly KPI brief", kind="pack", cron="0 8 * * 1",
                                     timezone="UTC", config={"template": "kpi_review", "output": {"kind": "email_brief"},
                                                             "deliver_to": [dest_id], "auto_deliver_after_n_clean_runs": n}).id


def _fire(schedule_id: str, i: int, *, void: bool = False) -> str:
    """One fire whose run completed with a report (what complete_from_run reports to _finish)."""
    content = {"kind": "executive", "title": f"KPI brief {i}", "report_data_hash": str(i) * 64, "insights": i, "metrics": 2,
               "alerts": 0, "files": {}}
    with session_scope() as s:
        srun = ScheduleRun(id=f"srun_{i}", schedule_id=schedule_id, workspace_id=WS, fire_key=f"{schedule_id}:{i}",
                           scheduled_for=datetime(2026, 1, 1) + timedelta(days=7 * i), trigger="cron", status="running",
                           result={"run_id": f"run_{i}"})
        s.add(srun)
        s.add(Artifact(id=f"art_{i}", workspace_id=WS, run_id=f"run_{i}", type="report", name="brief", version=1,
                       status="final", content=content, content_hash=stable_hash(content)))
        if void:
            s.add(VerificationRecord(id=f"vr_{i}", workspace_id=WS, run_id=f"run_{i}", subject_type="insight",
                                     subject_id=f"ins_{i}", verdict="verified", checks=[], verifier="rev.v1",
                                     fingerprint="f" * 64, dependencies=[], state="VOID", void_kind="data",
                                     void_reason="source changed"))
    sched._finish(f"srun_{i}", "succeeded", {"run_id": f"run_{i}", "report_artifact_id": f"art_{i}"})
    with session_scope() as s:
        return s.get(ScheduleRun, f"srun_{i}").result["pack"]


def _review(pack: dict, approve: bool) -> dict:
    with session_scope() as s:
        approval = decide(s, pack["approval_id"], _user(s, "usr_approver"), approve=approve, reason="checked")
        srun = packs.apply_review(s, approval)
        return dict(srun.result["pack"])


def _report_deliveries() -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(Delivery).where(Delivery.subject_type == "report"))))


def test_recurring_auto_deliver_after_clean_runs(env):
    dest = _destination("email", "leadership", {"recipients": ["cfo@example.com"]}, ["report"])
    schedule_id = _pack_schedule(dest)
    with session_scope() as s:
        cfg = s.get(Schedule, schedule_id).config
        assert cfg["version"] == "1.0.0" and cfg["output"] == {"kind": "email_brief"}  # the template is pinned

    first = _fire(schedule_id, 1)
    assert first["mode"] == "review" and first["reason"].startswith("first run") and _report_deliveries() == 0
    assert _review(first, approve=True) == {**first, "mode": "reviewed", "clean": True, "void": [],
                                            "deliveries": [_one_delivery("art_1")]}
    for i in (2, 3):  # streak 1 and 2 of 3: still reviewed
        pack = _fire(schedule_id, i)
        assert pack["mode"] == "review" and pack["streak"] == i - 1 and f"{i - 1} of 3" in pack["reason"]
        _review(pack, approve=True)
    fourth = _fire(schedule_id, 4)
    assert fourth["mode"] == "auto" and fourth["clean"] is True and fourth["streak"] == 3 and len(fourth["deliveries"]) == 1
    assert _report_deliveries() == 4

    void = _fire(schedule_id, 5, void=True)  # a VOID verification routes to review, whatever the streak
    assert void["mode"] == "review" and "VOID" in void["reason"] and void["void"] == ["vr_5"]
    assert _review(void, approve=False)["clean"] is False and _report_deliveries() == 4
    after = _fire(schedule_id, 6)
    assert after["mode"] == "review" and after["streak"] == 0 and not after["reason"].startswith("first run")


def _one_delivery(report_id: str) -> str:
    with session_scope() as s:
        return s.scalar(select(Delivery.id).where(Delivery.subject_id == report_id))


def test_review_is_bound_to_the_snapshot_and_destinations(env):
    dest = _destination("email", "leadership", {"recipients": ["cfo@example.com"]}, ["report"])
    schedule_id = _pack_schedule(dest)
    pack = _fire(schedule_id, 1)
    with session_scope() as s:  # the report changes after the review was requested
        art = s.get(Artifact, "art_1")
        art.content_hash = "0" * 64
    from analystos.core.errors import ApprovalRequired

    with pytest.raises(ApprovalRequired), session_scope() as s:
        approval = decide(s, pack["approval_id"], _user(s, "usr_approver"), approve=True)
        packs.apply_review(s, approval)
    assert _report_deliveries() == 0


def test_pack_schedule_validation(env):
    dest = _destination("teams", "ops", {"url_ref": "env:ANALYSTOS_WEBHOOK_SLACK_OPS"}, ["report", "alert"])
    with session_scope() as s:
        for bad in ({"template": "churn"}, {"template": "kpi_review", "output": {"kind": "fax"}},
                    {"template": "kpi_review", "auto_deliver_after_n_clean_runs": 0},
                    {"template": "kpi_review", "surprise": 1},
                    {"template": "kpi_review", "output": {"kind": "email_brief"}, "deliver_to": [dest]}):
            with pytest.raises(InvalidInput):
                sched.create_schedule(s, _user(s, "usr_owner"), WS, name="x", kind="pack", cron="0 8 * * 1", config=bad)
        ok = sched.create_schedule(s, _user(s, "usr_owner"), WS, name="cards", kind="pack", cron="0 8 * * 1",
                                   config={"template": "variance", "output": {"kind": "teams_card"}, "deliver_to": [dest]})
        assert ok.config["version"] == "1.0.0"


def test_pack_fire_starts_the_pinned_template(env, monkeypatch):
    from analystos.services import runs

    calls: list[dict] = []
    monkeypatch.setattr(runs, "create_run", lambda owner, ws, **kw: calls.append(kw) or SimpleNamespace(id="run_p"))
    dest = _destination("email", "leadership", {"recipients": ["cfo@example.com"]}, ["report"])
    schedule_id = _pack_schedule(dest)
    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        out = packs.execute(_user(s, "usr_owner"), WS, schedule_id, "srun_x", dict(sch.config))
    assert out["_pending"] and calls[0]["definition"] == {"key": "playbook.kpi_review", "version": "1.0.0", "source": "builtin"}
    assert calls[0]["origin"]["report"] == packs.OUTPUTS["email_brief"]["report"] and calls[0]["origin"]["pack"] == "kpi_review"


# ------------------------------------------------------------------------------------ T5 outbound
def _alert() -> str:
    with session_scope() as s:
        s.add(Alert(id="alr_c", workspace_id=WS, monitor_id=None, severity="critical", title="Payment failures up",
                    message="payment failure rate 4.6% vs 2.0% baseline (n=5000)", data={"rows": [[1, 2]]},
                    dedupe_key="k", status="open"))
    return "alr_c"


def test_slack_card_carries_aggregates_and_a_deep_link_only(env):
    dest = _destination("slack", "ops", {"url_ref": "env:ANALYSTOS_WEBHOOK_SLACK_OPS"}, ["alert"])
    alert_id = _alert()
    with session_scope() as s:
        d = deliveries.enqueue(s, WS, destination_ids=[dest], subject_type="alert", subject_id=alert_id)[0].id
    rec = Recorder()
    assert deliveries.attempt(d, transports={"slack": channels.ChannelTransport("slack", resolver=_public,
                                                                                http_transport=rec.transport)}) == "delivered"
    sent = json.loads(rec.requests[0].content)
    assert rec.requests[0].headers["host"] == "hooks.slack.com" and sent["text"].startswith("[critical] Payment failures up")
    assert "4.6%" in json.dumps(sent) and "rows" not in json.dumps(sent)  # the alert's data rows never leave
    assert sent["blocks"][-1]["elements"][0]["url"].endswith(f"/w/{WS}/alerts/{alert_id}")


def test_channel_url_must_be_a_platform_host(env):
    with session_scope() as s:  # the URL itself is a secret, so its host is checked when it is resolved (at send)
        dest = deliveries.create_destination(s, _user(s, "usr_owner"), WS, name="evil", kind="slack",
                                             config={"url_ref": "env:ANALYSTOS_WEBHOOK_EVIL"}, content_kinds=["alert"]).id
    _approve_destination(dest)
    with session_scope() as s:
        d = deliveries.enqueue(s, WS, destination_ids=[dest], subject_type="alert", subject_id=_alert())[0].id
    rec = Recorder()
    assert deliveries.attempt(d, transports={"slack": channels.ChannelTransport("slack", resolver=_public,
                                                                                http_transport=rec.transport)}) == "refused"
    assert not rec.requests
    with pytest.raises(InvalidInput), session_scope() as s:
        deliveries.create_destination(s, _user(s, "usr_owner"), WS, name="raw", kind="teams",
                                      config={"url": "https://x.webhook.office.com/a"}, content_kinds=["alert"])
    with pytest.raises(PolicyDenied):
        channels.resolve_channel_url("teams", "env:ANALYSTOS_WEBHOOK_SLACK_OPS")  # a Slack URL is not a Teams host


def test_graph_mail_transport_sends_through_microsoft_graph(env, monkeypatch):
    s = get_settings()
    for key, value in {"delivery_email_transport": "graph", "graph_base_url": "https://graph.example.com/v1.0",
                       "graph_sender": "analytics@example.com", "graph_token_ref": "env:GRAPH_TEST_TOKEN"}.items():
        monkeypatch.setattr(s, key, value)
    assert isinstance(deliveries.default_transports()["email"], channels.GraphMailTransport)
    dest = _destination("email", "ops mail", {"recipients": ["ops@example.com"]}, ["alert"])
    with session_scope() as sess:
        d = deliveries.enqueue(sess, WS, destination_ids=[dest], subject_type="alert", subject_id=_alert())[0].id
    rec = Recorder(status=202)
    assert deliveries.attempt(d, transports={"email": channels.GraphMailTransport(resolver=_public,
                                                                                  http_transport=rec.transport)}) == "delivered"
    req = rec.requests[0]
    assert req.url.path == "/v1.0/users/analytics@example.com/sendMail"
    assert req.headers["authorization"] == "Bearer graph-token-test"
    mail = json.loads(req.content)["message"]
    assert mail["toRecipients"] == [{"emailAddress": {"address": "ops@example.com"}}]


# ------------------------------------------------------------------------------------ T5 inbound
class FakeDesk:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, user, workspace_id, text, *, channel, process):  # noqa: ANN001
        self.calls.append({"user": user.id, "workspace_id": workspace_id, "text": text, "channel": channel,
                           "process": process})
        return {"id": f"rq_{len(self.calls)}"}


def _binding(kind: str, secret: str, **kw) -> str:  # noqa: ANN003
    with session_scope() as s:
        return channels.create_binding(s, _user(s, "usr_owner"), WS, kind=kind, name=f"{kind} intake",
                                       secret_ref=secret, **kw).id


def _slack(body: dict, *, ts: int | None = None, secret: str = SLACK_SECRET) -> tuple[dict, bytes]:
    raw = json.dumps(body).encode()
    ts = int(time.time()) if ts is None else ts
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": channels.slack_signature(secret, str(ts), raw)}, raw


def _slack_message(event_id: str, text: str, **extra) -> dict:  # noqa: ANN003
    return {"type": "event_callback", "team_id": "T123", "event_id": event_id,
            "event": {"type": "app_mention", "user": "U999", "text": text, "ts": "1.2", **extra}}


def test_slack_inbound_creates_a_desk_request_once(env):
    desk = FakeDesk()
    binding = _binding("slack", "env:ANALYSTOS_CHANNEL_SLACK", external_key="T123", default_requester_id="usr_analyst")
    challenge = channels.accept_inbound("slack", binding, *_slack({"type": "url_verification", "challenge": "abc"}))
    assert challenge.body == {"challenge": "abc"}
    out = channels.accept_inbound("slack", binding, *_slack(_slack_message("Ev1", "<@U0BOT> how many late orders last week?")),
                                  submit=desk)
    assert out.status_code == 200 and out.body["request_id"] == "rq_1"
    assert desk.calls == [{"user": "usr_analyst", "workspace_id": WS, "text": "how many late orders last week?",
                           "channel": "slack", "process": False}]
    again = channels.accept_inbound("slack", binding, *_slack(_slack_message("Ev1", "how many late orders?")), submit=desk)
    assert again.body == {"status": "duplicate"} and len(desk.calls) == 1
    bot = channels.accept_inbound("slack", binding, *_slack(_slack_message("Ev2", "a bot reply", bot_id="B1")), submit=desk)
    assert bot.body == {"status": "ignored"} and len(desk.calls) == 1
    for headers, raw in (_slack(_slack_message("Ev3", "hello there"), secret="wrong-secret-000000000"),
                         _slack(_slack_message("Ev4", "hello there"), ts=int(time.time()) - 600)):
        with pytest.raises(Unauthenticated):
            channels.accept_inbound("slack", binding, headers, raw, submit=desk)
    with pytest.raises(Unauthenticated):  # a message signed for this binding but from another Slack workspace
        channels.accept_inbound("slack", binding, *_slack({**_slack_message("Ev5", "hello there"), "team_id": "T999"}),
                                submit=desk)
    with pytest.raises(Unauthenticated):  # a Slack binding is not a Teams endpoint
        channels.accept_inbound("teams", binding, *_slack(_slack_message("Ev6", "hello there")), submit=desk)


def test_teams_outgoing_webhook_maps_the_sender_by_identity(env):
    desk = FakeDesk()
    with session_scope() as s:
        _user(s, "usr_viewer").attributes = {"teams_aad_object_id": "aad-viewer"}
    binding = _binding("teams", "env:ANALYSTOS_CHANNEL_TEAMS")
    body = json.dumps({"type": "message", "id": "act-1", "text": "<at>Analyst</at> revenue by region for Q3?",
                       "from": {"id": "29:x", "aadObjectId": "aad-viewer"}}).encode()
    out = channels.accept_inbound("teams", binding, {"Authorization": channels.teams_signature(TEAMS_TOKEN, body)}, body,
                                  submit=desk)
    assert out.body["type"] == "message" and "rq_1" in out.body["text"]
    assert desk.calls[0]["user"] == "usr_viewer" and desk.calls[0]["text"] == "revenue by region for Q3?"
    stranger = json.dumps({"type": "message", "id": "act-2", "text": "hello analysts", "from": {"aadObjectId": "aad-x"}}).encode()
    refused = channels.accept_inbound("teams", binding, {"Authorization": channels.teams_signature(TEAMS_TOKEN, stranger)},
                                      stranger, submit=desk)
    assert refused.body["text"].startswith("Not accepted") and len(desk.calls) == 1
    with session_scope() as s:
        rows = {m.external_id: m.status for m in s.scalars(select(ChannelMessage))}
        assert rows == {"act-1": "accepted", "act-2": "refused"}
    with pytest.raises(Unauthenticated):
        channels.accept_inbound("teams", binding, {"Authorization": "HMAC bm90LXZhbGlk"}, body, submit=desk)


def test_email_inbound_maps_the_sender_address(env):
    desk = FakeDesk()
    binding = _binding("email", "env:ANALYSTOS_CHANNEL_MAIL", external_key="analytics@example.com")
    doc = {"message_id": "<m1@example.com>", "from": "Owner <usr_owner@x>", "to": "analytics@example.com",
           "subject": "Churn question", "text": "Why did churn rise in September?"}
    raw = json.dumps(doc).encode()
    ts = str(int(time.time()))
    headers = {"X-AnalystOS-Timestamp": ts, "X-AnalystOS-Signature": deliveries.sign(EMAIL_SECRET, ts, raw)}
    out = channels.accept_inbound("email", binding, headers, raw, submit=desk)
    assert out.status_code == 202 and desk.calls[0]["user"] == "usr_owner"
    assert desk.calls[0]["text"] == "Churn question Why did churn rise in September?"


def test_bindings_need_a_namespaced_secret(env):
    with pytest.raises(InvalidInput), session_scope() as s:
        channels.create_binding(s, _user(s, "usr_owner"), WS, kind="slack", name="x", secret_ref="env:DATABASE_URL")
    assert channels.verify_teams("not base64!", b"{}", "HMAC x") is False

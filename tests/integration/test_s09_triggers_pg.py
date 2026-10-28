"""Spec S09 on Postgres with row-level security and strict tenancy: a signed webhook through the API (no user
session, no tenant bound) is enqueued as the rule's tenant, replays are recognised, the platform loop processes
it as its owner, the gate raises one alert into an alert group, the Teams send is refused until the rule is
approved and delivered after; the Kafka consumer enqueues under RLS; a signed Slack message creates a real
Request Desk request; another tenant sees none of these rows. Fake HTTP transports, no real workspace credentials."""
from __future__ import annotations

import contextvars
import ipaddress
import json
import os
import time

import httpx
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
SECRET = "pg-trigger-secret-0123456"  # test values only
SLACK_SECRET = "pg-slack-signing-secret-01"
CONDITION = {"enter": "observed >= baseline + 0.02 AND n >= 1000", "exit": "observed < baseline + 0.01"}
SCHEMA = {"type": "object", "required": ["observed", "baseline", "n"],
          "properties": {"observed": {"type": "number"}, "baseline": {"type": "number"}, "n": {"type": "integer"}}}


def _public(host: str, port: int) -> list:
    return [ipaddress.ip_address("93.184.216.34")]


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, User, Workspace, WorkspaceMember
    from analystos.runtime.context import default_router
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    env = {"ANALYSTOS_TRIGGER_PG": SECRET, "ANALYSTOS_WEBHOOK_PG_TEAMS": "https://teams.example.com/webhookb2/pg",
           "ANALYSTOS_CHANNEL_PG_SLACK": SLACK_SECRET}
    saved = {k: os.environ.get(k) for k in [*env, "OPENROUTER_API_KEY"]}
    os.environ.update(env)
    os.environ.pop("OPENROUTER_API_KEY", None)
    get_settings.cache_clear()
    default_router.cache_clear()
    s = get_settings()
    s.channel_teams_hosts, s.kafka_topic_acl = "teams.example.com", f"{DEFAULT_TENANT_ID}:payments.*"
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as sess:
        admin = sess.scalar(select(User).where(User.email == s.bootstrap_admin_email))
        approver = sess.scalar(select(User).where(User.email == "approver@analystos.local"))
        ws = Workspace(id=new_id("ws"), name="S09 triggers", objective="Keep payment failures low", created_by=admin.id,
                       settings={})
        sess.add(ws)
        sess.flush()
        sess.add_all([WorkspaceMember(workspace_id=ws.id, user_id=admin.id, role="owner"),
                      WorkspaceMember(workspace_id=ws.id, user_id=approver.id, role="approver")])
        ids = {"ws": ws.id, "admin": admin.id, "approver": approver.id}
    other = new_id("tnt")
    with system_scope(), session_scope() as sess:
        sess.add(Tenant(id=other, slug=f"s09-{other[-6:]}", name="S09 other tenant"))
    ids["other_tenant"] = other
    yield ids
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    default_router.cache_clear()


def _as(uid: str, s):  # noqa: ANN001, ANN202
    from analystos.db.models import User

    return s.get(User, uid)


def _approve(world, approval_id: str) -> None:
    from analystos.db.base import session_scope
    from analystos.governance.approvals import decide
    from analystos.services import deliveries
    from analystos.triggers import rules

    with session_scope() as s:
        approval = decide(s, approval_id, _as(world["approver"], s), approve=True, reason="ok")
        (rules.apply_decision if approval.action == rules.APPROVAL_ACTION else deliveries.apply_decision)(s, approval)


def _post_unbound(client, url: str, **kw):  # noqa: ANN001, ANN003, ANN202
    """An external caller: no session and no tenant bound in the request's context."""
    return contextvars.Context().run(client.post, url, **kw)


def _signed(payload: dict, event_id: str, secret: str = SECRET) -> tuple[dict, bytes]:
    from analystos.services.deliveries import sign

    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    return {"X-AnalystOS-Timestamp": ts, "X-AnalystOS-Signature": sign(secret, ts, body), "X-Event-Id": event_id,
            "Content-Type": "application/json"}, body


def test_webhook_to_teams_card_under_rls(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.base import session_scope
    from analystos.db.models import Alert, Delivery, Monitor
    from analystos.services import deliveries
    from analystos.services import monitors as mon
    from analystos.tenancy import system_scope, tenant_scope
    from analystos.triggers import processor, rules
    from analystos.triggers.channels import ChannelTransport
    from analystos.triggers.models import AlertGroup, TriggerEvent

    with session_scope() as s:
        dest = deliveries.create_destination(s, _as(world["admin"], s), world["ws"], name="payments-ops", kind="teams",
                                             config={"url_ref": "env:ANALYSTOS_WEBHOOK_PG_TEAMS"}, content_kinds=["alert"])
        dest_approval = dest.approval_id
    _approve(world, dest_approval)
    with session_scope() as s:
        monitor = mon.create_monitor(s, _as(world["admin"], s), world["ws"], name="payment failures", kind="metric_threshold",
                                     config={"metric": "payment_failure_rate", "op": ">", "value": 0.02, "condition": CONDITION,
                                             "scope": {"source": "payments_ro", "filters": {"region": "US"}},
                                             "notify": {"channel": "teams:payments-ops"}, "correlation_key": ["metric"]})
        monitor_id, rule_approval = monitor.id, monitor.approval_id
        rule_id = rules.create_rule(s, _as(world["admin"], s), world["ws"], monitor_id=monitor_id, name="pay hook",
                                    kind="webhook", action="observe", secret_ref="env:ANALYSTOS_TRIGGER_PG",
                                    payload_schema=SCHEMA).id
    client = TestClient(app)
    reading = {"observed": 0.06, "baseline": 0.02, "n": 5000}
    first = _post_unbound(client, f"/v2/triggers/webhook/{rule_id}", content=_signed(reading, "pg-1")[1],
                          headers=_signed(reading, "pg-1")[0])
    assert first.status_code == 202, first.text
    headers, body = _signed(reading, "pg-1")
    replay = _post_unbound(client, f"/v2/triggers/webhook/{rule_id}", content=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
    bad_headers, bad_body = _signed(reading, "pg-2", secret="not-the-rule-secret-0000")
    assert _post_unbound(client, f"/v2/triggers/webhook/{rule_id}", content=bad_body, headers=bad_headers).status_code == 401
    assert _post_unbound(client, "/v2/triggers/webhook/trg_nope", content=body, headers=headers).status_code == 401

    contextvars.Context().run(processor.process_due)  # the platform loop, as the scheduler runs it (no tenant bound)
    with session_scope() as s:
        events = list(s.scalars(select(TriggerEvent).where(TriggerEvent.rule_id == rule_id)))
        assert [e.status for e in events] == ["processed"]
        alert = s.scalar(select(Alert).where(Alert.monitor_id == monitor_id))
        assert alert is not None and alert.group_id and s.get(AlertGroup, alert.group_id).alert_count == 1
        assert s.get(Monitor, monitor_id).noise_state["phase"] == "alerting"
        delivery_id = s.scalar(select(Delivery.id).where(Delivery.subject_id == alert.id))
    sent: list[httpx.Request] = []
    teams = {"teams": ChannelTransport("teams", resolver=_public,
                                       http_transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))}
    assert contextvars.Context().run(deliveries.attempt, delivery_id, transports=teams) == "refused"  # rule pending
    _approve(world, rule_approval)
    with session_scope() as s:
        deliveries.redrive(s, _as(world["admin"], s), delivery_id)
    assert contextvars.Context().run(deliveries.attempt, delivery_id, transports=teams) == "delivered"
    assert len(sent) == 1 and json.loads(sent[0].content)["attachments"][0]["content"]["type"] == "AdaptiveCard"

    with tenant_scope(world["other_tenant"]), session_scope() as s:  # RLS: another tenant sees none of it
        assert s.scalar(select(TriggerEvent.id)) is None and s.scalar(select(AlertGroup.id)) is None
        assert s.get(Monitor, monitor_id) is None
    with system_scope(), session_scope() as s:
        tenants = {r[0] for r in s.execute(select(TriggerEvent.__table__.c.tenant_id).where(TriggerEvent.rule_id == rule_id))}
        assert tenants == {_tenant_of(s, world["ws"])}  # filled from the workspace by the trigger, not the caller


def _tenant_of(s, workspace_id: str) -> str:  # noqa: ANN001
    from analystos.db.models import Workspace

    return s.get(Workspace, workspace_id).tenant_id


def test_kafka_consumer_enqueues_as_the_rule_tenant(world):
    from analystos.db.base import session_scope
    from analystos.services import monitors as mon
    from analystos.triggers import kafka, rules
    from analystos.triggers.models import TriggerEvent

    with session_scope() as s:
        monitor_id = mon.create_monitor(s, _as(world["admin"], s), world["ws"], name="kafka failures", kind="metric_threshold",
                                        config={"metric": "payment_failure_rate_k", "op": ">", "value": 0.03,
                                                "condition": CONDITION}).id
        rule_id = rules.create_rule(s, _as(world["admin"], s), world["ws"], monitor_id=monitor_id, name="pay topic",
                                    kind="kafka", action="observe", topic="payments.failures", rule_set="pg",
                                    payload_schema=SCHEMA, transform={"observed": "/r", "baseline": "/b", "n": "/n",
                                                                      "event_id": "/id"}).id

    class Fake:
        def __init__(self):
            self.commits, self.topics = [], []
            self.batch = [kafka.Record("payments.failures", 0, 7, json.dumps({"id": "k-1", "r": 0.07, "b": 0.02,
                                                                              "n": 3000}).encode())]

        def subscribe(self, topics):  # noqa: ANN001
            self.topics = topics

        def poll(self, timeout):  # noqa: ANN001
            out, self.batch = self.batch, []
            return out

        def commit(self, records):  # noqa: ANN001
            self.commits.append(len(records))

        def close(self):
            pass

    fake = Fake()
    consumer = kafka.TriggerConsumer("pg", fake)

    def run():  # noqa: ANN202
        consumer.load_rules()
        return consumer.run_once()

    results = contextvars.Context().run(run)  # its own process: no tenant bound
    assert fake.topics == ["payments.failures"] and [r["status"] for r in results] == ["queued"] and fake.commits == [1]
    with session_scope() as s:
        ev = s.scalar(select(TriggerEvent).where(TriggerEvent.rule_id == rule_id))
        assert ev.event_id == "k-1" and ev.payload == {"observed": 0.07, "baseline": 0.02, "n": 3000}


def test_signed_slack_message_creates_a_desk_request(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.base import session_scope
    from analystos.desk.models import Request
    from analystos.tenancy import tenant_scope
    from analystos.triggers import channels
    from analystos.triggers.models import ChannelMessage

    with session_scope() as s:
        binding = channels.create_binding(s, _as(world["admin"], s), world["ws"], kind="slack", name="ops slack",
                                          secret_ref="env:ANALYSTOS_CHANNEL_PG_SLACK", external_key="TPG",
                                          default_requester_id=world["admin"]).id
    doc = {"type": "event_callback", "team_id": "TPG", "event_id": "EvPG1",
           "event": {"type": "app_mention", "user": "U1", "text": "<@UBOT> what was the payment failure rate last week?"}}
    raw = json.dumps(doc).encode()
    ts = str(int(time.time()))
    headers = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": channels.slack_signature(SLACK_SECRET, ts, raw),
               "Content-Type": "application/json"}
    client = TestClient(app)
    res = _post_unbound(client, f"/v2/channels/slack/{binding}", content=raw, headers=headers)
    assert res.status_code == 200, res.text
    request_id = res.json()["request_id"]
    with session_scope() as s:
        req = s.get(Request, request_id)
        assert req.channel == "slack" and req.requester_id == world["admin"] and req.workspace_id == world["ws"]
        assert req.text == "what was the payment failure rate last week?"
        assert s.scalar(select(ChannelMessage.request_id).where(ChannelMessage.binding_id == binding)) == request_id
    again = _post_unbound(client, f"/v2/channels/slack/{binding}", content=raw, headers=headers)
    assert again.json() == {"status": "duplicate"}
    with tenant_scope(world["other_tenant"]), session_scope() as s:
        assert s.get(Request, request_id) is None and s.scalar(select(ChannelMessage.id)) is None

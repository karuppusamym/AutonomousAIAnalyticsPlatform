"""S09 on the in-memory control plane, with fakes only (no services, no real workspace credentials):

* T1 signed webhook ingress: HMAC over timestamp.body, the five-minute skew, the replay cache, typed payloads,
  202 only after the event row exists; the processor hands the event to the monitor's gate.
* T2 Kafka: a fake consumer; topic ACL; transform -> typed payload; commit only after enqueue.
* T3 through the monitor: hysteresis, quiet-hours deferral and release, correlation into one alert group.
* Rule approval: `test_outbound_requires_approval`, re-approval when the scope changes, rule -> pinned goal.
"""
from __future__ import annotations

import ipaddress
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.core.config import get_settings
from analystos.core.errors import ApprovalRequired, InvalidInput, PolicyDenied, Unauthenticated
from analystos.db.base import session_scope
from analystos.db.models import Alert, Approval, AuditEvent, Delivery, DeliveryDestination, Monitor, Notification, User
from analystos.governance.approvals import decide
from analystos.services import deliveries
from analystos.services import monitors as mon
from analystos.triggers import ingress, kafka, processor, rules
from analystos.triggers.channels import ChannelTransport
from analystos.triggers.models import AlertGroup, TriggerEvent, TriggerRule

SECRET = "trigger-test-secret-0123"  # test values, set in the environment by the fixture
TEAMS_URL = "https://teams.example.com/webhookb2/test"
SPEC_CONDITION = {"enter": "observed >= baseline + 0.02 AND n >= 1000", "exit": "observed < baseline + 0.01"}
SCHEMA = {"type": "object", "required": ["observed", "baseline", "n"],
          "properties": {"observed": {"type": "number"}, "baseline": {"type": "number"}, "n": {"type": "integer"},
                         "processor": {"type": "string"}, "period": {"type": "string"}}}
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
        return httpx.Response(self.status, json={"ok": True})


@pytest.fixture
def env(world, sqlite_db, monkeypatch):  # noqa: F811
    from analystos.db import models
    from analystos.governance import approvals
    from analystos.services import platform_settings

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t] for t in TABLES])
    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone
    monkeypatch.setenv("ANALYSTOS_TRIGGER_PAY", SECRET)
    monkeypatch.setenv("ANALYSTOS_WEBHOOK_TEAMS_OPS", TEAMS_URL)
    s = get_settings()
    for key, value in {"outbound_private_hosts": "", "channel_teams_hosts": "teams.example.com",
                       "kafka_topic_acl": "*:payments.*", "trigger_skew_seconds": 300}.items():
        monkeypatch.setattr(s, key, value)
    monkeypatch.setattr(mon, "_triage", lambda monitor, workspace, result: (result.get("severity", "warning"), None))
    monkeypatch.setattr(platform_settings, "get", lambda: SimpleNamespace(
        monitors=SimpleNamespace(auto_investigation_enabled=False, triage_escalate_probability=0.8)))
    rec = Recorder()
    return SimpleNamespace(recorder=rec, transports={"teams": ChannelTransport("teams", resolver=_public,
                                                                               http_transport=rec.transport)})


def _user(s, uid: str) -> User:  # noqa: ANN001
    return s.get(User, uid)


def _monitor(name: str = "payment failures US", **extra) -> str:  # noqa: ANN003
    config = {"metric": "payment_failure_rate", "op": ">", "value": 0.02, "condition": SPEC_CONDITION,
              "scope": {"source": "payments_ro", "filters": {"region": "US"}}, **extra}
    with session_scope() as s:
        return mon.create_monitor(s, _user(s, "usr_owner"), WS, name=name, kind="metric_threshold", config=config).id


def _webhook_rule(monitor_id: str, action: str = "observe") -> str:
    with session_scope() as s:
        return rules.create_rule(s, _user(s, "usr_owner"), WS, monitor_id=monitor_id, name=f"hook-{monitor_id}",
                                 kind="webhook", action=action, secret_ref="env:ANALYSTOS_TRIGGER_PAY",
                                 payload_schema=SCHEMA).id


def _signed(payload: dict, event_id: str, *, ts: int | None = None, secret: str = SECRET) -> tuple[dict, bytes]:
    body = json.dumps(payload).encode()
    ts = int(time.time()) if ts is None else ts
    return ({"X-AnalystOS-Timestamp": str(ts), "X-AnalystOS-Signature": deliveries.sign(secret, str(ts), body),
             "X-Event-Id": event_id, "Content-Type": "application/json"}, body)


def _reading(observed: float, period: str, processor_name: str = "adyen") -> dict:
    return {"observed": observed, "baseline": 0.02, "n": 5000, "processor": processor_name, "period": period}


def _alerts(monitor_id: str | None = None) -> list[Alert]:
    with session_scope() as s:
        stmt = select(Alert).order_by(Alert.created_at)
        return list(s.scalars(stmt.where(Alert.monitor_id == monitor_id) if monitor_id else stmt))


def _alert_notifications() -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(Notification).where(Notification.kind == "alert"))))


def _approve(approval_id: str):  # noqa: ANN202
    with session_scope() as s:
        approval = decide(s, approval_id, _user(s, "usr_approver"), approve=True, reason="ok")
        if approval.action == rules.APPROVAL_ACTION:
            rules.apply_decision(s, approval)
        else:
            deliveries.apply_decision(s, approval)


def _teams_destination(name: str = "payments-ops") -> str:
    with session_scope() as s:
        dest = deliveries.create_destination(s, _user(s, "usr_owner"), WS, name=name, kind="teams",
                                             config={"url_ref": "env:ANALYSTOS_WEBHOOK_TEAMS_OPS"}, content_kinds=["alert"])
        dest_id, approval_id = dest.id, dest.approval_id
    _approve(approval_id)
    return dest_id


# ------------------------------------------------------------------------------------ T1 ingress
def test_signed_webhook_is_enqueued_once_and_replays_are_recognised(env):
    rule_id = _webhook_rule(_monitor())
    headers, body = _signed(_reading(0.05, "2026-10-27T08"), "evt-1")
    first = ingress.accept_webhook(rule_id, headers, body)
    assert first.status_code == 202 and first.body()["status"] == "accepted"
    with session_scope() as s:
        row = s.get(TriggerEvent, first.trigger_event_id)
        assert row.status == "queued" and row.payload["observed"] == 0.05  # durable before the 202
        assert (row.expires_at.replace(tzinfo=UTC) - row.received_at.replace(tzinfo=UTC)) >= timedelta(days=7) - timedelta(seconds=5)
    again = ingress.accept_webhook(rule_id, *_signed(_reading(0.05, "2026-10-27T08"), "evt-1"))
    assert again.status_code == 200 and again.duplicate and again.trigger_event_id == first.trigger_event_id
    with session_scope() as s:
        assert len(list(s.scalars(select(TriggerEvent)))) == 1


def test_unsigned_stale_or_unknown_requests_get_the_same_401(env):
    rule_id = _webhook_rule(_monitor())
    payload = _reading(0.05, "p")
    for headers, body in (_signed(payload, "e1", secret="another-secret-0123456"),
                          _signed(payload, "e2", ts=int(time.time()) - 301),
                          ({"X-Event-Id": "e3"}, json.dumps(payload).encode())):
        with pytest.raises(Unauthenticated):
            ingress.accept_webhook(rule_id, headers, body)
    with pytest.raises(Unauthenticated):
        ingress.accept_webhook("trg_unknown", *_signed(payload, "e4"))
    with session_scope() as s:
        refused = list(s.scalars(select(AuditEvent).where(AuditEvent.action == "trigger.auth_failed")))
        assert len(refused) == 4 and not list(s.scalars(select(TriggerEvent)))


def test_payload_must_match_the_rules_schema_and_carry_an_event_id(env):
    rule_id = _webhook_rule(_monitor())
    with pytest.raises(InvalidInput, match="schema"):
        ingress.accept_webhook(rule_id, *_signed({"observed": "high", "baseline": 0.02, "n": 10}, "e1"))
    headers, body = _signed(_reading(0.05, "p"), "")
    with pytest.raises(InvalidInput, match="X-Event-Id"):
        ingress.accept_webhook(rule_id, headers, body)
    with pytest.raises(InvalidInput, match="secret_ref"), session_scope() as s:
        rules.create_rule(s, _user(s, "usr_owner"), WS, monitor_id=_monitor("m2"), name="bad", kind="webhook",
                          secret_ref="env:DATABASE_PASSWORD")


def test_processed_events_run_the_monitor_gate_with_hysteresis(env):
    monitor_id = _monitor()
    rule_id = _webhook_rule(monitor_id)
    readings = [0.020, 0.045, 0.035, 0.041, 0.031, 0.025, 0.046]
    for i, r in enumerate(readings):
        ingress.accept_webhook(rule_id, *_signed(_reading(r, f"2026-10-27T{i:02d}"), f"evt-{i}"))
        processor.process_due()
    alerts = _alerts(monitor_id)
    assert len(alerts) == 2  # enter at 0.045, hold through the oscillation, exit at 0.025, re-enter at 0.046
    assert [a.status for a in alerts] == ["resolved", "open"]
    with session_scope() as s:
        statuses = [e.status for e in s.scalars(select(TriggerEvent).order_by(TriggerEvent.received_at))]
        assert statuses and all(st == "processed" for st in statuses)
        assert s.get(Monitor, monitor_id).noise_state["phase"] == "alerting"


# ------------------------------------------------------------------------------------ T3 through the monitor
def test_quiet_hours_defer_then_release(env, monkeypatch):
    monitor_id = _monitor(quiet_hours={"window": "22:00-07:00", "tz": "America/New_York"})
    night = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)  # 23:00 in New York
    monkeypatch.setattr(mon, "utcnow", lambda: night)
    out = mon.evaluate_monitor(monitor_id, observation=_reading(0.06, "2026-10-27T23"))
    alert = _alerts(monitor_id)[0]
    assert out["alert"] and alert.deferred_until is not None and alert.notified_at is None
    assert _alert_notifications() == 0  # recorded, not announced
    assert mon.release_deferred(now=night + timedelta(hours=1)) == 0  # still night
    morning = datetime(2026, 10, 28, 11, 5, tzinfo=UTC)
    monkeypatch.setattr(mon, "utcnow", lambda: morning)
    assert mon.release_deferred(now=morning) == 1
    assert _alert_notifications() == 1 and _alerts(monitor_id)[0].notified_at is not None
    assert mon.release_deferred(now=morning) == 0  # announced once


def test_correlated_alerts_merge_into_one_alert_group(env):
    a = _monitor("failures US (card)", correlation_key=["metric", "processor"])
    b = _monitor("failures US (wallet)", correlation_key=["metric", "processor"], value=0.03)
    mon.evaluate_monitor(a, observation=_reading(0.06, "p1"))
    mon.evaluate_monitor(b, observation=_reading(0.07, "p1"))
    with session_scope() as s:
        groups = list(s.scalars(select(AlertGroup)))
        assert len(groups) == 1 and groups[0].alert_count == 2 and set(groups[0].monitor_ids) == {a, b}
        assert groups[0].key_values == {"metric": "payment_failure_rate", "processor": "adyen"}
    assert _alert_notifications() == 1  # the alert group is announced once
    c = _monitor("failures US (other processor)", correlation_key=["metric", "processor"], value=0.04)
    mon.evaluate_monitor(c, observation=_reading(0.08, "p1", processor_name="stripe"))
    with session_scope() as s:
        assert len(list(s.scalars(select(AlertGroup)))) == 2
    for mid in (a, b):  # both recover: the alert group resolves
        mon.evaluate_monitor(mid, observation=_reading(0.021, "p2"))
    with session_scope() as s:
        statuses = {i.key_values["processor"]: i.status for i in s.scalars(select(AlertGroup))}
        assert statuses == {"adyen": "resolved", "stripe": "open"}


def test_caps_suppress_beyond_the_daily_limit(env):
    ids = [_monitor(f"m{i}", caps={"per_dataset_per_day": 2}, value=0.01 + i / 1000) for i in range(3)]
    for mid in ids:
        mon.evaluate_monitor(mid, observation=_reading(0.06, "p1"))
    assert len(_alerts()) == 2
    with session_scope() as s:
        assert s.get(Monitor, ids[2]).noise_state.get("suppressed") == 1


# ------------------------------------------------------------------------------------ rule approval
def test_outbound_requires_approval(env):
    dest_id = _teams_destination()
    monitor_id = _monitor(notify={"channel": "teams:payments-ops"})
    with session_scope() as s:
        m = s.get(Monitor, monitor_id)
        first_approval = m.approval_id
        approval = s.get(Approval, first_approval)
        assert approval.status == "pending" and approval.action == rules.APPROVAL_ACTION
        assert approval.payload["recipients"][0]["id"] == dest_id and approval.payload["scope"]["filters"] == {"region": "US"}
    mon.evaluate_monitor(monitor_id, observation=_reading(0.06, "p1"))
    with session_scope() as s:
        d = s.scalar(select(Delivery))
    assert deliveries.attempt(d.id, transports=env.transports) == "refused"  # the rule is not approved yet
    with session_scope() as s:
        assert "pending" in s.get(Delivery, d.id).last_error and not env.recorder.requests

    _approve(first_approval)
    with session_scope() as s:
        deliveries.redrive(s, _user(s, "usr_owner"), d.id)
    assert deliveries.attempt(d.id, transports=env.transports) == "delivered"
    card = json.loads(env.recorder.requests[0].content)
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard"
    assert content["actions"][0]["url"].endswith(f"/w/{WS}/alerts/{d.subject_id}")  # a deep link, no rows
    assert "rows" not in json.dumps(card)

    # Changing the scope needs a new approval: the old one is invalidated, sends are refused until approved.
    with session_scope() as s:
        m = s.get(Monitor, monitor_id)
        m.config = {**m.config, "scope": {"source": "payments_ro", "filters": {"region": "EU"}}}
        rules.after_config_change(s, _user(s, "usr_owner"), m)
        assert m.approval_id != first_approval and s.get(Approval, first_approval).status == "invalidated"
    mon.evaluate_monitor(monitor_id, observation=_reading(0.021, "p2"))  # exit
    mon.evaluate_monitor(monitor_id, observation=_reading(0.07, "p3"))  # re-enter: a new alert
    with session_scope() as s:
        d2 = s.scalar(select(Delivery).where(Delivery.id != d.id))
    assert deliveries.attempt(d2.id, transports=env.transports) == "refused"
    assert len(env.recorder.requests) == 1


def test_a_destination_outside_the_rule_is_refused_and_noise_edits_need_no_reapproval(env):
    _teams_destination()
    other = _teams_destination("finance")
    monitor_id = _monitor(notify={"channel": "teams:payments-ops"})
    with session_scope() as s:
        m = s.get(Monitor, monitor_id)
        _approve(m.approval_id)
    with session_scope() as s:
        m = s.get(Monitor, monitor_id)
        before = m.approval_id
        m.config = {**m.config, "cooldown": "2h", "caps": {"per_tenant_per_day": 50}}
        assert rules.after_config_change(s, _user(s, "usr_owner"), m) is None and m.approval_id == before
    mon.evaluate_monitor(monitor_id, observation=_reading(0.06, "p1"))
    with session_scope() as s:
        alert_id = s.scalar(select(Alert.id))
        bad = deliveries.enqueue(s, WS, destination_ids=[other], subject_type="alert", subject_id=alert_id,
                                 origin={"monitor_id": monitor_id})[0].id
    assert deliveries.attempt(bad, transports=env.transports) == "refused"
    with session_scope() as s:
        assert "not a recipient" in s.get(Delivery, bad).last_error


def test_rule_starts_its_pinned_playbook_only_when_approved(env, monkeypatch):
    from analystos.services import runs

    started: list[dict] = []
    monkeypatch.setattr(runs, "start_run_request", lambda owner, ws, **kw: (started.append(kw) or SimpleNamespace(id="run_t"), False))
    with pytest.raises(InvalidInput, match="pin a version"):
        _monitor("unpinned", on_trigger={"playbook": "playbook.investigate"})
    monitor_id = _monitor(on_trigger={"playbook": "playbook.investigate@1.0.0", "limits": {"max_cost_usd": 3, "max_steps": 12}})
    mon.evaluate_monitor(monitor_id, observation=_reading(0.06, "p1"))
    alert = _alerts(monitor_id)[0]
    assert "approval is pending" in alert.data["goal"]["refused"] and not started
    with session_scope() as s:
        _approve(s.get(Monitor, monitor_id).approval_id)
    mon.evaluate_monitor(monitor_id, observation=_reading(0.021, "p2"))
    mon.evaluate_monitor(monitor_id, observation=_reading(0.06, "p3"))
    assert started and started[0]["definition"] == {"key": "playbook.investigate", "version": "1.0.0", "source": "builtin"}
    assert started[0]["limits"] == {"max_cost_usd": 3, "max_steps": 12}
    assert started[0]["origin"]["scope"] == {"source": "payments_ro", "filters": {"region": "US"}}
    assert _alerts(monitor_id)[-1].investigation_run_id == "run_t"


def test_verify_rule_refuses_without_an_approval(env):
    monitor_id = _monitor()
    with session_scope() as s, pytest.raises(ApprovalRequired):
        rules.verify_rule(s, s.get(Monitor, monitor_id))


# ------------------------------------------------------------------------------------ T2 Kafka
class FakeConsumer:
    def __init__(self, batches: list[list[kafka.Record]]):
        self.batches, self.subscribed, self.commits, self.closed = list(batches), [], [], False

    def subscribe(self, topics):  # noqa: ANN001
        self.subscribed = list(topics)

    def poll(self, timeout):  # noqa: ANN001
        return self.batches.pop(0) if self.batches else []

    def commit(self, records):  # noqa: ANN001
        self.commits.append([r.offset for r in records])

    def close(self):
        self.closed = True


def _kafka_rule(monitor_id: str, topic: str = "payments.failures") -> str:
    with session_scope() as s:
        return rules.create_rule(s, _user(s, "usr_owner"), WS, monitor_id=monitor_id, name=f"kafka-{topic}", kind="kafka",
                                 action="observe", topic=topic, payload_schema=SCHEMA,
                                 transform={"observed": "/metrics/rate", "baseline": "/metrics/baseline",
                                            "n": "/metrics/n", "processor": "/processor", "event_id": "/id"}).id


def _record(offset: int, doc: dict, topic: str = "payments.failures") -> kafka.Record:
    return kafka.Record(topic, 0, offset, json.dumps(doc).encode())


def _doc(i: str, rate) -> dict:  # noqa: ANN001
    return {"id": i, "processor": "adyen", "metrics": {"rate": rate, "baseline": 0.02, "n": 4000}}


def test_kafka_consumer_commits_after_enqueue_and_rejects_bad_records(env):
    monitor_id = _monitor()
    rule_id = _kafka_rule(monitor_id)
    with pytest.raises(PolicyDenied), session_scope() as s:
        rules.create_rule(s, _user(s, "usr_owner"), WS, monitor_id=monitor_id, name="secret", kind="kafka", topic="hr.salaries")
    fake = FakeConsumer([[_record(10, _doc("k1", 0.06)), _record(11, _doc("k2", "high")), _record(12, _doc("k1", 0.06))]])
    consumer = kafka.TriggerConsumer("default", fake)
    assert consumer.load_rules() == ["payments.failures"] and fake.subscribed == ["payments.failures"]
    results = consumer.run_once()
    assert [r["status"] for r in results] == ["queued", "rejected", "duplicate"]
    assert fake.commits == [[10, 11, 12]]  # one commit, after the whole batch was enqueued or recorded
    with session_scope() as s:
        events = {e.event_id: e for e in s.scalars(select(TriggerEvent).where(TriggerEvent.rule_id == rule_id))}
        assert events["k1"].status == "queued" and events["k1"].payload == {"observed": 0.06, "baseline": 0.02,
                                                                             "n": 4000, "processor": "adyen"}
        assert events["k2"].status == "failed" and "schema" in events["k2"].error
    processor.process_due()
    assert len(_alerts(monitor_id)) == 1


def test_kafka_batch_is_not_committed_when_enqueue_fails(env, monkeypatch):
    _kafka_rule(_monitor())
    fake = FakeConsumer([[_record(1, _doc("x1", 0.06))]])
    consumer = kafka.TriggerConsumer("default", fake)
    consumer.load_rules()

    def broken(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ingress, "enqueue", broken)
    with pytest.raises(RuntimeError):
        consumer.run_once()
    assert fake.commits == []  # redelivered later; the replay cache drops the copy if it did land


def test_kafka_topic_acl_is_per_tenant_and_rechecked_at_load(env, monkeypatch):
    assert kafka.topic_allowed("t1", "payments.x", "t1:payments.*") and not kafka.topic_allowed("t2", "payments.x", "t1:payments.*")
    assert not kafka.topic_allowed("t1", "payments.x", "")
    _kafka_rule(_monitor())
    monkeypatch.setattr(get_settings(), "kafka_topic_acl", "tenant_other:*")
    fake = FakeConsumer([])
    assert kafka.TriggerConsumer("default", fake).load_rules() == [] and fake.subscribed == []
    with session_scope() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "trigger.topic_denied")) is not None
    assert kafka.pointer({"a": [{"b~/c": 1}]}, "/a/0/b~0~1c") == 1 and kafka.group_id("default") == "analystos-triggers-default"


def test_disabled_rule_events_are_suppressed(env):
    monitor_id = _monitor()
    rule_id = _webhook_rule(monitor_id)
    ingress.accept_webhook(rule_id, *_signed(_reading(0.06, "p1"), "e1"))
    with session_scope() as s:
        rules.set_enabled(s, _user(s, "usr_owner"), s.get(TriggerRule, rule_id), False)
    assert processor.process_due() == {"suppressed": 1}
    assert not _alerts(monitor_id)
    with pytest.raises(Unauthenticated):  # a disabled rule accepts nothing new
        ingress.accept_webhook(rule_id, *_signed(_reading(0.06, "p1"), "e2"))


def test_replay_cache_is_purged_after_its_window(env):
    rule_id = _webhook_rule(_monitor())
    ingress.accept_webhook(rule_id, *_signed(_reading(0.06, "p1"), "e1"))
    processor.process_due()
    assert processor.purge_expired(now=datetime.now(UTC) + timedelta(days=6)) == 0
    assert processor.purge_expired(now=datetime.now(UTC) + timedelta(days=8)) == 1
    with session_scope() as s:
        assert s.scalar(select(DeliveryDestination.id)) is None and not list(s.scalars(select(TriggerEvent)))

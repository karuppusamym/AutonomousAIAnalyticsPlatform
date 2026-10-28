"""Kafka trigger consumer (spec S09-T2, compose profile `events`).

One consumer group per rule set (`analystos-triggers-<rule_set>`) reads the topics its enabled Kafka rules
name, provided the rule's tenant may read that topic (`ANALYSTOS_KAFKA_TOPIC_ACL`: comma-separated
`<tenant id or *>:<topic glob>` entries; empty means no topic is allowed). Each record is transformed
(`rule.transform`: payload field -> JSON pointer into the record; `event_id` pointer, else
`<topic>:<partition>:<offset>`), checked against the rule's typed payload schema and durably enqueued as a
`trigger_event` — the same queue and replay cache as the webhook ingress. Offsets are committed only after
every record of the batch is enqueued (or recorded as rejected), so a crash redelivers rather than loses, and
the replay cache drops the redelivered copy.

The broker client is injected: `confluent_kafka` in production (optional extra), a fake in tests.
"""
from __future__ import annotations

import fnmatch
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select

from analystos.core.errors import AnalystOSError, FeatureUnavailable
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.governance.audit import audit
from analystos.tenancy import system_scope
from analystos.tenancy.background import as_owner_of, platform_loop
from analystos.triggers.models import TriggerEvent, TriggerRule

log = get_logger(__name__)


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    value: bytes
    key: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Consumer(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...
    def poll(self, timeout: float) -> list[Record]: ...
    def commit(self, records: list[Record]) -> None: ...
    def close(self) -> None: ...


def _settings():  # noqa: ANN202
    from analystos.core.config import get_settings

    return get_settings()


def acl_entries(raw: str | None = None) -> list[tuple[str, str]]:
    out = []
    for item in (raw if raw is not None else _settings().kafka_topic_acl or "").split(","):
        tenant, sep, pattern = item.strip().partition(":")
        if sep and tenant.strip() and pattern.strip():
            out.append((tenant.strip(), pattern.strip()))
    return out


def topic_allowed(tenant_id: str | None, topic: str, raw: str | None = None) -> bool:
    """Topic ACL: a tenant reads only topics an operator granted it (or granted every tenant with `*`)."""
    return any((tenant == "*" or tenant == tenant_id) and fnmatch.fnmatchcase(topic, pattern)
               for tenant, pattern in acl_entries(raw))


def group_id(rule_set: str) -> str:
    return f"analystos-triggers-{rule_set}"


def pointer(doc: Any, path: str) -> Any:
    """RFC 6901 JSON pointer; None when the path does not exist."""
    if path in ("", "/"):
        return doc
    cur = doc
    for raw in path.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def transform(rule_transform: dict[str, str], doc: Any) -> dict[str, Any]:
    """The typed payload: mapped fields only when the rule maps any, else the record's object as is."""
    fields = {k: v for k, v in (rule_transform or {}).items() if k != "event_id"}
    if not fields:
        return doc if isinstance(doc, dict) else {}
    return {k: pointer(doc, path) for k, path in fields.items() if pointer(doc, path) is not None}


@dataclass
class _Rule:
    id: str
    workspace_id: str
    tenant_id: str | None
    topic: str
    transform: dict[str, str]


class TriggerConsumer:
    """Consume one rule set: `run_once()` polls a batch, enqueues it and then commits it."""

    def __init__(self, rule_set: str, consumer: Consumer, *, poll_timeout: float = 1.0):
        self.rule_set, self.consumer, self.poll_timeout = rule_set, consumer, poll_timeout
        self.rules: dict[str, list[_Rule]] = {}
        self.denied: set[str] = set()

    @platform_loop
    def load_rules(self) -> list[str]:
        """Enabled Kafka rules of this rule set across tenants, minus topics outside the tenant's ACL."""
        from analystos.db.models import Workspace

        rules: dict[str, list[_Rule]] = {}
        with session_scope() as s:
            rows = s.execute(select(TriggerRule, Workspace.tenant_id).join(Workspace, Workspace.id == TriggerRule.workspace_id)
                             .where(TriggerRule.kind == "kafka", TriggerRule.enabled.is_(True),
                                    TriggerRule.rule_set == self.rule_set)).all()
            for rule, tenant_id in rows:
                if not topic_allowed(tenant_id, rule.topic or ""):
                    if rule.id not in self.denied:
                        self.denied.add(rule.id)
                        audit("system", "trigger.topic_denied", workspace_id=rule.workspace_id, target=rule.id,
                              decision="deny", reasons=[f"topic {rule.topic} is not in the tenant's ACL"], session=s)
                    continue
                rules.setdefault(rule.topic, []).append(_Rule(rule.id, rule.workspace_id, tenant_id, rule.topic,
                                                              dict(rule.transform or {})))
        self.rules = rules
        topics = sorted(rules)
        if topics:
            self.consumer.subscribe(topics)
        return topics

    def handle(self, record: Record) -> list[dict[str, Any]]:
        """Enqueue `record` for every rule on its topic. A record that is not valid for a rule is recorded as a
        failed trigger event (visible, not retried); a database failure raises so the batch is not committed."""
        out = []
        try:
            doc = json.loads(record.value or b"null")
        except ValueError:
            doc = None
        for rule in self.rules.get(record.topic, []):
            if not topic_allowed(rule.tenant_id, record.topic):  # the ACL may have shrunk since load_rules
                continue
            event_id = pointer(doc, rule.transform["event_id"]) if rule.transform.get("event_id") else None
            event_id = str(event_id) if event_id not in (None, "") else \
                (pointer(doc, "/event_id") or f"{record.topic}:{record.partition}:{record.offset}")
            out.append(self._enqueue(rule, str(event_id), doc, record))
        return out

    def _enqueue(self, rule: _Rule, event_id: str, doc: Any, record: Record) -> dict[str, Any]:
        from analystos.triggers.ingress import enqueue
        from analystos.triggers.rules import validate_payload

        with as_owner_of("trigger_rule", rule.id), session_scope() as s:
            row = s.get(TriggerRule, rule.id)
            if row is None or not row.enabled:
                return {"rule_id": rule.id, "status": "skipped"}
            try:
                payload = validate_payload(row, transform(rule.transform, doc))
            except AnalystOSError as exc:
                return self._reject(s, row, event_id, exc.message, record)
            ev, duplicate = enqueue(s, row, event_id=event_id, payload=payload, source="kafka")
            return {"rule_id": rule.id, "trigger_event_id": ev.id, "status": "duplicate" if duplicate else "queued"}

    def _reject(self, s, row: TriggerRule, event_id: str, reason: str, record: Record) -> dict[str, Any]:  # noqa: ANN001
        from datetime import timedelta

        if s.scalar(select(TriggerEvent.id).where(TriggerEvent.rule_id == row.id, TriggerEvent.event_id == event_id)):
            return {"rule_id": row.id, "status": "duplicate"}
        now = utcnow()
        meta = {"topic": record.topic, "partition": record.partition, "offset": record.offset}
        s.add(TriggerEvent(id=new_id("tev"), workspace_id=row.workspace_id, rule_id=row.id, event_id=event_id[:200],
                           source="kafka", payload={}, payload_hash=stable_hash(meta), status="failed", attempts=0,
                           result={"record": meta}, error=f"rejected: {reason}"[:2000], received_at=now, processed_at=now,
                           expires_at=now + timedelta(days=int(_settings().trigger_replay_days))))
        audit(f"trigger:{row.id}", "trigger.rejected", workspace_id=row.workspace_id, decision="deny", reasons=[reason],
              details=meta, session=s)
        return {"rule_id": row.id, "status": "rejected", "reason": reason}

    def run_once(self) -> list[dict[str, Any]]:
        records = self.consumer.poll(self.poll_timeout)
        results: list[dict[str, Any]] = []
        for record in records:
            results.extend(self.handle(record))  # raises on a database failure: nothing is committed
        if records:
            self.consumer.commit(records)  # commit after enqueue
        return results

    def run_forever(self, stop: threading.Event, *, reload_every: int = 60) -> None:
        polls = 0
        try:
            while not stop.is_set():
                if polls % reload_every == 0:
                    self.load_rules()
                polls += 1
                try:
                    self.run_once()
                except Exception:
                    log.exception("trigger consumer batch failed; it will be redelivered")
                    stop.wait(2.0)
        finally:
            self.consumer.close()


# ------------------------------------------------------------------------------------ production client
class ConfluentConsumer:
    """Adapter over `confluent_kafka.Consumer` (manual commits, earliest offset for a new group)."""

    def __init__(self, rule_set: str, factory: Callable[[dict[str, Any]], Any] | None = None):
        settings = _settings()
        if not settings.kafka_bootstrap_servers:
            raise FeatureUnavailable("the Kafka trigger consumer is not configured",
                                     details={"remedy": "set ANALYSTOS_KAFKA_BOOTSTRAP_SERVERS (compose profile `events`)"})
        if factory is None:
            try:
                from confluent_kafka import Consumer as _Consumer
            except ImportError:
                raise FeatureUnavailable("the Kafka client is not installed",
                                         details={"remedy": 'pip install "analystos[kafka]"'}) from None
            factory = _Consumer
        self._c = factory({"bootstrap.servers": settings.kafka_bootstrap_servers, "group.id": group_id(rule_set),
                           "enable.auto.commit": False, "auto.offset.reset": "earliest"})

    def subscribe(self, topics: list[str]) -> None:
        self._c.subscribe(topics)

    def poll(self, timeout: float) -> list[Record]:
        out = []
        for msg in self._c.consume(num_messages=100, timeout=timeout) or []:
            if msg.error():
                log.warning("kafka error: %s", msg.error())
                continue
            out.append(Record(msg.topic(), msg.partition(), msg.offset(), msg.value() or b"", msg.key(),
                              {k: (v.decode() if isinstance(v, bytes) else str(v)) for k, v in (msg.headers() or [])}))
        return out

    def commit(self, records: Iterable[Record]) -> None:
        from confluent_kafka import TopicPartition

        last: dict[tuple[str, int], int] = {}
        for r in records:
            last[(r.topic, r.partition)] = max(last.get((r.topic, r.partition), -1), r.offset)
        self._c.commit(offsets=[TopicPartition(t, p, o + 1) for (t, p), o in last.items()], asynchronous=False)

    def close(self) -> None:
        self._c.close()


def main(argv: list[str]) -> int:
    """`analystos triggers-consumer [--rule-set NAME]`: consume until interrupted."""
    import argparse

    parser = argparse.ArgumentParser(prog="analystos triggers-consumer")
    parser.add_argument("--rule-set", default="default")
    args = parser.parse_args(argv)
    stop = threading.Event()
    with system_scope():
        consumer = TriggerConsumer(args.rule_set, ConfluentConsumer(args.rule_set))
    try:
        consumer.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0

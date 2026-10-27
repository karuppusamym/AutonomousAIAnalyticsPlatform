"""S01-T5: the sealed form of an audit event is deterministic, so a chain verifies the same way on any
host, session time zone or key order, and any change to a sealed field changes the hash."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from analystos.db.models import AuditEvent
from analystos.governance.audit_chain import GENESIS, PLATFORM_CHAIN, chain_key, event_body, link_hash


def _event(**kw) -> AuditEvent:  # noqa: ANN003
    base = {"id": 7, "workspace_id": "ws_1", "run_id": None, "actor": "user:u", "action": "a.b", "target": "t",
            "decision": "allow", "reasons": ["r"], "details": {"b": 1, "a": [1.5, None]},
            "created_at": datetime(2026, 10, 27, 12, 0, tzinfo=UTC)}
    return AuditEvent(**{**base, **kw})


def test_hash_ignores_session_time_zone_and_key_order():
    utc = event_body(_event(), "tnt_a")
    ist = event_body(_event(created_at=datetime(2026, 10, 27, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))), "tnt_a")
    reordered = event_body(_event(details={"a": [1.5, None], "b": 1}), "tnt_a")
    assert link_hash(GENESIS, utc) == link_hash(GENESIS, ist) == link_hash(GENESIS, reordered)


def test_every_sealed_field_and_the_previous_link_change_the_hash():
    base = link_hash(GENESIS, event_body(_event(), "tnt_a"))
    variants = [_event(decision="deny"), _event(actor="user:x"), _event(details={"b": 2, "a": [1.5, None]}),
                _event(reasons=[]), _event(id=8), _event(created_at=datetime(2026, 10, 27, 12, 0, 1, tzinfo=UTC))]
    assert all(link_hash(GENESIS, event_body(v, "tnt_a")) != base for v in variants)
    assert link_hash(GENESIS, event_body(_event(), "tnt_b")) != base
    assert link_hash("1" * 64, event_body(_event(), "tnt_a")) != base


def test_platform_rows_have_their_own_chain():
    assert chain_key(None) == PLATFORM_CHAIN and chain_key("tnt_a") == "tnt_a"

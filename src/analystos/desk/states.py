"""Request lifecycle (spec S13 §2-§3). Pure: no database, so the rules are unit-tested as a table.

new → triaged → auto_answering → auto_answered | auto_with_review | in_investigation | escalated
  → answered → accepted | reopened | declined
"""
from __future__ import annotations

from datetime import datetime, timedelta

from analystos.core.errors import Conflict

STATES = ("new", "triaged", "auto_answering", "auto_answered", "auto_with_review", "in_investigation", "escalated",
          "answered", "accepted", "reopened", "declined")
CLASSES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9")
PRIORITIES = ("low", "normal", "high", "urgent")
CHANNELS = ("ui", "api", "mcp", "teams", "slack", "email")

TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"triaged", "declined"}),  # declined: linked as a duplicate at intake
    "triaged": frozenset({"auto_answering", "in_investigation", "escalated", "declined"}),
    "auto_answering": frozenset({"auto_answered", "auto_with_review", "escalated"}),
    "auto_answered": frozenset({"accepted", "reopened"}),
    "auto_with_review": frozenset({"answered", "escalated", "declined"}),  # the analyst approves/edits, or takes it over
    "in_investigation": frozenset({"auto_with_review", "escalated"}),
    "escalated": frozenset({"answered", "declined"}),
    "answered": frozenset({"accepted", "reopened"}),
    "reopened": frozenset({"escalated", "answered", "declined"}),
    "accepted": frozenset(),
    "declined": frozenset(),
}
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
OPEN = frozenset(STATES) - TERMINAL
# What the analyst queue shows: work waiting on a person.
QUEUE_STATES = frozenset({"escalated", "auto_with_review", "reopened"})
# A requester may see an answer in these states.
ANSWERED_STATES = frozenset({"auto_answered", "answered", "accepted"})

# Hours to the SLA by priority (business hours are a follow-up; the clock is wall time).
SLA_HOURS = {"urgent": 2, "high": 8, "normal": 24, "low": 72}


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())


def check_transition(src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise Conflict(f"a request cannot move from {src} to {dst}", details={"from": src, "to": dst})


def sla_due(created_at: datetime, priority: str, hours: dict[str, float] | None = None) -> datetime:
    table = {**SLA_HOURS, **(hours or {})}
    return created_at + timedelta(hours=float(table.get(priority, SLA_HOURS["normal"])))


def resolution_for(state: str, via: str | None) -> str | None:
    """How a closed request was resolved (FR-053): `auto` when the answer the requester accepted came from the
    auto path (with or without an analyst's review), `human` when an analyst wrote it."""
    if state == "declined":
        return "declined"
    if state != "accepted":
        return None
    return "auto" if via in ("auto", "auto_with_review") else "human"

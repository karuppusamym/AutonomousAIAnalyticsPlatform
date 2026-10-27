"""R4/R5 proposals (spec S13 §3.3): a recurring report becomes a draft schedule, a "tell me when" a draft monitor.

Drafts only. The desk never creates a schedule or a monitor: both are side effects that need the analyst role and the
platform's approval flow (CLAUDE.md rule 6). The draft goes into the escalated request's data pack; the analyst
completes it and submits it through the existing schedules / monitors API. Deterministic: no model reads the text.
"""
from __future__ import annotations

import re
from typing import Any

_I = re.IGNORECASE
_DOW = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5}
_EVERY_DOW = re.compile(r"\b(every|each|on) (monday|tuesday|wednesday|thursday|friday)s?\b", _I)
_CADENCES = [
    (re.compile(r"\b(quarterly|every quarter|each quarter)\b", _I), "0 9 1 1,4,7,10 *", "the first day of each quarter"),
    (re.compile(r"\b(month-end|month end)\b", _I), "0 9 1 * *", "the first day of each month (the month just ended)"),
    (re.compile(r"\b(monthly|every month|each month)\b", _I), "0 9 1 * *", "the first day of each month"),
    (re.compile(r"\b(weekly|every week|each week)\b", _I), "0 9 * * 1", "every Monday"),
    (re.compile(r"\b(daily|every day|each day|every morning|each morning)\b", _I), "0 9 * * *", "every day"),
]
_ABOVE = re.compile(r"\b(above|over|exceeds?|greater than|more than|higher than|rises? above|goes above)\s*"
                    r"(?:baseline\s*\+\s*)?(\d+(?:\.\d+)?)\s*(%|pp|percent)?", _I)
_BELOW = re.compile(r"\b(below|under|less than|lower than|drops? below|falls? below|goes below)\s*"
                    r"(?:baseline\s*-\s*)?(\d+(?:\.\d+)?)\s*(%|pp|percent)?", _I)
_BASELINE = re.compile(r"\bbaseline\b", _I)


def _name(text: str) -> str:
    return " ".join(text.split())[:80]


def schedule_draft(text: str) -> dict[str, Any]:
    m = _EVERY_DOW.search(text)
    if m:
        cron, when = f"0 9 * * {_DOW[m.group(2).lower()]}", f"every {m.group(2).capitalize()}"
    else:
        cron, when = next(((c, w) for rx, c, w in _CADENCES if rx.search(text)), ("0 9 * * 1", "every Monday (assumed)"))
    return {"kind": "schedule", "endpoint": "POST /api/workspaces/{workspace_id}/schedules",
            "body": {"name": _name(text), "kind": "report", "cron": cron, "timezone": "UTC",
                     "config": {"objective": text}},
            "summary": f"A report {when} at 09:00 UTC.", "needs": ["the report's content (a saved analysis or dashboard)"],
            "approval": "Creating it needs the analyst role; its first run and any external delivery need an approval."}


def monitor_draft(text: str) -> dict[str, Any]:
    above, below = _ABOVE.search(text), _BELOW.search(text)
    hit, op = (above, ">") if above else ((below, "<") if below else (None, None))
    drift = bool(_BASELINE.search(text))
    config: dict[str, Any] = {"objective": text}
    needs = ["the metric the monitor watches"]
    if hit:
        config |= {"op": op, "value": float(hit.group(2))}
        if hit.group(3):
            config["unit"] = "pp" if hit.group(3).lower() == "pp" else "%"
    else:
        needs.append("the threshold")
    kind = "metric_drift" if drift else "metric_threshold"
    summary = (f"Alert when the metric moves {'above' if op == '>' else 'below'} "
               f"{'its baseline by ' if drift else ''}{config['value']:g}{config.get('unit', '')}." if hit
               else "Alert on a threshold the analyst sets.")
    return {"kind": "monitor", "endpoint": "POST /api/workspaces/{workspace_id}/monitors",
            "body": {"name": _name(text), "kind": kind, "config": config, "auto_investigate": False},
            "summary": summary, "needs": needs,
            "approval": "Creating it needs the analyst role; notifications outside the platform need an approval."}


def propose(request_class: str | None, text: str) -> dict[str, Any] | None:
    if request_class == "R4":
        return schedule_draft(text)
    if request_class == "R5":
        return monitor_draft(text)
    return None

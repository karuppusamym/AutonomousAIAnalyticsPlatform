"""Number check (S07-T8): is a number someone quotes (another BI copilot, a spreadsheet, a slide, an agent) what this
workspace's governed path computes?

The question goes through Ask under the caller's own scope (verified query, certified metric, or generated SQL through
the gateway), so a check adds no new way to produce a number. The claimed value is data: it never reaches a model, so it
cannot anchor the answer, and the comparison is code with the numbers guard's rounding rule (a claim agrees when it is
within half a unit of its last written digit; "12.5%" matches 0.125 or 12.5).

Verdicts: ``confirmed`` / ``contradicted``, each with the authority of the computed side (``certified`` when a verified
query or certified metric answered, else ``ad_hoc``), or ``unverifiable`` (Ask did not answer, or its result does not
single out one number: name the row with ``label`` or the column with ``column``). The Ask turn is the evidence: its
SQL, query receipt, result hash, tables and freshness.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from analystos.core.errors import InvalidInput
from analystos.skills import selfcheck

VERSION = "number_check.v1"
VERDICTS = ("confirmed", "contradicted", "unverifiable")
GOVERNED_BY = ("registry", "semantic")
MAX_QUESTION, MAX_SOURCE, MAX_TOLERANCE = 1000, 120, 0.5
_CLAIM = re.compile(r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*(%?)\s*$")


@dataclass(frozen=True)
class Claim:
    text: str
    value: float
    decimals: int
    percent: bool


@dataclass(frozen=True)
class Pick:
    value: float | None
    column: str | None = None
    row_label: str | None = None
    reason: str | None = None


def parse_claim(claimed: Any) -> Claim:
    """One written number: ``1204``, ``1,204.5``, ``-3.2``, ``12.5%``. Its written precision sets the tolerance."""
    if isinstance(claimed, bool) or claimed is None:
        raise InvalidInput("claimed must be one number, e.g. 1204, 1,204.5 or 12.5%")
    text = str(claimed).strip()
    m = _CLAIM.match(text)
    if not m:
        raise InvalidInput("claimed must be one number, e.g. 1204, 1,204.5 or 12.5%")
    digits = m.group(1).replace(",", "")
    value = float(digits)
    if not math.isfinite(value):
        raise InvalidInput("claimed must be a finite number")
    decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
    return Claim(text, value, decimals, bool(m.group(2)))


def _text(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().casefold()


def pick_value(columns: Sequence[str], rows: Sequence[Sequence[Any]], *, label: str | None = None,
               column: str | None = None, truncated: bool = False) -> Pick:
    """The one number of an answer the claim is about: a scalar, the row named by ``label`` (any cell equal to it),
    and the numeric column named by ``column`` when there is more than one."""
    columns, rows = list(columns or []), [list(r) for r in rows or []]
    if not rows:
        return Pick(None, reason="the answer returned no rows")
    nums = selfcheck.numeric_columns(columns, rows)
    if column is not None:
        want_col = column.strip().casefold()
        idx = next((i for i, c in enumerate(columns) if str(c).casefold() == want_col), None)
        if idx is None:
            return Pick(None, reason=f"the answer has no column {column!r}; its columns are {', '.join(map(str, columns))}")
        if idx not in nums:
            return Pick(None, reason=f"column {columns[idx]!r} is not numeric")
        nums = [idx]
    row_label = None
    if label is not None:
        want = label.strip().casefold()
        hits = [(r, {i for i, v in enumerate(r) if v is not None and _text(v) == want}) for r in rows]
        hits = [(r, idx) for r, idx in hits if idx]
        if not hits:
            more = " among the returned rows (the result was truncated)" if truncated else ""
            return Pick(None, reason=f"no row is labelled {label!r}{more}")
        if len(hits) > 1:
            return Pick(None, reason=f"{len(hits)} rows are labelled {label!r}: ask for one row")
        row, matched = hits[0]
        rows, nums, row_label = [row], [i for i in nums if i not in matched], str(row[min(matched)])
    if not nums:
        return Pick(None, reason="the answer has no numeric column")
    if len(rows) != 1:
        return Pick(None, reason=f"the answer has {len(rows)} rows: name the row the number is about with `label`")
    if len(nums) != 1:
        names = ", ".join(str(columns[i]) for i in nums)
        return Pick(None, reason=f"the answer has {len(nums)} numeric columns ({names}): name one with `column`")
    value = rows[0][nums[0]] if nums[0] < len(rows[0]) else None
    if value is None:
        return Pick(None, column=str(columns[nums[0]]), row_label=row_label, reason="the computed value is empty")
    return Pick(float(value), column=str(columns[nums[0]]), row_label=row_label)


def compare(claim: Claim, computed: float, tolerance: float = 0.0) -> dict[str, Any]:
    """Does the claim equal the computed value as written? A percent is compared to the value and to 100× it (the
    closer one); ``tolerance`` (relative) only widens the rounding rule, for a claim stated as approximate."""
    scales = (1.0, 100.0) if claim.percent else (1.0,)
    diff, scale = min((abs(claim.value - computed * k), k) for k in scales)
    target = computed * scale
    allowed = max(0.5 * 10 ** (-claim.decimals), tolerance * abs(target))
    signed = claim.value - target
    return {"agrees": diff <= allowed + 1e-12 * max(1.0, abs(target)), "scale": scale, "compared_to": target,
            "difference": signed, "relative": (signed / abs(target)) if target else None, "allowed": allowed}


def evaluate(turn: Mapping[str, Any], claim: Claim, *, label: str | None = None, column: str | None = None,
             tolerance: float = 0.0) -> dict[str, Any]:
    """The verdict for an Ask turn (as `services.ask.turn_out` returns it) against the claim. Pure."""
    result = turn.get("result") or {}
    prov = turn.get("provenance") or {}
    governed = turn.get("answered_by") in GOVERNED_BY or turn.get("governance") == "governed"
    semantic = prov.get("semantic") or {}
    evidence = {"turn_id": turn.get("id"), "thread_id": turn.get("thread_id"), "sql": turn.get("sql"),
                "query_id": result.get("query_id"), "result_hash": result.get("result_hash"),
                "answered_by": turn.get("answered_by"), "governance": "governed" if governed else "ad_hoc",
                "verified_query": (turn.get("verified_query") or {}).get("name") if turn.get("verified_query") else None,
                "semantic_model_version": semantic.get("semantic_model_version", semantic.get("model_version")),
                "assets": [a.get("asset") for a in prov.get("assets") or [] if a.get("asset")],
                "staleness": turn.get("staleness"), "row_count": result.get("row_count"),
                "truncated": bool(result.get("truncated")), "cost": turn.get("cost")}
    out: dict[str, Any] = {"version": VERSION, "claimed": {"text": claim.text, "value": claim.value,
                                                           "percent": claim.percent},
                           "label": label, "column": column, "tolerance": tolerance, "evidence": evidence}
    if turn.get("status") != "answered":
        refusal = turn.get("refusal") or {}
        return {**out, "verdict": "unverifiable", "authority": None, "computed": None, "comparison": None,
                "reason": f"Ask did not answer: {refusal.get('title') or turn.get('status')}"
                          + (f". {refusal['message']}" if refusal.get("message") else ""),
                "remedy": refusal.get("remedy") or "Rephrase the question, or send it to the Request Desk."}
    pick = pick_value(result.get("columns") or [], result.get("rows") or [], label=label, column=column,
                      truncated=bool(result.get("truncated")))
    authority = "certified" if governed else "ad_hoc"
    if pick.value is None:
        return {**out, "verdict": "unverifiable", "authority": authority, "computed": None, "comparison": None,
                "reason": pick.reason, "remedy": "Name the row with `label` or the column with `column`, or ask for "
                                                 "exactly the one number."}
    cmp = compare(claim, pick.value, tolerance)
    verdict = "confirmed" if cmp["agrees"] else "contradicted"
    note = None
    if authority == "ad_hoc":
        note = ("Computed from generated SQL, not a certified definition: a difference may be a definition difference. "
                "Certify the metric, or send the question to the Request Desk.")
    return {**out, "verdict": verdict, "authority": authority,
            "computed": {"value": pick.value, "column": pick.column, "row_label": pick.row_label},
            "comparison": cmp, "reason": None if verdict == "confirmed" else
            f"claimed {claim.text}, computed {cmp['compared_to']:g}", "note": note}


# ------------------------------------------------------------------------------------------- service
def check(user: Any, workspace_id: str, question: str, claimed: Any, *, label: str | None = None,
          column: str | None = None, tolerance: float = 0.0, source: str | None = None,
          channel: str = "api") -> dict[str, Any]:
    """Run the check: a new Ask thread for the question (under the caller's scope), the verdict, an audit row."""
    from analystos.db.base import session_scope
    from analystos.governance.audit import audit
    from analystos.services import ask as ask_svc

    claim = parse_claim(claimed)
    question = " ".join((question or "").split())
    if not 3 <= len(question) <= MAX_QUESTION:
        raise InvalidInput(f"say what the number is (3 to {MAX_QUESTION} characters)")
    if not 0.0 <= float(tolerance) <= MAX_TOLERANCE:
        raise InvalidInput(f"tolerance is a relative share between 0 and {MAX_TOLERANCE}")
    source = (" ".join((source or "").split()) or None)
    if source and len(source) > MAX_SOURCE:
        raise InvalidInput(f"source is at most {MAX_SOURCE} characters")
    label = (label or "").strip() or None
    column = (column or "").strip() or None
    with session_scope() as s:
        thread = ask_svc.create_thread(s, s.merge(user), workspace_id, title=f"Number check: {question}"[:300],
                                       channel=channel)
    turn = ask_svc.ask_in_thread(user, thread["id"], question)
    out = {**evaluate(turn, claim, label=label, column=column, tolerance=float(tolerance)),
           "question": question, "source": source, "workspace_id": workspace_id}
    computed = out.get("computed") or {}
    audit(f"user:{user.id}", "number_check.completed", workspace_id=workspace_id, target=turn.get("id"),
          decision=out["verdict"], reasons=[out["reason"]] if out.get("reason") else [],
          details={"claimed": claim.text, "computed": computed.get("value"), "authority": out.get("authority"),
                   "source": source, "label": label, "column": column, "channel": channel,
                   "query_id": out["evidence"].get("query_id"), "result_hash": out["evidence"].get("result_hash")})
    return out

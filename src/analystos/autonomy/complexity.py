"""Complexity router (S06 §3.2, v1 §2.2): a goal -> `low | medium | complex` -> playbook + default limits.

Rules first, on deterministic features of the goal and its authorized scope: table count, join depth
(validated relationships between the selected tables), estimated scan (catalog row counts), ambiguity
flags (wording, and the inputs the candidate playbook requires but the question does not name), the
policy tier (masked or sensitive columns in scope, publication) and the requested artifacts.

Then the decision rung: `clarify_needed` (authority `escalate_only`) through the DecisionService.
A "clarify" answer can only add caution: it raises the policy floor to `medium` and makes the run
supervised (plan approval before any step). It can never lower a tier or remove a gate.

Users may raise the tier (`GoalIn.tier`), and may ask for a lower one than the rules chose, but
never below the floor: the policy tier and the decision rung's escalation.
"""
from __future__ import annotations

import re
from typing import Any

from analystos.contracts.goals import TIERS, ComplexityDecision, ComplexityFeatures, tier_max
from analystos.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_PLAYBOOK = "playbook.investigate"
# Default limits per tier; a playbook's `default_limits` override them, the client can only tighten.
TIER_LIMITS: dict[str, dict[str, Any]] = {
    "low": {"max_steps": 15, "max_duration_s": 900, "max_cost_usd": 0.25, "max_replans": 1, "max_retries": 4,
            "max_scan_bytes": 1 << 30},
    "medium": {"max_steps": 40, "max_duration_s": 2700, "max_cost_usd": 1.0, "max_replans": 2, "max_retries": 8,
               "max_scan_bytes": 10 << 30},
    "complex": {"max_steps": 80, "max_duration_s": 7200, "max_cost_usd": 2.5, "max_replans": 3, "max_retries": 16,
                "max_scan_bytes": 100 << 30},
}
LARGE_SCAN_ROWS, HUGE_SCAN_ROWS = 1_000_000, 10_000_000

_OPEN_ENDED = re.compile(r"\b(anything|insights?|interesting|look into|explore|tell me about|what'?s going on|"
                         r"something)\b", re.I)
_MEASURE = re.compile(r"\b(count|number|rate|ratio|share|percent(age)?|average|avg|mean|median|total|sum|revenue|"
                      r"cost|time|duration|volume|breach(es)?|sla|conversion|retention|churn|kpis?|variance|"
                      r"growth|backlog|margin|sales|orders?|incidents?|tickets?|quality|nulls?|duplicates?)\b", re.I)
_PERIOD = re.compile(r"\b(day|week|month|quarter|year|daily|weekly|monthly|quarterly|annual|ytd|mtd|qtd|"
                     r"since|last|prior|previous|period|20\d\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*",
                     re.I)
_EVENT = re.compile(r"\b(signups?|sign[- ]ups?|orders?|purchases?|visits?|sessions?|steps?|events?|checkouts?|"
                    r"registrations?|logins?|opened|created|resolved|closed)\b", re.I)
_REFERENCE = re.compile(r"^\s*(it|this|that|these|those|they)\b", re.I)
_DETECTORS = {"metric": _MEASURE, "period": _PERIOD, "event": _EVENT}


def ambiguity_flags(question: str) -> list[str]:
    flags = []
    words = re.findall(r"\w+", question)
    if len(words) < 6:
        flags.append("short_question")
    if _OPEN_ENDED.search(question) and not _MEASURE.search(question):
        flags.append("open_ended")
    if not _MEASURE.search(question):
        flags.append("nothing_to_measure")
    if question.count("?") > 1:
        flags.append("multi_part")
    if _REFERENCE.search(question):
        flags.append("unresolved_reference")
    return flags


def missing_inputs(question: str, playbook: Any) -> list[str]:
    """Required playbook inputs the question does not name (only the kinds a rule can detect)."""
    out = []
    for spec in getattr(getattr(playbook, "body", None), "inputs", None) or []:
        detector = _DETECTORS.get(spec.type)
        if spec.required and detector is not None and not detector.search(question):
            out.append(spec.name)
    return out


def rules_tier(f: ComplexityFeatures) -> tuple[str, list[str]]:
    reasons: list[str] = []
    tier = "low"

    def raise_to(t: str, why: str) -> None:
        nonlocal tier
        if TIERS.index(t) > TIERS.index(tier):
            tier = t
        reasons.append(f"{why} -> {t}")

    if f.table_count >= 4:
        raise_to("complex", f"{f.table_count} tables")
    elif f.table_count >= 2:
        raise_to("medium", f"{f.table_count} tables")
    if f.join_depth >= 2:
        raise_to("complex", f"join depth {f.join_depth}")
    elif f.join_depth == 1:
        raise_to("medium", "join depth 1")
    if f.estimated_scan_rows >= HUGE_SCAN_ROWS:
        raise_to("complex", f"~{f.estimated_scan_rows:,} rows to scan")
    elif f.estimated_scan_rows >= LARGE_SCAN_ROWS:
        raise_to("medium", f"~{f.estimated_scan_rows:,} rows to scan")
    blocking = [a for a in f.ambiguity_flags if a in ("open_ended", "multi_part", "nothing_to_measure")]
    if len(blocking) >= 2:
        raise_to("complex", "ambiguous: " + ", ".join(blocking))
    elif blocking:
        raise_to("medium", "ambiguous: " + blocking[0])
    if "dashboard_draft" in f.requested_artifacts:
        raise_to("medium", "a dashboard draft was requested")
    if not reasons:
        reasons.append("one table, no joins, small scan, clear question -> low")
    return tier, reasons


def policy_tier(*, denied_columns: int, sensitive_columns: int, restricted_columns: int, autonomous: bool,
                external_side_effect: bool) -> tuple[str, list[str]]:
    """What policy requires at least: masked/sensitive data and unattended publication need care."""
    tier, reasons = "low", []
    if restricted_columns:
        tier = "complex"
        reasons.append(f"{restricted_columns} restricted column(s) in scope -> complex")
    elif denied_columns or sensitive_columns:
        tier = "medium"
        reasons.append(f"{denied_columns + sensitive_columns} masked or sensitive column(s) in scope -> medium")
    if autonomous and external_side_effect and tier == "low":
        tier = "medium"
        reasons.append("an unattended run that may propose publication -> medium")
    return tier, reasons


def _candidates(snapshot: Any, question: str, tier: str) -> list[tuple[int, str, str]]:
    """(priority, id, the template's lowest tier) of templates whose intents match and that serve
    `tier` or a higher one. A template never takes a goal above its highest tier."""
    from analystos.capabilities.playbook import parse

    out = []
    for m in snapshot.list("Playbook"):
        pb = parse(m)
        hints = pb.body.router
        if hints is None or not hints.intents:
            continue
        tiers = sorted(hints.tiers or TIERS, key=TIERS.index)
        if TIERS.index(tier) > TIERS.index(tiers[-1]):
            continue
        if any(re.search(p, question, re.I) for p in hints.intents):
            out.append((hints.priority, m.id, tiers[0]))
    return sorted(out, key=lambda c: (-c[0], c[1]))


def choose_playbook(snapshot: Any, question: str, tier: str, runnable: Any) -> tuple[str, str, str]:
    """(playbook, why, its lowest tier): the matching template with the highest priority that may run
    here, else `playbook.investigate`. `runnable(id) -> str | None` returns why a playbook cannot run."""
    passed = []
    for _, pid, lowest in _candidates(snapshot, question, tier):
        why = runnable(pid)
        if why is None:
            return pid, f"intent matched {pid}", lowest
        passed.append(f"{pid}: {why}")
    if passed:
        return DEFAULT_PLAYBOOK, "fell back to playbook.investigate (" + "; ".join(passed) + ")", "low"
    return DEFAULT_PLAYBOOK, f"no template intent matched at tier {tier}", "low"


def default_limits(tier: str, playbook: Any) -> dict[str, Any]:
    body = getattr(playbook, "body", None)
    return {**TIER_LIMITS[tier], **(dict(body.default_limits) if body is not None else {})}


def clarify_rung(question: str, missing: list[str], *, decide: Any = None) -> dict[str, Any]:
    """`clarify_needed` (escalate_only): the DecisionService when one is given, else the rule itself."""
    from analystos.decisions.rules import RULES
    from analystos.decisions.types import Question

    q = Question.escalation("Is this analytics goal too ambiguous to plan without asking the user?",
                            levels=["proceed", "clarify"], baseline="proceed", escalate_to="clarify", escalate_at=0.7)
    facts = {"missing_inputs": missing}
    if decide is not None:
        try:
            d = decide("clarify_needed", {"question": question}, q, facts)
            return {"value": d.value, "backend": d.backend, "decision_id": d.id, "missing_inputs": missing}
        except Exception as exc:  # intake must not depend on a decision backend: the rule is the floor
            log.warning("clarify_needed decision failed (%s); using the rule", exc)
    proposal = RULES["clarify_needed"]({"question": question}, facts, q)
    return {"value": proposal.value, "backend": "rules", "decision_id": None, "missing_inputs": missing}


def route(features: ComplexityFeatures, *, question: str, snapshot: Any, runnable: Any, requested_tier: str | None = None,
          requested_playbook: str | None = None, policy_reasons: list[str] | None = None,
          decide: Any = None) -> ComplexityDecision:
    """Classify a goal. `snapshot` is the capability registry; `runnable(id)` says why a template cannot
    run in this workspace (disabled, or not certified for an autonomous run)."""
    from analystos.capabilities.playbook import parse

    r_tier, reasons = rules_tier(features)
    reasons = [*reasons, *(policy_reasons or [])]
    floor = features.policy_tier

    def pick(tier: str) -> tuple[str, str, str]:
        if requested_playbook:
            return requested_playbook, "requested", "low"
        return choose_playbook(snapshot, question, tier, runnable)

    pid, why, lowest = pick(tier_max(requested_tier or r_tier, floor))
    manifest = snapshot.manifests.get(pid)
    playbook = parse(manifest) if manifest is not None and manifest.kind == "Playbook" else None
    missing = missing_inputs(question, playbook) if playbook is not None else []
    if "nothing_to_measure" in features.ambiguity_flags:
        missing = ["what to measure", *missing]
    clarify = clarify_rung(question, missing, decide=decide)
    if clarify["value"] == "clarify":
        floor = tier_max(floor, "medium")
        reasons.append("clarify_needed escalated: " + (", ".join(missing) or "ambiguous") + " -> at least medium, supervised")
    if requested_tier and TIERS.index(requested_tier) < TIERS.index(floor):
        reasons.append(f"requested tier {requested_tier} is below the policy floor {floor}")
    tier = tier_max(requested_tier or r_tier, floor, lowest)
    if lowest != "low" and TIERS.index(lowest) > TIERS.index(tier_max(requested_tier or r_tier, floor)):
        reasons.append(f"{pid} serves {lowest} goals and up -> {lowest}")
    if not requested_playbook:
        again, why2, lowest2 = pick(tier)
        if again != pid:  # the floor moved past what the first template serves
            pid, why, tier = again, why2, tier_max(tier, lowest2)
            manifest = snapshot.manifests.get(pid)
            playbook = parse(manifest) if manifest is not None else None
    return ComplexityDecision(tier=tier, floor=floor, rules_tier=r_tier, requested_tier=requested_tier, playbook=pid,
                              playbook_reason=why, limits=default_limits(tier, playbook), features=features,
                              reasons=reasons, clarify=clarify)

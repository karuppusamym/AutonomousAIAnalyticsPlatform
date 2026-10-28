"""Claim labels and the causal-language guard (spec S07 §3.2, v1 §7.2).

Every finding carries one label, decided by code from what REV established, never by a model:

* ``observed``               a verified description of one measure (a trend, a concentration, a seasonal
                             pattern, a forecast): nothing is said about what goes with what;
* ``supported_association``  a verified, not weak, association between a measure and a segment or driver;
* ``possible_contributor``   an association that survived its test but not every check that would make it
                             supported (drafted, not yet verified, or verified but weak);
* ``rejected``               the claim failed a check that tests the claim itself (second method, re-run,
                             significance after correction, effect size, binding);
* ``unknown``                there was not enough to decide (sample too small, evidence missing).

A method says which kind of claim it makes in its manifest (`spec.claim: observation | association`,
default association); `spec.causal_design: true` marks a design that identifies causes (A/B, diff-in-diff:
P2, none is registered yet). Causal wording (causes, drives, leads to, due to ...) is allowed only when the
label is ``supported_association`` *and* the method is a causal design. The check runs inside the numbers
guard (`evidence.facts.bind_finding`) on text whose group and context labels are masked, so a segment value
such as "Due to vendor" is never read as a causal claim.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

ClaimLabel = Literal["observed", "supported_association", "possible_contributor", "rejected", "unknown"]
LABELS: tuple[str, ...] = ("observed", "supported_association", "possible_contributor", "rejected", "unknown")
ClaimKind = Literal["observation", "association"]

# Causal verbs and connectives. Nouns ("driver", "cause analysis") and hedged association words
# ("associated with", "coincides with") are not matched.
CAUSAL = re.compile(
    r"\b(caus(?:e|es|ed|ing)|driv(?:e|es|ing)|drove|driven\s+by|lead(?:s|ing)?\s+to|led\s+to|"
    r"result(?:s|ed|ing)?\s+in|due\s+to|because(?:\s+of)?|owing\s+to|attributable\s+to|responsible\s+for|"
    r"trigger(?:s|ed|ing)?|bring(?:s)?\s+about|brought\s+about|produc(?:e|es|ed)\s+(?:a|an|the)?\s*(?:rise|fall|increase|"
    r"decrease|drop))\b", re.I)
# REV checks whose failure means "not enough to decide", not "the claim is wrong".
UNDECIDED_CHECKS = frozenset({"sample_size", "evidence_complete", "representative_population", "data_version_stable"})


def _manifest_spec(method: str | None) -> Mapping[str, Any]:
    if not method:
        return {}
    from analystos import methods

    m = methods.current().manifests.get(method)
    return (m.spec or {}) if m is not None else {}


def claim_kind(method: str | None) -> ClaimKind:
    return "observation" if _manifest_spec(method).get("claim") == "observation" else "association"


def causal_design_methods() -> list[str]:
    """Registered methods whose design identifies causes (A/B, diff-in-diff). Empty today (P2)."""
    from analystos import methods

    return [n for n, m in methods.current().manifests.items() if (m.spec or {}).get("causal_design") is True]


def causal_allowed(label: str | None, method: str | None) -> bool:
    return label == "supported_association" and bool(method) and method in causal_design_methods()


def causal_terms(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).lower() for m in CAUSAL.finditer(text or "")))


def causal_problems(text: str, *, label: str | None, method: str | None) -> list[str]:
    """The guard: causal wording in `text` that the claim's label and method do not license."""
    terms = causal_terms(text)
    if not terms or causal_allowed(label, method):
        return []
    why = (f"the method {method} is not a causal design" if label == "supported_association"
           else f"the claim is labelled {label or 'unverified'}")
    return [f"causal language ({', '.join(repr(t) for t in terms[:4])}) is not allowed: {why}; "
            "state it as an association"]


def draft_label(method: str | None) -> ClaimLabel:
    """A supported result before REV: an observation is still unverified, an association is a possible contributor."""
    return "unknown" if claim_kind(method) == "observation" else "possible_contributor"


def label_for(method: str | None, *, verified: bool, checks: Iterable[Mapping[str, Any]] = (),
              strength: str | None = None) -> ClaimLabel:
    """The label REV's verdict earns (deterministic: the same checks give the same label)."""
    if verified:
        if claim_kind(method) == "observation":
            return "observed"
        return "possible_contributor" if strength == "weak" else "supported_association"
    failed = {str(c.get("check")) for c in checks if not c.get("passed")}
    if failed and failed <= UNDECIDED_CHECKS:
        return "unknown"
    return "rejected"


def label_of(insight: Any) -> ClaimLabel:
    """The stored label, or the one a finding written before labels existed implies from its status."""
    label = getattr(insight, "claim_label", None)
    if label in LABELS:
        return label  # type: ignore[return-value]
    status = getattr(insight, "status", None)
    if status == "verified":
        return "supported_association"
    if status in ("failed_verification", "rejected"):
        return "rejected"
    return "unknown"


__all__ = ["CAUSAL", "LABELS", "ClaimLabel", "causal_allowed", "causal_design_methods", "causal_problems", "causal_terms",
           "claim_kind", "draft_label", "label_for", "label_of"]

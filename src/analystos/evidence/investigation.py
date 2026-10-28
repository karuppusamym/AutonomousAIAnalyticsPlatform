"""Investigation workflow checks as playbook gates (spec S07-T5, v1 §7.2).

An analyst works in a fixed order: baseline -> segmentation -> hypotheses -> statistics -> alternatives ->
conclusions. Each is a deterministic check over what the run persisted (profile artifacts, hypotheses,
experiments, findings, verification records), declared on the step after which it can be judged:

    - key: profile
      checks: [baseline]                          # on_fail: warn (default)
    - key: insights
      checks: [segmentation, {name: statistics, on_fail: block}]

A check returns `issues` (what failed) and `limitations` (what a reader must know even when it passed).
`warn` records both on the step output and in every finding's Evidence object (`limitations[]`); `block`
fails the step with the issues. No model is asked: text in rows or hypotheses never changes a check.

* baseline      window, time zone, denominator, freshness and completeness of every profiled table
* hypotheses    each proposed hypothesis states a mechanism (rationale) and an executable, testable spec
* segmentation  materiality (group sizes), coverage of the population, mix effects separated or disclosed
* statistics    the test fits the spec's types, the minimum sample holds, multiple-testing correction applied
* alternatives  seasonality and traffic mix considered, and only on validated data
* conclusions   every finding carries a REV label, no unlicensed causal wording, no VOID verdict
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError

CHECK_NAMES = ("baseline", "segmentation", "hypotheses", "statistics", "alternatives", "conclusions")
STALE_DAYS = 30
MAX_NULL_RATE = 0.2
MIN_COVERAGE = 0.8
MIN_SEASONS = 2
SEASON = {"day": 7, "week": 52, "month": 12, "quarter": 4}
TZ_TYPES = ("timestamptz", "with time zone", "timestamp_tz", "datetimeoffset", "timestamp_ltz")


class InvestigationGateFailed(AnalystOSError):
    code, http_status = "investigation_gate_failed", 422


@dataclass
class CheckResult:
    check: str
    issues: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def passed(self) -> bool:
        return not self.issues

    def as_dict(self, on_fail: str = "warn") -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "on_fail": on_fail, "checked": self.checked,
                "issues": self.issues[:20], "limitations": self.limitations[:20]}


# ------------------------------------------------------------------------------------ loaders
def _profiles(s: Session, run: Any) -> list[dict[str, Any]]:
    from analystos.db.models import Artifact

    return [dict(a.content or {}) | {"_name": a.name} for a in s.scalars(
        select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "profile").order_by(Artifact.created_at))]


def _hypotheses(s: Session, run: Any) -> list[Any]:
    from analystos.db.models import Hypothesis

    return list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status != "superseded")
                          .order_by(Hypothesis.created_at)))


def _primaries(s: Session, run: Any) -> dict[str, Any]:
    from analystos.db.models import Experiment

    out: dict[str, Any] = {}
    for e in s.scalars(select(Experiment).where(Experiment.run_id == run.id, Experiment.role == "primary")
                       .order_by(Experiment.created_at, Experiment.id)):
        out.setdefault(e.hypothesis_id, e)  # the one the insight step read (its first primary)
    return out


def _insights(s: Session, run: Any) -> list[Any]:
    from analystos.db.models import Insight

    return list(s.scalars(select(Insight).where(Insight.run_id == run.id)))


def _when(v: Any) -> datetime | None:
    if not isinstance(v, str) or len(v) < 10:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------------ checks
def baseline(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    now = now or datetime.now(UTC)
    out = CheckResult("baseline")
    for p in _profiles(s, run):
        out.checked += 1
        table = p.get("_name", "").removeprefix("Profile ")
        rows = p.get("row_count")
        if not rows:
            out.issues.append(f"{table}: no rows to form a denominator")
            continue
        cols = [c for c in p.get("columns") or [] if isinstance(c, Mapping)]
        times = [c for c in cols if c.get("semantic_type") == "datetime" or c.get("type_family") == "datetime"]
        if not times:
            out.limitations.append(f"{table}: no time column, so the analysis window is the whole table")
        for c in times:
            lo, hi = _when(c.get("min")), _when(c.get("max"))
            if lo and hi:
                out.limitations.append(f"{table}.{c.get('name')}: window {lo.date()} to {hi.date()}")
            if not any(t in str(c.get("data_type") or "").lower() for t in TZ_TYPES):
                out.limitations.append(f"{table}.{c.get('name')}: timestamps carry no time zone; read as UTC")
            if hi and hi > now:
                out.limitations.append(f"{table}.{c.get('name')}: values dated after today (latest {hi.date()})")
            elif hi and (now - hi).days > STALE_DAYS:
                out.limitations.append(f"{table}.{c.get('name')}: latest value is {(now - hi).days} days old")
            counts = [float(m.get("count") or m.get("n") or 0) for m in c.get("monthly_counts") or [] if isinstance(m, Mapping)]
            if len(counts) >= 3 and counts[-1] < 0.5 * sorted(counts)[len(counts) // 2]:
                out.limitations.append(f"{table}.{c.get('name')}: the latest month has under half the median volume "
                                       "(it may be incomplete)")
        sparse = sorted(f"{c.get('name')} ({float(c.get('null_rate') or 0):.0%} empty)" for c in cols
                        if float(c.get("null_rate") or 0) > MAX_NULL_RATE)
        if sparse:
            out.limitations.append(f"{table}: incomplete columns: " + ", ".join(sparse[:8]))
    if not out.checked:
        out.limitations.append("no table was profiled; the baseline is unchecked")
    return out


def hypotheses(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    from analystos.contracts.analysis import AnalysisSpec

    out = CheckResult("hypotheses")
    for h in _hypotheses(s, run):
        out.checked += 1
        try:
            AnalysisSpec.model_validate(h.spec or {})
        except Exception as exc:  # noqa: BLE001 - a malformed spec is the finding, not a crash
            out.issues.append(f"{h.code}: no executable test ({str(exc).splitlines()[0][:160]})")
            continue
        if not (h.statement or "").strip():
            out.issues.append(f"{h.code}: states no claim")
        rationale = str((h.provenance or {}).get("rationale") or "").strip()
        if not rationale and h.origin not in ("carried", "registry", "user"):
            out.limitations.append(f"{h.code}: no mechanism stated (why the claim might hold)")
    if not out.checked:
        out.limitations.append("no hypothesis was proposed")
    return out


def _segmented(h: Any) -> bool:
    return bool((h.spec or {}).get("segment") or (h.spec or {}).get("drivers"))


def segmentation(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    from analystos import methods

    out = CheckResult("segmentation")
    exps = _primaries(s, run)
    supported = [h for h in _hypotheses(s, run) if h.status == "supported" and _segmented(h) and h.id in exps]
    for h in supported:
        out.checked += 1
        stat = dict(exps[h.id].result or {})
        groups = [g for g in stat.get("groups") or [] if isinstance(g, Mapping)]
        n = int(stat.get("n") or 0)
        min_size = int((h.spec or {}).get("min_group_size") or 30)
        kept = sum(int(g.get("n") or 0) for g in groups)
        if n and groups and kept / n < MIN_COVERAGE:
            out.limitations.append(f"{h.code}: the groups compared cover {kept / n:.0%} of the population")
        small = [str(g.get("segment")) for g in groups if 0 < int(g.get("n") or 0) < min_size]
        if small:
            out.issues.append(f"{h.code}: groups below the materiality floor of {min_size} rows: {', '.join(small[:5])}")
    mix = {n for n, m in methods.current().manifests.items() if (m.spec or {}).get("mix_effects")}
    tested = {(h.spec or {}).get("method") for h in _hypotheses(s, run) if h.id in exps}
    if supported and mix and not (tested & mix):
        out.limitations.append("mix effects were not separated from within-segment change (no decomposition was run)")
    return out


def statistics(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    from analystos import methods

    out = CheckResult("statistics")
    exps = _primaries(s, run)
    current = _hypotheses(s, run)  # a replan's superseded hypotheses are not in the family the insight step corrected
    pvals = [exps[h.id] for h in current if h.id in exps and (exps[h.id].result or {}).get("p_value") is not None]
    for h in current:
        e = exps.get(h.id)
        if e is None:
            continue
        out.checked += 1
        stat, name = dict(e.result or {}), (h.spec or {}).get("method")
        if name not in methods.names():
            out.issues.append(f"{h.code}: method {name!r} is not registered")
            continue
        if stat.get("p_value") is not None and stat.get("p_adjusted") is None and h.status in ("supported", "inconclusive"):
            out.issues.append(f"{h.code}: p-value without multiple-testing correction")
        family = stat.get("bh_family_size")
        if family is not None and int(family) != len(pvals):
            out.issues.append(f"{h.code}: corrected over {family} tests, but the run tested {len(pvals)}")
        floor = int((methods.current().manifests[name].spec or {}).get("min_sample") or 0)
        n = int(stat.get("n") or 0)
        if floor and n < floor:
            out.limitations.append(f"{h.code}: n={n} is below {name}'s minimum sample of {floor}")
    if pvals:
        out.limitations.append(f"Benjamini-Hochberg correction across {len(pvals)} tests")
    return out


def alternatives(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    from analystos import methods
    from analystos.evidence.verification import insight_states

    out = CheckResult("alternatives")
    hyps = {h.id: h for h in _hypotheses(s, run)}
    exps = _primaries(s, run)
    seasonal = {n for n, m in methods.current().manifests.items() if (m.spec or {}).get("seasonality")}
    tested = {(h.spec or {}).get("method") for h in hyps.values() if h.id in exps}
    findings = _insights(s, run)
    states = insight_states(s, [i.id for i in findings])
    for ins in findings:
        h = hyps.get(ins.hypothesis_id)
        if h is None:
            continue
        out.checked += 1
        spec = h.spec or {}
        rev = {c.get("check"): c for c in (ins.verification or {}).get("evaluate") or [] if isinstance(c, Mapping)}
        if rev.get("data_version_stable", {}).get("passed") is False or any(
                str(c).startswith("Data quality:") for c in ins.caveats or []):
            out.limitations.append(f"{ins.code}: alternatives were not tested: the data it rests on is not validated")
            continue
        time = spec.get("time") or {}
        n_periods = int(((exps.get(h.id).result or {}).get("highlights") or {}).get("n_periods") or 0) if h.id in exps else 0
        season = SEASON.get(str(time.get("grain") or ""))
        if time and season and n_periods >= MIN_SEASONS * season and seasonal and not (tested & seasonal):
            out.limitations.append(f"{ins.code}: seasonality was not tested for this {time.get('grain')}ly series")
        elif time and season and n_periods < MIN_SEASONS * season:
            out.limitations.append(f"{ins.code}: under {MIN_SEASONS} seasons of history; seasonality cannot be ruled out")
        if _segmented(h) and not spec.get("time"):
            out.limitations.append(f"{ins.code}: a change in traffic mix between segments is not ruled out "
                                   "(measured on one period)")
        if states.get(ins.id, {}).get("state") == "VOID":
            out.issues.append(f"{ins.code}: its verdict is VOID")
    return out


def conclusions(s: Session, run: Any, now: datetime | None = None) -> CheckResult:
    from analystos.evidence.claims import LABELS, causal_problems
    from analystos.evidence.verification import insight_states

    out = CheckResult("conclusions")
    hyps = {h.id: h for h in _hypotheses(s, run)}
    findings = _insights(s, run)
    states = insight_states(s, [i.id for i in findings])
    for ins in findings:
        out.checked += 1
        method = ((hyps.get(ins.hypothesis_id).spec if hyps.get(ins.hypothesis_id) else None) or {}).get("method")
        if ins.status in ("verified", "failed_verification") and ins.claim_label not in LABELS:
            out.issues.append(f"{ins.code}: no claim label")
        if ins.status == "draft":
            out.issues.append(f"{ins.code}: concluded without a REV verdict")
        problems = causal_problems(f"{ins.title}\n{ins.finding}", label=ins.claim_label, method=method)
        if problems:
            out.issues.append(f"{ins.code}: {problems[0]}")
        state = states.get(ins.id) or {}
        if state.get("state") == "VOID":
            out.issues.append(f"{ins.code}: its verdict is VOID ({(state.get('void') or {}).get('reason')})")
        elif ins.status == "verified" and not state.get("record_id"):
            out.issues.append(f"{ins.code}: verified without a verification record")
        if ins.claim_label in ("possible_contributor", "unknown"):
            out.limitations.append(f"{ins.code}: labelled {ins.claim_label.replace('_', ' ')}")
    return out


CHECKS: dict[str, Callable[..., CheckResult]] = {"baseline": baseline, "segmentation": segmentation,
                                                 "hypotheses": hypotheses, "statistics": statistics,
                                                 "alternatives": alternatives, "conclusions": conclusions}


# ------------------------------------------------------------------------------------ the gate
def run_gates(run_id: str, step_key: str, gates: list[Any]) -> list[dict[str, Any]]:
    """Evaluate a step's gates after its behaviour; `block` raises with the issues (the step fails)."""
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun
    from analystos.events.bus import emit

    results: list[dict[str, Any]] = []
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        for gate in gates:
            r = CHECKS[gate.name](s, run).as_dict(gate.on_fail)
            results.append(r)
            emit(run.workspace_id, "investigation.check", {"task": step_key, **{k: r[k] for k in (
                "check", "passed", "on_fail", "checked")}, "issues": r["issues"][:5]}, run_id=run_id, session=s)
    blocked = [r for r in results if not r["passed"] and r["on_fail"] == "block"]
    if blocked:
        raise InvestigationGateFailed(
            "investigation check failed: " + "; ".join(f"{r['check']}: {r['issues'][0]}" for r in blocked),
            details={"checks": blocked})
    return results


def run_limitations(session: Session, run: Any) -> list[str]:
    """Every gate's issues and limitations recorded on the run's steps, for the Evidence objects."""
    from analystos.db.models import RunTask

    out: list[str] = []
    for out_ in session.scalars(select(RunTask.output).where(RunTask.run_id == run.id)):
        for r in (out_ or {}).get("checks") or []:
            if isinstance(r, Mapping):
                out += [f"{r.get('check')}: {x}" for x in [*(r.get("issues") or []), *(r.get("limitations") or [])]]
    return list(dict.fromkeys(out))


__all__ = ["CHECKS", "CHECK_NAMES", "CheckResult", "InvestigationGateFailed", "run_gates", "run_limitations"]

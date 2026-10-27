"""Readiness assessment (workspace spec §2 steps 1-4, P4-04).

For one job kind over its inputs, every applicable check reports its own status, reason and remediation:

* ``capability``  an executor and a usable capability exist (`capabilities/job_kinds`), else *unsupported*;
* ``scope``       the inputs are in the caller's resolved scope and inside the brief's source constraints;
* ``freshness``   each table's last load against the brief's freshness tolerance;
* ``schema_drift`` the tables and named columns still exist as recorded (not deprecated by a crawl);
* ``grain``       a reviewed or validated grain per table (an inferred one is a question, not a fact);
* ``key_uniqueness`` the reviewed entity key is unique in the profiles;
* ``join_fanout`` how the tables join: declared cardinality, validated, never many-to-many;
* ``coverage``    enough time history for the job (forecast/predict);
* ``missingness`` null rates of the columns the job uses;
* ``label_availability`` (predict) a declared label that exists, varies and has a prediction moment.

Only profiles and records are read: no query runs and no model is asked. The verdict is decided by the
job kind's *required* checks (`job_kinds.JOB_KINDS`): any `unsupported` -> unsupported, any `fail` ->
blocked, any `needs_input` -> needs_input, else ready. Advisory checks are shown and never averaged in.
A prediction without a label is *blocked*; an explanation task is offered as an alternative that needs an
explicit choice, never applied in its place.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.capabilities import job_kinds
from analystos.contracts.brief import EvidenceRef, ReadinessAssessmentDoc, ReadinessCheck, ReadinessIn
from analystos.core.ids import new_id, ordered_utcnow, stable_hash, utcnow
from analystos.db.models import ReadinessAssessment, Relationship, Source, SourceAsset, SourceColumn, User, WorkOrder
from analystos.services import brief as brief_svc

DEFAULT_FRESHNESS_HOURS = 24 * 7
MAX_DEFAULT_ASSETS = 20
MISSING_FAIL, MISSING_WARN = 0.5, 0.2
MIN_HISTORY_DAYS = 28


def _check(check: str, status: str, reason: str, remediation: str | None = None, subject: str | None = None,
           evidence: list[EvidenceRef] | None = None) -> ReadinessCheck:
    return ReadinessCheck(check=check, status=status, reason=reason, remediation=remediation, subject=subject,  # type: ignore[arg-type]
                          evidence=evidence or [])


def _combine(check: str, parts: list[ReadinessCheck], ok_reason: str) -> ReadinessCheck:
    """One check over many subjects: the worst part decides, every part's reason is kept."""
    order = {"unsupported": 5, "fail": 4, "needs_input": 3, "warn": 2, "pass": 1, "not_applicable": 0}
    if not parts:
        return _check(check, "not_applicable", ok_reason)
    worst = max(parts, key=lambda p: order[p.status])
    if worst.status in ("pass", "not_applicable"):
        return _check(check, worst.status, ok_reason if len(parts) > 1 else parts[0].reason, subject=None,
                      evidence=[e for p in parts for e in p.evidence])
    bad = [p for p in parts if order[p.status] >= order["warn"]]
    return _check(check, worst.status, "; ".join(f"{p.subject + ': ' if p.subject else ''}{p.reason}" for p in bad),
                  "; ".join(dict.fromkeys(p.remediation for p in bad if p.remediation)) or None,
                  subject=", ".join(p.subject for p in bad if p.subject) or None, evidence=[e for p in bad for e in p.evidence])


class _Ctx:
    def __init__(self, session: Session, user: User, workspace_id: str, body: ReadinessIn):
        from analystos.governance.policy import resolve_scope

        self.session, self.user, self.workspace_id, self.body = session, user, workspace_id, body
        self.job = job_kinds.get(body.job_kind)
        self.brief_row = brief_svc.head(session, workspace_id)
        self.brief = brief_svc.assertions_of(self.brief_row)
        self.scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
        constraint = brief_svc.effective(self.brief, "constraints", "source_scope")
        self.allowed = set(constraint.value) if constraint is not None and isinstance(constraint.value, list) else None
        requested = list(dict.fromkeys(body.assets))
        if not requested:
            requested = sorted(self.allowed & set(self.scope.assets)) if self.allowed is not None else \
                sorted(self.scope.assets)[:MAX_DEFAULT_ASSETS]
        self.assets = requested
        self._rows: dict[str, SourceAsset | None] = {}

    def asset(self, fq: str) -> SourceAsset | None:
        if fq not in self._rows:
            schema, _, name = fq.partition(".")
            self._rows[fq] = self.session.scalar(select(SourceAsset).where(
                SourceAsset.workspace_id == self.workspace_id, SourceAsset.schema_name == schema, SourceAsset.name == name))
        return self._rows[fq]

    def column(self, ref: str) -> tuple[str, SourceColumn | None]:
        """`schema.table.column` or a bare column of one of the inputs -> (fq, row)."""
        parts = ref.split(".")
        fqs = [".".join(parts[:2])] if len(parts) >= 3 else self.assets
        name = parts[-1]
        for fq in fqs:
            a = self.asset(fq)
            if a is None:
                continue
            col = self.session.scalar(select(SourceColumn).where(SourceColumn.asset_id == a.id, SourceColumn.name == name))
            if col is not None:
                return f"{fq}.{name}", col
        return (f"{fqs[0]}.{name}" if fqs else name), None

    def time_column(self) -> str | None:
        if self.body.time_column:
            return self.body.time_column
        for fq in self.assets:
            a = brief_svc.effective(self.brief, "time_measures", "event_time", fq)
            if a is not None:
                return f"{fq}.{a.value}"
        return None

    def target(self) -> str | None:
        if self.body.target:
            return self.body.target
        a = brief_svc.effective(self.brief, "ml_objective", "target")
        return str(a.value) if a is not None and a.value else None


# ------------------------------------------------------------------------------------ checks
def capability(c: _Ctx) -> ReadinessCheck:
    from analystos.capabilities import enablement, registry

    reasons = []
    if (r := job_kinds.executor_reason(c.job, c.session, c.workspace_id)) is not None:
        reasons.append(r)
    _, cap = job_kinds.capability_state(c.job, registry.current(), enablement.overrides(c.session, c.workspace_id))
    reasons += cap
    if reasons:
        return _check("capability", "unsupported", "; ".join(r["message"] for r in reasons),
                      "; ".join(dict.fromkeys(r["remediation"] for r in reasons)))
    return _check("capability", "pass", f"{c.job.label} has an executor and a usable capability here")


def scope(c: _Ctx) -> ReadinessCheck:
    from analystos.governance.policy import member_role
    from analystos.security.auth import role_at_least

    role = "owner" if c.user.is_admin else (member_role(c.session, c.user, c.workspace_id) or "viewer")
    if not role_at_least(role, c.job.min_role):
        return _check("scope", "fail", f"{c.job.label} needs the {c.job.min_role} role; you are {role}",
                      "Ask a workspace owner for the role.")
    if not c.assets:
        return _check("scope", "needs_input", "no table is chosen and none is in your scope",
                      "Select tables in Data > Sources, or name the tables this work reads.")
    outside = [a for a in c.assets if a not in c.scope.assets]
    if outside:
        return _check("scope", "fail", f"not available to you: {', '.join(outside)}",
                      "Choose tables from your scope, or ask a workspace owner for access.", subject=", ".join(outside))
    if c.allowed is not None and (beyond := [a for a in c.assets if a not in c.allowed]):
        return _check("scope", "fail", f"outside the brief's source constraint: {', '.join(beyond)}",
                      "Change the brief's constraints.source_scope, or choose other tables.", subject=", ".join(beyond))
    return _check("scope", "pass", f"{len(c.assets)} table(s) in your scope",
                  evidence=[EvidenceRef(kind="asset", ref=a) for a in c.assets])


def freshness(c: _Ctx) -> ReadinessCheck:
    tol = brief_svc.effective(c.brief, "constraints", "freshness_tolerance_hours")
    hours = float(tol.value) if tol is not None and isinstance(tol.value, int | float) else None
    limit = timedelta(hours=hours or DEFAULT_FRESHNESS_HOURS)
    now, parts = utcnow(), []
    for fq in c.assets:
        a = c.asset(fq)
        if a is None:
            continue
        src = c.session.get(Source, a.source_id)
        if src is not None and src.execution_mode != "staged":
            parts.append(_check("freshness", "pass", "queried live at the source", subject=fq))
            continue
        if a.freshness_at is None:
            parts.append(_check("freshness", "warn", "load time unknown", "Refresh the source.", subject=fq))
            continue
        at = a.freshness_at if a.freshness_at.tzinfo else a.freshness_at.replace(tzinfo=now.tzinfo)
        age = now - at
        if age > limit:
            parts.append(_check("freshness", "fail", f"loaded {int(age.total_seconds() // 3600)} h ago, over the "
                                f"{'brief' if hours else 'default'} tolerance of {int(limit.total_seconds() // 3600)} h",
                                "Refresh the source (Data > Sources), or relax constraints.freshness_tolerance_hours.",
                                subject=fq, evidence=[EvidenceRef(kind="asset", ref=fq, detail={"freshness_at": at.isoformat()})]))
        else:
            parts.append(_check("freshness", "pass", f"loaded {int(age.total_seconds() // 3600)} h ago", subject=fq))
    return _combine("freshness", parts, "every table is within its freshness tolerance")


def schema_drift(c: _Ctx) -> ReadinessCheck:
    parts = []
    for fq in c.assets:
        a = c.asset(fq)
        if a is None:
            parts.append(_check("schema_drift", "fail", "the table is no longer recorded", "Re-crawl the source.", subject=fq))
        elif a.lifecycle == "deprecated":
            parts.append(_check("schema_drift", "fail", "the last crawl no longer found this table at the source",
                                "Re-crawl the source, or choose its replacement.", subject=fq))
        else:
            parts.append(_check("schema_drift", "pass", "present as recorded", subject=fq))
    for ref in [x for x in (c.body.target, c.body.time_column, *c.body.measures) if x]:
        where, col = c.column(ref)
        if col is None:
            parts.append(_check("schema_drift", "fail", f"column {ref} does not exist", "Choose an existing column.", subject=where))
    return _combine("schema_drift", parts, "every table and named column is present as recorded")


def grain(c: _Ctx) -> ReadinessCheck:
    parts = []
    for fq in c.assets:
        eff = brief_svc.effective(c.brief, "data_semantics", "grain", fq)
        if eff is not None:
            parts.append(_check("grain", "pass", f"{eff.value} ({eff.review_state})", subject=fq))
            continue
        sug = brief_svc.find(c.brief, "data_semantics", "grain", fq)
        if sug is not None and sug.review_state == "suggested":
            parts.append(_check("grain", "needs_input", f"inferred grain '{sug.value}' ({sug.origin}) awaits review",
                                "Review or correct the grain in the workspace brief.", subject=fq,
                                evidence=[EvidenceRef(kind="context", ref=sug.key)]))
        else:
            parts.append(_check("grain", "needs_input", "no reviewed row grain", "State the row grain in the workspace brief.",
                                subject=fq))
    return _combine("grain", parts, "every table has a reviewed grain")


def key_uniqueness(c: _Ctx) -> ReadinessCheck:
    parts = []
    for fq in c.assets:
        eff = brief_svc.effective(c.brief, "data_semantics", "entity_key", fq)
        if eff is None or not isinstance(eff.value, list):
            parts.append(_check("key_uniqueness", "needs_input", "no reviewed entity key",
                                "State (or review the suggested) entity key in the workspace brief.", subject=fq))
            continue
        res = brief_svc.key_uniqueness(c.session, c.workspace_id, fq, [str(x) for x in eff.value])
        ev = [EvidenceRef(kind="check", ref="key_uniqueness", detail=res)]
        if res["state"] == "unique":
            parts.append(_check("key_uniqueness", "pass", f"{', '.join(eff.value)}: {res['detail']}", subject=fq, evidence=ev))
        elif res["state"] == "duplicates":
            parts.append(_check("key_uniqueness", "fail", f"key {', '.join(eff.value)} is not unique ({res['detail']}): "
                                "the grain is ambiguous", "Correct the key or the grain, or deduplicate in a recipe.",
                                subject=fq, evidence=ev))
        else:
            parts.append(_check("key_uniqueness", "needs_input", f"uniqueness of {', '.join(eff.value)} unknown: {res['detail']}",
                                "Profile the table (re-crawl with profiling).", subject=fq, evidence=ev))
    return _combine("key_uniqueness", parts, "every entity key is unique")


def join_fanout(c: _Ctx) -> ReadinessCheck:
    rows = [c.asset(fq) for fq in c.assets]
    ids = {a.id: f"{a.schema_name}.{a.name}" for a in rows if a is not None}
    if len(ids) < 2:
        return _check("join_fanout", "not_applicable", "one table: no join")
    rels = list(c.session.scalars(select(Relationship).where(Relationship.workspace_id == c.workspace_id,
                                                             Relationship.from_asset_id.in_(ids),
                                                             Relationship.to_asset_id.in_(ids))))
    parts = []
    joined = set()
    for r in rels:
        subject = f"{ids[r.from_asset_id]}.{r.from_column}->{ids[r.to_asset_id]}.{r.to_column}"
        joined |= {r.from_asset_id, r.to_asset_id}
        ev = [EvidenceRef(kind="relationship", ref=r.id, detail={"cardinality": r.cardinality, "validated": r.validated})]
        if r.cardinality == "many_to_many":
            parts.append(_check("join_fanout", "fail", "many-to-many: every additive measure would be multiplied",
                                "Join through a bridge with a declared pre-aggregation, or choose one table.", subject, ev))
        elif not r.validated:
            parts.append(_check("join_fanout", "needs_input", f"{r.cardinality} declared but not validated on the data",
                                "Validate the relationship's cardinality (Data > Definitions).", subject, ev))
        else:
            parts.append(_check("join_fanout", "pass", f"{r.cardinality}, validated", subject=subject, evidence=ev))
    for aid, fq in ids.items():
        if aid not in joined:
            parts.append(_check("join_fanout", "needs_input", "no declared relationship to the other tables",
                                "Declare how this table joins (a relationship with its cardinality).", subject=fq))
    return _combine("join_fanout", parts, "every join follows a validated many-to-one relationship")


def coverage(c: _Ctx) -> ReadinessCheck:
    time_ref = c.time_column()
    needs_time = c.job.key in ("forecast", "predict")
    if not time_ref:
        return _check("coverage", "needs_input" if needs_time else "not_applicable",
                      "no event-time column is declared" if needs_time else "no time column in use",
                      "State the event time in the workspace brief (time_measures.event_time)." if needs_time else None)
    where, col = c.column(time_ref)
    prof = (col.profile or {}) if col is not None else {}
    if col is None or not prof:
        return _check("coverage", "needs_input" if needs_time else "warn", f"{where} is not profiled",
                      "Profile the table (re-crawl with profiling).", subject=where)
    months = [m for m in prof.get("monthly_counts") or [] if (m.get("count") or m.get("n") or 0)]
    span = None
    try:
        from datetime import datetime

        lo, hi = datetime.fromisoformat(str(prof.get("min"))[:19]), datetime.fromisoformat(str(prof.get("max"))[:19])
        span = (hi - lo).days
    except (TypeError, ValueError):
        pass
    detail = {"min": prof.get("min"), "max": prof.get("max"), "days": span, "months": len(months)}
    ev = [EvidenceRef(kind="profile", ref=where, detail=detail)]
    need = max(MIN_HISTORY_DAYS, 2 * 7 * (c.body.horizon or 0)) if needs_time else 1
    if span is None:
        return _check("coverage", "needs_input" if needs_time else "warn", "the time range is unknown",
                      "Profile the table.", subject=where, evidence=ev)
    if span < need:
        return _check("coverage", "fail" if needs_time else "warn", f"{span} days of history; {need} needed",
                      "Load more history, or shorten the horizon.", subject=where, evidence=ev)
    return _check("coverage", "pass", f"{span} days of history ({detail['min']} to {detail['max']})", subject=where, evidence=ev)


def missingness(c: _Ctx) -> ReadinessCheck:
    refs = [x for x in (c.target(), c.time_column(), *c.body.measures) if x]
    parts = []
    for ref in dict.fromkeys(refs):
        where, col = c.column(ref)
        if col is None:
            continue
        rate = (col.profile or {}).get("null_rate")
        if rate is None:
            parts.append(_check("missingness", "warn", "null rate unknown (not profiled)", "Profile the table.", subject=where))
        elif rate > MISSING_FAIL:
            parts.append(_check("missingness", "fail", f"{rate:.0%} missing", "Choose another column, or fill it upstream.",
                                subject=where, evidence=[EvidenceRef(kind="profile", ref=where, detail={"null_rate": rate})]))
        elif rate > MISSING_WARN:
            parts.append(_check("missingness", "warn", f"{rate:.0%} missing", "Results describe rows where it is present.",
                                subject=where, evidence=[EvidenceRef(kind="profile", ref=where, detail={"null_rate": rate})]))
        else:
            parts.append(_check("missingness", "pass", f"{rate:.0%} missing", subject=where))
    return _combine("missingness", parts, "no column in use is mostly missing")


def label_availability(c: _Ctx) -> ReadinessCheck:
    if c.job.key != "predict":
        return _check("label_availability", "not_applicable", "only prediction needs a label")
    target = c.target()
    if not target:
        sug = brief_svc.find(c.brief, "ml_objective", "target")
        why = f"the suggested label '{sug.value}' is not reviewed" if sug is not None else "no label (target) is declared"
        return _check("label_availability", "fail", f"{why}: a prediction could not be evaluated",
                      "Declare the label in the brief (ml_objective.target), or explicitly choose a descriptive job instead.")
    where, col = c.column(target)
    if col is None:
        return _check("label_availability", "fail", f"label {target} does not exist in the inputs",
                      "Choose an existing label column.", subject=where)
    prof = col.profile or {}
    ev = [EvidenceRef(kind="profile", ref=where, detail={k: prof.get(k) for k in ("null_rate", "distinct")})]
    if prof.get("null_rate") is not None and float(prof["null_rate"]) >= 1.0:
        return _check("label_availability", "fail", "the label is never observed", "Wait until labels mature, or choose another.",
                      subject=where, evidence=ev)
    if prof.get("distinct") is not None and int(prof["distinct"]) < 2:
        return _check("label_availability", "fail", "the label has one value: nothing to learn or evaluate",
                      "Choose a label that varies.", subject=where, evidence=ev)
    moment = brief_svc.effective(c.brief, "ml_objective", "prediction_moment")
    if moment is None:
        return _check("label_availability", "needs_input", "no prediction moment is declared (leakage cannot be checked)",
                      "State when the prediction is made (ml_objective.prediction_moment).", subject=where, evidence=ev)
    if not prof:
        return _check("label_availability", "needs_input", "the label is not profiled", "Profile the table.", subject=where)
    return _check("label_availability", "pass", f"label {target} observed ({prof.get('distinct')} values)", subject=where, evidence=ev)


CHECK_FUNCS = {"capability": capability, "scope": scope, "freshness": freshness, "schema_drift": schema_drift, "grain": grain,
               "key_uniqueness": key_uniqueness, "join_fanout": join_fanout, "coverage": coverage,
               "missingness": missingness, "label_availability": label_availability}


def verdict(checks: list[ReadinessCheck]) -> str:
    required = [c.status for c in checks if c.required]
    if "unsupported" in required:
        return "unsupported"
    if "fail" in required:
        return "blocked"
    if "needs_input" in required:
        return "needs_input"
    return "ready"


def evaluate(session: Session, user: User, workspace_id: str, body: ReadinessIn) -> ReadinessAssessmentDoc:
    """The assessment, not persisted."""
    c = _Ctx(session, user, workspace_id, body)
    checks: list[ReadinessCheck] = []
    for name in c.job.checks:
        checks.append(CHECK_FUNCS[name](c))
    for name in c.job.advisory:
        checks.append(CHECK_FUNCS[name](c).model_copy(update={"required": False}))
    status = verdict(checks)
    alternatives = []
    if c.job.key in ("predict", "forecast") and status in ("blocked", "unsupported"):
        alternatives.append({"job_kind": "explain", "requires_explicit_choice": True,
                             "note": "An explanation describes what happened; it is not a prediction. Choose it "
                                     "explicitly if that answers the question."})
    inputs = {**body.model_dump(mode="json"), "assets": c.assets, "job_kind": c.job.key}
    return ReadinessAssessmentDoc(workspace_id=workspace_id, job_kind=c.job.key, status=status,  # type: ignore[arg-type]
                                  brief_version=c.brief_row.version if c.brief_row is not None else 0, checks=checks,
                                  inputs=inputs, inputs_hash=stable_hash({**inputs, "scope": c.scope.scope_hash()}),
                                  alternatives=alternatives)


def assess(session: Session, user: User, workspace_id: str, body: ReadinessIn, *, work_order: WorkOrder | None = None) -> dict[str, Any]:
    """Evaluate and persist (an assessment id a later start can name)."""
    from analystos.events.bus import emit
    from analystos.governance.policy import require_role

    require_role(session, user, workspace_id, "viewer")
    out = evaluate(session, user, workspace_id, body)
    row = ReadinessAssessment(id=new_id("rdy"), workspace_id=workspace_id, job_kind=out.job_kind,
                              work_order_id=work_order.id if work_order is not None else None,
                              work_order_revision=work_order.revision if work_order is not None else None,
                              brief_version=out.brief_version, status=out.status,
                              checks=[c.model_dump(mode="json") for c in out.checks], inputs=out.inputs,
                              inputs_hash=out.inputs_hash or "", alternatives=out.alternatives, created_by=f"user:{user.id}",
                              created_at=ordered_utcnow())
    session.add(row)
    session.flush()
    emit(workspace_id, "readiness.assessed", {"assessment_id": row.id, "job_kind": out.job_kind, "status": out.status,
                                              "work_order_id": row.work_order_id}, actor=f"user:{user.id}", session=session)
    return view(row)


def view(row: ReadinessAssessment) -> dict[str, Any]:
    return ReadinessAssessmentDoc(id=row.id, workspace_id=row.workspace_id, job_kind=row.job_kind, status=row.status,  # type: ignore[arg-type]
                                  brief_version=row.brief_version, work_order_id=row.work_order_id,
                                  work_order_revision=row.work_order_revision,
                                  checks=[ReadinessCheck.model_validate(c) for c in row.checks], inputs=row.inputs,
                                  inputs_hash=row.inputs_hash, alternatives=row.alternatives,
                                  created_at=row.created_at.isoformat() if row.created_at else None).model_dump(mode="json")


def inputs_for_work_order(spec: Any) -> ReadinessIn:
    """What a typed work order reads: its input assets, the analyses' assets, the ML target and time."""
    assets = [i.asset for i in spec.inputs if i.asset]
    measures: list[str] = []
    target = time_column = None
    horizon = None
    payload = spec.spec
    if payload.type == "analysis":
        for a in payload.analyses:
            assets.append(a.asset)
    elif payload.type == "ml":
        target = getattr(payload, "target_metric", None) if payload.task in ("classify", "regress") else None
        time_column = getattr(payload, "time_column", None)
        horizon = getattr(payload, "horizon", None)
        if payload.task == "forecast" and payload.target_metric and "." in payload.target_metric:
            measures.append(payload.target_metric)
    return ReadinessIn(job_kind=job_kinds.WORK_ORDER_KIND.get(spec.kind, spec.kind), assets=list(dict.fromkeys(assets)),
                       target=target, time_column=time_column, horizon=horizon, measures=measures)


def assess_work_order(session: Session, user: User, wo: WorkOrder) -> dict[str, Any]:
    from analystos.contracts.work import WorkOrderSpec

    return assess(session, user, wo.workspace_id, inputs_for_work_order(WorkOrderSpec.model_validate(wo.spec)), work_order=wo)


def latest_for(session: Session, work_order_id: str) -> ReadinessAssessment | None:
    return session.scalar(select(ReadinessAssessment).where(ReadinessAssessment.work_order_id == work_order_id)
                          .order_by(ReadinessAssessment.created_at.desc(), ReadinessAssessment.id.desc()).limit(1))

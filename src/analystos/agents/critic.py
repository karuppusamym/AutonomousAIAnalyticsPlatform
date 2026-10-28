"""REV Critic Agent (§26, §28): Reason -> Evaluate -> Verify.

Verified == every deterministic check passes (method fit, sample size, adjusted significance,
effect size, reproducible re-run, independent second method, and since P4-03: the narrative binds to
the typed facts, the data version did not change during the run, and the evidence bundle is complete).
`verified` says the REV checks passed; it does not say the claim is confirmed: the bundle's validation
state (`evidence.bundle`) is `exploratory` for a discovery until a confirmation rule
(`evidence.confirmation`) passes. Model opinions (independent model family + JEV) are recorded and can
lower the review score or add caveats, but cannot make a finding true; the score is uncalibrated.

P8-15: the second method reads the same discovery rows as the primary test. Once a finding is verified its
claim is locked and the same test runs once on the held-out rows (`evidence.holdout`); that record is what
the `holdout_partition` rule reads. Every verified finding also carries its strength (`evidence.strength`),
and a weak or unconfirmed one says so in its own sentence."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from analystos import methods
from analystos.agents.common import ModelOutcome, compact_json, llm_json, llm_json_batch, task_output
from analystos.agents.insight import template_text
from analystos.agents.investigator import with_constraints
from analystos.artifacts.registry import link, link_queries
from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.contracts.evidence import DataManifest, Fact, HoldoutCheck
from analystos.core.errors import AnalystOSError, OutputContractViolation
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Experiment, Hypothesis, Insight, QueryExecution, by_code
from analystos.decisions import Question
from analystos.events.bus import emit
from analystos.evidence.bundle import assemble
from analystos.evidence.claims import CAUSAL, causal_problems, causal_terms, draft_label, label_for  # noqa: F401 (CAUSAL re-exported)
from analystos.evidence.confirmation import evaluate as confirm
from analystos.evidence.confirmation import is_replication, prior_claim
from analystos.evidence.facts import bind_finding
from analystos.evidence.holdout import from_record, lock_claim, not_evaluated, test_on_holdout
from analystos.evidence.manifest import changed_for_spec, current_entry, spec_assets
from analystos.evidence.strength import grade, qualify
from analystos.evidence.verification import insight_dependencies, record_verdict
from analystos.knowledge.attested import sql_hash
from analystos.llm.cache import estimate_tokens
from analystos.llm.config import family
from analystos.registries.hypotheses import spec_hash
from analystos.runtime.context import RunContext
from analystos.services.citations import insight_citations
from analystos.services.citations import record as record_citations
from analystos.services.platform_settings import get as platform

log = logging.getLogger(__name__)
MIN_N = 100


def _dump(v):
    return v.model_dump() if hasattr(v, "model_dump") else v


def narrative_family(narrative_source: str | None) -> str | None:
    """Model family that wrote the finding's wording (``llm:<provider>/<model>``), which the
    independent reviewer must not share. Template wording has no author model, so no family is
    excluded; guessing one would only narrow the reviewer pool for no independence gain."""
    if not narrative_source or not narrative_source.startswith("llm:"):
        return None
    model = narrative_source.split(":", 1)[1].strip()
    if not model or model == "deterministic":
        return None
    return family(model)


def representative_population(ctx: RunContext, asset: str) -> dict:
    """P4-C12: a claim is about the population sampled. Fails when the staged snapshot was truncated
    without a declared sample (or by first_n); records the sampling method either way."""
    from analystos.staging.snapshots import population_for

    return population_for(asset, ctx.scope.asset_sources.get(asset)).check()


def review_statistics(stat_d: dict) -> dict:
    """The statistics a reviewer reads: test, n, p (raw and adjusted), effect and the highlights, floats to
    4 significant digits and each value once (highlights repeat n and p)."""
    def short(v: Any) -> Any:
        return float(f"{v:.4g}") if isinstance(v, float) else v

    out = {k: short(stat_d.get(k)) for k in ("test", "n", "p_value", "p_adjusted", "effect_size", "effect_label")
           if stat_d.get(k) is not None}
    out.update({k: short(v) for k, v in (stat_d.get("highlights") or {}).items() if k not in out and v is not None})
    if stat_d.get("warnings"):
        out["warnings"] = stat_d["warnings"]
    return out


def _review_ok(review: Any) -> bool:
    return isinstance(review, dict) and isinstance(review.get("supports"), bool)


def independent_reviews(ctx: RunContext, states: list[dict]) -> dict[str, tuple[Any, str]]:
    """The opt-in independent-model review (P4-T02) of every finding of the run in one call
    (`verification_batch.v1`), from a family that wrote none of them. A malformed batch answer falls
    back to one call per finding; a finding the batch skipped gets no review (its deterministic checks
    decide, as when the model is unavailable)."""
    if not states:
        return {}
    if not ctx.policy.independent_model_verification:
        for st in states:
            ctx.router.record_skip("verification", ctx.call_ctx(),
                                   estimated_tokens=estimate_tokens(compact_json(st["review_payload"])) + 500,
                                   reason="workspace policy: independent-model verification is opt-in; deterministic REV checks decide")
        return {st["code"]: (None, "policy_off") for st in states}
    families = sorted({st["primary_family"] for st in states if st["primary_family"]})

    def single(st: dict) -> tuple[Any, str]:
        return llm_json(ctx, "verification", "verification.v1", st["review_payload"],
                        exclude_families=[st["primary_family"]] if st["primary_family"] else [])

    if len(states) == 1:
        return {states[0]["code"]: single(states[0])}
    items = [{"id": st["code"], **st["review_payload"]} for st in states]
    answers, model = llm_json_batch(ctx, "verification", "verification_batch.v1", items, list_key="reviews",
                                    exclude_families=families, max_tokens=min(8000, 300 + 250 * len(items)),
                                    validate=lambda d: None if any(_review_ok(r) for r in d["reviews"]) else "no usable review")
    if answers is None and isinstance(model, ModelOutcome):
        return {st["code"]: (None, model) for st in states}
    if answers is None:
        ctx.say("REV: the batched independent review was malformed; reviewing each finding on its own.", kind="decision")
        return {st["code"]: single(st) for st in states}
    out = {}
    for st in states:
        review = answers.get(st["code"])
        out[st["code"]] = ({k: v for k, v in review.items() if k != "id"}, model) if _review_ok(review) \
            else (None, f"not reviewed in the batch answer of {model}")
    return out


def _bound_allows_holdout(ctx: RunContext) -> bool:
    """A run keeps the agent manifests it was bound with. One bound to a critic from before P8-15 declares
    no held-out experiment, so the step is skipped for it (and says so) instead of failing verification."""
    try:
        probe = methods.for_playbook("flag_by_segment").name  # any registered method; the role is what is checked
        ctx.check_output("experiment", {"method": probe, "params": {}, "result": {}, "query_ids": [], "role": "holdout"})
        return True
    except OutputContractViolation:
        return False


def confirm_on_holdout(ctx: RunContext, st: dict, top: Any, direction: str | None) -> HoldoutCheck:
    """Lock the verified claim, then run its test once on the held-out rows (P8-15). A repeated review of
    the same hypothesis in this run reuses the first answer: the held-out rows are never read twice."""
    spec, spec_d, stat_d, code, alpha = st["spec"], st["spec_d"], st["stat_d"], st["code"], st["alpha"]
    part = (stat_d.get("details") or {}).get("partition") or {}
    if st["discovery"] is None:
        return not_evaluated(part.get("reason") or "the test read every row (it ran before held-out confirmation existed)")
    if not _bound_allows_holdout(ctx):
        return not_evaluated("this run is bound to a critic version without the held-out step")
    with session_scope() as s:
        done = s.scalar(select(Experiment).where(Experiment.run_id == ctx.run.id, Experiment.hypothesis_id == st["hypothesis_id"],
                                                 Experiment.role == "holdout"))
        if done is not None and (done.result or {}).get("holdout"):
            return HoldoutCheck.model_validate(done.result["holdout"])
    locked = lock_claim(spec=spec_d, stat=stat_d, top=top, direction=direction, spec_hash=spec_hash(spec_d))
    min_n = max(MIN_N, platform().analysis.min_sample_size)
    outcome = None
    try:
        run_sql = ctx.run_sql(ctx.scope.asset_sources.get(spec.asset))
        record, outcome = ctx.tools().invoke(
            "analysis.verify", {"insight": code, "method": spec.method, "partition": "holdout"},
            lambda: test_on_holdout(spec, run_sql, locked, st["discovery"], alpha=alpha, min_n=min_n,
                                    sample_rows=platform().analysis.sample_rows))
    except AnalystOSError as exc:
        record = not_evaluated(f"the held-out test did not run ({exc.code})").model_copy(
            update={"claim": locked.claim, "claim_hash": locked.claim_hash, "claim_locked_at": locked.locked_at})
    result = {**(_dump(outcome.stat) if outcome is not None else {}), "holdout": None}
    query_ids = list(outcome.query_ids) if outcome is not None else []
    ctx.check_output("experiment", {"method": spec.method, "params": {"holdout_of": code}, "result": result,
                                    "query_ids": query_ids, "role": "holdout"})
    with session_scope() as s:
        hexp = Experiment(id=new_id("exp"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=st["hypothesis_id"],
                          method=spec.method, params={"holdout_of": code, "claim": locked.claim, "claim_hash": locked.claim_hash},
                          result=result, query_ids=query_ids, role="holdout")
        record = record.model_copy(update={"experiment_id": hexp.id})
        hexp.result = {**result, "holdout": record.model_dump(mode="json")}
        s.add(hexp)
        link(s, ctx.workspace.id, ("insight", st["insight_id"]), "confirmation_tested_by", ("experiment", hexp.id), run_id=ctx.run.id)
        link_queries(s, ctx.workspace.id, ("experiment", hexp.id), query_ids, run_id=ctx.run.id,
                     assets=[spec.asset] if spec.asset else None)
    ctx.say(f"REV {code}: claim locked at {locked.locked_at}; held-out test ({record.partition or 'not run'}): "
            + ("supports it" if record.supported else f"does not support it ({record.reason})"), kind="decision")
    return record


def _citations(session: Any, ins: Any, bundle: dict, title: str, finding: str, narrative_source: str | None) -> Any:
    """The finding's measured and document citations (N-8), or None when they could not be built: they
    describe the claim, so a failure here is logged and never changes the verdict."""
    try:
        with session.begin_nested():
            return insight_citations(session, ins, bundle=bundle, title=title, finding=finding, narrative_source=narrative_source)
    except Exception:  # noqa: BLE001
        log.exception("could not build citations of %s", ins.id)
        return None


def verify_insights(ctx: RunContext) -> dict:
    from analystos.skills.analysis import verify_analysis

    with session_scope() as s:
        insights = [(i.id, i.code) for i in s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "draft")
                                                          .order_by(*by_code(Insight.code)))]
    quality = task_output(ctx.run.id, "quality").get("issues") or []
    verified_codes, failed_codes = [], []
    directions: dict[tuple, list] = {}
    # Two passes (Stream B): every finding's deterministic checks first, then ONE independent-model review
    # call for all of them (not one per finding), then each verdict. The review only adjusts confidence.
    states: list[dict] = []
    for insight_id, code in insights:
        ctx.check_control()
        with session_scope() as s:
            ins = s.get(Insight, insight_id)
            h = s.get(Hypothesis, ins.hypothesis_id)
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary"))
            queries = list(s.scalars(select(QueryExecution).where(QueryExecution.id.in_(exp.query_ids))))
            originals = {q.id: (q.sql, q.result_hash, q.source_id) for q in queries}
            receipts = [{"query_id": q.id, "role": "primary", "query_hash": sql_hash(q.executed_sql or q.sql),
                         "result_hash": q.result_hash, "fingerprint": q.fingerprint, "rows": q.row_count} for q in queries]
            finding, spec_d, stat_d, narrative_source = ins.finding, dict(h.spec), dict(exp.result), ins.narrative_source
            statement, title, draft, prior_caveats = h.statement, ins.title, dict(ins.evidence_bundle or {}), list(ins.caveats or [])
            origin, iteration, parent, hypothesis_id = h.origin, h.iteration, h.parent_id, h.id
            recorded_manifest = dict(s.get(AnalysisRun, ctx.run.id).data_manifest or {})
            prior = prior_claim(s, ctx.workspace.id, ctx.run.id, spec_d)
        spec = with_constraints(AnalysisSpec.model_validate(spec_d), ctx.run.constraints)
        discovery = from_record((stat_d.get("details") or {}).get("partition"))  # the rows the primary test read
        primary_family = narrative_family(narrative_source)  # before any rewrite below: who wrote the claim
        run_sql = ctx.run_sql(ctx.scope.asset_sources.get(spec.asset))
        alpha = ctx.policy.alpha
        # ---- Reason
        reason = {"claim": finding, "question": statement, "required_evidence": ["primary query result", f"{stat_d.get('test')} statistics",
                                                                                 "reproducible re-run", "independent second method"]}
        # ---- Evaluate
        checks = []
        groups = stat_d.get("groups") or []
        min_group = min((g.get("n") or 0 for g in groups), default=stat_d.get("n") or 0)
        checks.append({"check": "method_fit", "passed": not any("expected" in w.lower() and "<5" in w for w in stat_d.get("warnings") or []),
                       "detail": f"{stat_d.get('test')} for {spec.method}; assumptions: {', '.join(stat_d.get('assumptions') or []) or 'n/a'}"})
        min_n = max(MIN_N, platform().analysis.min_sample_size)  # the admin can raise the floor, never lower it
        checks.append({"check": "sample_size", "passed": (stat_d.get("n") or 0) >= min_n,
                       "detail": f"n={stat_d.get('n')}, smallest group={min_group} (min {min_n})"})
        p_adj = stat_d.get("p_adjusted", stat_d.get("p_value"))
        checks.append({"check": "significance_after_bh", "passed": p_adj is not None and p_adj < alpha, "detail": f"q={p_adj} alpha={alpha}"})
        checks.append({"check": "effect_size", "passed": bool(stat_d.get("supported")),
                       "detail": f"{stat_d.get('effect_label')}={stat_d.get('effect_size')}"})
        population = representative_population(ctx, spec.asset)
        checks.append(population)
        # S07-T3: causal wording only for a supported association from a causal design (`evidence.claims`); REV
        # rewrites anything else to the method's associational template before the verdict.
        overreach = causal_terms(f"{title}\n{finding}") if causal_problems(
            f"{title}\n{finding}", label=draft_label(spec.method), method=spec.method) else []
        if overreach:
            title, finding = template_text(stat_d, spec_d)
            narrative_source = "template"
        checks.append({"check": "no_overreach", "passed": True,
                       "detail": f"causal wording ({', '.join(overreach[:3])}) rewritten to association" if overreach
                       else "associational wording"})
        # ---- Evaluate (P4-03): the final wording binds to the typed facts; the data version held still
        facts = [Fact.model_validate(f) for f in (draft.get("claim") or {}).get("facts") or []]
        binding = bind_finding(spec_d, stat_d, facts, title, finding)
        checks.append({"check": "fact_binding", "passed": bool(facts) and binding.ok,
                       "detail": ((f"{len(binding.mentions)} number(s) bound to {len(facts)} facts" if binding.ok
                                   else "; ".join(binding.problems[:4])) if facts else "no typed facts recorded")})
        assets = spec_assets(spec_d)
        with session_scope() as s:
            entries = {asset: current_entry(s, asset, ctx.scope.asset_sources.get(asset)) for asset in assets}
        entry = entries[spec.asset]
        manifest = DataManifest.model_validate(recorded_manifest) if recorded_manifest else None
        recorded = manifest.entry(spec.asset) if manifest else None
        moved = changed_for_spec(manifest, spec_d, entries)
        checks.append({"check": "data_version_stable", "passed": not moved,
                       "detail": "; ".join(moved) if moved else
                       "; ".join(f"{asset}: {e.mode} {e.version_basis} version {(e.version or 'unversioned')[:12]}"
                                 for asset, e in entries.items())})
        dq = [q.get("message") for q in quality if q.get("severity") in ("warning", "critical") and
              any(c in str(q.get("column") or "") for c in [d.get("column") for d in (spec_d.get("outcome") or {}, spec_d.get("segment") or {}) if d])]
        # ---- Verify: reproducibility
        reproducible, repro_detail = bool(originals), [] if originals else ["no query records: nothing to re-run"]
        for qid, (sql, original_hash, source_id) in originals.items():
            try:
                # Same budgeted, tool-gated path as every other run statement (P4-C02); no cache, or
                # the "re-run" would only replay the stored result.
                rerun = ctx.run_sql(source_id or ctx.scope.asset_sources.get(spec.asset))
                again = rerun(sql, purpose="verification.rerun", use_cache=False)
                same = again.result_hash == original_hash
                reproducible &= same
                repro_detail.append(f"{qid}: {'identical' if same else 'DIFFERENT'} result hash")
            except AnalystOSError as exc:
                reproducible = False
                repro_detail.append(f"{qid}: re-run failed ({exc.code})")
        checks.append({"check": "reproducible_rerun", "passed": reproducible, "detail": "; ".join(repro_detail)})
        # ---- Verify: independent second method
        second = None
        try:
            second = ctx.tools().invoke("analysis.verify", {"insight": code, "method": spec.method},
                                        lambda spec=spec, run_sql=run_sql, stat_d=stat_d, alpha=alpha, discovery=discovery: verify_analysis(
                                            spec, run_sql, StatResult.model_validate(stat_d), alpha=alpha, partition=discovery))
            sd = _dump(second.stat)
            agrees = bool(sd.get("details", {}).get("agrees", sd.get("supported")))
            checks.append({"check": "second_method", "passed": agrees, "detail": f"{sd.get('test')}: p={sd.get('p_value')} "
                           f"effect={sd.get('effect_size')} agrees={agrees}"})
        except AnalystOSError as exc:
            checks.append({"check": "second_method", "passed": False, "detail": f"failed: {exc.code}"})
        # ---- Verify: independent model family (policy opt-in, P4-T02) + JEV (recorded, not decisive)
        review_payload = {"claim": finding, "hypothesis": statement, "method": spec.method,
                          "statistics": review_statistics(stat_d),
                          # one line per check: the verdict and its evidence, without repeating keys per check
                          "checks": [f"{c['check']} {'ok' if c['passed'] else 'FAILED'}: {c.get('detail') or ''}".rstrip(": ")
                                     for c in checks]}
        states.append({"insight_id": insight_id, "code": code, "spec": spec, "spec_d": spec_d, "stat_d": stat_d,
                       "finding": finding, "title": title, "narrative_source": narrative_source, "checks": checks,
                       "second": second, "p_adj": p_adj, "reproducible": reproducible, "dq": dq, "reason": reason,
                       "recorded_manifest": recorded_manifest, "recorded": recorded, "entry": entry, "prior": prior,
                       "origin": origin, "iteration": iteration, "parent": parent, "prior_caveats": prior_caveats,
                       "population": population, "facts": facts, "binding": binding, "receipts": receipts, "alpha": alpha,
                       "primary_family": primary_family, "review_payload": review_payload, "discovery": discovery,
                       "hypothesis_id": hypothesis_id})
    reviews = independent_reviews(ctx, states)
    for st in states:
        (insight_id, code, spec, spec_d, stat_d, finding, title, narrative_source, checks, second, p_adj, reproducible, dq,
         reason, recorded_manifest, recorded, entry, prior, origin, iteration, parent, prior_caveats, population, facts,
         binding, receipts, alpha) = (st[k] for k in (
            "insight_id", "code", "spec", "spec_d", "stat_d", "finding", "title", "narrative_source", "checks", "second",
            "p_adj", "reproducible", "dq", "reason", "recorded_manifest", "recorded", "entry", "prior", "origin", "iteration",
            "parent", "prior_caveats", "population", "facts", "binding", "receipts", "alpha"))
        review, review_model = reviews.get(code, (None, "unavailable"))
        ctx.check_control()
        # ADR-0015 escalate_only: the second opinion can add doubt (lower confidence), never add credit.
        second_opinion = ctx.decisions.decide(
            "rev_second_opinion", {"claim": finding, "evidence": str({k: stat_d.get(k) for k in (
                "test", "n", "p_adjusted", "effect_size", "effect_label", "highlights")})[:3000]},
            Question.escalation("Does `evidence` support `claim` as worded, without overreach?", levels=["none", "doubt"],
                                baseline="none", escalate_to="doubt", escalate_at=0.3, escalate_when="low"),
            ctx=ctx.call_ctx(), subject=f"insight:{insight_id}")
        jev = second_opinion if second_opinion.by_model and second_opinion.p is not None else None
        # ---- Contradictions
        key = (spec.asset, (spec_d.get("outcome") or {}).get("column"), (spec_d.get("segment") or {}).get("column"))
        top = (stat_d.get("highlights") or {}).get("top_segment")
        contradiction = [c for c, t in directions.get(key, []) if t != top and spec.filters == []]
        directions.setdefault(key, []).append((code, top))
        deterministic_ok = all(c["passed"] for c in checks)
        conf = 0.5
        if p_adj is not None:
            conf += 0.15 if p_adj < 0.001 else 0.1 if p_adj < 0.01 else 0.0
        conf += 0.1 if any(c["check"] == "second_method" and c["passed"] for c in checks) else -0.2
        conf += 0.1 if reproducible else -0.3
        caveats_extra = []
        if isinstance(review, dict):
            conf += 0.05 if review.get("supports") else -0.1
            if review.get("suggested_caveat"):
                caveats_extra.append(f"Reviewer: {str(review['suggested_caveat'])[:200]}")
        if second_opinion.value == "doubt":
            conf -= 0.1
        if dq:
            caveats_extra.append("Data quality: " + "; ".join(dq[:2]))
            conf -= 0.05
        if contradiction:
            caveats_extra.append(f"Contrasts with {', '.join(contradiction)} on the same outcome/segment.")
        # ---- Evidence bundle (P4-03): dimensions, missing-evidence refusal, discovery vs confirmation
        hl = stat_d.get("highlights") or {}
        pair = next((f for f in facts if f.primary), None)
        top = hl.get("top_segment", hl.get("top_driver"))
        direction = pair.direction if pair is not None else None
        method_impl = methods.get(spec.method) if spec.method in methods.names() else None
        strength = grade(stat_d, alpha=alpha)
        # P8-15: only a verified claim is locked and taken to the held-out rows; a failed one never reads them.
        holdout = confirm_on_holdout(ctx, st, top, direction) if deterministic_ok else None
        confirmation = confirm(verified=deterministic_ok, origin=origin, top=top, direction=direction, prior=prior,
                               current=recorded or entry, holdout=holdout.model_dump(mode="json") if holdout else None)
        if holdout is not None:
            holdout = holdout.model_copy(update={"confirmed": confirmation.rule == "holdout_partition"})
        if deterministic_ok:  # a weak or unconfirmed finding is not worded as an established fact
            finding = qualify(finding, confirmed=confirmation.passed, strength=strength.label if strength else None)
        bundle = assemble(
            spec=spec_d, stat=stat_d, second=_dump(second.stat) if second is not None else None, facts=facts,
            binding=binding, rev_checks=checks, receipts=receipts, manifest=recorded_manifest, entry=recorded or entry,
            population=population, caveats=list(dict.fromkeys(prior_caveats + caveats_extra)), confirmation=confirmation,
            replicated=deterministic_ok and is_replication(origin, prior, recorded or entry, top),
            reproducible=reproducible, review_score=None, family_size=int(stat_d.get("bh_family_size") or 0),
            alpha=alpha, origin=origin, iteration=iteration, parent=parent,
            claim_meta={"subject": top, "baseline": hl.get("baseline_segment"), "direction": direction,
                        "metric": (spec_d.get("outcome") or {}).get("label") or (spec_d.get("outcome") or {}).get("column"),
                        "spec_hash": spec_hash(spec_d), "title": title, "text": finding,
                        "rendered_from": narrative_source},
            optional=tuple(getattr(method_impl, "evidence_optional", ()) or ()),
            predictive=isinstance(hl.get("holdout_roc_auc"), int | float), strength=strength, holdout=holdout)
        if bundle.validation.missing_evidence:
            deterministic_ok = False
            ctx.say(f"REV {code}: refused, missing evidence: {', '.join(bundle.validation.missing_evidence)}.", kind="decision")
        checks.append({"check": "evidence_complete", "passed": not bundle.validation.missing_evidence,
                       "detail": ", ".join(bundle.validation.missing_evidence) or "all required dimensions present"})
        conf = max(0.0, min(0.99, conf)) if deterministic_ok else min(conf, 0.4)
        bundle.review_score.value = round(conf, 3)
        verification = {"reason": reason, "evaluate": checks, "verify": {
            "reproducible": reproducible, "second_method": _dump(second.stat) if second else None,
            "independent_model": {"model": review_model, "review": review} if isinstance(review, dict) else {"unavailable": review_model},
            "jev": {"p_supports": jev.p, "model": jev.model, "by": jev.backend, "decision_id": jev.id} if jev else None,
            "contradictions": contradiction}, "verified": deterministic_ok,
            "note": "Verification is grounded in deterministic checks and reproducible data; model opinions only adjust confidence."}
        if second is not None:
            ctx.check_output("experiment", {"method": spec.method, "params": {"verification_of": code},
                                            "result": _dump(second.stat), "query_ids": list(second.query_ids),
                                            "role": "verification"})
        with session_scope() as s:
            ins = s.get(Insight, insight_id)
            # N-8: measured and document evidence cited as separate kinds; a document claim that disagrees
            # with the measurement is a caveat (measured data wins), never a check that could verify or fail.
            cited = _citations(s, ins, bundle.model_dump(mode="json"), title, finding, narrative_source)
            if cited is not None:
                caveats_extra += [c.caveat() for c in cited.conflicts]
                bundle.claim["citations"] = cited.summary()
                verification["verify"]["citations"] = {**cited.summary(),
                                                       "conflicts": [c.model_dump(mode="json") for c in cited.conflicts]}
            ins.finding = finding
            ins.narrative_source = narrative_source
            ins.verified = deterministic_ok
            ins.verification = verification
            ins.confidence = round(conf, 3)
            ins.caveats = list(dict.fromkeys((ins.caveats or []) + caveats_extra))
            ins.status = "verified" if deterministic_ok else "failed_verification"
            ins.claim_label = label_for(spec.method, verified=deterministic_ok, checks=checks,
                                        strength=strength.label if strength else None)
            ins.title = title[:200]
            ins.evidence_bundle = bundle.model_dump(mode="json")
            ins.validation = bundle.validation.state
            ins.data_version = (bundle.data.get("manifest") or {}).get("version") or ins.data_version
            if second is not None:
                vexp = Experiment(id=new_id("exp"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=ins.hypothesis_id,
                                  method=spec.method, params={"verification_of": code}, result=_dump(second.stat),
                                  query_ids=list(second.query_ids), role="verification")
                s.add(vexp)
                link(s, ctx.workspace.id, ("insight", ins.id), "verified_by", ("experiment", vexp.id), run_id=ctx.run.id)
                link_queries(s, ctx.workspace.id, ("experiment", vexp.id), list(second.query_ids), run_id=ctx.run.id,
                             assets=[spec.asset] if spec.asset else None)
            if not deterministic_ok:
                h = s.get(Hypothesis, ins.hypothesis_id)
                h.status = "inconclusive"
            # P7-01 (ADR-0020): the verdict is bound to the versions it depended on; any change voids it.
            record = record_verdict(
                s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, subject_type="insight", subject_id=ins.id,
                verdict="verified" if deterministic_ok else "failed_verification", checks=checks,
                verifier=bundle.verifier_version, question_hash=spec_hash(spec_d), evidence_bundle=ins.evidence_bundle,
                dependencies=insight_dependencies(
                    s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=ins.hypothesis_id, spec=spec_d,
                    entry=recorded or entry, narrative_source=narrative_source, manifest=recorded_manifest))
            if cited is not None:
                record_citations(s, cited, workspace_id=ctx.workspace.id, run_id=ctx.run.id)
            emit(ctx.workspace.id, "insight.verified", {"code": code, "verified": deterministic_ok, "confidence": ins.confidence,
                                                        "validation": bundle.validation.state, "label": bundle.validation.label,
                                                        "claim_label": ins.claim_label,
                                                        "strength": strength.label if strength else None,
                                                        "failed_checks": [c["check"] for c in checks if not c["passed"]],
                                                        "verification_record": record.id, "fingerprint": record.fingerprint},
                 run_id=ctx.run.id, session=s)
        (verified_codes if deterministic_ok else failed_codes).append(code)
        ctx.say(f"REV {code}: {'VERIFIED' if deterministic_ok else 'FAILED'} ({bundle.validation.state}; review score "
                f"{conf:.2f}, uncalibrated); "
                + ", ".join(f"{c['check']}={'ok' if c['passed'] else 'fail'}" for c in checks), kind="decision")
    return {"verified": verified_codes, "failed": failed_codes}

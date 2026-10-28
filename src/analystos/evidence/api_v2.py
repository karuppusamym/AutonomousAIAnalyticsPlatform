"""Evidence API v2 (spec S07-T4, v1 §7.3): one finding as the `EvidenceV2` contract.

Nothing new is stored: the object is read from the `Insight` (claim, label, caveats, typed evidence bundle),
its primary `Experiment` (method, statistic), its newest `VerificationRecord` (review status, fingerprint,
void cause), the run's data manifest (dataset versions, coverage), the run's binding (method version) and
the finding's citation set. A run's evidence is built in a fixed number of statements, whatever its size.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.evidence import (
    EvidenceDatasetVersion,
    EvidenceEffect,
    EvidenceMethod,
    EvidenceSample,
    EvidenceV2,
)
from analystos.core.ids import stable_hash


def claim_id(spec: Mapping[str, Any], highlights: Mapping[str, Any] | None) -> str:
    """The claim's identity across runs (`services.changes.claim_key`): wording is not part of it."""
    from analystos.services.changes import claim_key

    return "clm_" + stable_hash([str(p) for p in claim_key(dict(spec or {}), dict(highlights or {}))])[:24]


def _method_version(run: Any, name: str | None) -> str | None:
    if not name:
        return None
    prefix = f"method.{name}@"
    for ref in (getattr(run, "capabilities", None) or {}).get("refs") or []:
        if isinstance(ref, str) and ref.startswith(prefix):
            return ref[len(prefix):]
    from analystos import methods

    m = methods.current().manifests.get(name)
    return m.version if m is not None else None


def _datasets(manifest: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[list[EvidenceDatasetVersion], float | None]:
    from analystos.evidence.manifest import spec_assets

    entries = {e.get("asset"): e for e in (manifest or {}).get("entries") or [] if isinstance(e, Mapping)}
    out, coverage = [], None
    for asset in spec_assets(dict(spec or {})):
        e = entries.get(asset) or {}
        out.append(EvidenceDatasetVersion(asset=asset, source_id=e.get("source_id"), version=e.get("version"),
                                          mode=e.get("mode") or "unknown", rows=e.get("rows"), staged_at=e.get("staged_at")))
        if asset == spec.get("asset") and e.get("rows") is not None and e.get("source_total_rows"):
            coverage = round(min(1.0, float(e["rows"]) / float(e["source_total_rows"])), 6)
    return out, coverage


def _metric_version(rec: Any, ins: Any) -> dict[str, str] | None:
    for d in (getattr(rec, "dependencies", None) or []):
        if d.get("kind") == "semantic":
            ref = str(d.get("ref") or "")
            return {"metric": ref.split("/", 1)[1] if "/" in ref else ref, "version": str(d.get("version_hash"))}
    sem = getattr(ins, "semantic", None) or {}
    if sem.get("metric") and sem.get("version"):
        return {"metric": str(sem["metric"]), "version": str(sem["version"])}
    return None


def _limitations(ins: Any, stat: Mapping[str, Any], rec: Any, gates: Iterable[str]) -> list[str]:
    out = list(ins.caveats or [])
    out += [f"REV check failed: {c.get('check')} ({c.get('detail') or ''})".rstrip(" ()")
            for c in (getattr(rec, "checks", None) or []) if isinstance(c, Mapping) and not c.get("passed")]
    out += list(gates)
    out += [w for w in stat.get("warnings") or [] if isinstance(w, str)]
    return list(dict.fromkeys(x for x in out if x))[:40]


def build(session: Session, insights: list[Any], run: Any) -> list[EvidenceV2]:
    from analystos.db.models import EvidenceCitationSet, Experiment, Hypothesis
    from analystos.evidence.claims import claim_kind, label_of
    from analystos.evidence.investigation import run_limitations
    from analystos.evidence.verification import latest, state_of

    if not insights:
        return []
    hyp_ids = [i.hypothesis_id for i in insights if i.hypothesis_id]
    hyps = {h.id: h for h in session.scalars(select(Hypothesis).where(Hypothesis.id.in_(hyp_ids)))} if hyp_ids else {}
    exps = {e.hypothesis_id: e for e in session.scalars(select(Experiment).where(Experiment.hypothesis_id.in_(hyp_ids),
                                                                                  Experiment.role == "primary"))} if hyp_ids else {}
    records = latest(session, "insight", [i.id for i in insights])
    cites = {c.subject_id: c for c in session.scalars(select(EvidenceCitationSet).where(
        EvidenceCitationSet.subject_type == "insight", EvidenceCitationSet.subject_id.in_([i.id for i in insights])))}
    gates = run_limitations(session, run)
    out = []
    for ins in insights:
        h, exp = hyps.get(ins.hypothesis_id), exps.get(ins.hypothesis_id)
        spec = dict((h.spec if h is not None else None) or {})
        stat = dict((exp.result if exp is not None else None) or {})
        rec = records.get(ins.id)
        state = state_of(rec)
        datasets, coverage = _datasets(run.data_manifest or {}, spec)
        ci = ({"low": stat.get("ci_low"), "high": stat.get("ci_high")}
              if stat.get("ci_low") is not None or stat.get("ci_high") is not None else None)
        qids = [e["id"] for e in ins.evidence or [] if isinstance(e, Mapping) and e.get("type") == "query" and e.get("id")]
        cs = cites.get(ins.id)
        out.append(EvidenceV2(
            evidence_id=ins.id, run_id=ins.run_id, claim_id=claim_id(spec, stat.get("highlights")), code=ins.code,
            label=label_of(ins), title=ins.title or "", description=ins.finding or "", dataset_versions=datasets,
            metric_version=_metric_version(rec, ins), query_execution_ids=list(dict.fromkeys(qids or (exp.query_ids if exp else []))),
            method=EvidenceMethod(name=spec.get("method"), version=_method_version(run, spec.get("method")),
                                  test=stat.get("test"), claim_kind=claim_kind(spec.get("method"))),
            effect=EvidenceEffect(value=stat.get("effect_size"), unit=stat.get("effect_label"), ci=ci),
            sample=EvidenceSample(n=int(stat.get("n") or ins.population_size or 0), coverage=coverage),
            limitations=_limitations(ins, stat, rec, gates), review_status=state["badge"], validation=ins.validation,
            fingerprint=state.get("fingerprint"), verification_record_id=state.get("record_id"),
            void_reason=((state.get("void") or {}).get("reason") if state.get("state") == "VOID" else None),
            citations=({"quantitative": cs.quantitative_count, "documents": cs.document_count,
                        "conflicts": cs.conflict_count} if cs is not None else None)))
    return out


def for_run(session: Session, run: Any) -> list[dict[str, Any]]:
    from analystos.db.models import Insight, by_code

    insights = list(session.scalars(select(Insight).where(Insight.run_id == run.id).order_by(*by_code(Insight.code))))
    return [e.model_dump(mode="json") for e in build(session, insights, run)]


def for_insight(session: Session, ins: Any) -> dict[str, Any]:
    from analystos.db.models import AnalysisRun

    return build(session, [ins], session.get(AnalysisRun, ins.run_id))[0].model_dump(mode="json")


__all__ = ["build", "claim_id", "for_insight", "for_run"]

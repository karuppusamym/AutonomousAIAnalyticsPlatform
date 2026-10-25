"""Verification records that void themselves (P7-01, ADR-0020).

A verdict is bound to what it depended on. Each dependency is a typed pair ``(kind, ref)`` with the
version it had when REV decided, and the record's fingerprint is ``sha256(canonical_json(sorted(deps)))``
(the approval-hash format, `core.ids.stable_hash`):

* ``query``      ref = the step: a hypothesis whose `AnalysisSpec` was compiled (version = spec hash +
                 normalized SQL hashes of its primary queries + compiler + dialect), or ``step:<id>`` for a
                 step object (P7-04; version = its current version row, replaced by every execution);
* ``data``       ref = ``<source_id>/<schema.table>``; version = the run's manifest entry (P4-03);
* ``semantic``   ref = ``<workspace_id>/<metric>``; version = the approved version of that KPI (or none);
* ``method``     ref = the method name; version = manifest id + version + digest of its code;
* ``context``    ref = a glossary entry / knowledge document id; version = its content hash;
* ``model_call`` ref = a model call id whose output survived into the claim; version = purpose, model, prompt;
* ``policy``     ref = the workspace id; version = the policy fields that shape scope, masking and REV;
* ``ml_dataset`` ref = ``<workspace_id>/<snapshot hash>``; version = the snapshot hash while its file verifies;
* ``ml_split``   ref = the split manifest row; version = its manifest hash;
* ``ml_package`` ref = the experiment; version = the sha256 of its package file now (tampering voids).

Every change path calls `dependency_changed` (or `void_dependents` when it already knows the new version)
in the transaction that makes the change; dependents found through the indexed ``(kind, ref)`` table turn
``VOID`` with the cause. The nightly `sweep` recomputes every live record's dependencies and voids what the
events missed; each such void is a *late void*, counted as a defect. Consumers read `state_of`, never the
old `verified` boolean alone. A void is never hidden and never carried forward: re-verification writes a
new record.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger

log = get_logger(__name__)

KINDS = ("query", "data", "semantic", "method", "context", "model_call", "policy", "ml_dataset", "ml_split", "ml_package")
LIVE = ("PENDING", "ACTIVE")
QUERY_COMPILER = "sqlbuild.v1"
# Policy fields that shape what a verdict saw (scope, masking, truncation) or how REV judged it (alpha).
POLICY_FIELDS = ("restricted_columns", "pii_columns", "pii_access", "attribute_rules", "max_rows", "alpha")
CONTEXT_SECTIONS = ("glossary", "business_rules", "metrics", "reviewed_business_context", "resolved_meaning")
SWEEP_EVERY = timedelta(hours=23)
SWEEP_LOCK = 0x5EC0_0701
UNKNOWABLE = "__unknowable__"  # the dependency has no fixed version now (pushdown data): never voids


@dataclass(frozen=True, order=True)
class Dependency:
    kind: str
    ref: str
    version_hash: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "version_hash": self.version_hash}


def fingerprint(deps: Iterable[Dependency | Mapping[str, Any]]) -> str:
    items = [d.as_dict() if isinstance(d, Dependency) else {k: str(d[k]) for k in ("kind", "ref", "version_hash")}
             for d in deps]
    return stable_hash(sorted(items, key=lambda d: (d["kind"], d["ref"], d["version_hash"])))


# ------------------------------------------------------------------------------------ current versions
def _spec_columns(spec: Mapping[str, Any], keys: Iterable[str] = ("outcome", "segment", "time")) -> set[str]:
    out: set[str] = set()
    derivations = [spec.get(k) for k in keys] + (list(spec.get("drivers") or []) if "drivers" in keys else [])
    for d in derivations:
        if isinstance(d, Mapping):
            out |= {str(d[c]) for c in ("column", "end_column") if d.get(c)}
        elif isinstance(d, str):
            out.add(d)
    return out


def _query_version(session: Session, ref: str) -> str | None:
    from analystos.connectors.kinds import dialect_for
    from analystos.db.models import Experiment, Hypothesis, QueryExecution, Source
    from analystos.knowledge.attested import sql_hash
    from analystos.registries.hypotheses import spec_hash

    if ref.startswith("step:"):  # a step (P7-04): its current version, which every execution replaces
        from analystos.db.models import AnalysisStep, AnalysisStepVersion

        step = session.get(AnalysisStep, ref[len("step:"):])
        if step is None:
            return None
        return session.scalar(select(AnalysisStepVersion.id).where(AnalysisStepVersion.step_id == step.id,
                                                                   AnalysisStepVersion.version == step.current_version))
    h = session.get(Hypothesis, ref)
    if h is None:
        return None
    exp = session.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary")
                         .order_by(Experiment.created_at.desc()).limit(1))
    ids = list((exp.query_ids if exp is not None else None) or [])
    queries = list(session.scalars(select(QueryExecution).where(QueryExecution.id.in_(ids)))) if ids else []
    dialects = set()
    for sid in sorted({q.source_id for q in queries if q.source_id}):
        src = session.get(Source, sid)
        try:
            dialects.add(dialect_for(src.kind, src.execution_mode) if src is not None else "unknown")
        except Exception:  # noqa: BLE001 - an unregistered kind still has an identity
            dialects.add(f"kind:{src.kind}")
    return stable_hash({"compiler": QUERY_COMPILER, "spec": spec_hash(dict(h.spec or {})),
                        "sql": sorted(sql_hash(q.executed_sql or q.sql) for q in queries), "dialects": sorted(dialects)})


def _data_version(session: Session, ref: str) -> str | None:
    from analystos.evidence.manifest import current_entry

    source_id, _, asset = ref.partition("/")
    entry = current_entry(session, asset, source_id or None)
    if entry.mode != "staged" or not entry.version:
        return UNKNOWABLE  # pushdown or unversioned now: nothing to compare (P4-03 limit)
    return entry.version


def _semantic_version(session: Session, ref: str) -> str | None:
    from analystos.db.models import SemanticMetric

    workspace_id, _, name = ref.partition("/")
    row = session.scalar(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id, SemanticMetric.name == name,
                                                      SemanticMetric.status == "approved")
                         .order_by(SemanticMetric.version.desc()).limit(1))
    return stable_hash({"approved": {"version": row.version, "hash": row.content_hash} if row is not None else None})


_digests: dict[tuple[str, int, int], str] = {}


def _code_digest(entry: str | None) -> str:
    """sha256 of the module behind a `python:` entry (cached per file mtime and size)."""
    if not entry or not entry.startswith("python:"):
        return "none"
    module = entry[len("python:"):].split(":", 1)[0]
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.origin or not os.path.isfile(spec.origin):
        module = module.rsplit(".", 1)[0]  # python:module.attr form
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            return "unresolvable"
        if spec is None or not spec.origin or not os.path.isfile(spec.origin):
            return "unresolvable"
    st = os.stat(spec.origin)
    key = (spec.origin, st.st_mtime_ns, st.st_size)
    if key not in _digests:
        with open(spec.origin, "rb") as fh:
            _digests[key] = hashlib.sha256(fh.read()).hexdigest()
    return _digests[key]


def _method_version(session: Session, ref: str) -> str | None:
    from analystos import methods

    m = methods.current().manifests.get(ref)
    if m is None:
        if ref.startswith("ml."):  # the ML method pack (P5-04): manifest + digest of the whole ml package
            from analystos.ml.methods import method_version

            return method_version(ref)
        return None
    return stable_hash({"id": m.id, "version": m.version, "code": _code_digest(m.entry)})


def _entry_hash(e: Any) -> str:
    return stable_hash({"kind": e.kind, "name": e.name, "body": e.body, "synonyms": list(e.synonyms or []),
                        "mapped_columns": list(e.mapped_columns or [])})


def _context_version(session: Session, ref: str) -> str | None:
    from datetime import date

    from analystos.db.models import ContextBinding, ContextEntry, KnowledgeDocument, WorkspaceBrief

    if ref.startswith("brief:"):
        _, workspace_id, key = ref.split(":", 2)
        brief = session.scalar(select(WorkspaceBrief).where(WorkspaceBrief.workspace_id == workspace_id)
                               .order_by(WorkspaceBrief.version.desc()).limit(1))
        fact = next((f for f in (brief.assertions if brief else []) if f.get("key") == key), None)
        return stable_hash(fact) if fact else None
    if ref.startswith("bind_"):
        binding = session.get(ContextBinding, ref)
        if binding:
            d = binding.definition
            today = date.today().isoformat()
            return stable_hash({"revision": binding.revision, "definition": d,
                                "effective": (not d.get("expires_on") or d["expires_on"] >= today)
                                             and (not d.get("effective_from") or d["effective_from"] <= today)})

    e = session.get(ContextEntry, ref)
    if e is not None:
        return _entry_hash(e)
    d = session.get(KnowledgeDocument, ref)
    return d.sha256 if d is not None else None


def _model_call_version(session: Session, ref: str) -> str | None:
    from analystos.db.models import ModelCall

    call = session.get(ModelCall, int(ref)) if ref.isdigit() else None
    if call is None:
        return None
    return stable_hash({"purpose": call.purpose, "model": call.model, "prompt": call.prompt_version})


def _policy_version(session: Session, ref: str) -> str | None:
    from analystos.db.models import Workspace
    from analystos.governance.policy import load_policy

    ws = session.get(Workspace, ref)
    if ws is None:
        return None
    doc = load_policy(session, ws).model_dump(mode="json")
    return stable_hash({k: doc.get(k) for k in POLICY_FIELDS})


def _ml_dataset_version(session: Session, ref: str) -> str | None:
    from analystos.core.config import get_settings
    from analystos.core.errors import AnalystOSError
    from analystos.ml.store import snapshots

    digest = ref.rsplit("/", 1)[-1]
    try:
        snapshots(get_settings().artifact_dir).get(digest)
    except AnalystOSError:
        return None
    return digest


def _ml_split_version(session: Session, ref: str) -> str | None:
    from analystos.db.models import MLSplit

    row = session.get(MLSplit, ref)
    return row.manifest_hash if row is not None else None


def _ml_package_version(session: Session, ref: str) -> str | None:
    from analystos.core.config import get_settings
    from analystos.db.models import MLExperiment
    from analystos.ml.store import MLStore

    exp = session.get(MLExperiment, ref)
    if exp is None or not exp.package_hash:
        return None
    return MLStore(get_settings().artifact_dir).file_hash(exp.package_hash)


RESOLVERS = {"query": _query_version, "data": _data_version, "semantic": _semantic_version, "method": _method_version,
             "context": _context_version, "model_call": _model_call_version, "policy": _policy_version,
             "ml_dataset": _ml_dataset_version, "ml_split": _ml_split_version, "ml_package": _ml_package_version}


def current_version(session: Session, kind: str, ref: str) -> str | None:
    """The dependency's version now: a hash, None when it no longer exists, or `UNKNOWABLE`."""
    if kind not in RESOLVERS:
        raise InvalidInput(f"unknown verification dependency kind {kind!r}; expected one of {KINDS}")
    return RESOLVERS[kind](session, ref)


# ------------------------------------------------------------------------------------ what a finding depends on
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _metric_columns(row: Any) -> set[str]:
    defn = row.definition or {}
    cols = {str(c).rsplit(".", 1)[-1] for c in defn.get("source_columns") or []}
    return cols | set(_IDENT.findall(row.expression or ""))


def metrics_for(session: Session, workspace_id: str, spec: Mapping[str, Any]) -> list[str]:
    """Live KPIs of the workspace semantic model that measure the finding's outcome."""
    from analystos.db.models import SemanticMetric

    outcome = _spec_columns(spec, ("outcome",))
    if not outcome:
        return []
    rows = session.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id,
                                                        SemanticMetric.status.in_(("approved", "proposed"))))
    return sorted({r.name for r in rows if _metric_columns(r) & outcome})


def cited_context(session: Session, run_id: str, spec: Mapping[str, Any], calls: Iterable[Any] = ()) -> list[str]:
    """Knowledge a finding stood on: glossary terms the run resolved onto the claim's columns, and the
    glossary / business-rule / metric sections carried by the model calls whose wording survived."""
    from analystos.db.models import Artifact

    columns = _spec_columns(spec, ("outcome", "segment", "time", "drivers"))
    ids: set[str] = set()
    package = session.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "context_package")
                             .order_by(Artifact.created_at.desc()).limit(1))
    for term in ((package.content or {}).get("resolved_terms") or []) if package is not None else []:
        if term.get("id") and {str(c).rsplit(".", 1)[-1] for c in term.get("columns") or []} & columns:
            ids.add(str(term["id"]))
    if package is not None:
        for r in (package.content or {}).get("grounding", {}).get("resolutions", []):
            if r.get("state") == "resolved" and (r.get("kind") == "population_rule" or
                    {str(c).rsplit(".", 1)[-1] for c in r.get("columns", [])} & columns):
                ids.add(str(r["id"]))
                ids.update(str(i) for i in r.get("evidence_ids", []))
        pinned = (package.content or {}).get("reviewed_context") or {}
        for fact in pinned.get("facts", []):
            if fact.get("key"):
                # The context package's own workspace is the authoritative receipt namespace.
                ids.add(f"brief:{package.workspace_id}:{fact['key']}")
    for call in calls:
        for r in call.context_receipts or []:
            if r.get("section") in CONTEXT_SECTIONS:
                ids.add(str(r.get("document_id") or r.get("id")))
    return sorted(ids)


def narrative_calls(session: Session, run_id: str, narrative_source: str | None) -> list[Any]:
    """The model calls whose wording is the claim (none when the template wrote it)."""
    from analystos.db.models import ModelCall

    if not narrative_source or not narrative_source.startswith("llm:"):
        return []
    model = narrative_source.split(":", 1)[1]
    return list(session.scalars(select(ModelCall).where(ModelCall.run_id == run_id, ModelCall.purpose == "insight_narrative",
                                                        ModelCall.status == "ok", ModelCall.model == model)
                                .order_by(ModelCall.id)))


def insight_dependencies(session: Session, *, workspace_id: str, run_id: str, hypothesis_id: str | None,
                         spec: Mapping[str, Any], entry: Any | None, narrative_source: str | None,
                         manifest: Mapping[str, Any] | None = None) -> list[Dependency]:
    """Every dependency of a finding's verdict, at the versions REV saw. A dependency whose version
    cannot be resolved now is left out (it could never be compared later)."""
    wanted: list[tuple[str, str]] = []
    if hypothesis_id:
        wanted.append(("query", hypothesis_id))
    if spec.get("method"):
        wanted.append(("method", str(spec["method"])))
    wanted.append(("policy", workspace_id))
    wanted += [("semantic", f"{workspace_id}/{name}") for name in metrics_for(session, workspace_id, spec)]
    calls = narrative_calls(session, run_id, narrative_source)
    wanted += [("context", cid) for cid in cited_context(session, run_id, spec, calls)]
    expected_context = {}
    if hypothesis_id:
        from analystos.db.models import Hypothesis

        hypothesis = session.get(Hypothesis, hypothesis_id)
        provenance = (hypothesis.provenance or {}) if hypothesis else {}
        for receipt in provenance.get("mapping_receipts", []):
            if receipt.get("version_hash"):
                expected_context[receipt["id"]] = receipt["version_hash"]
        for receipt in provenance.get("question_receipts", []):
            if receipt.get("version_hash"):
                expected_context[receipt["id"]] = receipt["version_hash"]
        for receipt in provenance.get("brief_receipts", []):
            expected_context[receipt["id"]] = receipt["sha256"]
        wanted += [("context", cid) for cid in expected_context]
    wanted += [("model_call", str(c.id)) for c in calls]
    deps = []
    from analystos.contracts.evidence import ManifestEntry
    from analystos.evidence.manifest import spec_assets

    recorded = [ManifestEntry.model_validate(e) for e in (manifest or {}).get("entries") or []
                if e.get("asset") in spec_assets(spec)]
    if not recorded and entry is not None:
        recorded = [entry]
    for e in recorded:  # the versions the claim read, not today's versions
        deps.append(Dependency("data", f"{e.source_id or ''}/{e.asset}",
                               e.version or f"unversioned:{e.mode}"))
    for kind, ref in dict.fromkeys(wanted):
        v = expected_context[ref] if kind == "context" and ref in expected_context else current_version(session, kind, ref)
        if v is not None and v != UNKNOWABLE:
            deps.append(Dependency(kind, ref, v))
    return sorted(set(deps))


# ------------------------------------------------------------------------------------ lifecycle
def record_verdict(session: Session, *, workspace_id: str, run_id: str | None, subject_type: str, subject_id: str,
                   verdict: str, checks: list[dict[str, Any]], verifier: str, dependencies: Iterable[Dependency],
                   question_hash: str | None = None, evidence_bundle: Mapping[str, Any] | None = None,
                   state: str = "ACTIVE") -> Any:
    """A new record for a verdict; any live record of the same subject becomes SUPERSEDED."""
    from analystos.db.models import VerificationDependency, VerificationRecord
    from analystos.events.bus import emit

    deps = sorted(set(dependencies))
    rec = VerificationRecord(id=new_id("ver"), workspace_id=workspace_id, run_id=run_id, subject_type=subject_type,
                             subject_id=subject_id, question_hash=question_hash, verdict=verdict, checks=list(checks),
                             verifier=verifier, fingerprint=fingerprint(deps), dependencies=[d.as_dict() for d in deps],
                             evidence_bundle_id=f"sha256:{stable_hash(dict(evidence_bundle))}" if evidence_bundle else None,
                             state=state, flags=[], created_at=utcnow())
    for old in session.scalars(select(VerificationRecord).where(VerificationRecord.subject_type == subject_type,
                                                                VerificationRecord.subject_id == subject_id,
                                                                VerificationRecord.state.in_(LIVE))):
        old.state, old.superseded_by = "SUPERSEDED", rec.id
    session.add(rec)
    session.flush()  # the record row first: dependency rows reference it
    session.add_all([VerificationDependency(record_id=rec.id, kind=d.kind, ref=d.ref, version_hash=d.version_hash) for d in deps])
    session.flush()
    emit(workspace_id, "verification.recorded", {"record_id": rec.id, "subject_type": subject_type, "subject_id": subject_id,
                                                 "verdict": verdict, "state": state, "fingerprint": rec.fingerprint,
                                                 "dependencies": len(deps)}, run_id=run_id, session=session)
    return rec


def supersede_subjects(session: Session, subject_type: str, subject_ids: Iterable[str], reason: str) -> int:
    """The subjects themselves were replaced (a replan superseded the run's findings)."""
    from analystos.db.models import VerificationRecord

    ids = list(subject_ids)
    if not ids:
        return 0
    n = 0
    for rec in session.scalars(select(VerificationRecord).where(VerificationRecord.subject_type == subject_type,
                                                                VerificationRecord.subject_id.in_(ids),
                                                                VerificationRecord.state.in_(LIVE))):
        rec.state, rec.void_detail = "SUPERSEDED", {"reason": reason}
        n += 1
    return n


def _void(session: Session, rec: Any, *, kind: str, ref: str, recorded: str, current: str | None, reason: str,
          event: str | None, late: bool) -> None:
    from analystos.events.bus import emit

    rec.state, rec.voided_at, rec.void_kind, rec.void_reason = "VOID", utcnow(), kind, reason
    rec.void_detail = {"kind": kind, "ref": ref, "recorded": recorded, "current": current, "event": event, "late": late}
    emit(rec.workspace_id, "verification.voided", {"record_id": rec.id, "subject_type": rec.subject_type,
                                                   "subject_id": rec.subject_id, "kind": kind, "ref": ref, "reason": reason,
                                                   "event": event, "late": late}, run_id=rec.run_id, session=session)


def void_dependents(session: Session, kind: str, ref: str, new_version: str | None, reason: str, *,
                    event: str | None = None, late: bool = False) -> list[str]:
    """Void every live record that depends on ``(kind, ref)`` at a version other than `new_version`
    (None: the dependency is gone). Call it in the transaction that makes the change. Returns record ids."""
    from analystos.db.models import VerificationDependency, VerificationRecord

    if kind not in KINDS:
        raise InvalidInput(f"unknown verification dependency kind {kind!r}; expected one of {KINDS}")
    if new_version == UNKNOWABLE:
        return []
    session.flush()
    voided: list[str] = []
    rows = session.execute(select(VerificationRecord, VerificationDependency.version_hash)
                           .join(VerificationDependency, VerificationDependency.record_id == VerificationRecord.id)
                           .where(VerificationDependency.kind == kind, VerificationDependency.ref == ref,
                                  VerificationRecord.state.in_(LIVE)))
    for rec, recorded in rows:
        if recorded == new_version or rec.state not in LIVE:
            continue
        _void(session, rec, kind=kind, ref=ref, recorded=recorded, current=new_version, reason=reason, event=event, late=late)
        voided.append(rec.id)
    return voided


def dependency_changed(session: Session, kind: str, ref: str, reason: str, *, event: str | None = None) -> list[str]:
    """A change path's hook: recompute the dependency's version and void what no longer matches."""
    return void_dependents(session, kind, ref, current_version(session, kind, ref), reason, event=event)


def recheck(session: Session, *, kinds: Iterable[str] | None = None, workspace_id: str | None = None,
            reason: str = "dependency changed", event: str | None = None, late: bool = False) -> dict[str, Any]:
    """Recompute every live dependency (optionally of some kinds / one workspace) and void mismatches.
    Used by change paths that touch many refs at once (a knowledge revision, a registry reload) and by
    the sweep."""
    from analystos.db.models import VerificationDependency, VerificationRecord

    session.flush()
    q = (select(VerificationDependency.kind, VerificationDependency.ref).distinct()
         .join(VerificationRecord, VerificationDependency.record_id == VerificationRecord.id)
         .where(VerificationRecord.state.in_(LIVE)))
    if kinds is not None:
        q = q.where(VerificationDependency.kind.in_(list(kinds)))
    if workspace_id is not None:
        q = q.where(VerificationRecord.workspace_id == workspace_id)
    voided: list[str] = []
    by_kind: dict[str, int] = {}
    pairs = sorted(session.execute(q).all())
    for kind, ref in pairs:
        why = _reason(kind, ref, reason)
        ids = void_dependents(session, kind, ref, current_version(session, kind, ref), why, event=event, late=late)
        if ids:
            by_kind[kind] = by_kind.get(kind, 0) + len(ids)
            voided += ids
    return {"dependencies": len(pairs), "voided": voided, "by_kind": by_kind}


def _reason(kind: str, ref: str, reason: str) -> str:
    what = {"query": "step", "data": "data snapshot", "semantic": "metric", "method": "method", "context": "knowledge",
            "model_call": "model call", "policy": "workspace policy", "ml_dataset": "dataset snapshot",
            "ml_split": "split manifest", "ml_package": "model package"}[kind]
    shown = ref.split("/", 1)[1] if kind in ("data", "semantic") and "/" in ref else ref
    return f"{what} {shown} changed ({reason})"


def sweep(session: Session, *, actor: str = "scheduler") -> dict[str, Any]:
    """Recompute every live record's dependencies; anything voided here was missed by its event."""
    from analystos.db.models import VerificationRecord, VerificationSweep
    from analystos.events.bus import emit

    started = utcnow()
    checked = session.scalar(select(func.count(VerificationRecord.id)).where(VerificationRecord.state.in_(LIVE))) or 0
    out = recheck(session, reason="nightly sweep: the change event was missed", event="verification.sweep", late=True)
    row = VerificationSweep(id=new_id("vsw"), actor=actor, checked=int(checked), late_voids=len(out["voided"]),
                            details={"dependencies": out["dependencies"], "by_kind": out["by_kind"],
                                     "voided": out["voided"][:500]}, started_at=started, finished_at=utcnow())
    session.add(row)
    if out["voided"]:
        log.warning("verification sweep: %s late void(s) %s: a change path voided nothing", len(out["voided"]), out["by_kind"])
        for ws in sorted({r.workspace_id for r in session.scalars(select(VerificationRecord)
                                                                  .where(VerificationRecord.id.in_(out["voided"])))}):
            emit(ws, "verification.sweep_completed", {"sweep_id": row.id, "late_voids": len(out["voided"]),
                                                      "by_kind": out["by_kind"]}, session=session)
    return {"sweep_id": row.id, "checked": int(checked), "late_voids": len(out["voided"]), "by_kind": out["by_kind"],
            "voided": out["voided"]}


def maybe_sweep_nightly(session: Session, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Called from the scheduler loop: once a day across scheduler processes (advisory lock)."""
    from analystos.db.models import VerificationSweep

    now = now or utcnow()
    if session.bind is not None and session.bind.dialect.name == "postgresql" and \
            not session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SWEEP_LOCK}):
        return None
    last = session.scalar(select(func.max(VerificationSweep.started_at)).where(VerificationSweep.actor == "scheduler"))
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)  # SQLite drops the zone
    if last is not None and now - last < SWEEP_EVERY:
        return None
    result = sweep(session, actor="scheduler")
    log.info("verification sweep: %s live records, %s late voids", result["checked"], result["late_voids"])
    return result


# ------------------------------------------------------------------------------------ read side
def latest(session: Session, subject_type: str, subject_ids: Iterable[str]) -> dict[str, Any]:
    """The newest record of each subject (VOID and SUPERSEDED included: a void is shown, never hidden)."""
    from analystos.db.models import VerificationRecord

    ids = list(dict.fromkeys(subject_ids))
    if not ids:
        return {}
    out: dict[str, Any] = {}
    for rec in session.scalars(select(VerificationRecord).where(VerificationRecord.subject_type == subject_type,
                                                                VerificationRecord.subject_id.in_(ids))
                               .order_by(VerificationRecord.created_at, VerificationRecord.id)):
        prev = out.get(rec.subject_id)
        if prev is not None and rec.superseded_by == prev.id:
            continue  # the record that replaced this one wins, whatever the clock says
        out[rec.subject_id] = rec
    return out


BADGE = {"ACTIVE": "verified", "PENDING": "pending", "VOID": "void", "SUPERSEDED": "superseded", "LEGACY": "legacy"}


def state_of(rec: Any | None) -> dict[str, Any]:
    """What the UI, reports and exports show. A VOID state always carries its cause."""
    if rec is None:
        return {"state": None, "badge": "unverified", "record_id": None, "void": None}
    badge = BADGE.get(rec.state, rec.state.lower())
    if rec.state == "ACTIVE" and rec.verdict != "verified":
        badge = "failed"
    return {"state": rec.state, "badge": badge, "record_id": rec.id, "verdict": rec.verdict, "verifier": rec.verifier,
            "fingerprint": rec.fingerprint, "dependencies": list(rec.dependencies or []),
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
            "void": ({"kind": rec.void_kind, "reason": rec.void_reason, "at": rec.voided_at.isoformat() if rec.voided_at else None,
                      "detail": rec.void_detail} if rec.state == "VOID" else None),
            "flags": list(rec.flags or [])}


def insight_states(session: Session, insight_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(insight_ids)
    recs = latest(session, "insight", ids)
    return {i: state_of(recs.get(i)) for i in ids}


def is_void(state: Mapping[str, Any] | None) -> bool:
    return bool(state) and state.get("state") == "VOID"


def void_cause(state: Mapping[str, Any] | None) -> str | None:
    """The cause of a VOID state as one line (None when the verdict is not void)."""
    if not is_void(state):
        return None
    v = state.get("void") or {}  # type: ignore[union-attr]
    return f"{v.get('kind') or 'dependency'}: {v.get('reason') or 'a dependency changed'}"


def void_label(state: Mapping[str, Any]) -> str:
    v = state.get("void") or {}
    return f"void ({v.get('kind') or 'dependency'}): {v.get('reason') or 'a dependency changed'}; needs re-verification"


def prior_verdicts(session: Session, workspace_id: str, question_hash: str | None, *, exclude_subject: str | None = None,
                   limit: int = 5) -> list[dict[str, Any]]:
    """Earlier verdicts on the same question, *offered* for comparison: never attached to a new answer."""
    from analystos.db.models import VerificationRecord

    if not question_hash:
        return []
    q = (select(VerificationRecord).where(VerificationRecord.workspace_id == workspace_id,
                                          VerificationRecord.question_hash == question_hash,
                                          VerificationRecord.state != "SUPERSEDED")
         .order_by(VerificationRecord.created_at.desc()).limit(limit + 1))
    out = []
    for rec in session.scalars(q):
        if rec.subject_id == exclude_subject:
            continue
        out.append({**state_of(rec), "subject_type": rec.subject_type, "subject_id": rec.subject_id, "run_id": rec.run_id,
                    "offered": True, "applied": False})
    return out[:limit]


def insight_view(session: Session, ins: Any) -> dict[str, Any]:
    """The verification block of a finding for the API: its record's state and prior verdicts offered."""
    state = insight_states(session, [ins.id])[ins.id]
    qh = None
    if state.get("record_id"):
        from analystos.db.models import VerificationRecord

        qh = session.get(VerificationRecord, state["record_id"]).question_hash
    return {**state, "prior_verdicts": prior_verdicts(session, ins.workspace_id, qh, exclude_subject=ins.id)}


def gate_publish(session: Session, run_id: str, insight_codes: Iterable[str]) -> None:
    """The publish gate: a chart that presents a VOID finding is refused, with the cause."""
    from analystos.db.models import Insight

    codes = sorted(set(insight_codes))
    if not codes:
        return
    rows = {i.id: i for i in session.scalars(select(Insight).where(Insight.run_id == run_id, Insight.code.in_(codes)))}
    states = insight_states(session, rows)
    void = [(rows[i].code, s) for i, s in states.items() if is_void(s)]
    if void:
        raise PolicyDenied(
            "publication refused: " + "; ".join(f"{code} is {void_label(s)}" for code, s in sorted(void)[:5])
            + ". Remedy: re-run the analysis so REV verifies these findings against the current dependencies.",
            details={"void_findings": [{"code": c, "record_id": s["record_id"], "void": s["void"]} for c, s in sorted(void)]})


def flag_wrong(session: Session, ins: Any, *, user_id: str, reason: str | None) -> dict[str, Any]:
    """A person says a finding is wrong: a reason is required and is stored with the record."""
    from analystos.db.models import VerificationRecord
    from analystos.events.bus import emit

    reason = " ".join((reason or "").split())
    if not reason:
        raise InvalidInput("flagging a finding as wrong needs a reason")
    flag = {"by": f"user:{user_id}", "reason": reason[:2000], "at": utcnow().isoformat()}
    rec_id = insight_states(session, [ins.id])[ins.id].get("record_id")
    if rec_id:
        rec = session.get(VerificationRecord, rec_id)
        rec.flags = [*(rec.flags or []), flag]
    emit(ins.workspace_id, "insight.flagged_wrong", {"insight_id": ins.id, "code": ins.code, "record_id": rec_id,
                                                     "reason": flag["reason"]}, run_id=ins.run_id, actor=flag["by"],
         session=session)
    return {"record_id": rec_id, **flag}


__all__ = ["KINDS", "LIVE", "UNKNOWABLE", "Dependency", "current_version", "dependency_changed", "fingerprint", "flag_wrong",
           "gate_publish", "insight_dependencies", "insight_states", "insight_view", "is_void", "latest",
           "maybe_sweep_nightly", "prior_verdicts", "recheck", "record_verdict", "state_of", "supersede_subjects",
           "sweep", "void_dependents", "void_label"]

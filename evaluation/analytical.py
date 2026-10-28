"""Analytical benchmark (P4-V01, spec v3 §10): precision and recall of *verified* findings and the
false-discovery rate against the nominal α, across ITSM, sales and finance, on seeded datasets with
planted effects and null controls (evaluation.datasets).

Two tiers score the same way:

* **component** (no services, CI and the default test suite): generator -> DuckDB -> profiling and
  crawler roles -> the enabled domain packs' templates + the core role playbook (+ the dataset's
  known-null control hypotheses) -> AnalysisSpec validation -> run_analysis -> Benjamini-Hochberg over
  every test -> the independent second method. A finding is *verified* when it is supported, still
  significant after BH and the second method agrees: the gates the critic applies. Many seeds, and
  global-null replicates (every planted effect set to zero), give the FDR estimate.
* **platform** (needs Postgres + the analytics plane): the same datasets uploaded as Parquet sources,
  staged by the loader, analysed by a real run (local orchestrator, the rule path unless models are
  enabled) with every query through the gateway; the run's verified insights are scored.

Scoring: every verified finding is classified against the dataset's truth graph (true = the outcome
and segment share an ancestor; pareto = a planted concentration; trend = a planted trend). Precision =
true / verified; recall = planted effects found (with the planted top segment where one is named) /
planted; FDR = false / max(verified, 1) per replicate, averaged (Benjamini-Hochberg's definition).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

from evaluation.datasets import Dataset, build, node

ALPHA = 0.05
DOMAINS = ("itsm", "sales", "finance")
SCHEMA = "bench"

# CI thresholds for the deterministic benchmark (set before the pilot; spec v3 §10). The FDR bound is
# the nominal α: verified findings must not be false more often than the level the tests are run at.
THRESHOLDS = {"precision_min": 0.90, "recall_min": 0.90, "fdr_max": ALPHA, "null_fdr_max": ALPHA}


@dataclass
class Finding:
    method: str
    outcome: str | None
    segment: str | None
    top: Any
    truth: str  # true | false | unscored
    planted: str | None = None
    statement: str = ""
    p_value: float | None = None
    p_adjusted: float | None = None


@dataclass
class ReplicateScore:
    domain: str
    seed: int
    effects: bool
    tier: str
    tested: int = 0
    null_tests: int = 0
    null_raw_significant: int = 0  # null hypotheses with raw p < α (test calibration, before thresholds)
    verified: int = 0
    true_positive: int = 0
    false_positive: int = 0
    unscored: int = 0
    planted: dict[str, bool] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    seconds: float = 0.0
    run_id: str | None = None
    status: str = "COMPLETED"  # platform tier: how the run ended
    # per analysis method (P4-03 method-specific evaluation): tested / null_tests / null_raw_significant
    tested_by_method: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def fdp(self) -> float:
        return self.false_positive / max(self.true_positive + self.false_positive, 1)


def classify(ds: Dataset, spec: dict[str, Any], top: Any = None, highlights: dict[str, Any] | None = None,
             text: str = "") -> tuple[str, str | None]:
    """(truth, planted id) for one finding on dataset `ds`."""
    method = spec.get("method")
    out, seg = node(spec.get("outcome")), node(spec.get("segment"))
    filtered = bool(spec.get("filters"))
    planted = None
    for p in ds.planted:
        if p.method != method or p.segment != seg or (p.method != "pareto" and p.outcome != out):
            continue
        if p.filtered is not None and p.filtered != filtered:
            continue
        top_ok = p.expect_top is None or (top is not None and str(top) == str(p.expect_top)) or \
            (top is None and p.expect_top.lower() in text.lower())
        planted = p.id if top_ok else None
        break
    if method == "pareto":
        truth = any(p.method == "pareto" and p.segment == seg and (p.filtered or False) == filtered for p in ds.planted)
        return ("true" if truth else "false"), planted
    if method == "trend":
        return ("true" if ds.trend else "false"), None
    if method == "driver_model":
        driver = _driver_node(ds, (highlights or {}).get("top_driver") or top)
        if driver is None or out is None:
            return "unscored", None
        return ("true" if ds.associated(out, driver) else "false"), None
    if out is None or seg is None:
        return "unscored", None
    return ("true" if ds.associated(out, seg) else "false"), planted


def _driver_node(ds: Dataset, label: Any) -> str | None:
    """A driver model names its top driver by label ("reassignment count", "opened after hours");
    map it back to a truth node, or None when it cannot be attributed."""
    if label is None:
        return None
    key = str(label).strip().lower().replace(" ", "_")
    for col in ds.frame.columns:
        if key == col.lower():
            return col
    if "after_hours" in key:
        ts = next((c for c in ds.frame.columns if c in key), None) or next(
            (c for c in ds.frame.columns if str(ds.frame[c].dtype).startswith("datetime")), None)
        return f"after_hours({ts})" if ts else None
    return None


def _score(score: ReplicateScore, ds: Dataset) -> ReplicateScore:
    score.verified = len(score.findings)
    score.true_positive = sum(f.truth == "true" for f in score.findings)
    score.false_positive = sum(f.truth == "false" for f in score.findings)
    score.unscored = sum(f.truth == "unscored" for f in score.findings)
    score.planted = {p.id: any(f.planted == p.id for f in score.findings) for p in ds.planted}
    return score


# ----------------------------------------------------------------------------- component tier
def _duck(ds: Dataset):
    import duckdb

    from evaluation.duck import DuckRunSQL

    con = duckdb.connect()
    con.execute(f"CREATE SCHEMA {SCHEMA}")
    con.register("_tmp", ds.frame)
    con.execute(f"CREATE TABLE {SCHEMA}.{ds.table} AS SELECT * FROM _tmp")
    con.unregister("_tmp")
    return DuckRunSQL(con)


def _describe(run_sql, fq: str):
    from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
    from analystos.skills import catalog as cat
    from analystos.skills.hypothesis_templates import Col
    from analystos.skills.profiling import profile_asset

    schema, name = fq.split(".")
    info = run_sql.con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? "
                               "AND table_name = ? ORDER BY ordinal_position", [schema, name]).fetchall()
    prof = profile_asset(run_sql, fq, [{"name": c, "data_type": t} for c, t in info])
    asset = DiscoveredAsset(source_name=name, name=name, columns=[DiscoveredColumn(name=c, data_type=cat.normalize_type(t))
                                                                  for c, t in info])
    cols = []
    for c in asset.columns:
        p = prof.column(c.name)
        cols.append(Col(name=c.name, semantic_type=p.semantic_type if p else None,
                        role=cat.infer_column_semantics(c, asset).semantic_role, profile=p.model_dump(mode="json") if p else {}))
    return cols


def _top(stat: Any) -> Any:
    hl = stat.highlights or {}
    for key in ("top_segment", "top_driver"):
        if hl.get(key) is not None:
            return hl[key]
    groups = [g for g in stat.groups or [] if g.get("segment") is not None]
    for key in ("rate", "median", "mean", "share"):
        vals = [g for g in groups if g.get(key) is not None]
        if vals:
            return max(vals, key=lambda g: g[key])["segment"]
    return None


def component_tests(domain: str, seed: int, *, effects: bool = True, n: int | None = None,
                    ds: Dataset | None = None) -> tuple[Dataset, Any, list[dict[str, Any]]]:
    """Every valid proposal of the component tier tested once: (dataset, DuckDB runner, [{spec, stat, statement}]).
    Shared by the benchmark below, the grounding suite (evaluation/grounding.py) and the held-out corpus
    (evaluation/heldout, which passes its own `ds`)."""
    from analystos.agents.investigator import proposals_for_table, validate_spec
    from analystos.capabilities import packs
    from analystos.contracts.analysis import AnalysisSpec
    from analystos.skills.analysis import run_analysis

    ds = ds if ds is not None else build(domain, seed, effects=effects, n=n)
    run_sql = _duck(ds)
    fq = f"{SCHEMA}.{ds.table}"
    cols = _describe(run_sql, fq)
    enabled = packs.enabled_for([ds.table], [c.name for c in cols], None)
    proposals = proposals_for_table(fq, cols, enabled)
    proposals += [{"statement": "null control", "spec": s, "control": True} for s in ds.control_specs]
    scope = SimpleNamespace(assets=[fq], columns={fq: [c.name for c in cols]}, denied_columns=[])
    types = {fq: {c.name: c.semantic_type for c in cols}}
    tested: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in proposals:
        spec = AnalysisSpec.model_validate(p["spec"])
        if validate_spec(spec, scope, types):
            continue
        key = spec.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        stat = run_analysis(spec, run_sql, alpha=ALPHA).stat
        tested.append({"spec": spec, "stat": stat, "statement": p.get("statement", "")})
    return ds, run_sql, tested


def run_component(domain: str, seed: int, *, effects: bool = True, n: int | None = None,
                  ds: Dataset | None = None) -> ReplicateScore:
    from analystos.skills.analysis import verify_analysis
    from analystos.skills.stats import benjamini_hochberg

    started = time.perf_counter()
    ds, run_sql, tested = component_tests(domain, seed, effects=effects, n=n, ds=ds)
    with_p = [t for t in tested if t["stat"].p_value is not None]
    for t, q in zip(with_p, benjamini_hochberg([t["stat"].p_value for t in with_p]), strict=True):
        t["q"] = float(q)
    score = ReplicateScore(domain=domain, seed=seed, effects=effects, tier="component", tested=len(tested))
    for t in tested:
        spec_d = t["spec"].model_dump(mode="json")
        truth, _ = classify(ds, spec_d, _top(t["stat"]), t["stat"].highlights)
        per = score.tested_by_method.setdefault(spec_d["method"], {"tested": 0, "null_tests": 0, "null_raw_significant": 0})
        per["tested"] += 1
        if truth == "false":
            score.null_tests += 1
            score.null_raw_significant += int(t["stat"].p_value is not None and t["stat"].p_value < ALPHA)
            per["null_tests"] += 1
            per["null_raw_significant"] += int(t["stat"].p_value is not None and t["stat"].p_value < ALPHA)
        if not (t["stat"].supported and t.get("q") is not None and t["q"] < ALPHA):
            continue
        if not verify_analysis(t["spec"], run_sql, t["stat"], alpha=ALPHA).agrees:
            continue
        truth, planted = classify(ds, spec_d, _top(t["stat"]), t["stat"].highlights)
        score.findings.append(Finding(method=spec_d["method"], outcome=node(spec_d.get("outcome")), segment=node(spec_d.get("segment")),
                                      top=_top(t["stat"]), truth=truth, planted=planted, statement=t["statement"],
                                      p_value=t["stat"].p_value, p_adjusted=t.get("q")))
    score.seconds = round(time.perf_counter() - started, 2)
    return _score(score, ds)


# ----------------------------------------------------------------------------- aggregation
@dataclass
class Summary:
    tier: str
    replicates: int
    tested: int
    verified: int
    true_positive: int
    false_positive: int
    precision: float | None
    recall: float | None
    fdr: float  # mean false-discovery proportion over replicates with effects
    null_fdr: float  # mean FDP over global-null replicates (= family-wise error rate there)
    null_tests: int
    null_raw_rate: float | None  # share of null tests with raw p < α (calibration before effect thresholds)
    planted_found: int
    planted_total: int
    by_domain: dict[str, dict[str, Any]] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)
    false_findings: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)  # platform runs that did not complete
    by_method: dict[str, dict[str, Any]] = field(default_factory=dict)  # P4-03: per analysis method


def by_method(scores: list[ReplicateScore]) -> dict[str, dict[str, Any]]:
    """Per-method verified findings, precision, null calibration and FDR under the global null (the mean,
    over global-null replicates, of that method's false-discovery proportion)."""
    names = sorted({m for r in scores for m in r.tested_by_method} | {f.method for r in scores for f in r.findings})
    out: dict[str, dict[str, Any]] = {}
    for m in names:
        tested = sum(r.tested_by_method.get(m, {}).get("tested", 0) for r in scores)
        null_tests = sum(r.tested_by_method.get(m, {}).get("null_tests", 0) for r in scores)
        raw = sum(r.tested_by_method.get(m, {}).get("null_raw_significant", 0) for r in scores)
        found = [f for r in scores for f in r.findings if f.method == m]
        tp, fp = sum(f.truth == "true" for f in found), sum(f.truth == "false" for f in found)
        null_reps = [r for r in scores if not r.effects]
        fdp = [sum(f.truth == "false" for f in r.findings if f.method == m) /
               max(sum(f.truth in ("true", "false") for f in r.findings if f.method == m), 1) for r in null_reps]
        out[m] = {"tested": tested, "verified": len(found), "true_positive": tp, "false_positive": fp,
                  "precision": round(tp / (tp + fp), 4) if tp + fp else None, "null_tests": null_tests,
                  "null_raw_rate": round(raw / null_tests, 4) if null_tests and any(r.tier == "component" for r in scores) else None,
                  "null_fdr": round(sum(fdp) / len(fdp), 4) if fdp else 0.0}
    return out


def summarize(scores: list[ReplicateScore], tier: str) -> Summary:
    def agg(rows: list[ReplicateScore]) -> dict[str, Any]:
        eff = [r for r in rows if r.effects]
        null = [r for r in rows if not r.effects]
        tp, fp = sum(r.true_positive for r in rows), sum(r.false_positive for r in rows)
        found = sum(sum(r.planted.values()) for r in eff)
        total = sum(len(r.planted) for r in eff)
        null_tests = sum(r.null_tests for r in rows)
        return {"replicates": len(rows), "tested": sum(r.tested for r in rows), "verified": tp + fp, "true_positive": tp, "false_positive": fp,
                "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                "recall": round(found / total, 4) if total else None,
                "fdr": round(sum(r.fdp for r in eff) / len(eff), 4) if eff else 0.0,
                "null_fdr": round(sum(r.fdp for r in null) / len(null), 4) if null else 0.0,
                "null_tests": null_tests,
                # raw p-values are only read in the component tier (the platform tier scores the stored run)
                "null_raw_rate": round(sum(r.null_raw_significant for r in rows) / null_tests, 4)
                if null_tests and any(r.tier == "component" for r in rows) else None,
                "planted_found": found, "planted_total": total,
                "seconds": round(sum(r.seconds for r in rows), 1)}

    overall = agg(scores)
    by_domain = {d: agg([s for s in scores if s.domain == d]) for d in dict.fromkeys(s.domain for s in scores)}
    missed = sorted({f"{s.domain}:{pid} (seed {s.seed})" for s in scores for pid, ok in s.planted.items() if not ok})
    false = sorted({f"{s.domain}: {f.method} {f.outcome} by {f.segment} (top {f.top}, seed {s.seed}, "
                    f"{'effects' if s.effects else 'global null'})" for s in scores for f in s.findings if f.truth == "false"})
    return Summary(tier=tier, replicates=len(scores), tested=overall["tested"], verified=overall["verified"], true_positive=overall["true_positive"],
                   false_positive=overall["false_positive"], precision=overall["precision"], recall=overall["recall"],
                   fdr=overall["fdr"], null_fdr=overall["null_fdr"], null_tests=overall["null_tests"],
                   null_raw_rate=overall["null_raw_rate"], planted_found=overall["planted_found"],
                   planted_total=overall["planted_total"], by_domain=by_domain, missed=missed, false_findings=false,
                   incomplete=[f"{s.domain} seed {s.seed}: {s.status} ({s.run_id})" for s in scores if s.status != "COMPLETED"],
                   by_method=by_method(scores))


def check(summary: Summary, thresholds: dict[str, float] = THRESHOLDS) -> list[str]:
    """Threshold violations (empty = pass)."""
    problems = [f"{summary.tier}: run did not complete: {x}" for x in summary.incomplete]
    if summary.precision is not None and summary.precision < thresholds["precision_min"]:
        problems.append(f"{summary.tier}: precision {summary.precision} < {thresholds['precision_min']}")
    if summary.recall is not None and summary.recall < thresholds["recall_min"]:
        problems.append(f"{summary.tier}: recall {summary.recall} < {thresholds['recall_min']}")
    if summary.fdr > thresholds["fdr_max"]:
        problems.append(f"{summary.tier}: FDR {summary.fdr} > nominal α {thresholds['fdr_max']}")
    if summary.null_fdr > thresholds["null_fdr_max"]:
        problems.append(f"{summary.tier}: FDR under the global null {summary.null_fdr} > {thresholds['null_fdr_max']}")
    for m, row in summary.by_method.items():  # P4-03: the bound holds per method, not only on average
        if row["null_fdr"] > thresholds["null_fdr_max"]:
            problems.append(f"{summary.tier}: {m} FDR under the global null {row['null_fdr']} > {thresholds['null_fdr_max']}")
    return problems


def run_component_suite(seeds: list[int], *, null_seeds: list[int] | None = None, domains: tuple[str, ...] = DOMAINS,
                        n: int | None = None) -> tuple[Summary, list[ReplicateScore]]:
    scores = [run_component(d, s, effects=True, n=n) for d in domains for s in seeds]
    scores += [run_component(d, s, effects=False, n=n) for d in domains for s in (null_seeds or [])]
    return summarize(scores, "component"), scores


def as_dict(summary: Summary, scores: list[ReplicateScore]) -> dict[str, Any]:
    return {"summary": asdict(summary), "replicates": [{**asdict(s), "fdp": s.fdp} for s in scores]}


# ----------------------------------------------------------------------------- platform tier
def _wait(run_id: str, timeout: float) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        # WAITING_USER: the run asks a person (a business definition, a plan approval); nobody answers here
        if status in ("COMPLETED", "FAILED", "CANCELLED", "WAITING_FOR_APPROVAL", "WAITING_USER"):
            return status
        time.sleep(0.5)
    return "TIMEOUT"


def _record_definitions(admin: Any, workspace_id: str, table: str, definitions: list[dict[str, Any]]) -> None:
    """Approved business definitions, recorded as the workspace owner would before an unattended run: context
    grounding stops a run whose question uses a term nobody has defined for this data (e.g. "recurring")."""
    from analystos.contracts.grounding import BindingIn
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope
    from analystos.services import context_grounding as grounding

    with session_scope() as s:
        user = s.merge(admin)
        scope = resolve_scope(s, user, workspace_id, minimum_role="editor")
        refs = {c["ref"].rsplit(".", 1)[1]: c["ref"] for c in grounding.catalog(s, scope)
                if c["ref"].rsplit(".", 2)[-2] == table}
        for d in definitions:
            grounding.save_binding(s, user, workspace_id, BindingIn(
                term=d["term"], definition=d["definition"], columns=[refs[c] for c in d["columns"]], state="approved",
                owner_id=user.id, evidence=d.get("evidence", "Benchmark definition reviewed with the dataset's generator."),
                examples=d.get("examples", "")))

def run_platform(domain: str, seed: int, *, effects: bool = True, n: int | None = None, timeout: float = 1200,
                 source: str = "csv", ds: Dataset | None = None, objective: str | None = None,
                 definitions: list[dict[str, Any]] | None = None) -> ReplicateScore:
    """One benchmark dataset through the real platform: Parquet upload -> file source -> discovery ->
    staged snapshot (loader) -> run (every query through QueryGateway) -> verified insights. Needs the
    control-plane database and the analytics plane; the orchestrator is whatever ANALYSTOS_ORCHESTRATOR
    says (`local` in CI). Whether models answer is decided by the environment (no key = the rule path)
    and the platform settings, exactly as in production.

    `source="duckdb"` uploads the dataset as a DuckDB database file instead and registers it with
    the pushdown opt-in (`execution_mode: pushdown`): nothing is staged, and every statement of the
    run executes in the DuckDB engine on the file (read-only), through the same gateway (DEX-001)."""
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Hypothesis, Insight, User
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    started = time.perf_counter()
    ds = ds if ds is not None else build(domain, seed, effects=effects, n=n)
    settings = get_settings()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == settings.bootstrap_admin_email))
        # No publication in a benchmark run; approved-metric gating is about publishing, not findings.
        ws = create_workspace(s, admin, name=f"V01 benchmark {domain} seed {seed}{'' if effects else ' null'} {new_id('b')[-6:]}",
                              objective=objective or f"Find what drives the outcomes in the {ds.table} data",
                              policy={"require_approved_metrics": False})
        s.flush()
        folder = settings.upload_dir / ws.id
        folder.mkdir(parents=True, exist_ok=True)
        if source == "duckdb":
            import duckdb

            con = duckdb.connect(str(folder / f"{ds.table}.duckdb"))
            con.register("frame", ds.frame)
            con.execute(f'CREATE TABLE "{ds.table}" AS SELECT * FROM frame')
            con.close()
            src = register_source(s, admin, ws.id, kind="duckdb", name=f"{domain} benchmark (DuckDB)",
                                  config={"path": f"{ws.id}/{ds.table}.duckdb", "execution_mode": "pushdown"}, secret_ref=None)
        else:
            ds.frame.to_parquet(folder / f"{ds.table}.parquet", index=False)
            src = register_source(s, admin, ws.id, kind="csv", name=f"{domain} benchmark",
                                  config={"path": f"{ws.id}/{ds.table}.parquet"}, secret_ref=None)
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, [ds.table])
    if definitions:
        _record_definitions(admin, ws_id, ds.table, definitions)
    run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    status = _wait(run.id, timeout)
    score = ReplicateScore(domain=domain, seed=seed, effects=effects, tier="platform", run_id=run.id)
    with session_scope() as s:
        hyps = list(s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id)))
        insights = list(s.execute(select(Insight, Hypothesis.spec).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                                  .where(Insight.run_id == run.id, Insight.status == "verified")))
        score.tested = sum(1 for h in hyps if h.status != "proposed")  # rejected = tested, not supported
        score.null_tests = sum(1 for h in hyps if h.spec and classify(ds, h.spec)[0] == "false")
        for insight, spec in insights:
            spec = spec or {}
            text = f"{insight.title} {insight.finding}"
            truth, planted = classify(ds, spec, None, None, text)
            if spec.get("method") == "driver_model":
                truth = _classify_driver_text(ds, spec, text)
            score.findings.append(Finding(method=spec.get("method", "?"), outcome=node(spec.get("outcome")),
                                          segment=node(spec.get("segment")), top=None, truth=truth, planted=planted,
                                          statement=insight.title))
    score.seconds = round(time.perf_counter() - started, 1)
    score = _score(score, ds)
    score.status = status
    return score


def _classify_driver_text(ds: Dataset, spec: dict[str, Any], text: str) -> str:
    """Platform insights name the driver in their text: true when a named column is an associated driver."""
    out = node(spec.get("outcome"))
    lowered = text.lower()
    named = [c for c in ds.frame.columns if c != out and (c in lowered or c.replace("_", " ") in lowered)]
    if not named or out is None:
        return "unscored"
    return "true" if any(ds.associated(out, c) for c in named) else "false"


def run_platform_suite(seeds: list[int], *, null_seeds: list[int] | None = None, domains: tuple[str, ...] = DOMAINS,
                       n: int | None = None) -> tuple[Summary, list[ReplicateScore]]:
    scores = [run_platform(d, s, effects=True, n=n) for d in domains for s in seeds]
    scores += [run_platform(d, s, effects=False, n=n) for d in domains for s in (null_seeds or [])]
    return summarize(scores, "platform"), scores

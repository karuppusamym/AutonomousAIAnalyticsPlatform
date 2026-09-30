"""Score AnalystOS end to end on the held-out corpus (P4-08, evaluation plan §1, §3, §5).

Every task ends in exactly one status:

  accepted                 a deliver task whose output meets its rubric
  correct_abstention       an abstain task that ended the way the rubric requires (no finding, refused, blocked)
  confident_wrong          a claim the rubric says is false: a false verified finding, an `improved` model on a null,
                           leaky or too-small dataset, an engineering output that differs from the reference or that
                           should have been refused or blocked
  incomplete               a deliver task with a partial, not wrong, output (some planted effects missed; an
                           engineering output that would not publish)
  unnecessary_abstention   a deliver task the platform declined (no findings, refused, denied, no improvement)
  wrong_abstention         an abstain task that abstained for another reason than the rubric's (e.g. a leak that
                           trained and found no improvement instead of being refused, or a governance denial
                           such as a disabled capability, `denied`, where the rubric expects a data refusal)
  error                    the harness or the platform raised

Two tiers run the same tasks and the same judges:

* **component** (no services; CI): analysis through `evaluation.analytical.run_component` (DuckDB, the
  platform's proposals, AnalysisSpec validation, statistics, Benjamini-Hochberg, the second method); engineering
  through `RecipeExecutor` on the DuckDB snapshot engine behind a gateway shim that validates every statement
  with `gateway.validator.validate_sql`; ML through `ml.jobs.run_ml_job`. No model is in these paths.
* **platform** (Postgres; the integration environment): each task's data is uploaded as a file source, discovered
  and staged; analysis is a real run (local orchestrator, every query through `QueryGateway`), engineering is
  `services.recipes.run_recipe` (preview for the rows, materialize for the published outcome), ML is
  `services.ml.start_experiment` on a published `ml_spec`. Models answer only when a provider key is set.

Governance and recovery tasks (corpus v2) are scenarios (`evaluation/heldout/scenarios.py`) run on both tiers: the
component tier over DuckDB behind the gateway validator and a SQLite control plane, the platform tier over uploaded
workspaces, `QueryGateway.execute`, recipe runs and the Postgres control plane.

Cost per accepted output = measured model spend / accepted outputs, plus CPU and wall seconds. Infrastructure is
not priced unless `cpu_usd_per_hour` is given: unknown cost is reported as unpriced, never as zero.
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from evaluation.heldout import generators as G

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.yaml"
LOCK = HERE / "corpus.lock.json"
ACTOR = "benchmark:heldout"
FAMILIES = ("analysis", "engineering", "ml", "governance", "recovery")
ABSTAIN_KINDS = {"analysis": ("no_finding",), "engineering": ("refused", "blocked"), "ml": ("no_finding", "refused"),
                 "governance": ("denied",), "recovery": ("refused", "blocked")}
STATUSES = ("accepted", "correct_abstention", "confident_wrong", "incomplete", "unnecessary_abstention",
            "wrong_abstention", "error")
# Proposed thresholds for the owner (evaluation plan §3); wired into config/eval_gates.yaml as a non-blocking report.
PROPOSED_THRESHOLDS = {"accepted_output_rate": {"min": 0.90}, "confident_wrong": {"max": 0},
                       "abstention_recall": {"min": 1.0}, "errors": {"max": 0}}


# ----------------------------------------------------------------------------- corpus
@dataclass(frozen=True)
class Task:
    id: str
    family: str
    domain: str
    generator: str
    seed: int
    objective: str
    rubric: dict[str, Any]
    effects: bool = True
    degenerate: str | None = None
    variant: str | None = None
    transform: dict[str, Any] | None = None  # transfer suite: rename/reorder the generated table

    @property
    def expect(self) -> str:
        return self.rubric["expect"]

    @property
    def abstain_kind(self) -> str | None:
        return self.rubric.get("abstain_kind")


@dataclass
class Corpus:
    version: int
    frozen: str
    tasks: list[Task]
    digest: str  # sha256 of corpus.yaml


def load_corpus(path: Path = CORPUS) -> Corpus:
    raw = path.read_bytes()
    doc = yaml.safe_load(raw)
    tasks = [Task(id=t["id"], family=t["family"], domain=t["domain"], generator=t["generator"], seed=int(t["seed"]),
                  objective=t["objective"], rubric=t["rubric"], effects=t.get("effects", True),
                  degenerate=t.get("degenerate"), variant=t.get("variant"), transform=t.get("transform")) for t in doc["tasks"]]
    return Corpus(version=int(doc["version"]), frozen=str(doc["frozen"]), tasks=tasks, digest=hashlib.sha256(raw).hexdigest())


def corpus_problems(corpus: Corpus) -> list[str]:
    """Structural checks of the corpus (the unit tests run them; no services)."""
    out = []
    ids = [t.id for t in corpus.tasks]
    if len(ids) != len(set(ids)):
        out.append("duplicate task ids")
    seeds = [t.seed for t in corpus.tasks]
    if len(seeds) != len(set(seeds)):
        out.append("a seed is used by two tasks")
    for t in corpus.tasks:
        if not 7000 <= t.seed <= 7999:
            out.append(f"{t.id}: seed {t.seed} outside the held-out range 7000-7999")
        if t.family not in FAMILIES:
            out.append(f"{t.id}: unknown family {t.family}")
        if t.expect not in ("deliver", "abstain"):
            out.append(f"{t.id}: expect must be deliver or abstain")
        if t.expect == "abstain" and t.abstain_kind not in ABSTAIN_KINDS.get(t.family, ()):
            out.append(f"{t.id}: an abstain {t.family} task needs abstain_kind in {ABSTAIN_KINDS.get(t.family)}")
        if t.family in ("governance", "recovery"):
            from evaluation.heldout import scenarios

            if t.generator not in scenarios.SCENARIOS:
                out.append(f"{t.id}: unknown scenario {t.generator}")
            elif scenarios.SCENARIOS[t.generator].expect != t.expect:
                out.append(f"{t.id}: scenario {t.generator} is written for expect {scenarios.SCENARIOS[t.generator].expect}")
        if t.transform and t.family != "analysis":
            out.append(f"{t.id}: a transform applies to analysis tasks only")
        if t.family == "analysis":
            if t.generator not in G.ANALYSIS:
                out.append(f"{t.id}: unknown analysis generator {t.generator}")
            if t.expect == "deliver" and not t.rubric.get("planted"):
                out.append(f"{t.id}: a deliver analysis task needs planted effects")
            if t.expect == "abstain" and t.effects:
                out.append(f"{t.id}: an abstain analysis task must have effects: false")
            nodes = set(t.rubric.get("truth") or {}) | {p for ps in (t.rubric.get("truth") or {}).values() for p in ps}
            for p in t.rubric.get("planted") or []:
                if p["outcome"] not in nodes or p["segment"] not in (t.rubric["truth"].get(p["outcome"]) or []):
                    out.append(f"{t.id}: planted {p['id']} is not an edge of the truth graph")
            for c in t.rubric.get("nulls") or []:
                if c in nodes:
                    out.append(f"{t.id}: null column {c} is in the truth graph")
    return out


def task_digest(task: Task) -> str:
    """sha256 of everything a task's verdict depends on: generated data, recipe/spec, reference, rubric."""
    h = hashlib.sha256()
    h.update(json.dumps({"rubric": task.rubric, "objective": task.objective}, sort_keys=True, default=str).encode())
    if task.family == "analysis":
        ds = analysis_dataset(task)
        # "\n", not os.linesep: on Windows the default CRLF changed every analysis digest and broke the lock
        h.update(ds.frame.to_csv(index=False, lineterminator="\n").encode())
    elif task.family == "engineering":
        tables, spec, ref = G.engineering(task.generator, task.seed, task.variant or "clean")
        h.update(json.dumps({"tables": tables, "spec": spec, "ref": ref}, sort_keys=True, default=str).encode())
    elif task.family in ("governance", "recovery"):
        from evaluation.heldout import scenarios

        h.update(json.dumps(scenarios.fixture(task), sort_keys=True, default=str).encode())
    else:
        data, spec = G.ml_task(task.generator, task.seed, task.variant or "")
        h.update(json.dumps({"data": data, "spec": spec}, sort_keys=True, default=str).encode())
    return h.hexdigest()


def lock_entries(corpus: Corpus) -> dict[str, Any]:
    return {"version": corpus.version, "tasks": {t.id: task_digest(t) for t in corpus.tasks}}


# ----------------------------------------------------------------------------- results
@dataclass
class TaskResult:
    id: str
    family: str
    domain: str
    expect: str
    abstain_kind: str | None
    status: str
    produced: str  # output | abstained | error
    abstained_as: str | None = None  # no_finding | refused | blocked | denied (authorization / enablement)
    reason: str = ""  # why this status, in words
    detail: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    cpu_seconds: float = 0.0
    model_calls: int = 0
    model_tokens: int = 0
    model_usd: float = 0.0
    run_ref: str | None = None  # run / recipe run / experiment id (platform)


def _result(task: Task, status: str, produced: str, **kw: Any) -> TaskResult:
    return TaskResult(id=task.id, family=task.family, domain=task.domain, expect=task.expect,
                      abstain_kind=task.abstain_kind, status=status, produced=produced, **kw)


def judge_abstention(task: Task, kind: str, reason: str, **kw: Any) -> TaskResult:
    """The platform declined (no finding, refused or blocked)."""
    if task.expect == "deliver":
        return _result(task, "unnecessary_abstention", "abstained", abstained_as=kind, reason=reason, **kw)
    ok = kind == task.abstain_kind
    return _result(task, "correct_abstention" if ok else "wrong_abstention", "abstained", abstained_as=kind,
                   reason=reason if ok else f"abstained as {kind}, rubric requires {task.abstain_kind}: {reason}", **kw)


# ----------------------------------------------------------------------------- analysis
def analysis_dataset(task: Task):
    from evaluation.datasets import Planted

    gen = G.ANALYSIS[task.generator]
    kwargs: dict[str, Any] = {"effects": task.effects}
    if task.degenerate:
        kwargs["degenerate"] = task.degenerate
    ds = gen(task.seed, **kwargs)
    if task.transform:
        ds = G.transfer(ds, task.transform, task.seed)
    ds.parents ={k: list(v) for k, v in (task.rubric.get("truth") or {}).items()}
    ds.planted = [Planted(p["id"], p["method"], p["outcome"], p["segment"], p.get("top")) for p in task.rubric.get("planted") or []]
    return ds


def judge_analysis(task: Task, score: Any, **kw: Any) -> TaskResult:
    findings = [{"method": f.method, "outcome": f.outcome, "segment": f.segment, "top": None if f.top is None else str(f.top),
                 "truth": f.truth, "planted": f.planted, "statement": f.statement} for f in score.findings]
    false = [f for f in findings if f["truth"] == "false"]
    found = sorted({f["planted"] for f in findings if f["planted"]})
    missed = [p["id"] for p in task.rubric.get("planted") or [] if p["id"] not in found]
    detail = {"tested": score.tested, "verified": len(findings), "true": sum(f["truth"] == "true" for f in findings),
              "false": len(false), "unscored": sum(f["truth"] == "unscored" for f in findings), "planted_found": found,
              "planted_missed": missed, "findings": findings}
    if score.status != "COMPLETED":
        return _result(task, "error", "error", reason=f"run ended {score.status}", detail=detail, **kw)
    if false:
        return _result(task, "confident_wrong", "output", detail=detail, reason="false verified finding(s): " + "; ".join(
            f"{f['method']} {f['outcome']} by {f['segment']} (top {f['top']})" for f in false), **kw)
    if task.expect == "abstain":
        note = "" if not findings else f" ({len(findings)} true non-planted finding(s) reported)"
        return judge_abstention(task, "no_finding", "no false finding" + note, detail=detail, **kw)
    if not findings:
        return judge_abstention(task, "no_finding", "no verified finding", detail=detail, **kw)
    if missed:
        return _result(task, "incomplete", "output", detail=detail, reason="missed planted effect(s): " + ", ".join(missed), **kw)
    return _result(task, "accepted", "output", detail=detail, reason="every planted effect verified, no false finding", **kw)


def run_analysis_component(task: Task) -> TaskResult:
    from evaluation.analytical import run_component

    score = run_component(task.domain, task.seed, ds=analysis_dataset(task))
    return judge_analysis(task, score)


def run_analysis_platform(task: Task) -> TaskResult:
    from evaluation.analytical import run_platform

    score = run_platform(task.domain, task.seed, ds=analysis_dataset(task), objective=task.objective)
    usage = _run_usage(score.run_id)
    return judge_analysis(task, score, run_ref=score.run_id, **usage)


def _run_usage(run_id: str | None) -> dict[str, Any]:
    if not run_id:
        return {}
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall

    with session_scope() as s:
        calls = [c for c in s.scalars(select(ModelCall).where(ModelCall.run_id == run_id)) if c.status in ("ok", "error")]
        return {"model_calls": len(calls), "model_tokens": sum((c.input_tokens or 0) + (c.output_tokens or 0) for c in calls),
                "model_usd": float(sum(c.cost_usd or 0.0 for c in calls))}


# ----------------------------------------------------------------------------- engineering
def judge_engineering(task: Task, *, refused: str | None = None, blocked: str | None = None, columns: list[str] | None = None,
                      rows: list[list[Any]] | None = None, dropped: int = 0, detail: dict[str, Any] | None = None,
                      **kw: Any) -> TaskResult:
    from evaluation.ask import results_match

    detail = dict(detail or {})
    if refused is not None:
        return judge_abstention(task, "refused", refused, detail=detail, **kw)
    if blocked is not None:
        if task.expect == "deliver":
            return _result(task, "incomplete", "abstained", abstained_as="blocked", reason=f"output blocked: {blocked}",
                           detail=detail, **kw)
        return judge_abstention(task, "blocked", blocked, detail=detail, **kw)
    if task.expect == "abstain":
        return _result(task, "confident_wrong", "output", detail=detail,
                       reason=f"published an output the rubric requires to be {task.abstain_kind}", **kw)
    _, _, ref = G.engineering(task.generator, task.seed, task.variant or "clean")
    ok, why = results_match(ref["rows"], rows or [])
    detail.update(rows=len(rows or []), reference_rows=len(ref["rows"]), dropped_rows=dropped,
                  reference_dropped_rows=ref["dropped_rows"], columns=columns)
    if not ok:
        return _result(task, "confident_wrong", "output", detail=detail, reason=f"output differs from the reference: {why}", **kw)
    if dropped != ref["dropped_rows"]:
        return _result(task, "confident_wrong", "output", detail=detail,
                       reason=f"dropped {dropped} rows, the reference drops {ref['dropped_rows']}", **kw)
    return _result(task, "accepted", "output", detail=detail, reason="output equals the reference", **kw)


class DuckGateway:
    """The component tier's stand-in for QueryGateway: every statement is validated by the real gateway
    validator against the task's scope, then executed on an in-memory DuckDB holding the source tables."""

    def __init__(self, tables: dict[str, dict[str, Any]]) -> None:
        import duckdb

        self.settings = SimpleNamespace(query_max_rows=100_000, query_timeout_seconds=120)
        self.con = duckdb.connect()
        types = {"integer": "INTEGER", "double": "DOUBLE", "text": "TEXT", "date": "DATE", "timestamp": "TIMESTAMP"}
        for asset, t in tables.items():
            schema, name = asset.split(".")
            self.con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cols = ", ".join(f'"{c}" {types[ty]}' for c, ty in zip(t["columns"], t["types"], strict=True))
            self.con.execute(f'CREATE TABLE "{schema}"."{name}" ({cols})')
            if t["rows"]:
                marks = ", ".join("?" for _ in t["columns"])
                self.con.executemany(f'INSERT INTO "{schema}"."{name}" VALUES ({marks})', t["rows"])
        self.statements = 0

    def run_sql_for(self, scope: Any, *, actor: str, run_id: str | None = None, source_id: str | None = None):
        from analystos.gateway.validator import validate_sql

        def run(sql: str, *, purpose: str, max_rows: int, use_cache: bool = False) -> Any:
            from analystos.core.errors import UpstreamUnavailable

            validate_sql(scope, sql, max_rows=max_rows)  # refuses exactly what the gateway would
            try:
                cur = self.con.execute(sql)
            except Exception as exc:  # noqa: BLE001 - surfaced exactly as QueryGateway.execute surfaces an engine failure
                raise UpstreamUnavailable(f"Query execution failed: {exc.__class__.__name__}") from exc
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchmany(max_rows + 1)]
            self.statements += 1
            return SimpleNamespace(columns=columns, rows=rows[:max_rows], truncated=len(rows) > max_rows,
                                   row_count=min(len(rows), max_rows), query_id=f"duck-{self.statements}")
        return run


def _scope(tables: dict[str, dict[str, Any]]):
    from analystos.contracts.policy import DataScope

    return DataScope(workspace_id="ws_heldout", user_id="u_heldout", role="analyst", source_ids=["src_heldout"],
                     assets=sorted(tables), asset_sources={a: "src_heldout" for a in tables},
                     columns={a: t["columns"] for a, t in tables.items()}, source_dialects={"src_heldout": "postgres"})


def component_recipe(tables: dict[str, dict[str, Any]], spec: dict[str, Any], *, loaded: set[str] | None = None) -> dict[str, Any]:
    """One recipe preview on the component tier, as keyword arguments for `judge_engineering`: refused,
    blocked, or the published columns/rows/dropped count. `loaded`: the tables actually present in the
    engine (default all); the scope still lists every table, as a catalog would before a load finished."""
    from analystos.contracts.recipe import RecipeInvalid, validate_recipe
    from analystos.core.errors import AnalystOSError
    from analystos.recipes.execute import RecipeExecutor, SnapshotStore, plan_execution
    from analystos.recipes.gates import evaluate, row_gates

    try:
        validated = validate_recipe(spec)
    except RecipeInvalid as exc:
        return {"refused": f"validation: {'; '.join(exc.problems)[:300]}"}
    scope = _scope(tables)
    gateway = DuckGateway({a: t for a, t in tables.items() if loaded is None or a in loaded})
    with tempfile.TemporaryDirectory(prefix="aos-heldout-") as tmp:
        try:
            plan = plan_execution(validated, scope, prefer="duckdb")
            ex = RecipeExecutor(gateway, scope, validated, plan, actor=ACTOR, store=SnapshotStore(Path(tmp) / "snapshots"))
            needed = {nid for o in validated.outputs() for nid in validated.ancestors(o.id)}
            preflight = [ex.preflight(n.id) for n in validated.recipe.nodes if n.op == "join" and n.id in needed]
            violated = [p for p in preflight if not p["ok"]]
            if violated:
                v = violated[0]
                return {"refused": f"join pre-flight: {v['join']} declared {v['declared']}, observed {v['observed']}",
                        "detail": {"preflight": preflight}}
            out = validated.outputs()[0]
            res = ex.query(ex.compiler.output_query(out.id, gates=row_gates(out)), purpose="recipe.preview:heldout")
            outcome = evaluate(out, res.columns, res.rows)
            snapshots = {a: s["snapshot"] for a, s in ex.snapshots.items()}
        except AnalystOSError as exc:
            return {"refused": f"{type(exc).__name__}: {exc.message[:300]}"}
    detail = {"preflight": preflight, "gates": outcome.summary()["gates"], "engine": plan.engine, "statements": gateway.statements,
              "snapshots": snapshots}
    if outcome.blocked:
        failed = [g["gate"] for g in outcome.results if g["status"] == "failed"]
        return {"blocked": f"fail gate {', '.join(failed)}", "detail": detail}
    return {"columns": outcome.columns, "rows": outcome.kept, "dropped": len(outcome.dropped), "detail": detail}


def run_engineering_component(task: Task) -> TaskResult:
    tables, spec, _ = G.engineering(task.generator, task.seed, task.variant or "clean")
    return judge_engineering(task, **component_recipe(tables, spec))


# ----------------------------------------------------------------------------- ML
def judge_ml(task: Task, out: dict[str, Any] | None, *, refused: str | None = None, **kw: Any) -> TaskResult:
    if refused is not None:
        return judge_abstention(task, "refused", refused, **kw)
    out = out or {}
    verdict = out.get("verdict")
    detail = {"status": out.get("status"), "verdict": verdict, "parity": out.get("_parity")}
    if out.get("status") != "succeeded":
        return _result(task, "error", "error", reason=f"job ended {out.get('status')}: {out.get('error')}", detail=detail, **kw)
    if verdict == "improved":
        if task.expect == "abstain":
            return _result(task, "confident_wrong", "output", detail=detail,
                           reason=f"`improved` where the rubric requires {task.abstain_kind} ({task.rubric.get('why')})", **kw)
        if out.get("_parity") is False:
            return _result(task, "incomplete", "output", detail=detail, reason="improved, but the baseline-parity checks fail", **kw)
        return _result(task, "accepted", "output", detail=detail, reason="improved over the baseline on identical splits", **kw)
    return judge_abstention(task, "no_finding", f"verdict {verdict}", detail=detail, **kw)


def run_ml_component(task: Task) -> TaskResult:
    from analystos.ml.jobs import run_ml_job
    from analystos.ml.store import snapshots
    from evaluation.ml import _parity

    (cols, rows, types), spec = G.ml_task(task.generator, task.seed, task.variant or "")
    with tempfile.TemporaryDirectory(prefix="aos-heldout-ml-") as art:
        job = {"kind": "train", "artifact_dir": art, "snapshot": snapshots(art).put(cols, rows), "column_types": types,
               "spec": spec, "as_of": "2026-01-01T00:00:00+00:00", "caps": {"max_trials": 4, "max_seconds": 120}}
        out = run_ml_job(job)
    if out.get("status") == "refused":
        problems = (out.get("readiness") or {}).get("problems") or [out.get("error") or "refused"]
        return judge_ml(task, None, refused="; ".join(problems)[:300])
    if out.get("status") == "succeeded":
        out["_parity"] = _parity(out)
    return judge_ml(task, out)


# ----------------------------------------------------------------------------- platform setup (Postgres)
def _admin():
    from evaluation.ask import _admin as admin

    return admin()


def _upload_workspace(name: str, objective: str, files: dict[str, tuple[list[str], list[list[Any]]]], *,
                      enable: tuple[str, ...] = (), policy: dict[str, Any] | None = None, stage: bool = True) -> dict[str, Any]:
    """A fresh workspace with the given CSV files as one staged file source."""
    import csv

    from analystos.capabilities import enablement, registry
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Source
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    admin = _admin()
    folder = f"heldout-{new_id('h')[-8:]}"
    upload = Path(get_settings().upload_dir) / folder
    upload.mkdir(parents=True, exist_ok=True)
    for table, (cols, rows) in files.items():
        with (upload / f"{table}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([["" if v is None else v for v in r] for r in rows])
    with session_scope() as s:
        ws = create_workspace(s, s.merge(admin), name=f"P4-08 held-out {name} {folder[-6:]}", objective=objective,
                              policy={"require_approved_metrics": False, **(policy or {})}, autonomy_level=3)
        s.flush()
        src = register_source(s, s.merge(admin), ws.id, kind="csv", name=f"held-out {name}", config={"path": folder},
                              secret_ref=None)
        s.flush()
        for cap in enable:
            enablement.set_enabled(s, s.merge(admin), ws.id, cap, True, registry.current())
        ws_id, src_id = ws.id, src.id
    if stage:
        discover_source(admin, src_id, ws_id)
        select_assets(admin, src_id, list(files), ws_id)
    with session_scope() as s:
        schema = s.get(Source, src_id).staging_schema or f"src_{src_id}"
    return {"ws": ws_id, "src": src_id, "schema": schema, "admin": admin, "folder": upload}


def platform_recipe(env: dict[str, Any], spec: dict[str, Any], *, engine: str | None = None) -> dict[str, Any]:
    """Save and run one recipe (preview for the rows, then materialize for the published outcome) in an
    uploaded workspace, as keyword arguments for `judge_engineering`; `denied` when authorization stopped it."""
    from analystos.contracts.recipe import RecipeInvalid
    from analystos.core.errors import AnalystOSError, Forbidden, PolicyDenied
    from analystos.db.base import session_scope
    from analystos.services.recipes import run_recipe, save_recipe

    spec = json.loads(json.dumps(spec).replace(f'"{G.SCHEMA}.', f'"{env["schema"]}.'))
    try:
        with session_scope() as s:
            rid = save_recipe(s, s.merge(env["admin"]), env["ws"], spec).id
    except RecipeInvalid as exc:
        return {"refused": f"validation: {'; '.join(exc.problems)[:300]}"}
    try:
        preview = run_recipe(env["admin"], rid, env["ws"], mode="preview", limit=100_000, engine=engine)
        done = run_recipe(env["admin"], rid, env["ws"], mode="materialize", engine=engine)
    except RecipeInvalid as exc:
        return {"refused": f"validation: {'; '.join(exc.problems)[:300]}"}
    except (Forbidden, PolicyDenied) as exc:
        return {"denied": f"{type(exc).__name__}: {exc.message[:300]}"}
    except AnalystOSError as exc:
        return {"refused": f"{type(exc).__name__}: {exc.message[:300]}", "run_ref": (exc.details or {}).get("recipe_run_id")}
    name = next(iter(preview["preview"]))
    shown = preview["preview"][name]
    gates = done["gates"].get(name, {})
    detail = {"preflight": done.get("preflight"), "gates": gates.get("gates"), "engine": (done.get("plan") or {}).get("engine"),
              "materialized": done["outputs"].get(name, {}).get("table"), "written_rows": done["outputs"].get(name, {}).get("row_count"),
              "snapshots": {a: s.get("snapshot") for a, s in (preview.get("snapshots") or {}).items()}}
    if done["status"] == "blocked":
        failed = [g["gate"] for g in gates.get("gates") or [] if g["status"] == "failed"]
        return {"blocked": f"fail gate {', '.join(failed) or '(schema policy)'}", "detail": detail, "run_ref": done["id"]}
    if detail["written_rows"] is not None and detail["written_rows"] != shown["row_count"]:
        detail["note"] = f"materialized {detail['written_rows']} rows, preview kept {shown['row_count']}"
    return {"columns": shown["columns"], "rows": shown["rows"], "dropped": shown["dropped_rows"], "detail": detail,
            "run_ref": done["id"]}


def run_engineering_platform(task: Task) -> TaskResult:
    tables, spec, _ = G.engineering(task.generator, task.seed, task.variant or "clean")
    env = _upload_workspace(task.id, task.objective, {a.split(".")[1]: (t["columns"], t["rows"]) for a, t in tables.items()})
    out = platform_recipe(env, spec)
    if "denied" in out:
        return judge_abstention(task, "denied", out["denied"])
    return judge_engineering(task, **out)


def run_ml_platform(task: Task) -> TaskResult:
    from analystos.contracts.definition import DefinitionDraftIn
    from analystos.core.errors import AnalystOSError, Forbidden, PolicyDenied
    from analystos.db.base import session_scope
    from analystos.services import definitions as defs
    from analystos.services import ml

    (cols, rows, _types), spec = G.ml_task(task.generator, task.seed, task.variant or "")
    table = spec["dataset"]["asset"].split(".")[1]
    # An ML brief implies a workspace whose owner enabled ML, as the governed-ML runbook says to.
    env = _upload_workspace(task.id, task.objective, {table: (cols, rows)}, enable=("playbook.train",))
    spec = {**spec, "dataset": {"asset": f"{env['schema']}.{table}"}}
    try:
        with session_scope() as s:
            row = defs.create_draft(s, s.merge(env["admin"]), env["ws"],
                                    DefinitionDraftIn(kind="ml_spec", key=task.id.lower().replace("-", "_"), spec=spec))
            defs.publish(s, s.merge(env["admin"]), row, row.revision)
            def_id = row.id
        out = ml.start_experiment(env["admin"], env["ws"], def_id)
    except (Forbidden, PolicyDenied) as exc:
        return judge_abstention(task, "denied", f"{type(exc).__name__}: {exc.message[:300]}")
    except AnalystOSError as exc:
        return judge_ml(task, None, refused=f"{type(exc).__name__}: {exc.message[:300]}",
                        run_ref=(exc.details or {}).get("experiment_id"))
    out = dict(out)
    out["_parity"] = None  # the service seals the report; parity is asserted by the component tier's checks
    return judge_ml(task, out, run_ref=out.get("id"))


# ----------------------------------------------------------------------------- governance and recovery
def judge_scenario(task: Task, out: dict[str, Any]) -> TaskResult:
    """A scenario ends `acted` (the action went through), `stopped` (a deterministic check stopped it, with
    its kind and reason) or `delivered` (a deliver scenario finished; `ok` says whether it met the rubric)."""
    detail, reason = dict(out.get("detail") or {}), str(out.get("reason") or "")
    if out["outcome"] == "stopped":
        return judge_abstention(task, out["kind"], reason, detail=detail)
    if out["outcome"] == "acted":
        if task.expect == "abstain":
            return _result(task, "confident_wrong", "output", detail=detail,
                           reason=f"the action went through where the rubric requires {task.abstain_kind}: {reason}")
        return _result(task, "incomplete", "output", detail=detail, reason=reason or "acted without meeting the rubric")
    if task.expect == "abstain":
        return _result(task, "confident_wrong", "output", detail=detail, reason=f"completed where the rubric requires "
                                                                               f"{task.abstain_kind}: {reason}")
    if out.get("wrong"):
        return _result(task, "confident_wrong", "output", detail=detail, reason=reason)
    return _result(task, "accepted" if out.get("ok") else "incomplete", "output", detail=detail, reason=reason)


def _scenario_runner(tier: str):
    def run(task: Task) -> TaskResult:
        from evaluation.heldout import scenarios

        return judge_scenario(task, scenarios.run(task, tier))
    return run


# ----------------------------------------------------------------------------- orchestration
RUNNERS = {
    "component": {"analysis": run_analysis_component, "engineering": run_engineering_component, "ml": run_ml_component,
                  "governance": _scenario_runner("component"), "recovery": _scenario_runner("component")},
    "platform": {"analysis": run_analysis_platform, "engineering": run_engineering_platform, "ml": run_ml_platform,
                 "governance": _scenario_runner("platform"), "recovery": _scenario_runner("platform")},
}


def run_task(task: Task, tier: str) -> TaskResult:
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        result = RUNNERS[tier][task.family](task)
    except Exception as exc:  # noqa: BLE001 - one task's failure is a recorded outcome, not the end of the run
        result = _result(task, "error", "error", reason=f"{type(exc).__name__}: {str(exc)[:300]}")
    result.seconds = round(time.perf_counter() - wall, 3)
    result.cpu_seconds = round(time.process_time() - cpu, 3)
    return result


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 3)


def _ratio(a: int | float, b: int | float) -> float | None:
    return round(a / b, 4) if b else None


def metrics(results: list[TaskResult], *, cpu_usd_per_hour: float | None = None) -> dict[str, Any]:
    deliver = [r for r in results if r.expect == "deliver"]
    abstain = [r for r in results if r.expect == "abstain"]
    abstained = [r for r in results if r.produced == "abstained"]
    accepted = sum(r.status == "accepted" for r in results)
    correct_abs = sum(r.status == "correct_abstention" for r in results)
    usd = sum(r.model_usd for r in results)
    cpu = sum(r.cpu_seconds for r in results)
    wall = [r.seconds for r in results]
    infra = None if cpu_usd_per_hour is None else round(cpu / 3600 * cpu_usd_per_hour, 6)
    return {
        "tasks": len(results), "deliver_tasks": len(deliver), "abstain_tasks": len(abstain),
        "statuses": {s: sum(r.status == s for r in results) for s in STATUSES},
        "accepted": accepted, "accepted_output_rate": _ratio(accepted, len(deliver)),
        "confident_wrong": sum(r.status == "confident_wrong" for r in results),
        "abstention_recall": _ratio(correct_abs, len(abstain)),  # abstain tasks that abstained the right way
        "abstention_precision": _ratio(sum(r.expect == "abstain" for r in abstained), len(abstained)),
        "unnecessary_abstentions": sum(r.status == "unnecessary_abstention" for r in results),
        "task_success_rate": _ratio(accepted + correct_abs, len(results)),
        "errors": sum(r.status == "error" for r in results),
        "latency_seconds": {"p50": _pct(wall, 0.5), "p95": _pct(wall, 0.95), "max": max(wall) if wall else None},
        "model": {"calls": sum(r.model_calls for r in results), "tokens": sum(r.model_tokens for r in results),
                  "usd": round(usd, 6)},
        "cost_per_accepted": {"model_usd": _ratio(usd, accepted) if accepted else None,
                              "cpu_seconds": _ratio(cpu, accepted) if accepted else None,
                              "wall_seconds": _ratio(sum(wall), accepted) if accepted else None,
                              "infrastructure_usd": (_ratio(infra, accepted) if accepted and infra is not None else "unpriced")},
    }


def summarize(results: list[TaskResult], **kw: Any) -> dict[str, Any]:
    return {"overall": metrics(results, **kw),
            "by_domain": {d: metrics([r for r in results if r.domain == d], **kw) for d in dict.fromkeys(r.domain for r in results)},
            "by_family": {f: metrics([r for r in results if r.family == f], **kw) for f in dict.fromkeys(r.family for r in results)}}


@dataclass
class Run:
    tier: str
    corpus_version: int
    corpus_sha256: str
    results: list[TaskResult]
    summary: dict[str, Any]
    seconds: float
    lock_ok: bool
    lock_mismatches: list[str]


def check_lock(corpus: Corpus, path: Path = LOCK) -> list[str]:
    """Tasks whose data, reference or rubric no longer hash to the frozen lock (empty = frozen)."""
    if not path.exists():
        return ["corpus.lock.json is missing"]
    lock = json.loads(path.read_text())
    if lock.get("version") != corpus.version:
        return [f"lock is for corpus version {lock.get('version')}, corpus.yaml is version {corpus.version}"]
    got = lock_entries(corpus)["tasks"]
    return sorted({*[t for t in got if lock["tasks"].get(t) != got[t]], *[t for t in lock["tasks"] if t not in got]})


def run(tier: str = "component", *, families: tuple[str, ...] = FAMILIES, only: set[str] | None = None,
        cpu_usd_per_hour: float | None = None, corpus: Corpus | None = None) -> Run:
    corpus = corpus or load_corpus()
    started = time.perf_counter()
    mismatches = check_lock(corpus)
    tasks = [t for t in corpus.tasks if t.family in families and (only is None or t.id in only)]
    results = [run_task(t, tier) for t in tasks]
    return Run(tier=tier, corpus_version=corpus.version, corpus_sha256=corpus.digest, results=results,
               summary=summarize(results, cpu_usd_per_hour=cpu_usd_per_hour), seconds=round(time.perf_counter() - started, 1),
               lock_ok=not mismatches, lock_mismatches=mismatches)


def gate_metrics(r: Run) -> dict[str, Any]:
    """Flat metrics for config/eval_gates.yaml (tier `heldout`)."""
    o = r.summary["overall"]
    return {"accepted_output_rate": o["accepted_output_rate"], "confident_wrong": o["confident_wrong"],
            "abstention_recall": o["abstention_recall"], "errors": o["errors"], "tasks": o["tasks"],
            "corpus_frozen": 1.0 if r.lock_ok else 0.0, "latency_p95_seconds": o["latency_seconds"]["p95"],
            "task_success_rate": o["task_success_rate"]}


def as_dict(r: Run) -> dict[str, Any]:
    return {"tier": r.tier, "corpus_version": r.corpus_version, "corpus_sha256": r.corpus_sha256, "seconds": r.seconds,
            "lock_ok": r.lock_ok, "lock_mismatches": r.lock_mismatches, "summary": r.summary,
            "results": [asdict(x) for x in r.results]}


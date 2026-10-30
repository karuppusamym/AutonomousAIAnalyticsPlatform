"""Steps over runs, Ask threads and notebooks (spec v4 §7, P7-04).

A step's identity (`AnalysisStep`) is stable; every execution is a new immutable version
(`AnalysisStepVersion`). The lifecycle of one version:

1. **Create** — `version + 1` with its spec, and in the same transaction `void_dependents(query,
   step:<id>, <new version id>)`: the previous version's verdict and every verdict that depended on it
   (downstream steps, forks, pins) turn VOID with the cause (ADR-0020, event `step.edited`).
2. **Execute** — through one `Runtime`: SQL and compiled `SemanticQuery`s through `QueryGateway`, an
   `AnalysisSpec` through `skills.analysis.run_analysis` on the gateway runner, restricted Python through
   `execute_python` (the `compute-py` pool when configured, else the sandbox) on upstream results only.
3. **Self-check** — `skills/selfcheck.py`. A failed check with a safe correction is applied and the
   step re-runs (at most `MAX_CORRECTIONS` rounds, each recorded); anything else flags the step.
4. **Record** — the result snapshot as an artifact (`ArtifactRef`), the receipts, the checks, and a
   `VerificationRecord` (subject `step`) whose dependencies are the step's own version, the versions of
   the steps it read, the data versions, the policy, semantic metrics and the method.

Editing re-runs the edited step and then its dependents in the same branch, each as a new version;
dependents in other branches are voided (shown void with their cause) and re-run only when asked.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.step import STEP_SNAPSHOT_KIND, ArtifactRef, Step, StepCheck, StepEdit, StepIn
from analystos.core.errors import AnalystOSError, InvalidInput, NotFound, PreconditionFailed
from analystos.core.ids import new_id, ordered_utcnow, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    AnalysisStep,
    AnalysisStepVersion,
    Artifact,
    Notebook,
    StepBranch,
    User,
)
from analystos.events.bus import emit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.skills import selfcheck

log = get_logger(__name__)
EXECUTED_KINDS = ("query", "method", "chart", "claim")
RECORDED_KINDS = ("plan", "recipe", "train")
CONTAINERS = ("run", "ask_thread", "notebook")
MAX_CORRECTIONS = 2
PURPOSE = "step"
PYTHON_IMPORTS = ("math", "statistics", "json", "numpy", "pandas")
SNAPSHOT_ROWS = 500


# ------------------------------------------------------------------------------------ runtime
class Runtime(Protocol):
    asset_sources: dict[str, str]
    dialect: str

    def sql(self, sql: str, *, source_id: str | None, step_id: str) -> Any: ...

    def semantic(self, query: dict[str, Any], *, step_id: str) -> tuple[str, dict[str, Any], Any]: ...

    def analysis(self, spec: dict[str, Any], *, step_id: str) -> dict[str, Any]: ...

    def python(self, code: str, inputs: dict[str, Any]) -> dict[str, Any]: ...


def execute_python(code: str, inputs: dict[str, Any], *, timeout_s: float = 30, memory_mb: int = 1024,
                   workspace_id: str | None = None) -> dict[str, Any]:
    """The one place a step's Python runs: the isolated `compute-py` pool when this installation runs it
    (P7-06; a pool failure raises, it never falls back), otherwise the sandbox (`sandbox/runner.py`,
    isolated, rlimited, no network, allow-listed numeric imports). Either way the code sees only `inputs`
    (upstream step results) and can reach no connection of any kind."""
    from analystos.workers.dispatch import pool_configured

    if pool_configured("compute-py"):
        from analystos.workers.python import run_python_isolated

        return run_python_isolated(code, inputs, allowed_imports=PYTHON_IMPORTS, timeout_s=timeout_s,
                                   memory_mb=memory_mb, workspace_id=workspace_id)
    from analystos.sandbox.runner import run_python

    r = run_python(code, inputs=inputs, timeout_s=timeout_s, memory_mb=memory_mb, allowed_imports=PYTHON_IMPORTS)
    return {"ok": r.ok, "result": r.result, "error": r.error, "stdout": r.stdout[-4000:], "duration_ms": r.duration_ms,
            "timed_out": r.timed_out, "isolation": r.isolation, "network_isolated": r.network_isolated}


class GatewayRuntime:
    """The governed runtime: the caller's resolved scope, the `sql.execute` tool gate and Ask budget, then
    `QueryGateway` — the same path as an Ask rerun with edited SQL."""

    def __init__(self, user: User, workspace_id: str):
        from analystos.services.ask import adhoc_context

        with session_scope() as s:
            self.ctx = adhoc_context(s, user, workspace_id)
        self.user, self.workspace_id = user, workspace_id
        self.asset_sources = dict(self.ctx.scope.asset_sources)
        self.dialect = next(iter(self.ctx.scope.source_dialects.values()), "postgres")

    def _gate(self) -> None:
        from analystos.agents.sql_agent import _authorize_ask, _check_budget

        _authorize_ask(self.ctx)
        _check_budget(self.ctx)

    @property
    def _actor(self) -> str:
        from analystos.governance.budgets import ask_actor

        return ask_actor(self.user.id)

    def sql(self, sql: str, *, source_id: str | None, step_id: str) -> Any:
        self._gate()
        gw = self.ctx.services.gateway
        if source_id:
            return gw.run_sql_for(self.ctx.scope, actor=self._actor, task_id=step_id, source_id=source_id)(sql, purpose=PURPOSE)
        return gw.execute(self.ctx.scope, sql, actor=self._actor, purpose=PURPOSE, run_id=None, task_id=step_id,
                          use_cache=False)

    def semantic(self, query: dict[str, Any], *, step_id: str) -> tuple[str, dict[str, Any], Any]:
        from analystos.contracts.semantic import SemanticQuery
        from analystos.semantic.compiler import compile_query

        catalog = self.ctx.semantic_catalog
        if not catalog:
            raise InvalidInput("a metric query needs an approved semantic model in this workspace")
        compiled = compile_query(SemanticQuery.model_validate(query), catalog, self.ctx.scope)
        compiled.provenance["policy_hash"] = stable_hash(self.ctx.policy.model_dump(mode="json"))
        return compiled.sql, compiled.provenance, self.sql(compiled.sql, source_id=None, step_id=step_id)

    def analysis(self, spec: dict[str, Any], *, step_id: str) -> dict[str, Any]:
        from analystos.contracts.analysis import AnalysisSpec
        from analystos.skills.analysis import run_analysis

        self._gate()
        a = AnalysisSpec.model_validate(spec)
        if a.joins:
            from analystos.agents.investigator import _join_errors
            from analystos.db.models import Relationship, SourceAsset
            from analystos.services.relationship_safety import admissible
            from analystos.skills.lookups import Lookup

            # Notebook methods use the same reviewed relationships as investigations. A syntactically
            # valid SQL join can still multiply rows, so the gateway's scope check is insufficient.
            with session_scope() as s:
                assets = list(s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == self.workspace_id)))
                by_id = {row.id: row for row in assets}
                names = {row.id: f"{row.schema_name}.{row.name}" for row in assets}
                rels = s.scalars(select(Relationship).where(Relationship.workspace_id == self.workspace_id,
                    Relationship.validated.is_(True) | (Relationship.origin == "user"),
                    Relationship.cardinality.in_(("many_to_one", "one_to_one"))))
                lookups = [Lookup(names[r.from_asset_id], r.from_column, names[r.to_asset_id], r.to_column)
                           for r in rels if r.from_asset_id in names and r.to_asset_id in names
                           and not (r.evidence or {}).get("rejected") and admissible(s, r, by_id[r.to_asset_id])]
            errors = _join_errors(a, self.ctx.scope, lookups)
            if errors:
                raise InvalidInput("analysis join is not approved: " + "; ".join(errors[:3]))
        runner = self.ctx.services.gateway.run_sql_for(self.ctx.scope, actor=self._actor, task_id=step_id,
                                                       source_id=self.ctx.scope.asset_sources.get(a.asset))
        out = run_analysis(a, runner, alpha=self.ctx.policy.alpha)
        return {"stat": out.stat.model_dump(mode="json"), "table": out.table, "query_ids": list(out.query_ids),
                "sql": list(out.sql), "asset": a.asset}

    def python(self, code: str, inputs: dict[str, Any]) -> dict[str, Any]:
        return execute_python(code, inputs, workspace_id=self.workspace_id)


def _runtime(user: User, workspace_id: str, runtime: Runtime | None) -> Runtime:
    return runtime if runtime is not None else GatewayRuntime(user, workspace_id)


# ------------------------------------------------------------------------------------ specs
def validate_spec(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The shape each kind needs (contracts/step.py); refused before anything is stored or run."""
    if not isinstance(spec, dict):
        raise InvalidInput("spec must be an object")
    spec = dict(spec)
    if kind == "query":
        if bool(spec.get("sql")) == bool(spec.get("semantic_query")):
            raise InvalidInput("a query step needs exactly one of sql or semantic_query")
        if spec.get("sql") is not None and (not isinstance(spec["sql"], str) or not spec["sql"].strip()):
            raise InvalidInput("sql must be a non-empty string")
        if spec.get("semantic_query") is not None:
            from pydantic import ValidationError

            from analystos.contracts.semantic import SemanticQuery

            try:
                spec["semantic_query"] = SemanticQuery.model_validate(spec["semantic_query"]).model_dump(mode="json")
            except ValidationError as exc:
                raise InvalidInput("semantic_query is not valid: " + "; ".join(e["msg"] for e in exc.errors()[:3])) from None
    elif kind == "method":
        if spec.get("analysis_spec") is not None:
            from pydantic import ValidationError

            from analystos.contracts.analysis import AnalysisSpec

            try:
                spec["analysis_spec"] = AnalysisSpec.model_validate(spec["analysis_spec"]).model_dump(mode="json")
            except ValidationError as exc:
                raise InvalidInput("analysis_spec is not valid: " + "; ".join(e["msg"] for e in exc.errors()[:3])) from None
        elif isinstance(spec.get("code"), str):
            from analystos.sandbox.runner import check_code

            if problems := check_code(spec["code"], PYTHON_IMPORTS):
                raise InvalidInput("the Python is refused by the sandbox policy: " + "; ".join(problems[:5]),
                                   details={"problems": problems})
        else:
            raise InvalidInput("a method step needs an analysis_spec or Python code")
    elif kind == "chart":
        chart = spec.get("chart")
        if not isinstance(chart, dict) or not chart.get("type"):
            raise InvalidInput("a chart step needs chart: {type, x, y}")
    elif kind == "claim":
        if not isinstance(spec.get("text"), str):
            raise InvalidInput("a claim step needs text")
    return spec


def spec_hash(kind: str, spec: dict[str, Any]) -> str:
    return stable_hash({"kind": kind, "spec": spec})


# ------------------------------------------------------------------------------------ access
@scoped_loader
def load_container(session: Session, user: User, container_type: str, container_id: str, workspace_id: str | None = None,
                   minimum: str = "viewer") -> Any:
    """The run, Ask thread (private to its author) or notebook a step thread belongs to."""
    if container_type == "run":
        return load_in_workspace(session, AnalysisRun, container_id, workspace_id, user=user, minimum=minimum, label="run")
    if container_type == "notebook":
        return load_in_workspace(session, Notebook, container_id, workspace_id, user=user, minimum=minimum, label="notebook")
    if container_type == "ask_thread":
        from analystos.services.ask import _thread_for

        thread = _thread_for(session, user, container_id, workspace_id)
        require_role(session, user, thread.workspace_id, minimum)
        return thread
    raise InvalidInput(f"container must be one of {CONTAINERS}")


@scoped_loader
def load_step(session: Session, user: User, step_id: str, workspace_id: str | None = None, minimum: str = "viewer") -> AnalysisStep:
    step = load_in_workspace(session, AnalysisStep, step_id, workspace_id, user=user, minimum=minimum, label="step")
    try:
        load_container(session, user, step.container_type, step.container_id, step.workspace_id, minimum)
    except NotFound:
        raise NotFound("step not found") from None
    return step


@scoped_loader
def load_branch(session: Session, user: User, branch_id: str, workspace_id: str | None = None, minimum: str = "viewer") -> StepBranch:
    branch = load_in_workspace(session, StepBranch, branch_id, workspace_id, user=user, minimum=minimum, label="branch")
    try:
        load_container(session, user, branch.container_type, branch.container_id, branch.workspace_id, minimum)
    except NotFound:
        raise NotFound("branch not found") from None
    return branch


# ------------------------------------------------------------------------------------ persistence
def main_branch(session: Session, workspace_id: str, container_type: str, container_id: str, actor: str) -> StepBranch:
    b = session.scalar(select(StepBranch).where(StepBranch.container_type == container_type,
                                                StepBranch.container_id == container_id,
                                                StepBranch.parent_branch_id.is_(None)).order_by(StepBranch.created_at).limit(1))
    if b is None:
        b = StepBranch(id=new_id("brn"), workspace_id=workspace_id, container_type=container_type, container_id=container_id,
                       name="main", base=[], status="open", merged_into=[], created_by=actor, created_at=ordered_utcnow())
        session.add(b)
        session.flush()
    return b


def version_row(session: Session, step: AnalysisStep, version: int | None = None) -> AnalysisStepVersion:
    v = session.scalar(select(AnalysisStepVersion).where(AnalysisStepVersion.step_id == step.id,
                                                         AnalysisStepVersion.version == (version or step.current_version)))
    if v is None:
        raise NotFound(f"step {step.id} has no version {version or step.current_version}")
    return v


def _next_seq(session: Session, branch_id: str) -> int:
    return (session.scalar(select(func.max(AnalysisStep.seq)).where(AnalysisStep.branch_id == branch_id)) or 0) + 1


def add_step(session: Session, *, workspace_id: str, branch: StepBranch, kind: str, title: str, spec: dict[str, Any],
             depends_on: Iterable[str], origin: dict[str, Any], actor: str, chart_spec: dict[str, Any] | None = None,
             forked_from: dict[str, Any] | None = None, reason: str = "created", seq: int | None = None) -> tuple[AnalysisStep, AnalysisStepVersion]:
    spec = validate_spec(kind, spec)
    deps = list(dict.fromkeys(depends_on))
    for d in deps:
        up = session.get(AnalysisStep, d)
        if up is None or up.container_type != branch.container_type or up.container_id != branch.container_id:
            raise InvalidInput(f"step {d} is not a step of this thread")
    step = AnalysisStep(id=new_id("stp"), workspace_id=workspace_id, branch_id=branch.id, container_type=branch.container_type,
                        container_id=branch.container_id, seq=seq or _next_seq(session, branch.id), kind=kind,
                        title=(title or kind)[:300], depends_on=deps, current_version=1, status="pending", origin=origin,
                        forked_from=forked_from, created_by=actor, created_at=ordered_utcnow())
    session.add(step)
    session.flush()
    ver = AnalysisStepVersion(id=new_id("stpv"), step_id=step.id, workspace_id=workspace_id, version=1, spec=spec,
                              spec_hash=spec_hash(kind, spec), inputs={}, status="pending", reason=reason, receipts=[],
                              chart_spec=chart_spec, checks=[], corrections=[], created_by=actor, created_at=ordered_utcnow())
    session.add(ver)
    session.flush()
    emit(workspace_id, "step.created", {"step_id": step.id, "kind": kind, "branch_id": branch.id,
                                        "container": {"type": branch.container_type, "id": branch.container_id}},
         run_id=branch.container_id if branch.container_type == "run" else None, actor=actor, session=session)
    return step, ver


def new_version(session: Session, step: AnalysisStep, *, actor: str, reason: str, spec: dict[str, Any] | None = None,
                title: str | None = None, chart_spec: dict[str, Any] | None = None) -> tuple[AnalysisStepVersion, list[str]]:
    """version + 1, and — in this transaction — void every verdict that depended on the previous version."""
    from analystos.evidence.verification import void_dependents

    old = version_row(session, step)
    spec = validate_spec(step.kind, spec) if spec is not None else dict(old.spec)
    ver = AnalysisStepVersion(id=new_id("stpv"), step_id=step.id, workspace_id=step.workspace_id, version=old.version + 1,
                              spec=spec, spec_hash=spec_hash(step.kind, spec), inputs={}, status="pending", reason=reason,
                              receipts=[], chart_spec=chart_spec if chart_spec is not None else old.chart_spec, checks=[],
                              corrections=[], created_by=actor, created_at=ordered_utcnow())
    session.add(ver)
    step.current_version, step.status = ver.version, "pending"
    if title:
        step.title = title[:300]
    session.flush()
    why = {"edited": "edited", "rerun": "re-run", "upstream_changed": "re-run after an upstream step changed"}.get(reason, reason)
    voided = void_dependents(session, "query", f"step:{step.id}", ver.id, f"step '{step.title}' {why} (v{old.version} -> "
                             f"v{ver.version})", event="step.edited")
    emit(step.workspace_id, "step.edited", {"step_id": step.id, "version": ver.version, "reason": reason,
                                            "voided_records": voided},
         run_id=step.container_id if step.container_type == "run" else None, actor=actor, session=session)
    return ver, voided


def _save_snapshot(session: Session, step: AnalysisStep, ver: AnalysisStepVersion, content: dict[str, Any], actor: str) -> dict[str, Any]:
    from analystos.artifacts.registry import save_artifact

    art = save_artifact(session, workspace_id=step.workspace_id, type_="step_result", name=f"{step.id}@v{ver.version}",
                        content=content, creator_user=actor.removeprefix("user:") if actor.startswith("user:") else None,
                        status="final")
    return ArtifactRef(artifact_id=art.id, version=art.version, kind=STEP_SNAPSHOT_KIND, content_hash=art.content_hash,
                       media_type="application/json").model_dump(mode="json")


def snapshot_content(session: Session, ref: dict[str, Any] | None) -> dict[str, Any]:
    if not ref:
        return {}
    art = session.get(Artifact, ref.get("id"))
    return dict(art.content or {}) if art is not None else {}


# ------------------------------------------------------------------------------------ observation context
def _asset_rows(session: Session, workspace_id: str, assets: Iterable[str]) -> dict[str, Any]:
    from analystos.db.models import SourceAsset

    out = {}
    for fq in set(assets):
        schema, _, name = fq.partition(".")
        a = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                                     SourceAsset.name == name))
        if a is not None:
            out[fq] = a
    return out


def relationships(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    from analystos.db.models import Relationship, SourceAsset

    assets = {a.id: f"{a.schema_name}.{a.name}" for a in session.scalars(select(SourceAsset).where(
        SourceAsset.workspace_id == workspace_id))}
    return [{"from_table": assets[r.from_asset_id], "from_column": r.from_column, "to_table": assets[r.to_asset_id],
             "to_column": r.to_column, "cardinality": r.cardinality, "validated": r.validated}
            for r in session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id))
            if r.from_asset_id in assets and r.to_asset_id in assets]


def column_values(session: Session, rows: dict[str, Any]) -> dict[str, list[Any]]:
    from analystos.db.models import SourceColumn

    out: dict[str, list[Any]] = {}
    for _fq, a in rows.items():
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id)):
            if set(c.tags or []) & {"pii", "restricted", "sensitive"}:
                continue
            vals = [tv.get("value") for tv in (c.profile or {}).get("top_values") or [] if isinstance(tv, dict)]
            if vals:
                out[f"{a.name.lower()}.{c.name.lower()}"] = vals
    return out


def dq_failures(session: Session, workspace_id: str, assets: Iterable[str]) -> list[dict[str, Any]]:
    """Gate results of the newest recipe run that produced each table read."""
    from analystos.db.models import RecipeRun

    wanted, out, seen = set(assets), [], set()
    for run in session.scalars(select(RecipeRun).where(RecipeRun.workspace_id == workspace_id)
                               .order_by(RecipeRun.created_at.desc()).limit(200)):
        for name, o in (run.outputs or {}).items():
            table = (o or {}).get("table")
            if table not in wanted or table in seen:
                continue
            seen.add(table)
            for g in ((run.gates or {}).get(name) or {}).get("gates") or []:
                out.append({"asset": table, "gate": g.get("key") or g.get("gate") or g.get("type"),
                            "status": g.get("status"), "detail": g.get("detail"), "recipe_run_id": run.id})
    return out


def _history(session: Session, step: AnalysisStep, before: int) -> list[float]:
    vals = []
    for v in session.scalars(select(AnalysisStepVersion).where(AnalysisStepVersion.step_id == step.id,
                                                               AnalysisStepVersion.version < before,
                                                               AnalysisStepVersion.status.in_(("ok", "flagged")))
                             .order_by(AnalysisStepVersion.version.desc()).limit(10)):
        c = snapshot_content(session, v.result_snapshot)
        h = selfcheck.headline(c.get("columns") or [], c.get("rows") or [])
        if h is not None:
            vals.append(h)
    return vals


def _populations(runtime: Runtime, assets: Iterable[str]) -> dict[str, dict[str, Any]]:
    from analystos.staging.snapshots import population_for

    return {a: population_for(a, runtime.asset_sources.get(a)).check() for a in sorted(set(assets))}


def _observe(session: Session, step: AnalysisStep, ver: AnalysisStepVersion, runtime: Runtime, *, sql: str | None,
             table: dict[str, Any], assets: list[str], upstream_values: list[float] | None = None) -> selfcheck.Observation:
    rows = _asset_rows(session, step.workspace_id, assets)
    return selfcheck.Observation(kind=step.kind if step.kind != "method" else "query", sql=sql, dialect=runtime.dialect,
                                 columns=list(table.get("columns") or []), rows=list(table.get("rows") or []),
                                 row_count=table.get("row_count"), truncated=bool(table.get("truncated")),
                                 history=_history(session, step, ver.version), populations=_populations(runtime, assets),
                                 relationships=relationships(session, step.workspace_id) if sql else [],
                                 dq=dq_failures(session, step.workspace_id, assets), column_values=column_values(session, rows),
                                 text=ver.spec.get("text"), upstream_values=upstream_values or [])


# ------------------------------------------------------------------------------------ execution
def _table_of(result: Any) -> dict[str, Any]:
    return {"columns": list(result.columns), "rows": [list(r) for r in result.rows[:SNAPSHOT_ROWS]], "row_count": result.row_count,
            "truncated": bool(result.truncated), "result_hash": result.result_hash}


def _receipt(result: Any, sql: str, role: str) -> dict[str, Any]:
    from analystos.knowledge.attested import sql_hash

    return {"kind": "query", "role": role, "query_id": result.query_id, "sql": sql, "query_hash": sql_hash(sql),
            "result_hash": result.result_hash, "rows": result.row_count, "truncated": bool(result.truncated),
            "referenced_assets": list(getattr(result, "referenced_assets", None) or [])}


def to_table(value: Any) -> dict[str, Any]:
    """A Python cell's `result` as a table: records, a mapping (one row) or a scalar."""
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        cols = list(dict.fromkeys(k for r in value for k in r))
        return {"columns": cols, "rows": [[r.get(c) for c in cols] for r in value[:SNAPSHOT_ROWS]], "row_count": len(value)}
    if isinstance(value, dict):
        return {"columns": list(value), "rows": [list(value.values())], "row_count": 1}
    if value is None:
        return {"columns": [], "rows": [], "row_count": 0}
    return {"columns": ["value"], "rows": [[value]], "row_count": 1}


def _number(v: Any) -> float | None:
    """A cell's numeric value: numbers, and numeric text such as a segment label "1" that a claim quotes."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    return None


def numbers_of(content: dict[str, Any]) -> list[float]:
    """Every number a claim reading this result may quote: result cells, the statistic, its groups, and the
    headline numbers the method derived (`stat.highlights`, e.g. a rate ratio a finding states as "3.0x")."""
    vals: list[float] = []
    for r in content.get("rows") or []:
        vals += [n for n in map(_number, r) if n is not None]
    stat = content.get("stat") or {}
    for part in (stat, stat.get("highlights") or {}, *(stat.get("groups") or [])):
        vals += [n for v in (part or {}).values() if not isinstance(v, str) and (n := _number(v)) is not None]
    return vals


class _Outcome:
    def __init__(self) -> None:
        self.table: dict[str, Any] = {}
        self.receipts: list[dict[str, Any]] = []
        self.checks: list[selfcheck.CheckResult] = []
        self.corrections: list[dict[str, Any]] = []
        self.assets: list[str] = []
        self.semantic: dict[str, Any] | None = None
        self.method: str | None = None
        self.snapshot_ref: dict[str, Any] | None = None  # reuse an upstream snapshot (charts)
        self.chart_spec: dict[str, Any] | None = None
        self.error: str | None = None
        self.status: str | None = None


def _run_query(session_factory: Any, step: AnalysisStep, ver: AnalysisStepVersion, runtime: Runtime, out: _Outcome) -> None:
    spec = ver.spec
    if spec.get("semantic_query"):
        sql, provenance, res = runtime.semantic(spec["semantic_query"], step_id=step.id)
        out.semantic = provenance
    else:
        sql = spec["sql"]
        res = runtime.sql(sql, source_id=spec.get("source_id"), step_id=step.id)
    for round_ in range(MAX_CORRECTIONS + 1):
        out.table = _table_of(res)
        out.assets = list(getattr(res, "referenced_assets", None) or [])
        out.receipts.append(_receipt(res, sql, "primary" if round_ == 0 else "corrected"))
        with session_factory() as s:
            obs = _observe(s, step, ver, runtime, sql=sql, table=out.table, assets=out.assets)
        out.checks = selfcheck.run(obs)
        fix = selfcheck.safe_correction(out.checks)
        if fix is None or round_ == MAX_CORRECTIONS or out.semantic is not None:
            break  # a governed (compiled) query is never rewritten: its failures flag the step
        out.corrections.append({"check": fix.check, "reason": fix.correction["reason"], "from_query_id": res.query_id,
                                "from_sql": sql, "to_sql": fix.correction["sql"], "round": round_ + 1})
        sql = fix.correction["sql"]
        res = runtime.sql(sql, source_id=spec.get("source_id"), step_id=step.id)
    for r in out.receipts[:-1]:
        r["role"] = "superseded_by_correction"


def _upstream(session: Session, step: AnalysisStep) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[str]]:
    """Upstream current version ids, their snapshot contents, and the ids of upstream steps with no result."""
    inputs, contents, missing = {}, {}, []
    for d in step.depends_on or []:
        up = session.get(AnalysisStep, d)
        if up is None:
            missing.append(d)
            continue
        v = version_row(session, up)
        inputs[d] = v.id
        if v.status not in ("ok", "flagged", "recorded") or (up.kind in EXECUTED_KINDS and not v.result_snapshot):
            missing.append(d)
        contents[d] = snapshot_content(session, v.result_snapshot)
    return inputs, contents, missing


def _dependencies(session: Session, step: AnalysisStep, ver: AnalysisStepVersion, out: _Outcome, runtime: Runtime) -> list[Any]:
    from analystos.evidence.manifest import current_entry
    from analystos.evidence.verification import UNKNOWABLE, Dependency, current_version

    deps = [Dependency("query", f"step:{step.id}", ver.id)]
    deps += [Dependency("query", f"step:{u}", vid) for u, vid in sorted(ver.inputs.items())]
    for asset in sorted(set(out.assets)):
        e = current_entry(session, asset, runtime.asset_sources.get(asset))
        if e.mode == "staged" and e.version:
            deps.append(Dependency("data", f"{e.source_id or ''}/{asset}", e.version))
    wanted = [("policy", step.workspace_id)]
    if out.method:
        wanted.append(("method", out.method))
    for m in (out.semantic or {}).get("metrics") or []:
        if m.get("name"):
            wanted.append(("semantic", f"{step.workspace_id}/{m['name']}"))
    for kind, ref in wanted:
        v = current_version(session, kind, ref)
        if v is not None and v != UNKNOWABLE:
            deps.append(Dependency(kind, ref, v))
    return sorted(set(deps))


def execute(user: User, step_id: str, *, runtime: Runtime | None = None) -> dict[str, Any]:
    """Execute the step's current (pending) version; returns the step view. Never raises for a failure of
    the step itself (a refused query, a sandbox error): the version records it as `failed`."""
    actor = f"user:{user.id}"
    with session_scope() as s:
        step = s.get(AnalysisStep, step_id)
        ver = version_row(s, step)
        inputs, contents, missing = _upstream(s, step)
        ver.inputs = inputs
        s.flush()
        s.expunge(step)
        s.expunge(ver)
    out = _Outcome()
    rt: Runtime | None = runtime
    if step.kind in RECORDED_KINDS:
        out.status = "recorded" if step.kind == "plan" or ver.spec.get("recorded") else "unsupported"
        if out.status == "unsupported":
            out.error = (f"a {step.kind} step runs through its own executor ({'recipes, P6' if step.kind == 'recipe' else 'ML, P5'}); "
                         "it is recorded here, not re-run")
    elif missing:
        out.status, out.error = "failed", f"upstream step(s) without a result: {', '.join(missing)}"
    else:
        try:
            rt = _runtime(user, step.workspace_id, runtime)
            _dispatch(step, ver, rt, out, contents)
        except AnalystOSError as exc:
            out.status, out.error = "failed", f"{exc.code}: {exc.message}"
    return _finish(step.id, ver.id, out, actor, rt)


def _dispatch(step: AnalysisStep, ver: AnalysisStepVersion, rt: Runtime, out: _Outcome, contents: dict[str, dict[str, Any]]) -> None:
    spec = ver.spec
    if step.kind == "query":
        _run_query(session_scope, step, ver, rt, out)
    elif step.kind == "method" and spec.get("analysis_spec"):
        res = rt.analysis(spec["analysis_spec"], step_id=step.id)
        stat = res["stat"]
        from analystos.evidence.manifest import spec_assets

        out.method, out.assets = str(spec["analysis_spec"].get("method")), spec_assets(spec["analysis_spec"])
        out.table = {"columns": list((res.get("table") or {}).get("columns") or []),
                     "rows": list((res.get("table") or {}).get("rows") or [])[:SNAPSHOT_ROWS],
                     "row_count": int(stat.get("n") or 0), "stat": stat}
        out.receipts = [{"kind": "query", "role": "method", "query_id": q, "sql": sql} for q, sql in zip(res["query_ids"],
                                                                                                        res["sql"], strict=False)]
        with session_scope() as s:
            obs = _observe(s, step, ver, rt, sql=None, table={**out.table, "rows": out.table["rows"] or [[stat.get("n")]],
                                                                "columns": out.table["columns"] or ["n"]}, assets=out.assets)
        out.checks = [selfcheck.empty_result(obs), selfcheck.truncation(obs), selfcheck.dq_gate(obs)]
    elif step.kind == "method":
        inputs = {k: [dict(zip(c.get("columns") or [], r, strict=False)) for r in c.get("rows") or []] for k, c in contents.items()}
        with session_scope() as s:  # notebook-friendly aliases: cell<N> by position
            for k in list(inputs):
                up = s.get(AnalysisStep, k)
                if up is not None:
                    inputs.setdefault(f"cell{up.seq}", inputs[k])
        started = time.perf_counter()
        r = rt.python(spec["code"], inputs)
        out.receipts = [{"kind": "python", "code_hash": stable_hash(spec["code"]), "isolation": r.get("isolation"),
                         "network_isolated": r.get("network_isolated"), "duration_ms": r.get("duration_ms") or
                         int((time.perf_counter() - started) * 1000), "timed_out": bool(r.get("timed_out")),
                         "inputs": dict(ver.inputs)}]
        if not r.get("ok"):
            out.status, out.error = "failed", f"python: {r.get('error') or 'failed'}"
            return
        out.table = to_table(r.get("result"))
        with session_scope() as s:
            obs = _observe(s, step, ver, rt, sql=None, table=out.table, assets=[])
        out.checks = [selfcheck.empty_result(obs), selfcheck.magnitude(obs)]
    elif step.kind == "chart":
        if len(step.depends_on or []) != 1:
            raise InvalidInput("a chart step draws exactly one upstream step")
        up = contents[step.depends_on[0]]
        chart = dict(ver.chart_spec or spec.get("chart") or {})
        cols = set(up.get("columns") or [])
        missing = [f for f in (chart.get("x"), chart.get("y")) if f and f not in cols]
        out.chart_spec, out.table = chart, {k: up.get(k) for k in ("columns", "rows", "row_count", "truncated", "result_hash")}
        out.checks = [selfcheck.CheckResult("chart_fields", not missing, f"not in the result: {', '.join(missing)}" if missing
                                            else "every charted field is in the result", evidence={"columns": sorted(cols)})]
    elif step.kind == "claim":
        values = [v for c in contents.values() for v in numbers_of(c)]
        obs = selfcheck.Observation(kind="claim", text=spec.get("text") or "", upstream_values=values)
        out.table = {"text": spec.get("text") or ""}
        out.checks = selfcheck.run(obs) if step.depends_on else []
        if not step.depends_on:
            out.status = "recorded"


def _finish(step_id: str, version_id: str, out: _Outcome, actor: str, runtime: Runtime | None) -> dict[str, Any]:
    from analystos.evidence.verification import record_verdict

    with session_scope() as s:
        step = s.get(AnalysisStep, step_id)
        ver = s.get(AnalysisStepVersion, version_id)
        corrected = {c["check"] for c in out.corrections}
        checks = []
        for r in out.checks:
            d = StepCheck(check=r.check, passed=r.passed, severity=r.severity if not r.passed else "info", detail=r.detail,
                          evidence=r.evidence, corrected=r.check in corrected and r.passed).model_dump(mode="json")
            if d["corrected"]:
                fix = next(c for c in out.corrections if c["check"] == r.check)
                d["detail"] = f"corrected ({fix['reason']}); now: {r.detail}"
            checks.append(d)
        for c in out.corrections:  # a check the correction made pass is still reported, with the correction
            if not any(x["check"] == c["check"] for x in checks):
                checks.append(StepCheck(check=c["check"], passed=True, severity="info", corrected=True,
                                        detail=f"corrected: {c['reason']}").model_dump(mode="json"))
        ver.receipts, ver.checks, ver.corrections = out.receipts, checks, out.corrections
        ver.chart_spec = out.chart_spec or ver.chart_spec
        if out.status in ("failed", "unsupported"):
            ver.status, ver.error = out.status, out.error
        elif out.status == "recorded" and not out.checks:
            ver.status = "recorded"
        else:
            ok = selfcheck.passed(out.checks)
            ver.status = "ok" if ok else "flagged"
        if out.table and ver.status in ("ok", "flagged", "recorded"):
            ver.result_snapshot = out.snapshot_ref or _save_snapshot(s, step, ver, {**out.table, "step_id": step.id,
                                                                                    "version": ver.version,
                                                                                    **({"semantic": out.semantic} if out.semantic else {})},
                                                                     actor)
        if ver.status in ("ok", "flagged") and runtime is not None:
            rec = record_verdict(s, workspace_id=step.workspace_id,
                                 run_id=step.container_id if step.container_type == "run" else None, subject_type="step",
                                 subject_id=step.id, verdict="verified" if ver.status == "ok" else "failed_verification",
                                 checks=checks, verifier=selfcheck.VERSION, dependencies=_dependencies(s, step, ver, out, runtime),
                                 question_hash=ver.spec_hash)
            ver.verification_record_id = rec.id
        ver.finished_at = utcnow()
        step.status = ver.status
        emit(step.workspace_id, "step.flagged" if ver.status == "flagged" else "step.executed",
             {"step_id": step.id, "version": ver.version, "status": ver.status, "corrections": len(out.corrections),
              "failed_checks": [c["check"] for c in checks if not c["passed"]]},
             run_id=step.container_id if step.container_type == "run" else None, actor=actor, session=s)
        s.flush()
        return view(s, step, ver)


# ------------------------------------------------------------------------------------ read side
def view(session: Session, step: AnalysisStep, ver: AnalysisStepVersion | None = None, *, inherited: bool = False) -> dict[str, Any]:
    from analystos.db.models import VerificationRecord
    from analystos.evidence.verification import state_of

    ver = ver or version_row(session, step)
    rec = session.get(VerificationRecord, ver.verification_record_id) if ver.verification_record_id else None
    return Step(id=step.id, version=ver.version, current_version=step.current_version, kind=step.kind, title=step.title,  # type: ignore[arg-type]
                status=ver.status, spec=ver.spec, spec_hash=ver.spec_hash, receipts=ver.receipts,  # type: ignore[arg-type]
                result_snapshot=ArtifactRef.model_validate(ver.result_snapshot) if ver.result_snapshot else None,
                chart_spec=ver.chart_spec, checks=[StepCheck.model_validate(c) for c in ver.checks or []],
                corrections=ver.corrections or [], verification_record=state_of(rec) if rec is not None else None,
                depends_on=list(step.depends_on or []), inputs=dict(ver.inputs or {}), branch_id=step.branch_id,
                container={"type": step.container_type, "id": step.container_id}, seq=step.seq, origin=step.origin or {},
                forked_from=step.forked_from, reason=ver.reason, error=ver.error, inherited=inherited,
                created_by=ver.created_by, created_at=ver.created_at.isoformat() if ver.created_at else None).model_dump(mode="json")


def versions(session: Session, step: AnalysisStep) -> list[dict[str, Any]]:
    return [view(session, step, v) for v in session.scalars(select(AnalysisStepVersion).where(
        AnalysisStepVersion.step_id == step.id).order_by(AnalysisStepVersion.version.desc()))]


def with_result(session: Session, step: AnalysisStep, version: int | None = None) -> dict[str, Any]:
    ver = version_row(session, step, version)
    return {**view(session, step, ver), "result": snapshot_content(session, ver.result_snapshot)}


def effective_steps(session: Session, branch: StepBranch) -> list[tuple[AnalysisStep, AnalysisStepVersion, bool]]:
    """A branch as shown: the steps it inherited at their fork-time versions, then its own at current versions."""
    out = []
    for b in branch.base or []:
        st = session.get(AnalysisStep, b["step_id"])
        if st is not None:
            out.append((st, version_row(session, st, int(b["version"])), True))
    for st in session.scalars(select(AnalysisStep).where(AnalysisStep.branch_id == branch.id).order_by(AnalysisStep.seq)):
        out.append((st, version_row(session, st), False))
    return out


def thread(session: Session, branch: StepBranch) -> dict[str, Any]:
    from analystos.services.branches import branch_view

    return {"branch": branch_view(branch), "steps": [view(session, st, v, inherited=inh) for st, v, inh in
                                                     effective_steps(session, branch)]}


# ------------------------------------------------------------------------------------ edit and re-run
def downstream(session: Session, step: AnalysisStep) -> list[str]:
    """Steps of the same branch that depend on this one, transitively, in dependency order."""
    rows = list(session.scalars(select(AnalysisStep).where(AnalysisStep.branch_id == step.branch_id)
                                .order_by(AnalysisStep.seq)))
    affected: set[str] = {step.id}
    order: list[str] = []
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r.id not in affected and set(r.depends_on or []) & affected:
                affected.add(r.id)
                changed = True
    remaining = [r for r in rows if r.id in affected and r.id != step.id]
    done = {step.id}
    while remaining:
        ready = [r for r in remaining if not (set(r.depends_on or []) & (affected - done))]
        if not ready:
            raise InvalidInput("the steps form a dependency cycle")
        for r in ready:
            order.append(r.id)
            done.add(r.id)
        remaining = [r for r in remaining if r.id not in done]
    return order


def _revise(user: User, step_id: str, *, reason: str, edit: StepEdit | None = None, expected_version: int | None = None,
            runtime: Runtime | None = None) -> dict[str, Any]:
    actor = f"user:{user.id}"
    with session_scope() as s:
        step = s.get(AnalysisStep, step_id, with_for_update=expected_version is not None)
        if expected_version is not None and expected_version != step.current_version:
            raise PreconditionFailed(f"step {step.id} is at version {step.current_version}, not {expected_version}",
                                     details={"current_version": step.current_version})
        if reason == "edited" and edit is not None and edit.spec is None and edit.title is None and edit.chart_spec is None:
            raise InvalidInput("nothing to change")
        _, voided = new_version(s, step, actor=actor, reason=reason, spec=edit.spec if edit else None,
                                title=edit.title if edit else None, chart_spec=edit.chart_spec if edit else None)
        after = downstream(s, step)
    rt = _runtime(user, _workspace_of(step_id), runtime) if runtime is None else runtime
    result = execute(user, step_id, runtime=rt)
    rerun = []
    for d in after:
        with session_scope() as s:
            _, more = new_version(s, s.get(AnalysisStep, d), actor=actor, reason="upstream_changed")
            voided += more
        rerun.append(execute(user, d, runtime=rt))
    return {"step": result, "rerun": rerun, "voided_records": sorted(set(voided))}


def _workspace_of(step_id: str) -> str:
    with session_scope() as s:
        return s.get(AnalysisStep, step_id).workspace_id


def edit(user: User, step_id: str, body: StepEdit, *, expected_version: int | None, runtime: Runtime | None = None) -> dict[str, Any]:
    """version + 1 with the edit; re-run it and its dependents; their earlier verdicts are VOID."""
    return _revise(user, step_id, reason="edited", edit=body, expected_version=expected_version, runtime=runtime)


def rerun(user: User, step_id: str, *, runtime: Runtime | None = None) -> dict[str, Any]:
    """Re-verification is explicit: a new version of the same spec on today's data, and its dependents."""
    return _revise(user, step_id, reason="rerun", runtime=runtime)


def create(user: User, workspace_id: str, branch_id: str, body: StepIn, *, runtime: Runtime | None = None,
           origin: dict[str, Any] | None = None) -> dict[str, Any]:
    with session_scope() as s:
        branch = s.get(StepBranch, branch_id)
        if branch is None or branch.workspace_id != workspace_id:
            raise NotFound("branch not found")
        own = {st.id for st in s.scalars(select(AnalysisStep).where(AnalysisStep.branch_id == branch.id))}
        visible = own | {b["step_id"] for b in branch.base or []}
        if bad := [d for d in body.depends_on if d not in visible]:
            raise InvalidInput(f"not a step of this branch: {', '.join(bad)}")
        step, _ = add_step(s, workspace_id=workspace_id, branch=branch, kind=body.kind, title=body.title or body.kind,
                           spec=body.spec, depends_on=body.depends_on, origin=origin or {"type": "user"},
                           actor=f"user:{user.id}", chart_spec=body.chart_spec)
        step_id = step.id
    return execute(user, step_id, runtime=runtime)


# ------------------------------------------------------------------------------------ ingest runs and Ask threads
def _record_copy(session: Session, step: AnalysisStep, ver: AnalysisStepVersion, *, verdict: str, checks: list[dict[str, Any]],
                 extra: Iterable[Any] = (), run_id: str | None = None) -> None:
    from analystos.evidence.verification import Dependency, record_verdict

    deps = [Dependency("query", f"step:{step.id}", ver.id), *[Dependency("query", f"step:{u}", v) for u, v in ver.inputs.items()],
            *extra]
    rec = record_verdict(session, workspace_id=step.workspace_id, run_id=run_id, subject_type="step", subject_id=step.id,
                         verdict=verdict, checks=checks, verifier=selfcheck.VERSION, dependencies=deps,
                         question_hash=ver.spec_hash)
    ver.verification_record_id = rec.id


def _ingested(session: Session, container_type: str, container_id: str) -> dict[str, str]:
    return {(st.origin or {}).get("id"): st.id for st in session.scalars(select(AnalysisStep).where(
        AnalysisStep.container_type == container_type, AnalysisStep.container_id == container_id)) if (st.origin or {}).get("id")}


def ingest_ask_thread(user: User, workspace_id: str, thread_id: str, *, view: bool = True) -> dict[str, Any]:
    """Each answered turn becomes a query step (a step-by-step answer, one per answered step): version 1 =
    the recorded answer, self-checked on its stored result; no query is re-run. Idempotent: turns already
    ingested are skipped. `view=False` (recording after an answer) skips building the thread view nobody reads."""
    from analystos.db.models import AskTurn

    actor = f"user:{user.id}"
    with session_scope() as s:
        load_container(s, s.merge(user), "ask_thread", thread_id, workspace_id)
        branch = main_branch(s, workspace_id, "ask_thread", thread_id, actor)
        done = _ingested(s, "ask_thread", thread_id)
        for turn in s.scalars(select(AskTurn).where(AskTurn.thread_id == thread_id, AskTurn.status == "answered")
                              .order_by(AskTurn.seq)):
            for origin_id, title, sql, chart, result in _turn_queries(turn):
                if origin_id in done:
                    continue
                _ingest_query(s, workspace_id, branch, actor, turn, origin_id, title, sql, chart, result)
        return thread(s, branch) if view else {"branch_id": branch.id}


def _turn_queries(turn: Any) -> list[tuple[str, str, str, Any, dict[str, Any]]]:
    """The queries an answered turn ran: one for a quick answer, one per answered step of a step-by-step
    (analyst) answer, so no step of it is missing from the Data Thread."""
    steps = [st for st in ((turn.analysis or {}).get("steps") or []) if st.get("status") == "answered" and st.get("sql")]
    if steps:
        return [(f"{turn.id}:step{st['n']}", f"{turn.question} — step {st['n']}: {st.get('goal') or st.get('question')}"[:300],
                 st["sql"], st.get("chart"), dict(st.get("result") or {})) for st in steps]
    return [(turn.id, turn.question[:300], turn.sql, turn.chart, dict(turn.result or {}))] if turn.sql else []


def _ingest_query(s: Session, workspace_id: str, branch: StepBranch, actor: str, turn: Any, origin_id: str, title: str,
                  sql: str, chart: Any, res: dict[str, Any]) -> None:
    from analystos.evidence.verification import UNKNOWABLE, Dependency, current_version

    spec = {"sql": sql}
    step, ver = add_step(s, workspace_id=workspace_id, branch=branch, kind="query", title=title,
                         spec=spec, depends_on=[], origin={"type": "ask_turn", "id": origin_id}, actor=actor,
                         chart_spec=chart if isinstance(chart, dict) else None, reason="ingested")
    table = {k: res.get(k) for k in ("columns", "rows", "row_count", "truncated", "result_hash")}
    obs = selfcheck.Observation(sql=sql, columns=list(res.get("columns") or []), rows=list(res.get("rows") or []),
                                row_count=res.get("row_count"), truncated=bool(res.get("truncated")),
                                relationships=relationships(s, workspace_id))
    results = [selfcheck.empty_result(obs), selfcheck.truncation(obs), selfcheck.grouping(obs), selfcheck.fanout(obs)]
    checks = [StepCheck(check=r.check, passed=r.passed, severity=r.severity if not r.passed else "info",
                        detail=r.detail, evidence=r.evidence).model_dump(mode="json") for r in results]
    ver.receipts = [{"kind": "query", "role": "primary", "query_id": res.get("query_id"), "sql": sql,
                     "result_hash": res.get("result_hash"), "rows": res.get("row_count"), "ask_turn_id": turn.id,
                     "referenced_assets": list(res.get("referenced_assets") or [])}]
    ver.checks, ver.status = checks, "ok" if selfcheck.passed(results) else "flagged"
    ver.result_snapshot = _save_snapshot(s, step, ver, {**table, "step_id": step.id, "version": 1}, actor)
    ver.finished_at, step.status = utcnow(), ver.status
    policy = current_version(s, "policy", workspace_id)
    _record_copy(s, step, ver, verdict="verified" if ver.status == "ok" else "failed_verification", checks=checks,
                 extra=[Dependency("policy", workspace_id, policy)] if policy not in (None, UNKNOWABLE) else [])
    _link(s, workspace_id, ("ask_turn", turn.id), "recorded_as", ("step", step.id))
    # The answer's provenance: the governed query it ran and every table the gateway saw in it.
    from analystos.artifacts.registry import link_queries

    link_queries(s, workspace_id, ("ask_turn", turn.id), [res["query_id"]] if res.get("query_id") else [],
                 assets=list(res.get("referenced_assets") or []))
    for fq in res.get("referenced_assets") or []:
        _link(s, workspace_id, ("ask_turn", turn.id), "reads", ("table", fq))


def record_quietly(container_type: str, workspace_id: str, container_id: str, user_id: str | None) -> None:
    """Record a finished run or an answered Ask thread as Data Thread steps as soon as it exists, as the
    person who asked, so the thread is never empty or behind (idempotent; a failure is logged, never
    raised into the run or the answer)."""
    from analystos.core.logging import get_logger

    try:
        with session_scope() as s:
            user = s.get(User, user_id) if user_id else None
            if user is None:
                return
            s.expunge(user)
        if container_type == "run":
            ingest_run(user, workspace_id, container_id)
        else:
            ingest_ask_thread(user, workspace_id, container_id, view=False)  # on the answer's path: no view
    except Exception as exc:  # noqa: BLE001 - recording is a convenience; the run/answer stands without it
        get_logger(__name__).warning("recording %s %s as steps failed: %s", container_type, container_id, exc)


def ingest_run(user: User, workspace_id: str, run_id: str) -> dict[str, Any]:
    """A run as steps: its plan, each tested hypothesis as a method step (the recorded experiment), each
    finding as a claim step whose verdict is the finding's (dependencies of its record carried over)."""
    from analystos.db.models import Experiment, Hypothesis, Insight, VerificationRecord
    from analystos.evidence.verification import Dependency, latest

    actor = f"user:{user.id}"
    with session_scope() as s:
        run = load_container(s, s.merge(user), "run", run_id, workspace_id)
        branch = main_branch(s, workspace_id, "run", run_id, actor)
        done = _ingested(s, "run", run_id)
        plan_id = done.get(f"plan:{run_id}")
        if plan_id is None:
            plan, _ = add_step(s, workspace_id=workspace_id, branch=branch, kind="plan", title=f"Plan v{run.plan_version}",
                               spec={"text": run.objective, "plan_version": run.plan_version, "plan_hash": run.plan_hash,
                                     "recorded": True}, depends_on=[], origin={"type": "plan", "id": f"plan:{run_id}"},
                               actor=actor, reason="ingested")
            pv = version_row(s, plan)
            pv.status, plan.status, pv.finished_at = "recorded", "recorded", utcnow()
            plan_id = plan.id
        method_of: dict[str, str] = {}
        for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id).order_by(Hypothesis.created_at, Hypothesis.code)):
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary")
                           .order_by(Experiment.created_at.desc()).limit(1))
            if exp is None:
                continue
            if h.id in done:
                method_of[h.id] = done[h.id]
                continue
            step, ver = add_step(s, workspace_id=workspace_id, branch=branch, kind="method", title=f"{h.code}: {h.statement}"[:300],
                                 spec={"analysis_spec": dict(h.spec or {})}, depends_on=[plan_id],
                                 origin={"type": "hypothesis", "id": h.id}, actor=actor, reason="ingested")
            ver.inputs = {plan_id: version_row(s, s.get(AnalysisStep, plan_id)).id}
            ver.receipts = [{"kind": "query", "role": "method", "query_id": q} for q in exp.query_ids or []]
            stat = dict(exp.result or {})
            ver.result_snapshot = _save_snapshot(s, step, ver, {"columns": [], "rows": [], "row_count": stat.get("n"),
                                                                "stat": stat, "step_id": step.id, "version": 1}, actor)
            ver.status, step.status, ver.finished_at = "recorded", "recorded", utcnow()
            method_of[h.id] = step.id
        insights = list(s.scalars(select(Insight).where(Insight.run_id == run_id).order_by(Insight.created_at)))
        records = latest(s, "insight", [i.id for i in insights])
        for ins in insights:
            if ins.id in done or ins.hypothesis_id not in method_of:
                continue
            mid = method_of[ins.hypothesis_id]
            step, ver = add_step(s, workspace_id=workspace_id, branch=branch, kind="claim", title=f"{ins.code}: {ins.title}"[:300],
                                 spec={"text": ins.finding, "insight_id": ins.id}, depends_on=[mid],
                                 origin={"type": "insight", "id": ins.id}, actor=actor, reason="ingested")
            ver.inputs = {mid: version_row(s, s.get(AnalysisStep, mid)).id}
            rec: VerificationRecord | None = records.get(ins.id)
            ver.checks = list(rec.checks or []) if rec is not None else []
            ver.status = "ok" if rec is not None and rec.verdict == "verified" else ("flagged" if rec is not None else "recorded")
            ver.result_snapshot = _save_snapshot(s, step, ver, {"text": ins.finding, "step_id": step.id, "version": 1}, actor)
            ver.finished_at, step.status = utcnow(), ver.status
            if rec is not None:
                carried = [Dependency(d["kind"], d["ref"], d["version_hash"]) for d in rec.dependencies or []]
                _record_copy(s, step, ver, verdict=rec.verdict, checks=ver.checks, extra=carried, run_id=run_id)
                if rec.state == "VOID":  # a void verdict is never carried forward as live
                    from analystos.evidence.verification import void_dependents

                    void_dependents(s, "query", f"step:{step.id}", "__carried_void__", f"the finding's verdict is void: "
                                    f"{rec.void_reason}", event="step.edited")
            _link(s, workspace_id, ("insight", ins.id), "recorded_as", ("step", step.id))
        return thread(s, branch)


def _link(session: Session, workspace_id: str, from_: tuple[str, str], relation: str, to: tuple[str, str],
          run_id: str | None = None) -> None:
    from analystos.artifacts.registry import link

    link(session, workspace_id, from_, relation, to, run_id=run_id)

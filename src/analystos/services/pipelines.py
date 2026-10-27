"""Pipelines (P6-01..P6-03): versions, dry runs, runs, and managed materialization.

* **Versions** (ADR-0021): a PipelineSpec is saved as a draft version, validated against the recipes it
  names; publishing requires those recipes to be published. Runs and schedules use published versions
  (drafts only in a workspace whose settings say `environment: dev`).
* **Dry run**: `pipelines/dryrun.py` on the output recipe: per-source scope and snapshot manifest, compiled
  SQL, key/fan-out/unmatched checks, gates, budgets and the reconciled virtual output. The output rows are
  stored as a content-addressed candidate snapshot. When the pipeline names a destination and every check
  passes, a hash-bound approval (`pipeline_materialize`) is requested for exactly that candidate.
* **Run**: the recipes in order, materialized into the workspace's managed recipe-output source through
  the loader; the output recipe runs incrementally when the pipeline (or the recipe) declares it.
* **Materialize** (P6-03): `verify_for_execution` immediately before the write, then the managed writer
  (its own identity) stages the candidate as a new version table, validates it in the database and
  promotes it atomically; the previous good version is the rollback pointer. A retry of the same approved
  candidate resumes from its checkpoint (idempotency key = destination + candidate + spec). An invalid
  candidate is dropped and never replaces the good version; failures and stale destinations raise alerts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.work import PipelineSpec
from analystos.core.config import get_settings
from analystos.core.errors import (
    AnalystOSError,
    ApprovalRequired,
    Conflict,
    Forbidden,
    InvalidInput,
    PolicyDenied,
)
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import (
    Approval,
    BuildTarget,
    Materialization,
    Pipeline,
    PipelineRun,
    RecipeVersion,
    Source,
    SourceAsset,
    User,
    Workspace,
    WriterDestination,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.pipelines import spec as pipeline_spec
from analystos.pipelines.writer import DEFAULT_ENGINE
from analystos.tenancy.background import platform_loop

ACTION = "pipeline_materialize"
RUN_MODES = ("auto", "full", "reconcile", "replay", "backfill")
PREVIEW_ROWS = 50


# ------------------------------------------------------------------------------------ versions
def _resolve_recipes(session: Session, workspace_id: str, spec: PipelineSpec, *, runnable: bool) -> dict[str, RecipeVersion]:
    """The recipe version of each step: the pinned version, else the published one. `runnable` refuses
    drafts outside a dev workspace (ADR-0021); a save may validate against the newest draft."""
    from analystos.services.definitions import is_dev

    dev = is_dev(session, workspace_id)
    out: dict[str, RecipeVersion] = {}
    for ref in spec.recipes:
        q = select(RecipeVersion).where(RecipeVersion.workspace_id == workspace_id, RecipeVersion.name == ref.name)
        if ref.version is not None:
            row = session.scalar(q.where(RecipeVersion.version == ref.version))
        else:
            row = session.scalar(q.where(RecipeVersion.status == "published").order_by(RecipeVersion.version.desc()).limit(1))
            if row is None and (dev or not runnable):
                row = session.scalar(q.order_by(RecipeVersion.version.desc()).limit(1))
        if row is None:
            continue
        if runnable and row.status == "draft" and not dev:
            raise PolicyDenied(f"recipe {ref.name} v{row.version} is a draft; publish it (drafts run only in a dev "
                               "workspace)")
        out[ref.name] = row
    return out


def validate_spec(session: Session, workspace_id: str, spec: dict[str, Any], *, runnable: bool = False) -> dict[str, Any]:
    p = pipeline_spec.parse(spec)
    rows = _resolve_recipes(session, workspace_id, p, runnable=runnable)
    validated = pipeline_spec.check(p, {n: r.spec for n, r in rows.items()})
    return {"pipeline": p, "recipes": rows, "validated": validated}


def save_pipeline(session: Session, user: User, workspace_id: str, spec: dict[str, Any]) -> Pipeline:
    """Validate a PipelineSpec against its recipes and store it as a new draft version (unchanged content
    returns the latest version)."""
    require_role(session, user, workspace_id, "editor")
    p = validate_spec(session, workspace_id, spec)["pipeline"]
    body = p.spec()
    digest = stable_hash(body)
    latest = session.scalar(select(Pipeline).where(Pipeline.workspace_id == workspace_id, Pipeline.name == p.name)
                            .order_by(Pipeline.version.desc()).limit(1).with_for_update())
    if latest is not None and latest.spec_hash == digest:
        return latest
    row = Pipeline(id=new_id("pip"), workspace_id=workspace_id, name=p.name, version=(latest.version + 1) if latest else 1,
                   status="draft", spec=body, spec_hash=digest, created_by=user.id)
    session.add(row)
    session.flush()
    for r in p.recipes:
        rec = session.scalar(select(RecipeVersion).where(RecipeVersion.workspace_id == workspace_id,
                                                         RecipeVersion.name == r.name).order_by(RecipeVersion.version.desc()))
        if rec is not None:
            from analystos.artifacts.registry import link

            link(session, workspace_id, ("recipe", rec.id), "composed_into", ("pipeline", row.id))
    audit(f"user:{user.id}", "pipeline.saved", workspace_id=workspace_id, target=row.id,
          details={"name": p.name, "version": row.version, "spec_hash": digest}, session=session)
    emit(workspace_id, "pipeline.saved", {"pipeline_id": row.id, "name": p.name, "version": row.version},
         actor=f"user:{user.id}", session=session)
    return row


@scoped_loader
def get_pipeline(session: Session, user: User, pipeline_id: str, workspace_id: str | None = None,
                 minimum: str = "viewer") -> Pipeline:
    return load_in_workspace(session, Pipeline, pipeline_id, workspace_id, user=user, minimum=minimum, label="pipeline")


@scoped_loader
def publish_pipeline(session: Session, user: User, pipeline_id: str, workspace_id: str | None = None) -> Pipeline:
    row = get_pipeline(session, user, pipeline_id, workspace_id, minimum="editor")
    validate_spec(session, row.workspace_id, row.spec, runnable=True)
    for other in session.scalars(select(Pipeline).where(Pipeline.workspace_id == row.workspace_id, Pipeline.name == row.name,
                                                        Pipeline.status == "published", Pipeline.id != row.id)):
        other.status = "superseded"
    row.status, row.published_at, row.published_by = "published", utcnow(), user.id
    audit(f"user:{user.id}", "pipeline.published", workspace_id=row.workspace_id, target=row.id,
          details={"name": row.name, "version": row.version, "spec_hash": row.spec_hash}, session=session)
    emit(row.workspace_id, "pipeline.published", {"pipeline_id": row.id, "name": row.name, "version": row.version},
         actor=f"user:{user.id}", session=session)
    return row


def list_pipelines(session: Session, user: User, workspace_id: str) -> list[Pipeline]:
    require_role(session, user, workspace_id, "viewer")
    return list(session.scalars(select(Pipeline).where(Pipeline.workspace_id == workspace_id)
                                .order_by(Pipeline.name, Pipeline.version.desc())))


def pipeline_view(row: Pipeline) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("id", "workspace_id", "name", "version", "status", "spec", "spec_hash",
                                         "created_by", "created_at", "published_at")}


def run_view(row: PipelineRun) -> dict[str, Any]:
    return {k: getattr(row, k) for k in ("id", "workspace_id", "pipeline_id", "pipeline_name", "pipeline_version",
                                         "spec_hash", "mode", "status", "recipes", "plan", "manifest", "sql", "checks",
                                         "reconciliation", "candidate", "recipe_run_ids", "query_ids", "plan_hash",
                                         "approval_id", "error", "created_by", "created_at", "finished_at")}


@scoped_loader
def get_run(session: Session, user: User, run_id: str, workspace_id: str | None = None, minimum: str = "viewer") -> PipelineRun:
    return load_in_workspace(session, PipelineRun, run_id, workspace_id, user=user, minimum=minimum, label="pipeline run")


def list_runs(session: Session, user: User, workspace_id: str, pipeline: str | None = None) -> list[PipelineRun]:
    require_role(session, user, workspace_id, "viewer")
    q = select(PipelineRun).where(PipelineRun.workspace_id == workspace_id)
    if pipeline:
        q = q.where(PipelineRun.pipeline_name == pipeline)
    return list(session.scalars(q.order_by(PipelineRun.created_at.desc()).limit(200)))


def _finish(run_id: str, status: str, **fields: Any) -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(PipelineRun, run_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.status, row.finished_at = status, utcnow()
        kind = "dry_run" if row.mode == "dry_run" else "run"
        event = {"failed": f"pipeline.{kind}.failed", "blocked": f"pipeline.{kind}.blocked"}.get(status, f"pipeline.{kind}.completed")
        emit(row.workspace_id, event, {"pipeline_run_id": row.id, "pipeline_id": row.pipeline_id, "status": status,
                                       "mode": row.mode, "error": (row.error or "")[:300] or None},
             actor=row.created_by, session=s)
        if status in ("failed", "blocked"):
            from analystos.services.notifications import notify

            notify(s, row.workspace_id, kind="pipeline", title=f"Pipeline {row.pipeline_name} {kind.replace('_', ' ')} {status}",
                   body=(row.error or "a check or gate failed; see the run")[:1000],
                   link={"type": "pipeline_run", "id": row.id})
        return run_view(row)


def _start_run(s: Session, user: User, row: Pipeline, mode: str, recipes: dict[str, RecipeVersion]) -> str:
    run = PipelineRun(id=new_id("prun"), workspace_id=row.workspace_id, pipeline_id=row.id, pipeline_name=row.name,
                      pipeline_version=row.version, spec_hash=row.spec_hash, mode=mode, status="running",
                      recipes=[{"name": n, "id": r.id, "version": r.version, "spec_hash": r.spec_hash, "status": r.status}
                               for n, r in recipes.items()],
                      plan={}, manifest={}, sql={}, checks=[], reconciliation={}, candidate={}, recipe_run_ids=[],
                      query_ids=[], created_by=f"user:{user.id}")
    s.add(run)
    s.flush()
    from analystos.artifacts.registry import link

    link(s, row.workspace_id, ("pipeline", row.id), "ran", ("pipeline_run", run.id))
    return run.id


# ------------------------------------------------------------------------------------ dry run
def _input_catalog(s: Session, workspace_id: str, validated: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for node in validated.sources():
        schema, name = node.asset.split(".", 1)
        a = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                               SourceAsset.name == name))
        if a is not None:
            snap = a.snapshot or {}
            out[node.asset] = {"source_id": a.source_id, "content_fingerprint": snap.get("content_fingerprint"),
                               "staged_at": a.freshness_at, "row_count": a.row_count, "load_id": snap.get("load_id"),
                               "incremental_watermark": (snap.get("incremental") or {}).get("watermark")}
    return out


def _manifest(s: Session, scope: Any, plan: Any, validated: Any, inputs: dict[str, dict[str, Any]],
              snapshots: dict[str, Any]) -> dict[str, Any]:
    """Per-source scope and snapshot manifest: what each source contributes, under which authorization."""
    from analystos.staging.roles import role_for

    settings = get_settings()
    by_source: dict[str, dict[str, Any]] = {}
    declared: dict[str, list[str]] = {}
    for node in validated.sources():
        cols = declared.setdefault(node.asset, [])
        cols += [c.name for c in node.output_schema or [] if c.name not in cols]
    for asset, source_id in sorted(plan.sources.items()):
        src = s.get(Source, source_id)
        entry = by_source.setdefault(source_id, {
            "source_id": source_id, "kind": src.kind if src else None, "name": src.name if src else None,
            "execution_mode": src.execution_mode if src else None, "dialect": scope.source_dialects.get(source_id),
            "read_identity": f"query gateway as workspace reader role {role_for(settings, scope.workspace_id)}",
            "authorized_assets": sorted(a for a, sid in scope.asset_sources.items() if sid == source_id),
            "denied_columns": sorted(c for c in scope.denied_columns
                                     if scope.asset_sources.get(c.rsplit(".", 1)[0]) == source_id),
            "assets": {}})
        snap = snapshots.get(asset) or {}
        entry["assets"][asset] = {"columns_read": declared.get(asset, []), **{k: v for k, v in (inputs.get(asset) or {}).items()
                                                                             if k != "source_id"},
                                  "snapshot": snap.get("snapshot"), "snapshot_rows": snap.get("rows"),
                                  "snapshot_query_id": snap.get("query_id")}
    return {"scope_hash": scope.scope_hash(), "policy_version": scope.policy_version, "engine": plan.engine,
            "cross_source": len(by_source) > 1,
            "join_strategy": ("each source read through its own gateway scope into an immutable snapshot; the validated "
                              "join runs in the recipe engine over the snapshots") if plan.engine == "duckdb" else
                             "pushdown: one governed statement on the single source",
            "sources": by_source}


def _destination(s: Session, workspace_id: str, spec: PipelineSpec) -> WriterDestination:
    d = spec.destination
    assert d is not None
    dest = s.scalar(select(WriterDestination).where(WriterDestination.engine == d.engine,
                                                    WriterDestination.schema_name == d.schema_name))
    if dest is None or dest.workspace_id != workspace_id or dest.status != "active":
        raise Forbidden(f"{d.schema_name} is not an allowlisted destination of this workspace; an owner designates it")
    if dest.tables and d.table not in dest.tables:
        raise Forbidden(f"{d.schema_name}.{d.table} is not on the destination's table allowlist ({', '.join(dest.tables)})")
    return dest


def _current(s: Session, dest_id: str, table: str) -> Materialization | None:
    return s.scalar(select(Materialization).where(Materialization.destination_id == dest_id,
                                                  Materialization.table_name == table,
                                                  Materialization.status == "promoted"))


def _late_rows(user: User, ws: str, p: PipelineSpec, v: Any, outcome: Any, scope: Any, run_id: str,
               query_ids: list[str], *, candidate_complete: bool) -> dict[str, Any]:
    """`late_rows` of the reconciliation (`pipelines/late.py`): the candidate against the committed incremental
    output, whose keys behind the cutoff are read through the gateway under the requester's scope."""
    from datetime import UTC, datetime

    from analystos.pipelines import late
    from analystos.runtime.context import default_gateway
    from analystos.services.recipes import OUTPUT_SOURCE_KIND, output_table
    from analystos.staging.incremental import as_text
    from analystos.staging.loader import StagingLoader

    inc = p.incremental or v.recipe.incremental
    if inc is None:
        return late.not_measured(late.NO_WATERMARK)
    with session_scope() as s:
        src = s.scalar(select(Source).where(Source.workspace_id == ws, Source.kind == OUTPUT_SOURCE_KIND))
        schema, source_id = (src.staging_schema, src.id) if src is not None else (None, None)
    out = next(o for o in v.outputs() if o.name == p.output.output)
    table = output_table(p.output_recipe(), out.name)
    asset = f"{schema}.{table}"
    if not (source_id and schema):
        return late.not_measured("no incremental run has committed an output yet: the first run reads every row")
    # the scope check comes first: the stored watermark describes the committed output's data
    if scope.asset_sources.get(asset) != source_id:
        return late.not_measured(f"the committed output {asset} is not in your data scope")
    state = StagingLoader(get_settings()).read_state(source_id, table)

    def ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def committed(cutoff: Any) -> tuple[list[Any], bool]:
        cols = ", ".join(ident(c) for c in (*inc.key, inc.watermark))
        # an explicit UTC offset: a timestamptz column is compared in UTC whatever the session time zone
        bound = f"'{as_text(cutoff.replace(tzinfo=UTC))}'" if isinstance(cutoff, datetime) else str(cutoff)
        try:
            res = default_gateway().execute(scope, f"SELECT {cols} FROM {ident(schema)}.{ident(table)} "
                                                   f"WHERE {ident(inc.watermark)} < {bound}",
                                            actor=f"user:{user.id}", purpose=f"pipeline.late_rows:{run_id}", use_cache=False)
        except AnalystOSError as exc:
            raise late.NotMeasured(f"the committed output could not be read: {exc.code}: {exc.message}") from exc
        query_ids.append(res.query_id)
        return list(res.rows), not res.truncated

    return late.measure(inc, outcome.columns, outcome.kept, stored_watermark=(state or {}).get("watermark"),
                        committed=committed, candidate_complete=candidate_complete)


@scoped_loader
def dry_run(user: User, pipeline_id: str, workspace_id: str | None = None, *, engine: str | None = None) -> dict[str, Any]:
    """Dry-run the pipeline's output (nothing is written to any table). With a destination and every check
    passing, requests the approval to materialize exactly this candidate."""
    from analystos.pipelines import dryrun
    from analystos.recipes.execute import RecipeExecutor, default_store, plan_execution
    from analystos.runtime.context import default_gateway
    from analystos.workflows.orchestrator import run_recipe_compute

    settings = get_settings()
    now = utcnow()
    with session_scope() as s:
        row = get_pipeline(s, user, pipeline_id, workspace_id, minimum="analyst")
        ws, spec_hash = row.workspace_id, row.spec_hash
        resolved = validate_spec(s, ws, row.spec, runnable=True)
        p: PipelineSpec = resolved["pipeline"]
        v = resolved["validated"][p.output_recipe()]
        scope = resolve_scope(s, user, ws, minimum_role="analyst")
        inputs = _input_catalog(s, ws, v)
        previous_rows = None
        if p.destination is not None:
            dest = _destination(s, ws, p)
            cur = _current(s, dest.id, p.destination.table)
            previous_rows = cur.row_count if cur else None
        run_id = _start_run(s, user, row, "dry_run", resolved["recipes"])
    executor = None
    try:
        plan = plan_execution(v, scope, prefer=engine)
        store = default_store(settings)
        executor = RecipeExecutor(default_gateway(), scope, v, plan, actor=f"user:{user.id}", store=store,
                                  compute=run_recipe_compute, max_rows=p.budgets.max_rows)
        result = dryrun.execute(p, v, executor, inputs=inputs, previous_rows=previous_rows, run_id=run_id, now=now)
    except AnalystOSError as exc:
        _finish(run_id, "failed", error=f"{exc.code}: {exc.message}"[:2000],
                query_ids=list(executor.query_ids) if executor else [])
        raise
    outcome = result["outcome"]
    out_node = next(o for o in v.outputs() if o.name == p.output.output)
    digest = store.put(outcome.columns, outcome.kept)
    candidate = {"snapshot": digest, "row_count": len(outcome.kept), "columns": [c.model_dump() for c in out_node.output_schema or []],
                 "keys": list(out_node.keys), "preview": outcome.kept[:PREVIEW_ROWS],
                 "gates": outcome.summary()}
    with session_scope() as s:
        manifest = _manifest(s, scope, plan, v, inputs, executor.snapshots)
    query_ids = list(executor.query_ids)
    complete = all(c["ok"] for c in result["checks"] if c["check"] == "complete_output")
    try:  # a diagnostic: it never fails the dry run or strands its run row
        late_part = _late_rows(user, ws, p, v, outcome, scope, run_id, query_ids, candidate_complete=complete)
    except AnalystOSError as exc:
        from analystos.pipelines.late import not_measured

        late_part = not_measured(f"{exc.code}: {exc.message}")
    reconciliation = {**result["reconciliation"], **late_part}
    plan_hash = stable_hash({"spec_hash": spec_hash, "pipeline_run": run_id,
                             "recipes": sorted((n, r.spec_hash) for n, r in resolved["recipes"].items()),
                             "inputs": {a: i.get("content_fingerprint") for a, i in sorted(inputs.items())},
                             "snapshots": {a: sn.get("snapshot") for a, sn in sorted(executor.snapshots.items())},
                             "candidate": digest})
    status = "blocked" if not result["ok"] else ("awaiting_approval" if p.destination is not None else "succeeded")
    fields = {"plan": plan.to_dict(), "manifest": manifest, "sql": result["sql"], "checks": result["checks"],
              "reconciliation": reconciliation, "candidate": candidate, "query_ids": query_ids,
              "plan_hash": plan_hash,
              "error": None if result["ok"] else "; ".join(f"{c['check']} {c.get('join') or c.get('gate') or c.get('name') or c.get('asset') or ''}".strip()
                                                           for c in result["checks"] if not c["ok"])[:2000]}
    if status == "awaiting_approval":
        with session_scope() as s:
            run = s.get(PipelineRun, run_id)
            for k, val in fields.items():
                setattr(run, k, val)
            dest = _destination(s, ws, p)
            approval = _request_approval(s, user, run, p, dest)
            fields["approval_id"] = approval.id
    view = _finish(run_id, status, **fields)
    return view


def approval_payload(s: Session, run: PipelineRun, spec: PipelineSpec, dest: WriterDestination) -> dict[str, Any]:
    """What an approval to materialize binds. Recomputed at execution: another promotion in between (the
    rollback pointer moved), a changed destination or candidate no longer matches the approved hash."""
    current = _current(s, dest.id, spec.destination.table)
    return {"action": ACTION, "pipeline_run_id": run.id, "pipeline_id": run.pipeline_id, "pipeline_spec_hash": run.spec_hash,
            "recipes": [{k: r[k] for k in ("name", "version", "spec_hash")} for r in run.recipes],
            "destination_id": dest.id, "destination": f"{dest.engine}/{dest.schema_name}.{spec.destination.table}",
            "candidate": run.candidate.get("snapshot"), "row_count": run.candidate.get("row_count"),
            "columns": run.candidate.get("columns"), "keys": run.candidate.get("keys"),
            "previous_materialization": current.id if current else None}


def _request_approval(s: Session, user: User, run: PipelineRun, spec: PipelineSpec, dest: WriterDestination) -> Approval:
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace

    ws = get_workspace(s, run.workspace_id)
    payload = approval_payload(s, run, spec, dest)
    return request_approval(s, workspace_id=run.workspace_id, run_id=None, action=ACTION, payload=payload,
                            plan_hash=run.plan_hash, policy_version=ws.policy_version, requested_by=user.id,
                            risk_tier="high", destination=payload["destination"],
                            affected_assets=[f"{dest.schema_name}.{spec.destination.table}"],
                            evidence={"pipeline_run_id": run.id, "checks": run.checks, "reconciliation": run.reconciliation})


# ------------------------------------------------------------------------------------ run (managed output)
@scoped_loader
def run_pipeline(user: User, pipeline_id: str, workspace_id: str | None = None, *, mode: str = "auto",
                 window: tuple[Any, Any] | None = None, engine: str | None = None) -> dict[str, Any]:
    """Run the pipeline's recipes in order into the managed recipe-output source; the output recipe runs by
    watermark window when the pipeline declares `incremental` (`mode` = auto | full | reconcile | replay |
    backfill). Writing to a destination is `materialize` after a dry run and an approval, never this."""
    from analystos.services.recipes import run_recipe

    if mode not in RUN_MODES:
        raise InvalidInput(f"mode must be one of {', '.join(RUN_MODES)}")
    with session_scope() as s:
        row = get_pipeline(s, user, pipeline_id, workspace_id, minimum="editor")
        ws = row.workspace_id
        resolved = validate_spec(s, ws, row.spec, runnable=True)
        p: PipelineSpec = resolved["pipeline"]
        recipes = resolved["recipes"]
        run_id = _start_run(s, user, row, mode, recipes)
    recipe_runs: list[str] = []
    blocked = False
    try:
        for ref in p.recipes:
            is_output = ref.name == p.output_recipe()
            out = run_recipe(user, recipes[ref.name].id, ws, mode="materialize", engine=engine,
                             refresh=mode if is_output else "auto", window=window if is_output else None,
                             incremental=p.incremental if is_output else None)
            recipe_runs.append(out["id"])
            if out["status"] == "blocked":
                blocked = True
                break
    except AnalystOSError as exc:
        _finish(run_id, "failed", recipe_run_ids=recipe_runs, error=f"{exc.code}: {exc.message}"[:2000])
        raise
    return _finish(run_id, "blocked" if blocked else "succeeded", recipe_run_ids=recipe_runs,
                   plan={"recipe_runs": recipe_runs, "mode": mode},
                   reconciliation=_run_late_rows(p, recipes[p.output_recipe()], recipe_runs[-1] if not blocked and recipe_runs
                                                 else None),
                   error="a fail gate or the strict schema policy blocked an output; the last good output was kept"
                   if blocked else None)


def _run_late_rows(p: PipelineSpec, output_recipe: RecipeVersion, output_run_id: str | None) -> dict[str, Any]:
    """A run reads only its watermark window, so it cannot see rows behind it: `late_rows` stays unmeasured
    (None, with why) and the dry run, which computes the whole output, is where they are counted."""
    from analystos.contracts.recipe import Incremental
    from analystos.db.models import RecipeRun
    from analystos.pipelines import late

    inc = p.incremental or (Incremental.model_validate(output_recipe.spec["incremental"])
                            if (output_recipe.spec or {}).get("incremental") else None)
    if inc is None:
        return late.not_measured(late.NO_WATERMARK)
    window = None
    if output_run_id:
        with session_scope() as s:
            r = s.get(RecipeRun, output_run_id)
            window = (r.plan or {}).get("incremental") if r is not None else None
    return late.not_measured("a run reads only its watermark window [since, until], so rows behind the late window "
                             "are not visible to it; a dry run of this pipeline counts them",
                             watermark_column=inc.watermark, late_window_seconds=inc.late_window, window=window)


# ------------------------------------------------------------------------------------ destinations
def designate_destination(session: Session, user: User, workspace_id: str, schema: str, *,
                          tables: list[str] | None = None, engine: str = DEFAULT_ENGINE) -> WriterDestination:
    """A workspace owner (or platform admin) allowlists a destination schema (and optionally its tables).
    Provisions the writer role and the schema, then records the destination."""
    from analystos.build.gateway import source_schemas
    from analystos.pipelines.writer import check_destination, provision_destination, writer_role_for

    if not user.is_admin:
        require_role(session, user, workspace_id, "owner")
    if engine != DEFAULT_ENGINE:
        raise InvalidInput(f"engine {engine} has no writer yet (supported: {DEFAULT_ENGINE})")
    for t in tables or []:
        check_destination(schema, t)
    forbidden = source_schemas(session) | {t.schema_name for t in session.scalars(select(BuildTarget))}
    taken = session.scalar(select(WriterDestination).where(WriterDestination.engine == engine,
                                                           WriterDestination.schema_name == schema))
    if taken is not None and taken.workspace_id != workspace_id:
        raise Forbidden(f"schema {schema} is already a destination of another workspace")
    grants = provision_destination(get_settings(), workspace_id, schema, forbidden=forbidden)
    dest = taken or WriterDestination(id=new_id("wdst"), workspace_id=workspace_id, engine=engine, schema_name=schema,
                                      writer_role=writer_role_for(get_settings(), workspace_id), created_by=user.id)
    dest.status, dest.provisioning, dest.tables = "active", grants, sorted(set(tables or []))
    session.add(dest)
    session.flush()
    audit(f"user:{user.id}", "writer_destination.provisioned", workspace_id=workspace_id, target=f"{engine}/{schema}",
          decision="allow", details={**grants, "tables": dest.tables}, session=session)
    emit(workspace_id, "writer_destination.provisioned", {"destination_id": dest.id, "schema": schema, "tables": dest.tables,
                                                          "writer_role": dest.writer_role}, actor=f"user:{user.id}", session=session)
    return dest


def list_destinations(session: Session, user: User, workspace_id: str) -> list[WriterDestination]:
    require_role(session, user, workspace_id, "viewer")
    return list(session.scalars(select(WriterDestination).where(WriterDestination.workspace_id == workspace_id)
                                .order_by(WriterDestination.schema_name)))


def list_materializations(session: Session, user: User, workspace_id: str, table: str | None = None) -> list[Materialization]:
    require_role(session, user, workspace_id, "viewer")
    q = select(Materialization).where(Materialization.workspace_id == workspace_id)
    if table:
        q = q.where(Materialization.table_name == table)
    return list(session.scalars(q.order_by(Materialization.created_at.desc()).limit(200)))


def materialization_view(m: Materialization) -> dict[str, Any]:
    return {k: getattr(m, k) for k in ("id", "workspace_id", "destination_id", "schema_name", "table_name", "version",
                                       "version_table", "pipeline_id", "pipeline_run_id", "approval_id", "status",
                                       "row_count", "content_fingerprint", "previous_id", "checkpoint", "error",
                                       "created_by", "created_at", "promoted_at")}


# ------------------------------------------------------------------------------------ materialize
def _reverify(s: Session, mat: Materialization, approval_id: str | None, payload: dict[str, Any], plan_hash: str | None) -> None:
    """A retry resumes an approval already consumed for exactly this materialization: the same checks as
    `verify_for_execution` except the status, which is `executed` by this materialization."""
    from analystos.governance.policy import member_role
    from analystos.security.auth import APPROVER_ROLES, role_at_least

    approval = s.get(Approval, approval_id) if approval_id else None
    if approval is None or approval.id != mat.approval_id or approval.status != "executed":
        raise ApprovalRequired("a retry needs the approval that started this materialization")
    if stable_hash(payload) != approval.payload_hash or plan_hash != approval.plan_hash:
        raise ApprovalRequired("the candidate, destination or rollback pointer changed since the approval; dry-run again")
    ws = s.get(Workspace, approval.workspace_id)
    if ws is None or ws.status != "active" or ws.policy_version != approval.policy_version:
        raise ApprovalRequired("policy changed after approval; a new approval is required")
    requester, approver = s.get(User, approval.requested_by), s.get(User, approval.decided_by or "")
    if not role_at_least(member_role(s, requester, ws.id) if requester and requester.active else None, "editor"):
        raise PolicyDenied("requester no longer holds rights in this workspace")
    if (member_role(s, approver, ws.id) if approver and approver.active else None) not in APPROVER_ROLES and \
            not (approver and approver.is_admin):
        raise PolicyDenied("approver no longer holds approval rights in this workspace")


@scoped_loader
def _claim(user: User, run_id: str, approval_id: str | None, workspace_id: str | None) -> dict[str, Any]:
    from analystos.governance.approvals import consume, verify_for_execution

    refusal: AnalystOSError | None = None
    with session_scope() as s:
        run = get_run(s, user, run_id, workspace_id, minimum="editor")
        s.refresh(run, with_for_update=True)
        pipeline = s.get(Pipeline, run.pipeline_id)
        spec = pipeline_spec.parse(pipeline.spec)
        if spec.destination is None:
            raise InvalidInput("this pipeline names no destination; nothing to materialize")
        if run.mode != "dry_run" or not run.candidate.get("snapshot"):
            raise InvalidInput("materialize a dry run's candidate")
        dest = _destination(s, run.workspace_id, spec)
        table = spec.destination.table
        key = stable_hash({"destination": dest.id, "table": table, "candidate": run.candidate["snapshot"],
                           "spec_hash": run.spec_hash, "pipeline_run": run.id})
        mat = s.scalar(select(Materialization).where(Materialization.idempotency_key == key))
        if mat is not None and mat.status in ("promoted", "superseded", "rolled_back"):
            return {"reused": True, "materialization_id": mat.id}
        payload_now = approval_payload(s, run, spec, dest)
        try:
            if mat is not None:  # a retry: resume from the checkpoint under the approval it consumed
                payload = {**payload_now, "previous_materialization": mat.previous_id}
                if payload_now["previous_materialization"] != mat.previous_id:
                    raise ApprovalRequired("another version was promoted since this materialization started; dry-run again")
                _reverify(s, mat, approval_id, payload, run.plan_hash)
            else:
                if run.status != "awaiting_approval":
                    raise Conflict(f"pipeline run {run.id} is {run.status}; only a dry run awaiting approval materializes")
                if not approval_id:
                    raise ApprovalRequired("a materialization runs only under an approved, hash-bound approval")
                approval = s.get(Approval, approval_id, with_for_update=True)
                if approval is None or approval.action != ACTION or approval.workspace_id != run.workspace_id or \
                        (approval.payload or {}).get("pipeline_run_id") != run.id:
                    raise ApprovalRequired("the approval does not cover this pipeline run")
                # Immediately before the side effect: status, expiry, payload (candidate, destination, rollback
                # pointer), plan hash, policy version, requester and approver rights.
                verify_for_execution(s, approval_id, payload=payload_now, plan_hash=run.plan_hash)
        except (ApprovalRequired, PolicyDenied, Forbidden, Conflict) as exc:
            refusal = exc
            audit(f"user:{user.id}", "pipeline.materialize_refused", workspace_id=run.workspace_id, target=run.id,
                  decision="deny", reasons=[exc.message[:300]], details={"approval_id": approval_id}, session=s)
            emit(run.workspace_id, "pipeline.materialization.failed", {"pipeline_run_id": run.id, "reason": exc.message[:300],
                                                                       "stage": "authorization"},
                 actor=f"user:{user.id}", session=s)
        if refusal is None:
            if mat is None:
                consume(s, s.get(Approval, approval_id))  # single use, before the side effect
                version = (s.scalar(select(func.max(Materialization.version)).where(
                    Materialization.destination_id == dest.id, Materialization.table_name == table)) or 0) + 1
                from analystos.pipelines.writer import version_table

                mat = Materialization(id=new_id("mat"), workspace_id=run.workspace_id, destination_id=dest.id,
                                      schema_name=dest.schema_name, table_name=table, version=version,
                                      version_table=version_table(table, version), pipeline_id=run.pipeline_id,
                                      pipeline_run_id=run.id, approval_id=approval_id, idempotency_key=key,
                                      candidate=run.candidate["snapshot"], status="approved",
                                      previous_id=payload_now["previous_materialization"],
                                      checkpoint={"approved_at": utcnow().isoformat()}, created_by=f"user:{user.id}")
                s.add(mat)
                run.approval_id = approval_id
            mat.error = None
            s.flush()
            audit(f"user:{user.id}", "pipeline.materialize_started", workspace_id=run.workspace_id, target=mat.id,
                  decision="allow", details={"approval_id": approval_id, "destination": payload_now["destination"],
                                             "version": mat.version, "checkpoint": mat.checkpoint}, session=s)
            claimed = {"materialization_id": mat.id, "workspace_id": run.workspace_id, "schema": dest.schema_name,
                       "table": table, "version": mat.version, "candidate": dict(run.candidate),
                       "checkpoint": dict(mat.checkpoint or {}), "keys": list(spec.output.keys),
                       "previous_id": mat.previous_id, "run_id": run.id, "pipeline_id": run.pipeline_id}
    if refusal is not None:
        raise refusal
    return claimed


def _checkpoint(mat_id: str, **fields: Any) -> None:
    with session_scope() as s:
        mat = s.get(Materialization, mat_id)
        mat.checkpoint = {**(mat.checkpoint or {}), **fields}


@scoped_loader
def materialize(user: User, run_id: str, approval_id: str | None, workspace_id: str | None = None) -> dict[str, Any]:
    """Write a dry run's approved candidate to its destination through the managed writer (see module doc)."""
    from analystos.artifacts.registry import link
    from analystos.contracts.recipe import Column
    from analystos.pipelines.writer import ManagedWriter
    from analystos.recipes.execute import default_store
    from analystos.services.recipes import _batches

    claimed = _claim(user, run_id, approval_id, workspace_id)
    if claimed.get("reused"):
        with session_scope() as s:
            return materialization_view(s.get(Materialization, claimed["materialization_id"]))
    settings = get_settings()
    mat_id, ws = claimed["materialization_id"], claimed["workspace_id"]
    schema, table, version, cp = claimed["schema"], claimed["table"], claimed["version"], claimed["checkpoint"]
    writer = ManagedWriter(settings)
    stage = "stage"
    try:
        cand = claimed["candidate"]
        columns, rows = default_store(settings).get(cand["snapshot"])  # refused if the file no longer matches its hash
        declared = [Column.model_validate(c) for c in cand["columns"]]
        if [c.name for c in declared] != list(columns) or len(rows) != cand["row_count"]:
            raise Conflict("the candidate snapshot does not match the approved candidate")
        if not cp.get("staged"):
            staged = writer.stage(ws, schema, table, version, _batches(declared, rows))
            _checkpoint(mat_id, staged=True, staged_at=utcnow().isoformat(), staged_rows=staged["row_count"],
                        staged_fingerprint=staged["content_fingerprint"])
            with session_scope() as s:
                m = s.get(Materialization, mat_id)
                m.status, m.row_count, m.content_fingerprint = "staged", staged["row_count"], staged["content_fingerprint"]
        stage = "validate"
        checks = writer.validate(ws, schema, table, version, keys=claimed["keys"], expected_rows=cand["row_count"])
        _checkpoint(mat_id, validated=all(c["ok"] for c in checks), validation=checks)
        if not all(c["ok"] for c in checks):
            writer.drop_versions(ws, schema, table, [version])
            _checkpoint(mat_id, staged=False)
            raise InvalidInput("the staged version failed validation and was dropped; the good version stays: " +
                               "; ".join(f"{c['check']}" for c in checks if not c["ok"]), details={"checks": checks})
        stage = "promote"
        writer.promote(ws, schema, table, version, [c.name for c in declared])
    except Exception as exc:
        err = f"{exc.code}: {exc.message}" if isinstance(exc, AnalystOSError) else f"{type(exc).__name__}: {exc}"
        _fail(user, mat_id, stage, err[:2000])
        raise
    with session_scope() as s:
        mat = s.get(Materialization, mat_id)
        prev = s.get(Materialization, mat.previous_id) if mat.previous_id else None
        if prev is not None and prev.status == "promoted":
            prev.status = "superseded"
        mat.status, mat.promoted_at, mat.error = "promoted", utcnow(), None
        mat.checkpoint = {**(mat.checkpoint or {}), "promoted_at": mat.promoted_at.isoformat()}
        fq = f"{schema}.{table}"
        link(s, ws, ("pipeline_run", claimed["run_id"]), "materialized", ("table", fq))
        link(s, ws, ("materialization", mat.id), "promoted_to", ("table", fq))
        audit(f"user:{user.id}", "pipeline.materialized", workspace_id=ws, target=fq, decision="allow",
              details={"materialization_id": mat.id, "version": version, "row_count": mat.row_count,
                       "previous": mat.previous_id, "approval_id": mat.approval_id}, session=s)
        emit(ws, "pipeline.materialized", {"materialization_id": mat.id, "destination": fq, "version": version,
                                           "row_count": mat.row_count, "previous_id": mat.previous_id},
             actor=f"user:{user.id}", session=s)
        keep = {mat.id, mat.previous_id}
        old = [m for m in s.scalars(select(Materialization).where(
            Materialization.destination_id == mat.destination_id, Materialization.table_name == table,
            Materialization.status.in_(("superseded", "rolled_back", "failed"))).order_by(Materialization.version.desc()))
            if m.id not in keep]
        expired = old[max(settings.writer_keep_versions - 2, 0):]
        view = materialization_view(mat)
    if expired:
        dropped = set(writer.drop_versions(ws, schema, table, [m.version for m in expired]))
        with session_scope() as s:
            for m in expired:
                if m.version_table in dropped:
                    row = s.get(Materialization, m.id)
                    row.checkpoint = {**(row.checkpoint or {}), "version_table_dropped": True}
    return view


def _fail(user: User, mat_id: str, stage: str, error: str) -> None:
    from analystos.services.notifications import notify

    with session_scope() as s:
        mat = s.get(Materialization, mat_id)
        mat.status, mat.error = "failed", error
        mat.checkpoint = {**(mat.checkpoint or {}), "failed_at": utcnow().isoformat(), "failed_stage": stage}
        fq = f"{mat.schema_name}.{mat.table_name}"
        audit(f"user:{user.id}", "pipeline.materialization_failed", workspace_id=mat.workspace_id, target=fq,
              decision="error", reasons=[error[:300]], details={"materialization_id": mat.id, "stage": stage}, session=s)
        emit(mat.workspace_id, "pipeline.materialization.failed", {"materialization_id": mat.id, "destination": fq,
                                                                   "stage": stage, "error": error[:300]},
             actor=f"user:{user.id}", session=s)
        notify(s, mat.workspace_id, kind="pipeline", title=f"Materialization of {fq} failed ({stage})",
               body=f"{error[:800]}\nThe previous good version stays in place. Retry the materialization to resume.",
               link={"type": "materialization", "id": mat.id})


@scoped_loader
def rollback(user: User, materialization_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    """Re-point a destination at the version its current one replaced (the rollback pointer). Owner only:
    the target version was itself approved and promoted; nothing new is written."""
    from analystos.pipelines.writer import ManagedWriter

    with session_scope() as s:
        mat = load_in_workspace(s, Materialization, materialization_id, workspace_id, user=user, minimum="owner",
                                label="materialization")
        if mat.status != "promoted":
            raise Conflict(f"materialization {mat.id} is {mat.status}; only the current version rolls back")
        prev = s.get(Materialization, mat.previous_id) if mat.previous_id else None
        if prev is None or (prev.checkpoint or {}).get("version_table_dropped"):
            raise Conflict("there is no previous good version to roll back to")
        pipeline = s.get(Pipeline, mat.pipeline_id) if mat.pipeline_id else None
        columns = [c["name"] for c in (s.get(PipelineRun, prev.pipeline_run_id).candidate or {}).get("columns", [])] \
            if prev.pipeline_run_id else []
        info = {"ws": mat.workspace_id, "schema": mat.schema_name, "table": mat.table_name, "version": prev.version,
                "columns": columns, "current": mat.id, "previous": prev.id, "pipeline": pipeline.name if pipeline else None}
    ManagedWriter(get_settings()).promote(info["ws"], info["schema"], info["table"], info["version"], info["columns"])
    with session_scope() as s:
        cur, prev = s.get(Materialization, info["current"]), s.get(Materialization, info["previous"])
        cur.status, prev.status = "rolled_back", "promoted"
        fq = f"{info['schema']}.{info['table']}"
        audit(f"user:{user.id}", "pipeline.rolled_back", workspace_id=info["ws"], target=fq, decision="allow",
              details={"from": cur.id, "to": prev.id, "version": prev.version}, session=s)
        emit(info["ws"], "pipeline.rolled_back", {"destination": fq, "from_materialization": cur.id,
                                                  "to_materialization": prev.id, "version": prev.version},
             actor=f"user:{user.id}", session=s)
        return materialization_view(prev)


# ------------------------------------------------------------------------------------ freshness alerts
@platform_loop
def check_freshness(now: datetime | None = None) -> list[dict[str, Any]]:
    """A published pipeline's destination whose good version is older than `freshness.max_age_hours` raises
    one alert per version (event + notification), from the scheduler's housekeeping."""
    from analystos.services.notifications import notify

    now = now or utcnow()
    raised = []
    with session_scope() as s:
        for row in s.scalars(select(Pipeline).where(Pipeline.status == "published")):
            try:
                p = pipeline_spec.parse(row.spec)
            except InvalidInput:
                continue
            if p.destination is None or p.freshness is None:
                continue
            dest = s.scalar(select(WriterDestination).where(WriterDestination.engine == p.destination.engine,
                                                            WriterDestination.schema_name == p.destination.schema_name,
                                                            WriterDestination.workspace_id == row.workspace_id))
            cur = _current(s, dest.id, p.destination.table) if dest else None
            if cur is None or cur.promoted_at is None or (cur.checkpoint or {}).get("freshness_alerted_at"):
                continue
            age = (now - cur.promoted_at).total_seconds() / 3600
            if age <= p.freshness.max_age_hours:
                continue
            fq = f"{cur.schema_name}.{cur.table_name}"
            cur.checkpoint = {**(cur.checkpoint or {}), "freshness_alerted_at": now.isoformat()}
            payload = {"pipeline_id": row.id, "pipeline": row.name, "destination": fq, "materialization_id": cur.id,
                       "age_hours": round(age, 2), "max_age_hours": p.freshness.max_age_hours}
            emit(row.workspace_id, "pipeline.freshness.breached", payload, actor="scheduler", session=s)
            notify(s, row.workspace_id, kind="pipeline", title=f"{fq} is stale ({age:.1f} h old)",
                   body=f"Pipeline {row.name} declares freshness {p.freshness.max_age_hours} h; the good version is "
                        f"{age:.1f} h old. Run and materialize the pipeline.", link={"type": "pipeline", "id": row.id})
            raised.append(payload)
    return raised

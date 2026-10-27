"""ELT build service (P4-E04): designate targets, start `elt_build` runs, plan a build job.

`plan_build` is the `elt_plan` step: it turns the source run's virtual dataset and KPIs into a dbt
project, dry-runs it (dbt parse with no connection, the static guard over files and the parsed
manifest, candidate tests and a row-count estimate read through the query gateway), records the
rollback plan, and asks for an approval bound to the project hash, engine, target schema and the run's
plan hash. Nothing is written to the analytics plane until the BuildGateway runs the approved job.
"""
from __future__ import annotations

import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.artifacts.registry import current_plan_filter, link, save_artifact
from analystos.build import project as dbt_project
from analystos.build.gateway import BUILD_ACTION, BuildGateway, approval_payload, require_target, source_schemas
from analystos.build.runner import write_profile, write_project
from analystos.build.targets import DEFAULT_ENGINE, build_role_for, provision_target
from analystos.contracts.bi import MetricDef
from analystos.core.config import get_settings
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Artifact, BuildJob, BuildTarget, RunTask, Source, User
from analystos.events.bus import emit
from analystos.governance.approvals import request_approval
from analystos.governance.audit import audit
from analystos.governance.policy import denied, evaluate, require_role

PLAYBOOK = "playbook.elt_build"
RUN_STEP = "elt_run"
BYTES_PER_VALUE = 16  # a labelled rough figure: Postgres gives no byte estimate through a read-only SELECT


# ------------------------------------------------------------------------------ targets
def designate_target(session: Session, user: User, workspace_id: str, schema: str, *,
                     engine: str = DEFAULT_ENGINE) -> BuildTarget:
    """A workspace owner (or platform admin) designates a schema the builds of this workspace may
    write. Provisions the build role and the schema, then records the target."""
    if not user.is_admin:
        require_role(session, user, workspace_id, "owner")
    if engine != DEFAULT_ENGINE:
        raise InvalidInput(f"engine {engine} has no build path yet (supported: {DEFAULT_ENGINE})")
    settings = get_settings()
    taken = session.scalar(select(BuildTarget).where(BuildTarget.engine == engine, BuildTarget.schema_name == schema))
    if taken is not None and taken.workspace_id != workspace_id:
        raise Forbidden(f"schema {schema} is already a build target of another workspace")
    grants = provision_target(settings, workspace_id, schema, source_schemas=source_schemas(session))
    target = taken or BuildTarget(id=new_id("btg"), workspace_id=workspace_id, engine=engine, schema_name=schema,
                                  build_role=build_role_for(settings, workspace_id), created_by=user.id)
    target.status, target.provisioning = "active", grants
    session.add(target)
    session.flush()
    audit(f"user:{user.id}", "build_target.provisioned", workspace_id=workspace_id, target=f"{engine}/{schema}",
          decision="allow", details=grants, session=session)
    emit(workspace_id, "build_target.provisioned", {"schema": schema, "engine": engine, "build_role": target.build_role},
         actor=f"user:{user.id}", session=session)
    return target


def start_build(user: User, workspace_id: str, *, from_run_id: str, target_schema: str,
                engine: str = DEFAULT_ENGINE, autonomy_level: int | None = None) -> AnalysisRun:
    """Start an `elt_build` run for a completed run's dataset and KPIs. Fails fast on an undesignated
    target or a run without a dataset; the run itself re-checks everything."""
    from analystos.services.runs import create_run

    with session_scope() as s:
        require_role(s, user, workspace_id, "editor")
        require_target(s, workspace_id, engine, target_schema)
        source = s.get(AnalysisRun, from_run_id)
        if source is None or source.workspace_id != workspace_id:
            raise NotFound(f"run {from_run_id} not found")
        dataset = s.scalar(select(Artifact).where(Artifact.run_id == from_run_id, Artifact.type == "dataset",
                                                  current_plan_filter(source)).order_by(Artifact.created_at.desc()))
        if dataset is None:
            raise InvalidInput(f"run {from_run_id} has no dataset to build")
        source_id = dataset.content.get("source_id")
    return create_run(user, workspace_id, objective=f"Build run {from_run_id}'s dataset and KPIs into {target_schema} with dbt",
                      source_ids=[source_id] if source_id else None,
                      # a build is human-in-the-loop by nature: never autonomous (the playbook is not certified for it)
                      autonomy_level=min(2 if autonomy_level is None else autonomy_level, 2), playbook=PLAYBOOK,
                      origin={"type": "build", "from_run": from_run_id, "target_schema": target_schema, "engine": engine})


# ------------------------------------------------------------------------------ plan (elt_plan step)
def _inputs(s: Session, workspace_id: str, from_run_id: str) -> tuple[Artifact, list[Artifact]]:
    source = s.get(AnalysisRun, from_run_id)
    if source is None or source.workspace_id != workspace_id:
        raise NotFound(f"run {from_run_id} not found in this workspace")
    arts = list(s.scalars(select(Artifact).where(Artifact.run_id == from_run_id, Artifact.type.in_(["dataset", "metric"]),
                                                 current_plan_filter(source)).order_by(Artifact.created_at)))
    datasets = [a for a in arts if a.type == "dataset"]
    if not datasets:
        raise InvalidInput(f"run {from_run_id} has no dataset to build")
    return datasets[-1], [a for a in arts if a.type == "metric"]


def select_kpis(session: Session, workspace_id: str, policy: Any, metrics: list[MetricDef]) -> tuple[list[MetricDef], list[dict]]:
    """The KPIs a build materializes as dbt metrics come from the workspace semantic model (P4-K03):
    a run KPI whose name has an approved version with the same expression is built from that approved
    definition. Under `require_approved_metrics` nothing else is built (the dataset still is); without
    it, run-validated KPIs are built too and labelled as such."""
    from analystos.semantic import ossie
    from analystos.semantic.service import approved_metrics, to_metricdef

    approved = approved_metrics(session, workspace_id)
    out, skipped = [], []
    for m in metrics:
        row = approved.get(m.name)
        if row is not None and row.normalized_expression == ossie.normalize_expression(m.sql_expression):
            out.append(to_metricdef(row))
        elif getattr(policy, "require_approved_metrics", False):
            skipped.append({"metric": m.name, "reason": "not approved in the workspace semantic model "
                                                        "(policy require_approved_metrics); approve it, then plan the build again"})
        elif m.status in ("validated", "approved"):
            out.append(m)
        else:
            skipped.append({"metric": m.name, "reason": f"status {m.status}: only validated or approved KPIs are built"})
    return out, skipped


def plan_build(ctx: Any) -> dict[str, Any]:
    from analystos.gateway.validator import validate_sql

    settings = get_settings()
    origin = ctx.run.origin or {}
    from_run, target_schema = origin.get("from_run"), origin.get("target_schema")
    engine = origin.get("engine") or DEFAULT_ENGINE
    if not from_run or not target_schema:
        raise InvalidInput("an elt_build run needs origin.from_run and origin.target_schema")
    with session_scope() as s:
        require_target(s, ctx.workspace.id, engine, target_schema)
        ds_art, metric_arts = _inputs(s, ctx.workspace.id, from_run)
        dataset, ds_id = dict(ds_art.content), ds_art.id
        metric_ids = {a.name: a.id for a in metric_arts}
        metrics_all = [MetricDef.model_validate(a.content) for a in metric_arts]
        src = s.get(Source, dataset.get("source_id")) if dataset.get("source_id") else None
        if src is None or src.workspace_id != ctx.workspace.id:
            raise InvalidInput("the dataset's source is not in this workspace")
        if src.execution_mode != "staged":
            raise InvalidInput(f"source {src.id} is queried in place ({src.kind}); builds on {engine} read staged sources only "
                               "until engines and federation land (P4-E01/E03)")
    # The dataset is re-validated against this run's scope: a column restricted since, or an asset
    # no longer selected, stops the build before anything is generated.
    validated = validate_sql(ctx.scope, dataset["sql"], max_rows=1)
    allowed = sorted(set(validated.referenced_assets))
    with session_scope() as s:
        metrics, skipped_kpis = select_kpis(s, ctx.workspace.id, ctx.policy, metrics_all)

    # Dry run 1: candidate tests and the size, read through the query gateway (reader identity).
    candidates = dbt_project.candidate_tests(dataset)
    run_sql = ctx.run_sql(src.id)
    probe = run_sql(dbt_project.test_probe_sql(dataset["sql"], candidates), purpose="build.dry_run", use_cache=False)
    tests = dbt_project.passing_tests(candidates, probe.rows[0])
    rows = int(probe.rows[0][0] or 0)
    generated = dbt_project.generate(dataset, metrics, tests)
    dbt_project.check_files(generated.files, allowed_sources=set(allowed))
    relations = generated.relations(target_schema)

    # Dry run 2: dbt parses the exact files (no database connection) and the manifest it resolves is checked.
    job_id = new_id("bld")
    gateway = BuildGateway(settings)
    dry_dir = gateway.job_dir(job_id) / "dry_run"
    write_project(dry_dir / "project", generated.files)
    write_profile(dry_dir / "profile", None, schema=target_schema)
    parsed = gateway.runner.parse(dry_dir)
    if not parsed.ok:
        raise InvalidInput("dbt parse failed on the generated project: " + parsed.log_tail[-600:])
    resolved = dbt_project.check_manifest(parsed.manifest, project=generated.name, target_schema=target_schema,
                                          allowed_sources=set(allowed))
    shutil.rmtree(dry_dir, ignore_errors=True)
    dry_run = {"runner": gateway.runner.name, "command": parsed.command, "ok": True, "dbt_version": resolved["dbt_version"],
               "manifest": resolved, "ossie_version": (parsed.osi_document or {}).get("version"),
               "tests": {"candidates": candidates, "passing": tests, "dropped": [t for t in candidates if t not in tests]},
               "allowed_sources": allowed, "metrics": generated.metrics,
               "skipped_metrics": skipped_kpis + generated.skipped_metrics, "notes": generated.notes}
    estimate = {"rows": rows, "columns": len(dataset.get("columns") or []), "models": len(relations),
                "tests": len(resolved["tests"]), "approx_bytes": rows * len(dataset.get("columns") or []) * BYTES_PER_VALUE,
                "method": "COUNT(*) of the dataset through the query gateway; bytes = rows x columns x 16 (rough)",
                "query_id": probe.query_id}

    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id)
        previous = s.scalar(select(BuildJob).where(BuildJob.workspace_id == ctx.workspace.id, BuildJob.engine == engine,
                                                   BuildJob.target_schema == target_schema, BuildJob.status == "succeeded")
                            .order_by(BuildJob.finished_at.desc()))
        rollback = {"strategy": "drop the relations this job creates or replaces",
                    "statements": [f'DROP TABLE IF EXISTS "{r.split(".")[0]}"."{r.split(".")[1]}" CASCADE' for r in relations
                                   if not r.endswith(f".{dbt_project.TIME_SPINE}")]
                    + [f'DROP VIEW IF EXISTS "{target_schema}"."{dbt_project.TIME_SPINE}" CASCADE'
                       for r in relations if r.endswith(f".{dbt_project.TIME_SPINE}")],
                    "restore": (f"re-run build job {previous.id} (same target; needs its own approval)" if previous else
                                "nothing to restore: no earlier build wrote this target"),
                    "previous_job_id": previous.id if previous else None}
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="transformation",
                            name=f"dbt:{generated.name}", creator_agent=ctx.agent.id,
                            content={"kind": "dbt_project", "project_name": generated.name, "project_hash": generated.hash,
                                     "engine": engine, "target_schema": target_schema, "relations": relations,
                                     "files": generated.files, "source_run_id": from_run, "dataset_artifact_id": ds_id})
        link(s, ctx.workspace.id, ("dataset", ds_id), "materialized_by", ("transformation", art.id), run_id=ctx.run.id)
        for m in generated.metrics:
            if m["metric"] in metric_ids:
                link(s, ctx.workspace.id, ("metric", metric_ids[m["metric"]]), "materialized_by", ("transformation", art.id),
                     run_id=ctx.run.id)
        job = BuildJob(id=job_id, workspace_id=ctx.workspace.id, run_id=ctx.run.id, source_run_id=from_run, artifact_id=art.id,
                       engine=engine, runner=gateway.runner.name, target_schema=target_schema, project_name=generated.name,
                       project_files=generated.files, project_hash=generated.hash, plan_hash=run.plan_hash,
                       relations=relations, dry_run=dry_run, estimate=estimate, rollback=rollback, status="awaiting_approval",
                       created_by=f"agent:{ctx.agent.id}")
        s.add(job)
        s.flush()
        user = s.get(User, run.requested_by)
        decision = evaluate(s, user, ctx.identity, "build", destination=f"{engine}/{target_schema}")
        if decision.decision == "deny":
            raise denied(decision, "build denied: " + ", ".join(decision.reasons))
        approval = request_approval(
            s, workspace_id=ctx.workspace.id, run_id=run.id, action=BUILD_ACTION, payload=approval_payload(job),
            plan_hash=run.plan_hash, policy_version=run.policy_version, requested_by=run.requested_by, risk_tier="high",
            destination=f"{engine}/{target_schema}", affected_assets=relations,
            evidence={"dry_run": {k: dry_run[k] for k in ("runner", "dbt_version", "ossie_version", "tests", "skipped_metrics")},
                      "estimate": estimate, "rollback": rollback, "policy": decision.model_dump(),
                      "risk_tier_basis": "deterministic: a build writes to the customer's engine and is always approval-gated"})
        job.approval_id = approval.id
        task = s.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == RUN_STEP))
        if task is not None:
            task.input = {**task.input, "approval_id": approval.id, "job_id": job.id}
        emit(ctx.workspace.id, "build.planned", {"job_id": job.id, "project_hash": generated.hash, "target_schema": target_schema,
                                                 "relations": relations, "rows": rows, "approval_id": approval.id},
             run_id=run.id, session=s)
        payload_hash = approval.payload_hash
    ctx.say(f"dbt project {generated.name} ({len(generated.files)} files, hash {generated.hash[:12]}) parsed cleanly with "
            f"{len(resolved['tests'])} tests and {len(generated.metrics)} metrics; about {rows:,} rows into {target_schema}. "
            f"Build awaits approval {approval.id} (payload hash {payload_hash[:12]}); nothing is written until a person approves.",
            kind="decision")
    return {"job_id": job_id, "approval_id": approval.id, "project_hash": generated.hash, "relations": relations,
            "estimate": estimate}


def run_build(ctx: Any) -> dict[str, Any]:
    """The `elt_run` side-effect step: the BuildGateway re-verifies and runs the approved job."""
    job_id, approval_id = ctx.task.input.get("job_id"), ctx.task.input.get("approval_id")
    if not job_id:
        raise InvalidInput("no build job was planned for this run")
    ctx.check_control()
    result = BuildGateway(get_settings()).execute(job_id, approval_id, actor=f"agent:{ctx.agent.id}")
    ctx.say(f"Built {', '.join(result['relations']) or 'nothing'} with dbt ({result['counts']}). "
            f"Rollback plan: {result['rollback'].get('strategy')}.", kind="decision")
    return result


# ------------------------------------------------------------------------------ review (P4-U05)
def previous_job(session: Session, job: BuildJob) -> BuildJob | None:
    """The job this one is reviewed against: the latest earlier job of the same workspace, engine and
    target schema, whatever its outcome (a refused plan is still what was last proposed there)."""
    return session.scalar(select(BuildJob).where(BuildJob.workspace_id == job.workspace_id, BuildJob.engine == job.engine,
                                                 BuildJob.target_schema == job.target_schema, BuildJob.id != job.id,
                                                 BuildJob.created_at < job.created_at)
                          .order_by(BuildJob.created_at.desc()))


def job_diff(session: Session, job: BuildJob, against_id: str | None = None) -> dict[str, Any]:
    """The generated project file by file against an earlier job (default: `previous_job`). A job of
    another workspace is never a comparison basis: its files are not the caller's to read."""
    from analystos.build.diff import file_diff

    if against_id:
        base = session.get(BuildJob, against_id)
        if base is None or base.workspace_id != job.workspace_id:
            raise NotFound(f"build job {against_id} not found in this workspace")
        basis = "requested"
    else:
        base = previous_job(session, job)
        basis = "previous_job_same_target" if base else "none"
    out = file_diff(dict(base.project_files or {}) if base else None, dict(job.project_files or {}))
    out.update({"job_id": job.id, "project_hash": job.project_hash, "basis": basis,
                "against": ({"job_id": base.id, "status": base.status, "project_hash": base.project_hash,
                             "created_at": base.created_at.isoformat() if base.created_at else None} if base else None),
                "identical": base is not None and base.project_hash == job.project_hash})
    return out


def approval_summary(session: Session, job: BuildJob) -> dict[str, Any] | None:
    """The state of the approval covering a job, for the Build view. The decision itself is made in the
    approvals inbox, never here."""
    from analystos.db.models import Approval

    approval = session.get(Approval, job.approval_id) if job.approval_id else None
    if approval is None:
        return None
    return {"id": approval.id, "status": approval.status, "action": approval.action, "payload_hash": approval.payload_hash,
            "plan_hash": approval.plan_hash, "policy_version": approval.policy_version, "risk_tier": approval.risk_tier,
            "requested_by": approval.requested_by, "decided_by": approval.decided_by,
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None, "reason": approval.reason,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None}

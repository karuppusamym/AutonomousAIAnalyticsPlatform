"""analystos CLI: migrate | provision-analytics-roles | seed | demo-seed | worker | scheduler | api | export-contracts | replay-run | packs | calibrate | knowledge | check-semantics | audit"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analystos.core.config import DATA_ROOT, STATE_ROOT, get_settings


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(DATA_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DATA_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")
    provision_analytics_roles()
    refresh_knowledge_index()


def refresh_knowledge_index() -> None:
    """Index packs whose head revision is not indexed yet (e.g. the platform pack migration 0018
    creates). Best effort, like role provisioning: `analystos knowledge reindex` rebuilds it all."""
    from analystos.core.logging import get_logger
    from analystos.db.base import session_scope
    from analystos.knowledge.index import refresh

    try:
        with session_scope() as s:
            refresh(s)
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).warning("knowledge index refresh skipped: %s", type(exc).__name__)


def provision_analytics_roles() -> dict:
    """Self-healing step for per-workspace analytics reader roles (spec v3 tenant isolation):
    give the loader CREATEROLE when the control-plane identity may, then move every existing staged
    schema from the shared reader grant to its workspace's role. Best effort: an unreachable
    analytics DB must not block a control-plane migration (loads repair their own schema)."""
    from sqlalchemy import select
    from sqlalchemy.engine import make_url

    from analystos.core.logging import get_logger
    from analystos.db.base import session_scope
    from analystos.db.models import Source
    from analystos.staging.roles import backfill, provision_loader_createrole

    log = get_logger(__name__)
    settings = get_settings()
    try:
        provision_loader_createrole(settings.database_url, make_url(settings.analytics_loader_url).username or "")
        with session_scope() as s:
            staged = [(r.id, r.workspace_id) for r in s.scalars(select(Source).where(Source.execution_mode == "staged"))]
        out = backfill(settings, staged)
    except Exception as exc:  # noqa: BLE001
        log.warning("analytics role provisioning skipped: %s", str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)
        return {"error": type(exc).__name__}
    try:  # the build (write) login of the BuildGateway (P4-E06); clusters from before it lack it
        from analystos.build.targets import ensure_builder_login

        out["builder_login"] = ensure_builder_login(settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("build login provisioning skipped: %s", str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)
    try:  # the managed output writer's login (P6-03); clusters from before it lack it
        from analystos.pipelines.writer import ensure_writer_login

        out["writer_login"] = ensure_writer_login(settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("writer login provisioning skipped: %s", str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)
    return out


def seed() -> None:
    from sqlalchemy import select

    from analystos.capabilities import packs
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.knowledge.platform import sync_from_domain_packs
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope
    from analystos.tools.registry import seed_registries

    settings = get_settings()
    with system_scope(), session_scope() as s:  # platform rows; bootstrap users join the default tenant
        users = [(settings.bootstrap_admin_email, "Platform Admin", settings.bootstrap_admin_password, True, {"pii_clearance": False})]
        if settings.env == "dev":  # demo accounts with a known password never exist outside development (Helm sets production)
            users += [("analyst@analystos.local", "Ada Analyst", "ChangeMe123!", False, {}),
                      ("approver@analystos.local", "Priya Approver", "ChangeMe123!", False, {})]
        for email, name, pw, admin, attrs in users:
            if not s.scalar(select(User).where(User.email == email)):
                s.add(User(id=new_id("usr"), email=email, name=name, password_hash=hash_password(pw), is_admin=admin, attributes=attrs))
        seed_registries(s)
        # Domain knowledge comes from the installed domain packs (packs/<name>/knowledge) and lives in
        # the read-only platform knowledge pack (P4-K01); an unchanged sync writes no new revision.
        report = sync_from_domain_packs(s, packs.installed())
    print("seeded users, registries and domain-pack knowledge "
          f"(platform pack revision {report['revision']}, {report['documents']} documents, "
          f"{'updated' if report['changed'] else 'unchanged'})")


def list_packs() -> None:
    from analystos.capabilities import packs

    for p in packs.installed():
        print(f"{p.ref:24} {len(p.templates.get('templates') or []):2} templates  {len(p.knowledge):2} knowledge docs  "
              f"{len(p.kpis):2} KPIs  applies_when={p.applies_when}  {p.summary}")


def export_contracts(out_dir: str | None = None) -> None:
    from analystos.capabilities.agents import AgentBody
    from analystos.contracts import (
        analysis,
        bi,
        brief,
        capability,
        citations,
        definition,
        evidence,
        goals,
        platform,
        policy,
        recipe,
        registry,
        scenario,
        semantic,
        step,
        tool_manifest,
        work,
        worker,
    )

    out = Path(out_dir) if out_dir else STATE_ROOT / "contracts"
    out.mkdir(parents=True, exist_ok=True)
    models = {"agent": registry.AgentSpec, "agent_manifest": AgentBody, "tool": registry.ToolSpec, "skill": registry.SkillSpec, "policy": policy.WorkspacePolicyDoc,
              "data_scope": policy.DataScope, "policy_decision": policy.PolicyDecision, "analysis_spec": analysis.AnalysisSpec,
              "stat_result": analysis.StatResult, "partition": analysis.Partition, "chart": bi.ChartSpec, "dashboard": bi.DashboardSpec, "metric": bi.MetricDef,
              "dataset": bi.DatasetDef, "publish_bundle": bi.PublishBundle,
              "platform_settings": platform.PlatformSettings, "capability": capability.CapabilityManifest,
              "semantic_model": semantic.SemanticModelDoc, "semantic_metric": semantic.SemanticMetricDef,
              "semantic_query": semantic.SemanticQuery,
              "scenario_spec": scenario.ScenarioSpec, "scenario_result": scenario.ScenarioResult,
              "evidence_bundle": evidence.EvidenceBundle, "data_manifest": evidence.DataManifest, "fact": evidence.Fact,
              "evidence_v2": evidence.EvidenceV2,
              "citations": citations.Citations,
              "definition_ref": definition.DefinitionRef, "pin_status": definition.PinStatus, "work_order": work.WorkOrderSpec,
              "recipe": recipe.Recipe, "workspace_brief": brief.WorkspaceBriefDoc,
              "readiness_assessment": brief.ReadinessAssessmentDoc, "job_kind": brief.JobKindAvailability,
              "step": step.Step, "branch": step.Branch,
              "pipeline": work.PipelineSpec, "task_envelope": worker.TaskEnvelope,
              "task_dispatch": worker.TaskDispatch, "task_result": worker.TaskResult, "artifact_ref": worker.ArtifactRef,
              "goal": goals.GoalIn, "run_limits": goals.RunLimits, "goal_signal": goals.SignalIn,
              "tool_manifest": tool_manifest.ToolManifestV2}
    for name, model in models.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    from analystos.contracts.events import EVENT_TYPES

    (out / "events.json").write_text(json.dumps(sorted(EVENT_TYPES), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(models) + 1} contract files to {out}")


def replay_run(run_id: str, *, check: bool, out: str | None) -> int:
    """Reconstruct a run's model inputs/outputs from storage; with --check, re-execute every
    recorded call offline through the router (ReplayTransport) and compare with the recording."""
    from analystos.llm.replay import run_report

    report = run_report(run_id, check=check)
    text = json.dumps(report, indent=2, default=str)
    if out:
        from pathlib import Path

        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {report['summary']['calls']} model calls of run {run_id} to {out}")
    else:
        print(text)
    if check:
        replay = report["replay"]
        print(f"replay: {replay['matched']}/{replay['checked']} calls reproduced offline, "
              f"{len(replay['mismatches'])} mismatches", file=sys.stderr)
        return 1 if replay["mismatches"] else 0
    return 0


def calibrate(*, dry_run: bool) -> int:
    """Decision calibration now (the scheduler also runs it nightly): Brier/ECE per purpose x backend,
    downgrading or restoring backends against the admin thresholds."""
    from analystos.db.base import session_scope
    from analystos.decisions.calibration import run_calibration

    with session_scope() as s:
        result = run_calibration(s, actor="cli", dry_run=dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def schedules_main(argv: list[str]) -> int:
    """`analystos schedules disable-demo`: stop demo/evidence schedules and monitors from running (and
    spending model credit) on a dev stack. Marked items only, unless --all."""
    parser = argparse.ArgumentParser(prog="analystos schedules")
    parser.add_argument("command", choices=["disable-demo"])
    parser.add_argument("--all", action="store_true", help="every enabled schedule and monitor, not only [demo] ones")
    parser.add_argument("--workspace", help="only this workspace id")
    parser.add_argument("--dry-run", action="store_true", help="list what would be disabled")
    args = parser.parse_args(argv)
    from analystos.db.base import session_scope
    from analystos.services.schedules import disable_demo

    with session_scope() as s:
        result = disable_demo(s, include_all=args.all, workspace_id=args.workspace, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    verb = "would disable" if args.dry_run else "disabled"
    print(f"{verb} {len(result['schedules'])} schedules and {len(result['monitors'])} monitors", file=sys.stderr)
    return 0


def audit_main(argv: list[str]) -> int:
    """`analystos audit seal|verify|export`: the tamper-evident audit chain (S01-T5). Exit 4 when a chain is broken."""
    parser = argparse.ArgumentParser(prog="analystos audit")
    parser.add_argument("command", choices=["seal", "verify", "export"])
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--tenant", help="one tenant's chain (default: every chain, the platform chain included)")
    who.add_argument("--platform", action="store_true", help="only the platform chain (events without a tenant)")
    parser.add_argument("--full", action="store_true", help="seal: also rows older than the sealing window")
    parser.add_argument("--out", help="export: directory (default ANALYSTOS_AUDIT_EXPORT_DIR)")
    args = parser.parse_args(argv)
    from analystos.governance import audit_chain as ac

    targets = [args.tenant] if args.tenant else [None] if args.platform else ac.chains()
    if args.command == "seal":
        result: object = {ac.chain_key(t): ac.seal(t, full=args.full)["sealed"] for t in targets}
    elif args.command == "verify":
        result = [ac.verify_and_record(t, "cli") for t in targets]
    else:
        out = args.out or get_settings().audit_export_dir
        if not out:
            print("no export directory: pass --out or set ANALYSTOS_AUDIT_EXPORT_DIR", file=sys.stderr)
            return 2
        result = [ac.export(t, Path(out), actor="cli") for t in targets]
    print(json.dumps(result, indent=2, default=str))
    broken = args.command == "verify" and any(not r["ok"] for r in result)  # type: ignore[union-attr]
    return 4 if broken else 0


def context_main(argv: list[str]) -> int:
    """`analystos context reconcile [--workspace W]...`: catch the Context Ledger up with its owner tables (S03-T1)
    and print the consistency check. Exit 5 when a workspace is still inconsistent afterwards."""
    parser = argparse.ArgumentParser(prog="analystos context")
    parser.add_argument("command", choices=["reconcile"])
    parser.add_argument("--workspace", action="append", help="only this workspace (repeatable; default: every workspace)")
    args = parser.parse_args(argv)
    from sqlalchemy import select

    from analystos.context import ledger
    from analystos.db.base import session_scope
    from analystos.db.models import Workspace

    with session_scope() as s:
        ids = args.workspace or list(s.scalars(select(Workspace.id).where(Workspace.deleted_at.is_(None)).order_by(Workspace.id)))
    out, bad = {}, False
    for ws in ids:
        with session_scope() as s:
            counts = ledger.reconcile(s, ws)
            problems = ledger.check_consistency(s, ws)
        bad = bad or any(problems.values())
        out[ws] = {"changes": counts, "problems": {k: len(v) for k, v in problems.items()}}
    print(json.dumps(out, indent=2))
    return 5 if bad else 0


def bi_sync(workspace_ids: list[str] | None = None) -> dict:
    """Superset roles per workspace and users from workspace membership (P4-02)."""
    from analystos.db.base import session_scope
    from analystos.publishing.superset import SupersetPublisher
    from analystos.services.bi_access import sync_bi_access

    publisher = SupersetPublisher()
    try:
        with session_scope() as session:
            return sync_bi_access(session, publisher, workspace_ids)
    finally:
        publisher.close()


# Long-running servers bind tenants per request, per run and per claimed item; demo-seed goes through the API.
_SELF_BINDING = ("worker", "scheduler", "api", "demo-seed")


def main(argv: list[str] | None = None) -> int:
    """Operator commands (migrate, seed, calibrate, knowledge, bi-sync, ...) are cross-tenant platform
    maintenance run with the owner's credentials: they run in ``system_scope`` (S01-T4)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((a for a in argv if not a.startswith("-")), None)
    if command in _SELF_BINDING:
        return _main(argv)
    from analystos.tenancy import system_scope

    with system_scope():
        return _main(argv)


def _main(argv: list[str]) -> int:
    if argv[:1] == ["knowledge"]:
        from analystos.knowledge.cli import main as knowledge_main

        return knowledge_main(argv[1:])
    if argv[:1] == ["schedules"]:
        return schedules_main(argv[1:])
    if argv[:1] == ["audit"]:
        return audit_main(argv[1:])
    if argv[:1] == ["context"]:
        return context_main(argv[1:])
    if argv[:1] == ["demo-seed"]:  # a ready demo workspace through the running API (`analystos demo-seed --help`)
        from analystos.demo.seed import main as demo_seed

        return demo_seed(argv[1:])
    if argv[:1] == ["check-semantics"]:  # offline semantic model validation (ADR-0019 §6)
        from analystos.semantic.check import main as check_semantics

        rest = [a for a in argv[1:] if a != "--json"]
        return check_semantics(rest, as_json="--json" in argv[1:])
    parser = argparse.ArgumentParser(prog="analystos")
    parser.add_argument("command", choices=["migrate", "provision-analytics-roles", "seed", "worker", "scheduler", "api",
                                            "export-contracts", "replay-run", "packs", "calibrate", "knowledge", "schedules", "audit", "context",
                                            "bi-sync", "sandbox-status", "check-semantics", "demo-seed"],
                        help="knowledge: `analystos knowledge --help` (reindex, reembed, import, export, ...); "
                             "demo-seed: `analystos demo-seed --help` (a ready demo workspace through the API); "
                             "check-semantics: `analystos check-semantics PATH... [--json]` (offline model validation); "
                             "schedules: `analystos schedules disable-demo [--all] [--workspace W] [--dry-run]`; "
                             "audit: `analystos audit seal|verify|export [--tenant T | --platform] [--full] [--out DIR]`; "
                             "context: `analystos context reconcile [--workspace W]` (Context Ledger projection)")
    parser.add_argument("run_id", nargs="?", help="replay-run: the analysis run id")
    parser.add_argument("--check", action="store_true", help="replay-run: re-execute recorded calls offline and compare")
    parser.add_argument("--out", help="replay-run: write the JSON report to this file; export-contracts: the directory")
    parser.add_argument("--queues", help="worker: comma-separated workloads to serve (analysis, compute, publish, crawl, elt; "
                                         "default ANALYSTOS_WORKER_QUEUES or all). The isolated pools compute-py and "
                                         "compute-ml (ADR-0022) are named alone and run without credentials")
    parser.add_argument("--conformance", action="store_true",
                        help="worker, isolated pools only: also serve the conformance probe (tests/conformance/worker)")
    parser.add_argument("--dry-run", action="store_true", help="calibrate: report without downgrading or restoring")
    parser.add_argument("--workspace", action="append", help="bi-sync: only members of this workspace (repeatable)")
    args = parser.parse_args(argv)
    if args.command == "calibrate":
        return calibrate(dry_run=args.dry_run)
    if args.command == "replay-run":
        if not args.run_id:
            parser.error("replay-run needs a run id")
        return replay_run(args.run_id, check=args.check, out=args.out)
    if args.command == "migrate":
        migrate()
    elif args.command == "provision-analytics-roles":
        print(json.dumps(provision_analytics_roles(), indent=2))
    elif args.command == "seed":
        seed()
    elif args.command == "worker":
        from analystos.workflows.worker import run_worker

        code = run_worker(args.queues, conformance=True) if args.conformance else run_worker(args.queues)
        if isinstance(code, int) and code:
            return code
    elif args.command == "scheduler":
        from analystos.services.schedules import run_scheduler

        run_scheduler()
    elif args.command == "api":
        import uvicorn

        uvicorn.run("analystos.api.app:app", host="0.0.0.0", port=8000)
    elif args.command == "export-contracts":
        export_contracts(args.out)
    elif args.command == "packs":
        list_packs()
    elif args.command == "bi-sync":
        print(json.dumps(bi_sync(args.workspace), indent=2))
    elif args.command == "sandbox-status":
        from analystos.sandbox.isolation import status

        st = status(refresh=True)
        print(st.model_dump_json(indent=2))
        return 0 if st.available else 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

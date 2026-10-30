"""Analytics Supervisor: plan framing, plan-approval gate and run consolidation."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from analystos.agents.common import defer_compile, llm_json, model_gate
from analystos.artifacts.registry import link, save_artifact
from analystos.context.service import add_entry
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Insight, RunTask, by_code
from analystos.events.bus import emit
from analystos.graph.projection import project_workspace
from analystos.runtime.context import RunContext, Services
from analystos.runtime.plan import base_plan

if TYPE_CHECKING:
    from analystos.capabilities.playbook import Playbook


def build_plan(run_id: str, services: Services, playbook: Playbook | None = None) -> dict:
    """The run's playbook (default investigate.v1) instantiated for this objective, with the
    supervisor's framing (questions, audience, focus) when the playbook asks for it."""
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        objective, level, instructions = run.objective, run.autonomy_level, run.instructions
    framing: dict = {}
    # Framing needs the catalog, which needs a context; build a light pseudo-context for the supervisor.
    try:
        if playbook is not None and not playbook.body.framing:
            raise LookupError("this playbook does not frame")
        ctx = _supervisor_ctx(run_id, services)
        payload = defer_compile(ctx, "planning", {"objective": objective,
                                                "user_instructions": [i.get("text") for i in instructions]})
        # Framing is optional: the lifecycle skeleton is valid without it, so `auto` skips the model.
        data, model = llm_json(ctx, "planning", "planning.v1", payload, validate=_valid_framing) \
            if model_gate(ctx, "planning", payload, deterministic_ok=True) else (None, "deterministic")
        if isinstance(data, dict):
            framing = data
            ctx.say(f"Framed the objective into {len(data.get('questions') or [])} analytical questions.", kind="decision",
                    data={"model": model, "questions": data.get("questions")})
    except Exception:  # planning framing is optional; the lifecycle skeleton is always valid
        framing = {}
    questions = [str(q) for q in (framing.get("questions") or [])][:8]
    audience = [a for a in (framing.get("audience") or ["executive", "operational"]) if a in ("executive", "operational")]
    return base_plan(objective, autonomy_level=level, questions=questions, audience=audience or ["executive", "operational"],
                     focus=[str(f) for f in (framing.get("focus") or [])][:6], playbook=playbook)


def _valid_framing(data: object) -> str | None:
    """Escalation check (schema): framing is an object whose questions are a list."""
    if not isinstance(data, dict):
        return "answer is not an object"
    if not isinstance(data.get("questions"), list) or not data["questions"]:
        return "no analytical questions in the framing"
    return None


def _supervisor_ctx(run_id: str, services: Services) -> RunContext:
    """A RunContext for planning before tasks exist (uses a transient task row)."""
    from analystos.core.ids import new_id

    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "_planning"))
        if task is None:
            task = RunTask(id=new_id("tsk"), run_id=run_id, key="_planning", agent_id="supervisor", title="Planning",
                           status="COMPLETED", plan_version=0, seq=-1, input={"optional": True})
            s.add(task)
    return RunContext.load(run_id, "_planning", services)


def plan_approved(ctx: RunContext) -> dict:
    with session_scope() as s:
        approval = s.get(Approval, ctx.task.input.get("approval_id"))
        if approval and approval.status == "approved":
            approval.status = "executed"
    return {"approved": True}


_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def finding_standing(ins: Insight) -> dict:
    """P8-15: whether a verified finding was confirmed on separate data, and its strength."""
    validation = (ins.evidence_bundle or {}).get("validation") or {}
    return {"confirmed": ins.validation == "confirmed", "strength": (validation.get("strength") or {}).get("label")}


def standing_note(facts: list[dict]) -> str:
    """The summary's plain-language line for findings that are not established facts (empty when none)."""
    leads = [f["code"] for f in facts if not (f.get("standing") or {}).get("confirmed")]
    weak = [f["code"] for f in facts if (f.get("standing") or {}).get("strength") == "weak"]
    lines = []
    if leads:
        lines.append("_Not yet confirmed on separate data, so treat these as leads worth checking, not established facts: "
                     f"{', '.join(leads)}._")
    if weak:
        lines.append(f"_Weak evidence (the effect only just clears the minimum size that counts): {', '.join(weak)}._")
    return "\n".join(lines)


def period_note(run_id: str) -> str:
    """A guided run's requested comparison on every row and its period data facts (empty when it has none)."""
    from analystos.agents.common import task_output

    comparisons = task_output(run_id, "verify").get("period_comparison") or []
    data_facts = (task_output(run_id, "hypotheses").get("period") or {}).get("facts") or []
    lines = [f"- {c['sentence']}" for c in comparisons if c.get("sentence")] + [f"- Data fact: {f}" for f in data_facts]
    return ("**Requested period**\n" + "\n".join(lines)) if lines else ""


def finalize(ctx: RunContext) -> dict:
    with session_scope() as s:
        insights = list(s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "verified")
                                  .order_by(*by_code(Insight.code))))
        facts = [{"code": i.code, "title": i.title, "finding": i.finding, "confidence": i.confidence,
                  "impact": i.business_impact, "standing": finding_standing(i)} for i in insights]
    summary_md, source = None, "template"
    payload = {"objective": ctx.run.objective, "facts": facts}
    data, model = llm_json(ctx, "summarization", "run_summary.v1", payload) \
        if model_gate(ctx, "summarization", payload, deterministic_ok=True) else (None, "deterministic")
    if isinstance(data, dict) and isinstance(data.get("summary_markdown"), str):
        allowed = {n for f in facts for n in _NUM.findall(str(f))}
        used = set(_NUM.findall(data["summary_markdown"]))
        if used <= allowed | {str(i) for i in range(0, 11)}:
            summary_md, source = data["summary_markdown"], f"llm:{model}"
    if summary_md is None:
        summary_md = "\n".join(f"- **{f['title']}** — {f['finding']}" for f in facts) or "- No finding passed verification."
    if standing_note(facts):  # deterministic, after any model wording: the summary never states a lead as fact
        summary_md = f"{summary_md.rstrip()}\n\n{standing_note(facts)}"
    if period_note(ctx.run.id):  # PILOT-FIX-01: the requested period, its comparison and data facts, deterministic
        summary_md = f"{summary_md.rstrip()}\n\n{period_note(ctx.run.id)}"
    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id)
        tasks = {t.key: t for t in s.scalars(select(RunTask).where(RunTask.run_id == ctx.run.id))}
        published = tasks.get("publish") and tasks["publish"].status == "COMPLETED"
        run.summary = {**(run.summary or {}), "summary_markdown": summary_md, "summary_source": source,
                       "verified_insights": len(facts), "published": bool(published),
                       "publication": (tasks["publish"].output if published else None)}
        add_entry(s, workspace_id=ctx.workspace.id, kind="episode", name=f"Run {run.id}: {run.objective[:120]}",
                  body=summary_md, origin="agent")
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="narrative",
                            name="Executive summary", content={"markdown": summary_md, "source": source},
                            creator_agent="supervisor", status="final")
        link(s, ctx.workspace.id, ("run", ctx.run.id), "summarized_by", (art.type, art.id), run_id=ctx.run.id)
        for i in facts:
            link(s, ctx.workspace.id, (art.type, art.id), "cites", ("insight", i["code"]), run_id=ctx.run.id)
        from analystos.registries.hypotheses import register_run

        registered = register_run(s, run.id)  # the hypothesis registry scheduled re-analysis replays (P4-T05)
        origin = run.origin or {}
        if origin.get("previous_run_id"):
            from analystos.services.changes import diff_runs

            changes = diff_runs(s, origin["previous_run_id"], run.id)
            run.summary = {**run.summary, "changes": changes}
            ctx.say(f"Compared with run {origin['previous_run_id']}: {len(changes['new'])} new, {len(changes['persisting'])} persisting, "
                    f"{len(changes['changed'])} changed, {len(changes['resolved'])} resolved findings"
                    f"{'; ' + str(len(changes['new_questions'])) + ' new questions (novelty round)' if changes.get('new_questions') else ''}.",
                    kind="decision")
        graph = project_workspace(s, ctx.workspace.id)
        run.summary = {**run.summary, "graph_projection": graph}
        emit(ctx.workspace.id, "analysis.completed", {"verified_insights": len(facts), "published": bool(published)},
             run_id=ctx.run.id, session=s)
        run.finished_at = utcnow()
    # every investigation leaves a report in Outputs (a schedule may ask for another kind or formats)
    report_cfg = (ctx.run.origin or {}).get("report") or DEFAULT_REPORT
    report_id = _scheduled_report(ctx, report_cfg)
    return {"verified_insights": len(facts), "published": bool(published), "graph": graph, "report_artifact_id": report_id,
            "registered_hypotheses": registered}


DEFAULT_REPORT = {"kind": "executive", "formats": ["html", "pdf", "xlsx"]}


def _scheduled_report(ctx: RunContext, report_cfg: dict) -> str | None:
    """The run's report (the one a schedule asked for, else the executive report) is best effort: the summary is already committed, so a disabled
    feature or a missing PDF/XLSX extra is recorded on the run (`report_error`) and never fails the run."""
    from analystos.core.logging import get_logger
    from analystos.services.reports import generate_report

    try:
        with session_scope() as s:
            art = generate_report(s, ctx.run.id, kind=report_cfg.get("kind", "weekly_summary"),
                                  formats=tuple(report_cfg.get("formats", ["html", "pdf", "xlsx"])), actor=f"agent:{ctx.agent.id}",
                                  finalizing=True)
            run = s.get(AnalysisRun, ctx.run.id)
            run.summary = {**(run.summary or {}), "report_artifact_id": art.id}
            return art.id
    except Exception as exc:  # noqa: BLE001 - recorded and said; the findings stand without the report
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        get_logger(__name__).warning("report for run %s not generated: %s", ctx.run.id, message)
        with session_scope() as s:
            run = s.get(AnalysisRun, ctx.run.id)
            run.summary = {**(run.summary or {}), "report_error": str(message)[:1000]}
        ctx.say(f"The report was not generated: {str(message)[:300]}. The findings and summary are saved.", kind="decision")
        return None

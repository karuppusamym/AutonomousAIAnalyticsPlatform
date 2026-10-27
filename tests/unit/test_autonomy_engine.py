"""Autonomy engine without services (S06-T2..T4): the complexity router's floor, budget counters in the
orchestrator loop, COMPLETED_WITH_LIMITATIONS / EXPIRED, remaining-budget events, typed replans,
immutable superseded plans and approval invalidation."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from analystos.autonomy import budget
from analystos.autonomy.complexity import ambiguity_flags, policy_tier, route, rules_tier
from analystos.capabilities import registry
from analystos.contracts.goals import ApprovalPolicy, ComplexityFeatures, GoalIn, RunLimits, SignalIn
from analystos.core.errors import ApprovalRequired, BudgetExceeded
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Insight, RunEvent, RunTask, Workspace
from analystos.runtime import engine


@pytest.fixture(autouse=True)
def _fresh_limits_cache():
    budget._LIMITS.clear()
    yield
    budget._LIMITS.clear()


def _run(*tasks: tuple, limits: dict | None = None, status: str = "RUNNING", playbook: str | None = None,
         started_ago: float = 0.0, plan_version: int = 1) -> str:
    """tasks: (key, status, depends_on[, attempts[, input]])."""
    with session_scope() as s:
        s.add(Workspace(id="ws_1", name="Autonomy tests", created_by="usr_1", status="active", settings={}))
        s.add(AnalysisRun(id="run_1", workspace_id="ws_1", objective="find the drivers of SLA breaches", status=status,
                          plan={"steps": []}, plan_version=plan_version, plan_hash="h1", scope={"hash": "h"}, instructions=[],
                          constraints={}, control="run", requested_by="usr_1", summary={}, origin={"type": "goal"},
                          capabilities={"playbook": playbook} if playbook else {}, limits=limits or {},
                          started_at=utcnow() - timedelta(seconds=started_ago)))
        for i, (key, st, deps, *rest) in enumerate(tasks):
            attempts = rest[0] if rest else (1 if st in ("COMPLETED", "FAILED", "RUNNING") else 0)
            s.add(RunTask(id=f"tsk_{i}", run_id="run_1", key=key, agent_id="a", title=key, status=st, depends_on=deps,
                          input=(rest[1] if len(rest) > 1 else {}), output={"note": f"{key} output"}, attempts=attempts,
                          plan_version=plan_version, seq=i * 10))
    return "run_1"


def _task(key: str) -> RunTask:
    with session_scope() as s:
        return s.scalar(select(RunTask).where(RunTask.run_id == "run_1", RunTask.key == key))


def _events(type_: str) -> list[dict]:
    with session_scope() as s:
        return [e.payload for e in s.scalars(select(RunEvent).where(RunEvent.run_id == "run_1", RunEvent.type == type_)
                                             .order_by(RunEvent.id))]


# ------------------------------------------------------------------------------ contracts
def test_limits_only_tighten_and_approvals_cannot_be_waived():
    base = RunLimits(max_steps=30, max_cost_usd=0.5)
    got = base.tightened({"max_steps": 50, "max_cost_usd": 0.1, "max_replans": 1})
    assert got.as_dict() == {"max_steps": 30, "max_cost_usd": 0.1, "max_replans": 1}
    with pytest.raises(ValidationError):
        ApprovalPolicy(publish="not_required")
    with pytest.raises(ValidationError):
        GoalIn(workspace_id="ws", question="why?")  # too short
    with pytest.raises(ValidationError, match="payload.text"):
        SignalIn(type="redirect", payload={})
    with pytest.raises(ValidationError):
        RunLimits(max_steps=0)


# ------------------------------------------------------------------------------ complexity router
def test_rules_tier_from_features():
    assert rules_tier(ComplexityFeatures(table_count=1))[0] == "low"
    assert rules_tier(ComplexityFeatures(table_count=2))[0] == "medium"
    assert rules_tier(ComplexityFeatures(table_count=1, join_depth=2))[0] == "complex"
    assert rules_tier(ComplexityFeatures(table_count=1, estimated_scan_rows=20_000_000))[0] == "complex"
    assert rules_tier(ComplexityFeatures(table_count=1, requested_artifacts=["dashboard_draft"]))[0] == "medium"
    assert rules_tier(ComplexityFeatures(table_count=1, ambiguity_flags=["open_ended", "multi_part"]))[0] == "complex"
    assert "nothing_to_measure" in ambiguity_flags("Tell me something interesting about this")
    assert ambiguity_flags("What is the SLA breach rate by assignment group last month?") == []
    assert policy_tier(denied_columns=2, sensitive_columns=0, restricted_columns=0, autonomous=False,
                       external_side_effect=False)[0] == "medium"
    assert policy_tier(denied_columns=0, sensitive_columns=0, restricted_columns=1, autonomous=False,
                       external_side_effect=False)[0] == "complex"
    assert policy_tier(denied_columns=0, sensitive_columns=0, restricted_columns=0, autonomous=True,
                       external_side_effect=True)[0] == "medium"


def _route(question: str, **kw):
    snap = registry.load(packs_dir=None, entry_points=False)
    feats = kw.pop("features", None) or ComplexityFeatures(table_count=1, ambiguity_flags=ambiguity_flags(question))
    return route(feats, question=question, snapshot=snap, runnable=kw.pop("runnable", lambda pid: None), **kw)


def test_tier_cannot_be_lowered():
    """A user may raise the tier; a request below the policy floor keeps the floor."""
    q = "What is the SLA breach rate by assignment group last month?"
    masked = ComplexityFeatures(table_count=1, policy_tier="medium")
    d = _route(q, features=masked, requested_tier="low")
    assert (d.floor, d.tier) == ("medium", "medium")
    assert any("below the policy floor" in r for r in d.reasons)
    assert _route(q, features=masked, requested_tier="complex").tier == "complex"  # raising is allowed
    plain = ComplexityFeatures(table_count=3)  # rules say medium; no policy floor: a lower request is honoured
    assert _route(q, features=plain, requested_tier="low").tier == "low"
    assert _route(q, features=plain).tier == "medium"
    assert _route(q, features=ComplexityFeatures(table_count=1, policy_tier="complex"), requested_tier="low").tier == "complex"


def test_clarify_rung_only_escalates():
    vague = _route("Tell me something interesting about the data")
    assert vague.clarify["value"] == "clarify" and "what to measure" in vague.clarify["missing_inputs"]
    assert vague.floor == "medium" and vague.tier in ("medium", "complex")
    clear = _route("What is the SLA breach rate by assignment group last month?")
    assert clear.clarify["value"] == "proceed" and clear.tier == "low"

    class Model:  # a model rung says "proceed" on a question the rule escalates: the rule's escalation stands
        def __call__(self, purpose, state, question, facts):
            raise RuntimeError("backend down")

    assert _route("Tell me something interesting about the data", decide=Model()).clarify["backend"] == "rules"


def test_variance_requires_its_inputs_and_falls_back_when_not_runnable():
    d = _route("Explain the variance versus budget")  # no period named: variance requires one
    assert d.playbook == "playbook.variance" and d.clarify["value"] == "clarify" and "period" in d.clarify["missing_inputs"]
    off = _route("Explain the month-end variance in volume versus the prior month",
                 runnable=lambda pid: "disabled for this workspace" if pid == "playbook.variance" else None)
    assert off.playbook == "playbook.investigate" and "playbook.variance: disabled" in off.playbook_reason
    asked = _route("Explain the month-end variance in volume versus the prior month", requested_playbook="playbook.funnel")
    assert asked.playbook == "playbook.funnel" and asked.playbook_reason == "requested"


def test_a_template_never_takes_a_goal_above_its_tiers():
    q = "Run a data quality review of nulls and duplicates in the incident table"
    assert _route(q).playbook == "playbook.dq_review"
    big = ComplexityFeatures(table_count=6, join_depth=3)
    assert _route(q, features=big).playbook == "playbook.investigate"  # dq_review serves low and medium only


# ------------------------------------------------------------------------------ budget counters
def test_get_state_dispatches_no_more_than_the_step_budget(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "NEW", ["a"]), ("c", "NEW", ["a"]), ("d", "NEW", ["a"]), limits={"max_steps": 3})
    state = engine.get_state("run_1")
    assert state["ready"] == ["b", "c"]
    assert state["budget"]["remaining"]["max_steps"] == 2 and state["budget"]["used"]["steps"] == 1


def test_step_budget_used_up_with_work_left_ends_with_limitations(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "COMPLETED", ["a"]), ("c", "NEW", ["b"]), limits={"max_steps": 2})
    state = engine.get_state("run_1")
    assert state["limited"].startswith("max_steps (2 of 2)") and state["expired"] is False


def test_hard_counters_stop_the_run_and_running_steps_drain_first(sqlite_db, budget_counters):
    _run(("a", "COMPLETED", []), ("b", "RUNNING", ["a"]), ("c", "NEW", ["a"]), limits={"max_cost_usd": 0.10})
    budget_counters._redis.set(budget_counters.run_key("run_1", "usd"), 0.12)
    state = engine.get_state("run_1")
    assert state["running"] == ["b"] and state["draining"][0].startswith("max_cost_usd")
    with session_scope() as s:
        s.scalar(select(RunTask).where(RunTask.key == "b")).status = "COMPLETED"
    assert engine.get_state("run_1")["limited"].startswith("max_cost_usd (0.12 of 0.1)")


def test_without_limits_the_loop_is_unchanged(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "NEW", ["a"]))
    assert engine.get_state("run_1") == {"ready": ["b"]}


def test_a_step_refused_by_a_budget_is_a_stop_not_a_failure(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "FAILED", ["a"]), ("c", "NEW", ["b"]), limits={"max_steps": 20})
    with session_scope() as s:
        s.scalar(select(RunTask).where(RunTask.key == "b")).error = "budget_exceeded: run cost budget $0.10 exhausted"
    state = engine.get_state("run_1")
    assert "fail" not in state and "b: budget_exceeded" in state["limited"]


def test_wall_clock_while_waiting_for_a_person_expires(sqlite_db):
    _run(("plan_approval", "WAITING_USER", [], 0, {"approval_id": "apr_1"}), ("context", "NEW", ["plan_approval"]),
         limits={"max_duration_s": 60}, started_ago=120, status="WAITING_USER")
    with session_scope() as s:
        s.add(Approval(id="apr_1", workspace_id="ws_1", run_id="run_1", action="execute_plan", risk_tier="medium",
                       payload={}, payload_hash="x", plan_hash="h1", policy_version=1, requested_by="usr_1",
                       status="pending", expires_at=utcnow() + timedelta(days=1), evidence={}))
    state = engine.get_state("run_1")
    assert state["expired"] is True and state["limited"].startswith("max_duration_s")


def test_finish_with_limitations_keeps_validated_partials(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "COMPLETED", ["a"]), ("c", "NEW", ["b"]), ("d", "WAITING_USER", ["b"]),
         limits={"max_steps": 2})
    with session_scope() as s:
        for code, status in (("F1", "verified"), ("F2", "draft")):
            s.add(Insight(id=f"ins_{code}", workspace_id="ws_1", run_id="run_1", code=code, title=code, finding=code,
                          status=status, verified=status == "verified"))
        s.add(Approval(id="apr_pub", workspace_id="ws_1", run_id="run_1", action="publish_dashboard", risk_tier="high",
                       payload={}, payload_hash="x", plan_hash="h1", policy_version=1, requested_by="usr_1",
                       status="approved", expires_at=utcnow() + timedelta(days=1), evidence={}))
    reason = engine.get_state("run_1")["limited"]
    engine.finish_run("run_1", "COMPLETED_WITH_LIMITATIONS", reason)
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        assert run.status == "COMPLETED_WITH_LIMITATIONS" and run.finished_at is not None
        lim = run.summary["limitations"]
        assert lim["verified_findings"] == ["ins_F1"] and lim["unverified_findings"] == 1
        assert lim["completed_steps"] == ["a", "b"] and lim["not_run"] == ["c", "d"]
        assert s.get(Approval, "apr_pub").status == "invalidated"  # nothing is published after a budget stop
    assert _task("c").status == "SKIPPED" and _task("c").error.startswith("not run: max_steps")
    assert _events("run.limited")[0]["outcome"] == "COMPLETED_WITH_LIMITATIONS"
    engine.finish_run("run_1", "COMPLETED")  # terminal: a late finish changes nothing
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "COMPLETED_WITH_LIMITATIONS"


def test_remaining_budget_is_streamed_after_every_step(sqlite_db, monkeypatch):
    from analystos.agents import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "dispatch", lambda ctx: {"ok": True})
    monkeypatch.setattr(engine.RunContext, "load", classmethod(lambda cls, run_id, key, services: object()))
    _run(("a", "NEW", []), ("b", "NEW", ["a"]), limits={"max_steps": 5, "max_cost_usd": 1.0})
    assert engine.execute_task("run_1", "a", services=object())["status"] == "COMPLETED"
    ev = _events("run.budget")
    assert len(ev) == 1 and ev[0]["task"] == "a"
    assert ev[0]["remaining"]["max_steps"] == 4 and ev[0]["remaining"]["max_cost_usd"] == 1.0
    assert ev[0]["exhausted"] == []


def test_retry_budget_makes_a_crash_final(sqlite_db, monkeypatch):
    _run(("a", "COMPLETED", [], 3), ("b", "NEW", ["a"], 1), limits={"max_retries": 2})
    assert engine._is_last_attempt("run_1", "b") is True  # a's two retries used the run's allowance
    with session_scope() as s:
        s.get(AnalysisRun, "run_1").limits = {"max_retries": 5}
    assert engine._is_last_attempt("run_1", "b") is False


def test_scan_budget_refuses_a_statement_past_the_limit(budget_counters):
    budget.record_scan("run_x", 600)
    budget.check_scan("run_x", {"max_scan_bytes": 1000}, 300)
    with pytest.raises(BudgetExceeded, match="scan budget"):
        budget.check_scan("run_x", {"max_scan_bytes": 1000}, 500)
    budget.record_scan("run_x", 400)
    with pytest.raises(BudgetExceeded):
        budget.check_scan("run_x", {"max_scan_bytes": 1000}, 0)


def test_goal_cost_limit_tightens_the_model_budget(sqlite_db, budget_counters):
    from analystos.llm.router import CallContext
    from analystos.runtime.usage import DbUsageSink

    _run(("a", "NEW", []), limits={"max_cost_usd": 0.05})
    budget_counters._redis.set(budget_counters.run_key("run_1", "usd"), 0.06)
    budget_counters._redis.set(budget_counters.run_key("run_1", "tokens"), 10)
    with pytest.raises(BudgetExceeded, match="goal limit"):
        DbUsageSink(budget_counters).check_budget(CallContext(workspace_id="ws_1", run_id="run_1"), "hypothesis_generation")


# ------------------------------------------------------------------------------ replans
def _approved(plan_hash: str, payload: dict) -> str:
    with session_scope() as s:
        s.add(Approval(id="apr_x", workspace_id="ws_1", run_id="run_1", action="publish_dashboard", risk_tier="high",
                       payload=payload, payload_hash=stable_hash(payload), plan_hash=plan_hash, policy_version=1,
                       requested_by="usr_1", decided_by="usr_2", status="approved",
                       expires_at=utcnow() + timedelta(days=1), evidence={}))
    return "apr_x"


INVESTIGATE = (("context", "COMPLETED", []), ("metadata", "COMPLETED", []), ("profile", "COMPLETED", ["metadata"]),
               ("quality", "COMPLETED", ["profile"], 1, {"optional": True}),
               ("hypotheses", "COMPLETED", ["context", "profile", "quality"]), ("test:H-1", "COMPLETED", ["hypotheses"]),
               ("insights", "COMPLETED", ["hypotheses", "test:*"]), ("verify", "COMPLETED", ["insights"]),
               ("dataset", "NEW", ["verify"]), ("publish", "NEW", ["dataset"], 0, {"optional": True}))


def test_replan_invalidates_approval(sqlite_db):
    """An approval bound to the old plan hash is invalid after a replan, and cannot execute."""
    from analystos.autonomy.replan import replan
    from analystos.governance.approvals import verify_for_execution

    _run(*INVESTIGATE)
    with session_scope() as s:
        old_hash = engine.run_hash(s.get(AnalysisRun, "run_1"))
        s.get(AnalysisRun, "run_1").plan_hash = old_hash
    payload = {"bundle": "b1"}
    _approved(old_hash, payload)
    out = replan("run_1", "empty_result", "the breach-rate test returned no rows", actor="user:usr_1")
    assert out["plan_version"] == 2 and out["approvals_invalidated"] == 1 and out["reason_code"] == "empty_result"
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        assert run.plan_hash != old_hash
        assert s.get(Approval, "apr_x").status == "invalidated"
        with pytest.raises(ApprovalRequired):
            verify_for_execution(s, "apr_x", payload=payload, plan_hash=run.plan_hash)
    ev = _events("run.replanned")[-1]
    assert ev["reason_code"] == "empty_result" and ev["trigger"] == "redirect" and ev["approvals_invalidated"] == 1


def test_an_approval_of_the_new_plan_is_not_satisfied_by_the_old_hash(sqlite_db, monkeypatch):
    """Even an approval that escaped invalidation cannot execute against the replanned run."""
    from analystos.autonomy.replan import replan
    from analystos.governance import approvals
    from analystos.governance.approvals import verify_for_execution

    monkeypatch.setattr(approvals, "utcnow", lambda: utcnow().replace(tzinfo=None))  # SQLite returns naive times

    _run(*INVESTIGATE)
    replan("run_1", "user_redirect", "only the Network group", actor="user:usr_1")
    payload = {"bundle": "b1"}
    _approved("h1", payload)  # approved against the superseded hash
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        with pytest.raises(ApprovalRequired, match="plan changed"):
            verify_for_execution(s, "apr_x", payload=payload, plan_hash=run.plan_hash)


def test_replan_keeps_prior_steps_and_evidence_immutable(sqlite_db):
    from analystos.autonomy.models import RunPlanVersion
    from analystos.autonomy.replan import plan_version, replan

    _run(*INVESTIGATE)
    with session_scope() as s:
        s.add(Insight(id="ins_1", workspace_id="ws_1", run_id="run_1", code="F1", title="F1", finding="F1",
                      status="verified", verified=True))
    replan("run_1", "rejected_hypothesis", "H-1 is not plausible", actor="user:usr_1")
    with session_scope() as s:
        snap = s.scalar(select(RunPlanVersion).where(RunPlanVersion.run_id == "run_1", RunPlanVersion.version == 1))
        assert snap.reason_code == "rejected_hypothesis" and snap.plan_hash == "h1" and snap.actor == "user:usr_1"
        hyp = next(t for t in snap.tasks if t["key"] == "hypotheses")
        assert hyp["status"] == "COMPLETED" and hyp["output"] == {"note": "hypotheses output"}
        assert any(t["key"] == "test:H-1" for t in snap.tasks)
        assert s.get(Insight, "ins_1").status == "superseded"  # marked, never deleted
        old = plan_version(s, s.get(AnalysisRun, "run_1"), 1)
        assert old["current"] is False and old["superseded_by"]["reason_code"] == "rejected_hypothesis"
        assert plan_version(s, s.get(AnalysisRun, "run_1"), None)["version"] == 2
    assert _task("hypotheses").status == "NEW" and _task("test:H-1") is None
    assert _task("context").status == "COMPLETED" and _task("context").plan_version == 2  # reused


def test_reason_codes_map_to_playbook_boundaries(sqlite_db):
    from analystos.autonomy.replan import replan

    _run(*INVESTIGATE)
    out = replan("run_1", "quality_failure", "the quality step found duplicate keys", actor="engine")
    assert out["tasks_reset"] >= 4  # hypotheses onward (investigate.v1 declares no quality boundary: redirect)
    assert _task("quality").status == "COMPLETED"


def test_budget_downgrade_drops_optional_work_and_redoes_nothing(sqlite_db):
    from analystos.autonomy.replan import replan

    _run(*INVESTIGATE)
    out = replan("run_1", "budget_downgrade", "half the budget is gone", actor="engine")
    assert out["tasks_reset"] == 0 and out["tasks_dropped"] == 1 and out["plan_version"] == 2
    assert _task("publish").status == "SKIPPED" and _task("publish").error == "dropped by replan: budget_downgrade"
    assert _task("dataset").status == "NEW" and _task("hypotheses").status == "COMPLETED"


def test_replan_budget_and_reason_codes_are_enforced(sqlite_db):
    from analystos.autonomy.replan import replan
    from analystos.core.errors import InvalidInput

    _run(*INVESTIGATE, limits={"max_replans": 1})
    with pytest.raises(InvalidInput, match="reason_code"):
        replan("run_1", "because", "x", actor="engine")
    replan("run_1", "user_redirect", "only Network", actor="engine")
    with pytest.raises(BudgetExceeded, match="replan budget"):
        replan("run_1", "user_redirect", "and only P1", actor="engine")
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").plan_version == 2  # the refused replan changed nothing


def test_template_specific_boundary_resets_only_its_steps(sqlite_db):
    from analystos.autonomy.replan import replan

    _run(("metadata", "COMPLETED", []), ("profile", "COMPLETED", ["metadata"]),
         ("quality", "COMPLETED", ["profile"]), ("finalize", "NEW", ["quality"]), playbook="playbook.dq_review")
    out = replan("run_1", "quality_failure", "rules changed", actor="engine")
    assert out["tasks_reset"] == 2 and _task("quality").status == "NEW" and _task("profile").status == "COMPLETED"


def test_a_plan_gate_waiting_at_replan_asks_again_and_never_opens(sqlite_db, monkeypatch):
    """Replanning while the plan waits for approval: the old approval is invalidated and the gate asks
    for a new one bound to the new hash; it is not skipped (which would run the plan unapproved)."""
    from analystos.autonomy.replan import replan
    from analystos.governance import approvals

    monkeypatch.setattr(approvals, "utcnow", lambda: utcnow().replace(tzinfo=None))  # SQLite returns naive times
    _run(("plan_approval", "WAITING_USER", [], 0, {"approval_id": "apr_plan"}), ("context", "NEW", ["plan_approval"]),
         status="WAITING_USER")
    with session_scope() as s:
        s.add(Approval(id="apr_plan", workspace_id="ws_1", run_id="run_1", action="execute_plan", risk_tier="medium",
                       payload={}, payload_hash="x", plan_hash="h1", policy_version=1, requested_by="usr_1",
                       status="pending", expires_at=(utcnow() + timedelta(days=1)).replace(tzinfo=None), evidence={}))
    replan("run_1", "user_redirect", "only P1 incidents", actor="user:usr_1")
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        gate = s.scalar(select(RunTask).where(RunTask.key == "plan_approval"))
        new = s.get(Approval, gate.input["approval_id"])
        assert s.get(Approval, "apr_plan").status == "invalidated"
        assert new.id != "apr_plan" and new.status == "pending" and new.plan_hash == run.plan_hash
    state = engine.get_state("run_1")
    assert state.get("waiting_user") == ["plan_approval"] and _task("context").status == "NEW"

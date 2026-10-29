"""Playbook simulator (S06-T6, spec §3.7): dry-run a playbook against a fixture workspace.

It answers "what would this playbook do here?" before anyone can enable it: the plan the engine would
materialize (`Playbook.build_plan`), which steps `when` / `skip_when` drop for the fixture's run, which
agents the workspace would refuse (enablement and certification, asked the way `bind_run` asks), then a
walk of the DAG in dependency waves with every agent replaced by a deterministic mock. On the way it
counts the run budget the way `autonomy/budget.py` does (steps are tasks claimed once, a re-run after a
replan is a retry; steps, retries and replans are allowances, cost stops the run once reached), stops at
every approval gate with the fixture's decision, and applies the fixture's replans with the engine's
reset and approval-invalidation rules.

It never executes: no gateway, no model router, no dispatch, no side effect and nothing persisted. Mock
cost is each agent's declared `budget.usd` (an upper bound per task), so a cost limit is checked against
what the agents may spend, not what a model happened to charge.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from analystos.capabilities.playbook import Playbook, evaluate
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.goals import REPLAN_REASONS, RunLimits
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import stable_hash

FIXTURES_FILE = Path(__file__).with_name("simulation_fixtures.yaml")
MAX_TASKS = 400  # expansion counts cannot make the walk unbounded
MOCKS = {"agents": "deterministic: each step completes with a fixture output; no model call (model rungs are mocked)",
         "queries": "none: no statement reaches the gateway", "side_effects": "none: a side effect is reported, never performed",
         "approvals": "decided by the fixture; nothing is requested in the approvals inbox"}
Outcome = Literal["completed", "completed_with_limitations", "rejected", "failed", "refused"]


class ReplanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str
    after: str | None = None  # the step after which it happens; default: the first step declaring a boundary for it

    @field_validator("reason_code")
    @classmethod
    def _code(cls, v: str) -> str:
        if v not in REPLAN_REASONS:
            raise ValueError(f"reason_code must be one of {', '.join(REPLAN_REASONS)}")
        return v


Decision = Literal["approve", "reject"]


class SimulationFixture(BaseModel):
    """The run a playbook would get. A fixture is data: it can only describe a run, never add a tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="custom", min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    objective: str = Field(default="Simulated objective", max_length=500)
    autonomy_level: int = Field(default=2, ge=0, le=5)
    origin: dict[str, Any] = Field(default_factory=lambda: {"type": "api"})
    limits: dict[str, Any] = Field(default_factory=dict)  # tightened with the playbook's default_limits
    approvals: Decision | dict[str, Decision] = "approve"  # one decision for every gate, or per gate step key
    fail: list[str] = Field(default_factory=list, max_length=50)  # step keys (or `prefix:*`) whose mock fails
    expansions: dict[str, int] = Field(default_factory=dict)  # expansion prefix -> tasks it adds (default 1)
    replans: list[ReplanEvent] = Field(default_factory=list, max_length=5)
    enable: list[str] = Field(default_factory=list, max_length=50)  # capability overrides layered on the workspace's
    disable: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("limits")
    @classmethod
    def _limits(cls, v: dict[str, Any]) -> dict[str, Any]:
        return RunLimits.model_validate(v).as_dict()

    @field_validator("expansions")
    @classmethod
    def _expansions(cls, v: dict[str, int]) -> dict[str, int]:
        if any(not isinstance(n, int) or n < 0 or n > 20 for n in v.values()):
            raise ValueError("an expansion adds 0 to 20 tasks")
        return v

    def decision(self, key: str) -> Decision:
        return self.approvals if isinstance(self.approvals, str) else self.approvals.get(key, "approve")

    def fails(self, key: str) -> bool:
        return key in self.fail or any(f.endswith("*") and key.startswith(f[:-1]) for f in self.fail)


_lock = threading.Lock()
_fixtures: dict[str, SimulationFixture] | None = None


def fixtures() -> dict[str, SimulationFixture]:
    """The built-in fixture workspaces, by name."""
    global _fixtures
    with _lock:
        if _fixtures is None:
            raw = yaml.safe_load(FIXTURES_FILE.read_text(encoding="utf-8")) or []
            _fixtures = {f.name: f for f in (SimulationFixture.model_validate(r) for r in raw)}
        return dict(_fixtures)


def fixture(ref: str | dict[str, Any] | SimulationFixture | None) -> SimulationFixture:
    """A built-in fixture by name, or a custom one (`{name, autonomy_level, ...}`; `base` starts from a built-in)."""
    if isinstance(ref, SimulationFixture):
        return ref
    if ref is None or isinstance(ref, str):
        name = ref or "supervised"
        found = fixtures().get(name)
        if found is None:
            raise NotFound(f"no simulation fixture {name}; one of {', '.join(fixtures())}")
        return found
    if not isinstance(ref, dict):
        raise InvalidInput("a fixture is a built-in name or an object")
    body = dict(ref)
    base = body.pop("base", None)
    start = fixture(base).model_dump() if base else {}
    try:
        return SimulationFixture.model_validate({**start, **body})
    except ValidationError as exc:
        raise InvalidInput("fixture does not validate: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None


# ------------------------------------------------------------------------------------ what the workspace allows
class Gatekeeper:
    """Whether a capability may run in the fixture workspace, asked the way `capabilities/binding.bind_run` asks."""

    def __init__(self, manifest: CapabilityManifest, *, session: Any = None, workspace_id: str | None = None,
                 fx: SimulationFixture, workspace_source: bool) -> None:
        from analystos.capabilities import enablement, registry

        replace: dict[str, CapabilityManifest] = {manifest.id: manifest}
        self.agent_problems: dict[str, str] = {}
        explicit: dict[str, bool] = {}
        if session is not None and workspace_id:
            from analystos.capabilities.agent_forms import grant_problems, playbook_uses, workspace_agents

            for m in workspace_agents(session, workspace_id, playbook_uses(manifest) - set(replace)).values():
                replace[m.id] = m
                if problems := grant_problems(session, workspace_id, m):
                    self.agent_problems[m.id] = f"agent {m.ref} exceeds this workspace's grants: {problems[0]}"
            explicit = enablement.overrides(session, workspace_id)
        if workspace_source:  # a published workspace playbook is enabled by its publication (bind_run)
            explicit.setdefault(manifest.id, True)
        explicit.update({i: True for i in fx.enable})
        explicit.update({i: False for i in fx.disable})
        self.snapshot = registry.pinned(registry.current(), replace.values())
        self.explicit = explicit
        self.autonomous = fx.autonomy_level >= 3 or (fx.origin or {}).get("type") in enablement.AUTONOMOUS_ORIGINS

    def problem(self, cap_id: str | None) -> str | None:
        from analystos.capabilities import enablement

        m = self.snapshot.manifests.get(cap_id or "")
        if m is None:
            return f"capability {cap_id} is not registered"
        return self.agent_problems.get(m.id) or enablement.usable(m, self.snapshot, self.explicit,
                                                                  autonomous_run=self.autonomous)

    def budget(self, cap_id: str) -> dict[str, Any]:
        m = self.snapshot.manifests.get(cap_id)
        return dict((m.spec.get("budget") if m is not None else None) or {})

    def purposes(self, cap_id: str) -> list[str]:
        spec = (m.spec if (m := self.snapshot.manifests.get(cap_id)) is not None else None) or {}
        return list(dict.fromkeys(([spec["model_purpose"]] if spec.get("model_purpose") else [])
                                  + list(spec.get("model_purposes") or [])))

    def refs(self, ids: set[str]) -> list[str]:
        return sorted(self.snapshot.manifests[i].ref for i in ids if i in self.snapshot.manifests)


# ------------------------------------------------------------------------------------ the walk
@dataclass
class _Task:
    key: str
    use: str
    behaviour: str | None
    type: str
    title: str
    depends_on: list[str]
    optional: bool
    status: str = "NEW"  # NEW | COMPLETED | SKIPPED | FAILED
    detail: str = ""
    attempts: int = 0
    approval: str | None = None  # a side effect: the approval its gate handed it
    sticky: bool = False  # skipped before it ran (skip_when, a refused optional agent): a replan does not revive it
    meta: dict[str, Any] = field(default_factory=dict)


def _satisfied(dep: str, tasks: dict[str, _Task], waiting: str) -> bool:
    """`runtime/plan.dep_satisfied` for the mock tasks."""
    if dep.endswith(":*"):
        prefix = dep[:-1]
        return all(t.status in ("COMPLETED", "SKIPPED", "FAILED") for k, t in tasks.items()
                   if k.startswith(prefix) and k != waiting and waiting not in t.depends_on)
    t = tasks.get(dep)
    return t is not None and (t.status in ("COMPLETED", "SKIPPED") or (t.status == "FAILED" and t.optional))


def static_warnings(pb: Playbook) -> list[str]:
    """Things the schema allows but a reviewer should see before signing."""
    out: list[str] = []
    gated = {s.approval_for for s in pb.body.steps if s.approval_for}
    for s in pb.body.steps:
        if s.type == "side_effect" and s.key not in gated:
            out.append(f"side effect {s.key} has no approval_gate naming it (approval_for): it is always skipped")
        if s.type == "side_effect" and not s.optional:
            out.append(f"side effect {s.key} is required: a rejected or invalidated approval fails the whole run")
    if any(s.type == "side_effect" for s in pb.body.steps) and pb.manifest.side_effect != "write_external":
        out.append(f"the playbook declares side_effect {pb.manifest.side_effect} but has a side_effect step "
                   "(declare write_external)")
    if not pb.body.default_limits:
        out.append("no default_limits: a run is bounded by the goal's limits and policy caps only")
    return out


def simulate(manifest: CapabilityManifest | dict[str, Any], fx: str | dict[str, Any] | SimulationFixture | None = None, *,
             session: Any = None, workspace_id: str | None = None, workspace_source: bool = False) -> dict[str, Any]:
    """The report of one dry run. `workspace_source` simulates a workspace playbook as it would run once
    published (its publication enables and certifies the composition; its agents are still checked one by one)."""
    from analystos.runtime.plan import plan_hash

    if isinstance(manifest, dict):
        manifest = CapabilityManifest.model_validate({**manifest, "source": manifest.get("source") or "simulation"})
    if workspace_source:
        manifest = manifest.model_copy(update={"certification": manifest.certification.model_copy(
            update={"status": "certified", "evidence": "simulated as published in this workspace"})})
    fixture_ = fixture(fx)
    pb = Playbook(manifest)
    gate = Gatekeeper(manifest, session=session, workspace_id=workspace_id, fx=fixture_, workspace_source=workspace_source)
    limits = RunLimits.model_validate(pb.body.default_limits or {}).tightened(fixture_.limits or None)
    plan = pb.build_plan(fixture_.objective, autonomy_level=fixture_.autonomy_level)
    ns = {"run": {"autonomy_level": fixture_.autonomy_level, "origin": fixture_.origin or {}}}
    report: dict[str, Any] = {
        "playbook": {"id": manifest.id, "version": manifest.version, "ref": manifest.ref,
                     "content_hash": stable_hash(manifest.model_dump(mode="json", exclude={"source"}))},
        "fixture": fixture_.model_dump(mode="json"), "mocks": MOCKS, "warnings": static_warnings(pb),
        "limits": limits.as_dict(), "plan": plan, "timeline": [], "waves": [], "approvals": [], "replans": [],
    }
    timeline: list[dict[str, Any]] = report["timeline"]
    used = {"steps": 0, "retries": 0, "replans": 0, "cost_usd": 0.0, "llm_calls": 0, "queries": 0}
    plan_version = 1
    ids: set[str] = {manifest.id}
    tasks: dict[str, _Task] = {}

    def note(task: _Task | None, status: str, detail: str, **extra: Any) -> None:
        timeline.append({"seq": len(timeline) + 1, "key": task.key if task else "_run", "agent": task.use if task else None,
                         "behaviour": task.behaviour if task else None, "type": task.type if task else "run",
                         "status": status, "detail": detail, "plan_version": plan_version,
                         "budget": {k: round(v, 6) if isinstance(v, float) else v for k, v in used.items()}, **extra})

    def finish(outcome: Outcome, reason: str | None = None, stopped_by: str | None = None) -> dict[str, Any]:
        for t in tasks.values():
            if t.status == "NEW":
                note(t, "not_reached", reason or "the run ended before this step")
        report["outcome"], report["reason"], report["plan_version"] = outcome, reason, plan_version
        remaining = {k: max(0, round(getattr(limits, k) - used[c], 6)) for k, c in
                     (("max_steps", "steps"), ("max_retries", "retries"), ("max_replans", "replans"),
                      ("max_cost_usd", "cost_usd")) if getattr(limits, k) is not None}
        report["budget"] = {"limits": limits.as_dict(), "used": {k: round(v, 6) if isinstance(v, float) else v
                                                                  for k, v in used.items()},
                            "remaining": remaining, "stopped_by": stopped_by}
        report["refs"] = gate.refs(ids)
        report["plan_hash"] = plan_hash(plan, constraints={}, scope_hash="simulation", plan_version=plan_version,
                                        capabilities=report["refs"])
        report["approval_gates"] = [{"key": s.key, "payload": s.payload, "approval_for": s.approval_for, "type": s.type}
                                    for s in pb.body.steps if s.key in pb.approval_gates()]
        report["digest"] = stable_hash({k: report[k] for k in ("outcome", "reason", "budget", "approvals", "replans")}
                                       | {"timeline": [{k: e[k] for k in ("key", "status", "plan_version")} for e in timeline]})
        return report

    # Binding: the playbook itself, then every step's agents (bind_run): a refused optional step is skipped.
    if problem := gate.problem(manifest.id):
        note(None, "refused", problem)
        return finish("refused", problem)
    for step in plan["steps"]:
        spec = pb.step(step["key"])
        t = _Task(key=step["key"], use=spec.use or "", behaviour=spec.behaviour, type=spec.type, title=step["title"],
                  depends_on=list(step["depends_on"]), optional=bool(step["optional"]))
        tasks[t.key] = t
        uses = [spec.use] + [e.use for e in spec.expands]
        reasons = [r for u in uses if (r := gate.problem(u))]
        if spec.skip_when and evaluate(spec.skip_when.if_, ns):
            t.status, t.detail, t.sticky = "SKIPPED", spec.skip_when.reason, True
            note(t, "skipped", spec.skip_when.reason)
        elif reasons and t.optional:
            t.status, t.detail, t.sticky = "SKIPPED", reasons[0], True
            note(t, "skipped", f"optional step not bound here: {reasons[0]}")
        elif reasons:
            note(t, "refused", reasons[0])
            return finish("refused", f"step {t.key}: {reasons[0]}")
        else:
            from analystos.capabilities import enablement

            for u in uses:
                ids.update(i for i in enablement.closure(gate.snapshot, u)
                           if gate.snapshot.manifests[i].kind != "Playbook")
    excluded = [s.key for s in pb.body.steps if s.key not in tasks]
    for key in excluded:
        timeline.append({"seq": len(timeline) + 1, "key": key, "agent": pb.steps[key].use, "behaviour": pb.steps[key].behaviour,
                         "type": pb.steps[key].type, "status": "excluded",
                         "detail": f"not in the plan: `when: {pb.steps[key].when}` is false for this run",
                         "plan_version": plan_version, "budget": dict(used)})

    pending_replans = list(fixture_.replans)
    approvals: list[dict[str, Any]] = report["approvals"]

    def request(t: _Task, payload: str) -> dict[str, Any]:
        decision = fixture_.decision(t.key)
        a = {"id": f"sim_apr_{len(approvals) + 1}", "gate": t.key, "payload": payload, "for": t.meta.get("approval_for"),
             "plan_version": plan_version, "decision": decision, "status": "approved" if decision == "approve" else "rejected",
             "binds": "plan hash" if payload == "plan" else "payload hash + plan hash"}
        approvals.append(a)
        return a

    def replan(ev: ReplanEvent, after: str) -> str | None:
        nonlocal plan_version
        if limits.max_replans is not None and used["replans"] >= limits.max_replans:
            report["replans"].append({"after": after, "reason_code": ev.reason_code, "status": "refused",
                                      "detail": "max_replans is used up (429)"})
            note(None, "replan_refused", f"{ev.reason_code} after {after}: max_replans is used up")
            return None
        trigger = pb.trigger_for(ev.reason_code)
        used["replans"] += 1
        plan_version += 1
        reset_keys = pb.reset_keys(trigger) if trigger else set()
        prefixes = pb.removed_prefixes(trigger) if trigger else ()
        dropped, reset, removed = [], [], []
        for key, t in list(tasks.items()):
            if trigger is None and t.status == "NEW" and t.optional:
                t.status, t.detail = "SKIPPED", f"dropped by replan: {ev.reason_code}"
                dropped.append(key)
            elif prefixes and key.startswith(prefixes):
                del tasks[key]
                removed.append(key)
            elif key in reset_keys and not t.sticky:
                t.status, t.detail, t.approval = "NEW", "", None
                reset.append(key)
        invalidated = [a["id"] for a in approvals if a["status"] in ("approved", "pending")]
        for a in approvals:
            if a["id"] in invalidated:
                a["status"] = "invalidated"
        report["replans"].append({"after": after, "reason_code": ev.reason_code, "trigger": trigger, "status": "applied",
                                  "plan_version": plan_version, "reset": reset, "removed": removed, "dropped": dropped,
                                  "approvals_invalidated": invalidated})
        note(None, "replanned", f"{ev.reason_code} after {after}: plan v{plan_version}; reset {', '.join(reset) or 'nothing'}"
             + (f"; approvals invalidated: {', '.join(invalidated)}" if invalidated else ""))
        return trigger

    def due_replan(t: _Task) -> ReplanEvent | None:
        for ev in pending_replans:
            trigger = pb.trigger_for(ev.reason_code)
            spec = pb.step(t.key)
            declares = spec is not None and any(b.trigger == trigger for b in spec.replan_boundary)
            if ev.after == t.key or (ev.after is None and (declares or trigger is None)):
                pending_replans.remove(ev)
                return ev
        return None

    wave = 0
    while True:
        ready = [t for t in tasks.values() if t.status == "NEW" and all(_satisfied(d, tasks, t.key) for d in t.depends_on)]
        if not ready:
            break
        wave += 1
        report["waves"].append([t.key for t in ready])
        for t in ready:
            if t.status != "NEW" or t.key not in tasks:
                continue  # a replan earlier in this wave reset or removed it
            if t.type == "side_effect" and not t.approval:
                t.status, t.detail = "SKIPPED", "its approval gate produced no approved approval"
                note(t, "skipped", t.detail, wave=wave)
                continue
            rerun = t.attempts > 0
            if rerun and limits.max_retries is not None and used["retries"] >= limits.max_retries:
                return finish("completed_with_limitations", f"max_retries reached before {t.key}", "max_retries")
            if not rerun and limits.max_steps is not None and used["steps"] >= limits.max_steps:
                return finish("completed_with_limitations", f"max_steps reached before {t.key}", "max_steps")
            if limits.max_cost_usd is not None and used["cost_usd"] >= limits.max_cost_usd:
                return finish("completed_with_limitations", f"max_cost_usd reached before {t.key}", "max_cost_usd")
            t.attempts += 1
            used["retries" if rerun else "steps"] += 1
            spec = pb.step(t.key)
            budget = gate.budget(t.use)
            used["cost_usd"] += float(budget.get("usd") or 0.0)
            used["llm_calls"] += int(budget.get("llm_calls") or 0)
            used["queries"] += int(budget.get("queries") or 0)
            if t.type == "approval_gate" and spec is not None and spec.payload == "plan":
                a = request(t, "plan")
                if a["status"] == "rejected":
                    t.status = "COMPLETED"
                    note(t, "approval_rejected", "the approver rejects the plan: the run stops", wave=wave, approval=a["id"])
                    return finish("rejected", f"plan approval rejected at {t.key}")
                t.status = "COMPLETED"
                note(t, "approval_granted", "waits for plan approval (WAITING_USER), then the approver approves the plan hash",
                     wave=wave, approval=a["id"])
            elif t.type == "approval_gate" and spec is not None:
                t.meta["approval_for"] = spec.approval_for
                a = request(t, spec.payload or "")
                target = tasks.get(spec.approval_for or "")
                if a["status"] == "approved" and target is not None:
                    target.approval = a["id"]
                t.status = "COMPLETED"
                note(t, "approval_granted" if a["status"] == "approved" else "approval_rejected",
                     f"requests approval of the {spec.payload} for {spec.approval_for}; the approver "
                     f"{'approves' if a['status'] == 'approved' else 'rejects'} it", wave=wave, approval=a["id"])
            elif t.type == "side_effect":
                a = next((x for x in approvals if x["id"] == t.approval), None)
                if a is None or a["status"] != "approved":
                    t.status = "FAILED"
                    note(t, "failed", "verify_for_execution refuses: its approval was invalidated", wave=wave)
                    if not t.optional:
                        return finish("failed", f"required side effect {t.key} has no valid approval")
                    continue
                a["status"] = "executed"
                t.status = "COMPLETED"
                note(t, "side_effect", f"would verify approval {a['id']} (verify_for_execution) and act; "
                     "not performed in a simulation", wave=wave, approval=a["id"])
            elif fixture_.fails(t.key):
                t.status = "FAILED"
                if not t.optional:
                    note(t, "failed", "mock failure (fixture): a required step failed, the run fails", wave=wave)
                    return finish("failed", f"required step {t.key} failed")
                note(t, "failed", "mock failure (fixture): optional, downstream continues", wave=wave)
            else:
                t.status = "COMPLETED"
                added: list[str] = []
                for e in spec.expands if spec is not None else []:
                    for i in range(fixture_.expansions.get(e.prefix, 1)):
                        key = f"{e.prefix}sim_{i + 1}"
                        if key in tasks or len(tasks) >= MAX_TASKS:
                            continue
                        # expanded tasks are optional, as the behaviours add them (agents/investigator.py)
                        tasks[key] = _Task(key=key, use=e.use, behaviour=e.behaviour, type="agent",
                                           title=f"{t.title} ({e.prefix}{i + 1})", depends_on=[t.key], optional=True)
                        added.append(key)
                purposes = [p for p in (gate.snapshot.manifests[t.use].spec.get("model_purposes") or [])] \
                    if t.use in gate.snapshot.manifests else []
                note(t, "completed", "mock output" + (f"; expands {', '.join(added)}" if added else "")
                     + (f"; model purposes mocked: {', '.join(purposes)}" if purposes else ""), wave=wave)
            if t.status in ("COMPLETED", "FAILED") and (ev := due_replan(t)) is not None:
                replan(ev, t.key)
    if pending_replans:
        report["warnings"].append("replans that never happened (no step matched): "
                                  + ", ".join(f"{e.reason_code} after {e.after or 'a boundary step'}" for e in pending_replans))
    left = [t.key for t in tasks.values() if t.status == "NEW"]
    if left:
        return finish("failed", f"steps whose dependencies can never complete: {', '.join(left)}")
    return finish("completed")


def simulate_all(manifest: CapabilityManifest | dict[str, Any], names: list[str | dict[str, Any]] | None = None,
                 **kw: Any) -> list[dict[str, Any]]:
    return [simulate(manifest, n, **kw) for n in (names or list(fixtures()))]


PASSING: tuple[str, ...] = ("completed", "completed_with_limitations", "rejected")


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """What a definition's test evidence keeps of a report (the full report is returned to the caller)."""
    return {"fixture": report["fixture"]["name"], "outcome": report["outcome"], "reason": report.get("reason"),
            "digest": report["digest"], "plan_hash": report.get("plan_hash"), "budget": report.get("budget"),
            "approvals": len(report.get("approvals") or []), "warnings": report.get("warnings") or []}

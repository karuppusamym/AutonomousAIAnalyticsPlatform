"""Playbooks (spec v3 §3.3, P4-X02): the run DAG as versioned data, interpreted by the engine.

A playbook is the `spec` of a `kind: Playbook` capability. What used to be magic keys in the engine
are step types and step fields:

  type: approval_gate   waits for a hash-bound approval. `payload: plan` -> the engine requests it
                        when the plan materializes and this step waits (WAITING_USER);
                        `payload: <other>` -> the step's behaviour requests it and hands it to the
                        `side_effect` step named by `approval_for`.
  type: side_effect     acts outside the platform: runs only with an approved approval in its input
                        (skipped when its gate produced none); the engine re-checks cancel right
                        before it, and the behaviour calls `verify_for_execution`.
  replan_boundary       what a redirect / a rejected finding resets (this step and/or downstream).
  optional, after       failure tolerance and dependencies (`<prefix>*` waits for every expanded task).
  expands               dynamic tasks the step's behaviour may add (`test:<code>`, `followups:<n>`).
  when                  the step exists in the plan only when the condition holds (plan inputs).
  gates_roots           while present, steps without dependencies wait for this step.
  skip_when             the step is materialized SKIPPED when the condition holds for the run.
  params                data the step's behaviour reads (e.g. `prefer_methods` for the investigator);
                        scalars and lists of scalars only, never code.

A template playbook (S06 §3.8) also declares `inputs`, `outputs`, `default_limits` (contracts.goals.
RunLimits) and `router` (the intents and tiers the complexity router maps to it). `replan_boundary`
triggers are `redirect`, `finding_rejected` or a replan reason code (contracts.goals.REPLAN_REASONS);
a reason code the playbook does not declare falls back to `REASON_TRIGGER`.

The plan dict a playbook builds is exactly the v1 shape (key, agent, title, depends_on, optional),
so `investigate.v1` reproduces the v1 plan hash for the same inputs.
"""
from __future__ import annotations

import re
import threading
from collections import deque
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from analystos.contracts.capability import CapabilityManifest

DEFAULT_PLAYBOOK = "playbook.investigate"
_COND = re.compile(r"^\s*([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)*)\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$")


def parse_condition(expr: str) -> tuple[str, str, Any]:
    """`run.origin.publish == 'skip'` -> (path, op, literal). Only a single comparison is allowed:
    playbooks are data, and a condition language that can call code would make them code."""
    m = _COND.match(expr or "")
    if not m:
        raise ValueError(f"condition {expr!r} must be '<path> <op> <literal>'")
    literal = yaml.safe_load(m.group(3))
    if isinstance(literal, (dict, list)):
        raise ValueError(f"condition {expr!r}: the literal must be a scalar")
    return m.group(1), m.group(2), literal


def evaluate(expr: str, namespace: dict[str, Any]) -> bool:
    path, op, literal = parse_condition(expr)
    value: Any = namespace
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if op == "==":
        return value == literal
    if op == "!=":
        return value != literal
    if value is None or literal is None:
        return False
    try:
        return {"<": value < literal, "<=": value <= literal, ">": value > literal, ">=": value >= literal}[op]
    except TypeError:
        return False


class Expansion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str
    use: str
    behaviour: str | None = None

    @model_validator(mode="after")
    def _prefix(self) -> Expansion:
        if not re.fullmatch(r"[a-z_]+:", self.prefix):
            raise ValueError(f"expansion prefix {self.prefix!r} must look like 'name:'")
        return self


Trigger = Literal["redirect", "finding_rejected", "missing_dataset", "empty_result", "rejected_hypothesis",
                  "quality_failure", "user_redirect", "budget_downgrade"]
# A replan reason the playbook declares no boundary for resets what this boundary resets. budget_downgrade
# resets nothing: it drops the optional steps that have not run (runtime/engine.apply_replan).
REASON_TRIGGER: dict[str, str | None] = {"user_redirect": "redirect", "rejected_hypothesis": "redirect",
                                         "missing_dataset": "redirect", "empty_result": "redirect",
                                         "quality_failure": "redirect", "budget_downgrade": None}


class ReplanBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: Trigger
    reset: Literal["self_and_downstream", "downstream"]


class SkipWhen(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    if_: str = Field(alias="if")
    reason: str


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    use: str | None = None  # agent capability id
    behaviour: str | None = None  # named behaviour of the agent; default = the agent's entry
    type: Literal["agent", "approval_gate", "side_effect"] = "agent"
    after: list[str] = Field(default_factory=list)
    optional: bool = False
    when: str | None = None
    skip_when: SkipWhen | None = None
    payload: str | None = None  # approval_gate: what the approval binds ("plan" = engine-requested)
    approval_for: str | None = None  # approval_gate: the side_effect step that waits for this approval
    gates_roots: bool = False
    replan_boundary: list[ReplanBoundary] = Field(default_factory=list)
    expands: list[Expansion] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> Step:
        for k, v in self.params.items():
            items = v if isinstance(v, list) else [v]
            if not all(isinstance(i, (str, int, float, bool)) or i is None for i in items):
                raise ValueError(f"step {self.key}: param {k} must be a scalar or a list of scalars")
        if not re.fullmatch(r"[a-z_]+", self.key):
            raise ValueError(f"step key {self.key!r} must be lower snake case")
        if self.use is None:
            raise ValueError(f"step {self.key}: `use` names the agent that runs it")
        if self.type == "approval_gate" and not self.payload:
            raise ValueError(f"approval_gate step {self.key} needs `payload`")
        if self.type == "approval_gate" and self.payload != "plan" and not self.approval_for:
            raise ValueError(f"approval_gate step {self.key} with payload {self.payload} needs `approval_for`")
        for cond in (self.when, self.skip_when.if_ if self.skip_when else None):
            if cond:
                parse_condition(cond)
        return self

    @property
    def waits_for_approval(self) -> bool:
        return self.type == "side_effect" or (self.type == "approval_gate" and self.payload == "plan")


class PlaybookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["question", "metric", "dimension", "period", "tables", "segment", "event", "comparison"]
    required: bool = False
    description: str = ""


class RouterHints(BaseModel):
    """How the complexity router (autonomy/complexity.py) picks this playbook: a question matching one
    of `intents` (case-insensitive regexes) at one of `tiers`."""

    model_config = ConfigDict(extra="forbid")

    intents: list[str] = Field(default_factory=list)
    tiers: list[Literal["low", "medium", "complex"]] = Field(default_factory=list)
    priority: int = 0  # higher wins when several templates match

    @model_validator(mode="after")
    def _regexes(self) -> RouterHints:
        for pattern in self.intents:
            re.compile(pattern)
        return self


class PlaybookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framing: bool = True  # the supervisor frames questions/audience/focus before the plan materializes
    steps: list[Step]
    inputs: list[PlaybookInput] = Field(default_factory=list)
    outputs: list[Literal["investigation", "dashboard_draft", "report", "kpi_review", "variance_report",
                          "funnel_report", "cohort_report", "dq_report", "dashboard"]] = Field(default_factory=list)
    default_limits: dict[str, Any] = Field(default_factory=dict)  # contracts.goals.RunLimits
    router: RouterHints | None = None

    @model_validator(mode="after")
    def _graph(self) -> PlaybookBody:
        keys = [s.key for s in self.steps]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate step keys")
        patterns = {e.prefix + "*" for s in self.steps for e in s.expands}
        seen: set[str] = set()
        for s in self.steps:
            for dep in s.after:
                if dep not in keys and dep not in patterns:
                    raise ValueError(f"step {s.key} depends on unknown step {dep}")
                if dep in keys and dep not in seen:
                    raise ValueError(f"step {s.key} depends on {dep}, which is declared later (steps are topologically ordered)")
            if s.approval_for:
                target = next((t for t in self.steps if t.key == s.approval_for), None)
                if target is None or target.type != "side_effect":
                    raise ValueError(f"step {s.key}: approval_for must name a side_effect step")
            seen.add(s.key)
        if self.default_limits:
            from analystos.contracts.goals import RunLimits

            RunLimits.model_validate(self.default_limits)
        prefixes = [e.prefix for s in self.steps for e in s.expands]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("an expansion prefix is declared twice")
        return self


class Playbook:
    """A parsed playbook plus the engine questions asked of it."""

    def __init__(self, manifest: CapabilityManifest) -> None:
        if manifest.kind != "Playbook":
            raise ValueError(f"{manifest.id} is not a Playbook")
        self.manifest = manifest
        self.body = PlaybookBody.model_validate(manifest.spec)
        self.steps = {s.key: s for s in self.body.steps}
        self._owner = {e.prefix: s.key for s in self.body.steps for e in s.expands}
        self._expansions = {e.prefix: e for s in self.body.steps for e in s.expands}

    @property
    def ref(self) -> str:
        return self.manifest.ref

    @property
    def dynamic_prefixes(self) -> tuple[str, ...]:
        return tuple(self._expansions)

    def is_dynamic(self, key: str) -> bool:
        return key.startswith(self.dynamic_prefixes) if self.dynamic_prefixes else False

    def expansion(self, key: str) -> Expansion | None:
        return next((e for p, e in self._expansions.items() if key.startswith(p)), None)

    def step(self, key: str) -> Step | None:
        """The static step for a task key (None for an expanded task or a key the playbook does not know)."""
        return self.steps.get(key)

    def uses(self) -> list[tuple[str, str | None, bool]]:
        """(capability id, behaviour, optional) of every step and expansion."""
        out = [(s.use, s.behaviour, s.optional) for s in self.body.steps if s.use]
        out += [(e.use, e.behaviour, self.steps[self._owner[e.prefix]].optional) for e in self._expansions.values()]
        return out

    # ------------------------------------------------------------------------------ plan
    def build_plan(self, objective: str, *, autonomy_level: int, questions: list[str] | None = None,
                   audience: list[str] | None = None, focus: list[str] | None = None) -> dict[str, Any]:
        ns = {"run": {"autonomy_level": autonomy_level}}
        active = [s for s in self.body.steps if not s.when or evaluate(s.when, ns)]
        gate = next((s.key for s in active if s.gates_roots), None)
        steps = []
        for s in active:
            deps = list(s.after)
            if gate and not deps and s.key != gate:
                deps = [gate]
            steps.append({"key": s.key, "agent": s.use.split(".", 1)[1], "title": s.title, "depends_on": deps,
                          "optional": s.optional})
        return {"objective": objective, "questions": questions or [], "audience": audience or ["executive", "operational"],
                "focus": focus or [], "steps": steps}

    # ------------------------------------------------------------------------------ replanning
    def _children(self) -> dict[str, set[str]]:
        children: dict[str, set[str]] = {k: set() for k in self.steps}
        for s in self.body.steps:
            for dep in s.after:
                parent = self._owner.get(dep[:-1], dep) if dep.endswith("*") else dep
                children.setdefault(parent, set()).add(s.key)
        return children

    def approval_gates(self) -> list[str]:
        """Steps that wait for a person: approval gates and the side effects they authorize."""
        return [s.key for s in self.body.steps if s.type in ("approval_gate", "side_effect")]

    def trigger_for(self, reason_code: str) -> str | None:
        """The boundary trigger a replan reason resets: the playbook's own boundary for the code, else the
        fallback (`REASON_TRIGGER`); None = reset nothing (budget_downgrade)."""
        if any(b.trigger == reason_code for s in self.body.steps for b in s.replan_boundary):
            return reason_code
        return REASON_TRIGGER.get(reason_code, reason_code)

    def reset_keys(self, trigger: str) -> set[str]:
        """Steps a replan of this kind resets, from the steps declaring a boundary for it."""
        children = self._children()
        out: set[str] = set()
        for s in self.body.steps:
            for b in s.replan_boundary:
                if b.trigger != trigger:
                    continue
                queue = deque([s.key] if b.reset == "self_and_downstream" else children.get(s.key, ()))
                while queue:
                    k = queue.popleft()
                    if k not in out:
                        out.add(k)
                        queue.extend(children.get(k, ()))
        return out

    def removed_prefixes(self, trigger: str) -> tuple[str, ...]:
        """Expanded tasks are removed when the step that expanded them is reset (it expands again)."""
        reset = self.reset_keys(trigger)
        return tuple(p for p, owner in self._owner.items() if owner in reset)


_cache: dict[str, Playbook] = {}
_lock = threading.Lock()


def parse(manifest: CapabilityManifest) -> Playbook:
    """Parsed playbooks are cached by ref + content, so a reload with a new version never reuses a stale parse."""
    from analystos.core.ids import stable_hash

    key = f"{manifest.ref}:{stable_hash(manifest.spec)}"
    with _lock:
        pb = _cache.get(key)
    if pb is None:
        pb = Playbook(manifest)
        with _lock:
            _cache[key] = pb
    return pb

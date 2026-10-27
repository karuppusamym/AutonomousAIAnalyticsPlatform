"""Goal API v2 contracts (spec S06 §3.1, architecture §5.2).

A goal is the durable request; its run executes a bound playbook under `RunLimits`. Limits only
tighten: the complexity router proposes defaults, the client may lower any of them, and the
workspace policy caps (per-run cost, queries) still apply on top."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GoalMode = Literal["goal_autonomous", "goal_supervised"]
Output = Literal["investigation", "dashboard_draft", "report"]
Tier = Literal["low", "medium", "complex"]
TIERS: tuple[str, ...] = ("low", "medium", "complex")
SignalType = Literal["pause", "resume", "cancel", "redirect", "approve_step"]

# replan(reason_code, detail) -> plan_version + 1 (S06 §3.5)
REPLAN_REASONS: tuple[str, ...] = ("missing_dataset", "empty_result", "rejected_hypothesis", "quality_failure",
                                   "user_redirect", "budget_downgrade")
ReplanReason = Literal["missing_dataset", "empty_result", "rejected_hypothesis", "quality_failure", "user_redirect",
                       "budget_downgrade"]

LIMIT_KEYS: tuple[str, ...] = ("max_steps", "max_duration_s", "max_scan_bytes", "max_cost_usd", "max_replans",
                               "max_rows", "max_retries")


class RunLimits(BaseModel):
    """Budget counters a run may not exceed. None = not limited by the goal (policy caps still hold)."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1, le=500)
    max_duration_s: int | None = Field(default=None, ge=10, le=7 * 24 * 3600)
    max_scan_bytes: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_replans: int | None = Field(default=None, ge=0, le=20)
    max_rows: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0, le=100)

    def tightened(self, other: RunLimits | dict[str, Any] | None) -> RunLimits:
        """The stricter of each limit: a client may lower a default, never raise it."""
        if other is None:
            return self
        o = other if isinstance(other, RunLimits) else RunLimits.model_validate(other)
        out: dict[str, Any] = {}
        for k in LIMIT_KEYS:
            mine, theirs = getattr(self, k), getattr(o, k)
            out[k] = theirs if mine is None else mine if theirs is None else min(mine, theirs)
        return RunLimits(**out)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class DataScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalPolicy(BaseModel):
    """Side effects always need a hash-bound approval; the API cannot waive one (CLAUDE.md rule 6)."""

    model_config = ConfigDict(extra="forbid")

    publish: Literal["required"] = "required"
    notify: Literal["required"] = "required"


class GoalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    question: str = Field(min_length=10, max_length=4000)
    data_scope: DataScope = Field(default_factory=DataScope)
    mode: GoalMode = "goal_supervised"
    playbook: str | None = None  # "investigate.v1", "kpi_review.v1@2", a capability id or a workspace playbook key
    requested_outputs: list[Output] = Field(default_factory=lambda: ["investigation"])
    limits: RunLimits | None = None
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    tier: Tier | None = None  # a requested tier: may raise the router's tier, never lower it
    request_id: str | None = Field(default=None, max_length=80)

    @field_validator("requested_outputs")
    @classmethod
    def _outputs(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("request at least one output")
        return list(dict.fromkeys(v))

    @field_validator("question")
    @classmethod
    def _question(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("describe the goal (at least 10 characters)")
        return v


class SignalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SignalType
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload(self) -> SignalIn:
        if self.type == "redirect" and not str(self.payload.get("text") or "").strip():
            raise ValueError("a redirect needs payload.text")
        if self.type == "approve_step" and not (self.payload.get("step") or self.payload.get("approval_id")):
            raise ValueError("approve_step needs payload.step or payload.approval_id")
        return self


class ComplexityFeatures(BaseModel):
    table_count: int = 0
    join_depth: int = 0
    estimated_scan_rows: int = 0
    ambiguity_flags: list[str] = Field(default_factory=list)
    policy_tier: Tier = "low"
    requested_artifacts: list[str] = Field(default_factory=list)


class ComplexityDecision(BaseModel):
    """The router's answer: a tier, the playbook it maps to, and default limits (S06 §3.2)."""

    tier: Tier
    floor: Tier  # what policy requires; a request can raise the tier above it, never below
    rules_tier: Tier
    requested_tier: Tier | None = None
    playbook: str
    playbook_reason: str
    limits: dict[str, Any]
    features: ComplexityFeatures
    reasons: list[str] = Field(default_factory=list)
    clarify: dict[str, Any] = Field(default_factory=dict)  # the clarify_needed decision rung


def tier_max(*tiers: str | None) -> str:
    return max((t for t in tiers if t), key=TIERS.index, default="low")

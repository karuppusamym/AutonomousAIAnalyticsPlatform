"""Policy model (FND-011), data scope and governance decisions (§45-§46)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Decision = Literal["allow", "deny", "approval_required"]


class AttributeRule(BaseModel):
    """ABAC (SEC-003): data a caller may only see when their user attributes match. Attributes come
    from the IdP (OIDC claims mapped in config/oidc.yaml) or an administrator. Rules only remove data
    from a scope, never add: `assets` ("schema.table", "*.table") are dropped whole, `columns`
    ("schema.table.column", "*.column") and columns carrying one of `column_tags` become denied.
    `require` maps an attribute to its accepted values; every listed attribute must match (a user
    attribute that is a list matches when any element does)."""

    id: str = ""
    assets: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    column_tags: list[str] = Field(default_factory=list)
    require: dict[str, list[str]] = Field(default_factory=dict, validate_default=True)

    @field_validator("require")
    @classmethod
    def _require_is_a_condition(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """An empty `require` (or an attribute with no accepted value) is satisfied by everyone, or by
        no one: either way the rule would silently do nothing or everything. Refuse it."""
        if not value:
            raise ValueError("attribute rule needs at least one required attribute")
        empty = sorted(k for k, accepted in value.items() if not accepted)
        if empty:
            raise ValueError(f"attribute rule lists no accepted values for {', '.join(empty)}")
        return value


Classification = Literal["public", "internal", "confidential", "restricted"]
CLASSIFICATION_ORDER: dict[str, int] = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
AggregationGrain = Literal["none", "aggregate", "row"]
Audience = Literal["self", "workspace", "tenant", "external"]
AbacAttribute = Literal["classification", "purpose", "region", "aggregation_grain", "audience"]
DecisionPoint = Literal["tool", "query", "action"]


class AbacAttributes(BaseModel):
    """The five ABAC attributes of one decision (spec S01 §3.7), derived by code at the decision point, never
    taken from a prompt or model output. `classification` is the highest class of the data the call can touch;
    `purpose` the business purpose (a gateway purpose such as `monitor.freshness` also matches its class
    `monitor`); `region` the caller's region (user attribute `region`), None when unknown; `aggregation_grain`
    `row` or `aggregate` for a query (syntactic: the outermost SELECT), `none` for a call that reads no rows;
    `audience` who sees the result (`external` for publish, export and external-write tools)."""

    classification: Classification = "internal"
    purpose: str = "analysis"
    region: str | None = None
    aggregation_grain: AggregationGrain = "none"
    audience: Audience = "workspace"


class AbacCondition(BaseModel):
    """One test on an attribute. `in`/`not_in` compare values (a region value also matches its sub-regions:
    `eu` matches `eu-west-1`; a purpose value also matches the purpose's class). `at_least`/`at_most` compare
    classifications by order. An unknown region is never `in` a list and always `not_in` it (fail closed)."""

    attribute: AbacAttribute
    op: Literal["in", "not_in", "at_least", "at_most"] = "in"
    values: list[str] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _non_blank(cls, value: list[str]) -> list[str]:
        if any(not str(v).strip() for v in value):
            raise ValueError("condition values must not be blank")
        return value

    @model_validator(mode="after")
    def _ordered_ops_compare_classifications(self) -> AbacCondition:
        if self.op in ("at_least", "at_most") and (self.attribute != "classification" or len(self.values) != 1):
            raise ValueError("at_least/at_most compare one classification value")
        if self.attribute == "classification":
            unknown = [v for v in self.values if v not in CLASSIFICATION_ORDER]
            if unknown:
                raise ValueError(f"unknown classification {', '.join(unknown)}")
        return self


class AbacRule(BaseModel):
    """A workspace ABAC rule (S01-T6). Rules only deny: a call is refused when every condition in `when` holds.
    `reason` and `remedy` are returned to the caller as `403 policy_denied {rule, reason, remedy}`."""

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    when: list[AbacCondition] = Field(min_length=1)
    applies_to: list[DecisionPoint] = Field(default_factory=lambda: ["tool", "query", "action"], min_length=1)
    reason: str = Field(default="", max_length=500)
    remedy: str = Field(default="", max_length=500)


class AbacScope(BaseModel):
    """ABAC facts resolved with a DataScope, so the gateway can decide every query without another read:
    the caller's region, the data residency (workspace policy, else the tenant), the classification of each
    readable column that is not `internal`, and the workspace's ABAC rules at the scope's policy version.
    Never part of the scope hash: these are inputs to a decision made on every call, not part of what the
    scope authorizes."""

    region: str | None = None
    residency: str | None = None
    column_classes: dict[str, Classification] = Field(default_factory=dict)  # "schema.table.column" -> class
    rules: list[AbacRule] = Field(default_factory=list)


class PolicyInput(BaseModel):
    """What a policy engine decides on (built-in or OPA `input`). Identity facts plus the ABAC attributes."""

    point: DecisionPoint
    action: str
    tenant_id: str | None = None
    workspace_id: str | None = None
    user_id: str | None = None
    role: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    source_id: str | None = None
    assets: list[str] = Field(default_factory=list)
    residency: str | None = None
    policy_version: int | None = None
    attributes: AbacAttributes = Field(default_factory=AbacAttributes)


class AbacDecision(BaseModel):
    allow: bool
    rule: str | None = None
    reason: str | None = None
    remedy: str | None = None
    engine: str = "builtin"


class RowFilter(BaseModel):
    """Row-level security (P7-02, ADR-0019): rows of the matching assets a caller may see. `predicate` is a
    boolean SQL expression over the asset's own columns and may reference the caller's attributes as
    `{{user.<attribute>}}` (also `{{user.id}}`, `{{user.email}}`, `{{user.role}}`). Values become literals,
    never SQL text; a list value expands inside `IN (...)`. A missing attribute fails closed: the asset
    is withheld from that caller. Applied by the semantic compiler and again by the gateway validator, so
    no query path reads the asset unfiltered. `exempt_roles` see every row (for example owner)."""

    id: str = Field(min_length=1, max_length=80)
    assets: list[str] = Field(min_length=1)  # "schema.table" or "*.table"
    predicate: str = Field(min_length=1, max_length=2000)
    exempt_roles: list[str] = Field(default_factory=list)

    @field_validator("predicate")
    @classmethod
    def _predicate_parses(cls, value: str) -> str:
        from analystos.governance.row_filters import predicate_problem

        problem = predicate_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class ContextProviderConfig(BaseModel):
    kind: Literal["local", "okf_import", "mcp"]
    slug: str | None = None  # okf_import: the imported pack
    location: str | None = None  # okf_import: bundle directory or .zip below the upload directory
    server: str | None = None  # mcp: registered MCP server name
    product_key: str | None = None  # mcp: Atlas context product
    version: int | None = None  # mcp: Atlas context product version
    tool: str | None = None  # mcp: default atlas__get_knowledge_context
    max_chars: int | None = Field(default=None, ge=1_000, le=48_000)


class WorkspacePolicyDoc(BaseModel):
    """Versioned per-workspace policy. Values here may only tighten the platform ceilings."""

    max_rows: int = 50_000
    query_timeout_seconds: int = 30
    max_queries_per_run: int = 200
    # S02-T4: refuse a statement whose estimated scan exceeds this many bytes (0 = no workspace budget).
    max_scan_bytes: int = 0
    # S02-T7 (FR-051): may a run join data from several sources (each leg staged as a handle, joined in
    # DuckDB)? `none` keeps every statement on one source.
    data_movement: Literal["none", "staged_ok"] = "staged_ok"
    # Steps of one goal run that may run at the same time (S06 §3.6); each source also caps its own slots.
    max_parallel_steps: int = Field(default=4, ge=1, le=16)
    # Ad-hoc Ask (NL -> SQL outside a run): rolling one-hour statement budgets, repairs included.
    ask_queries_per_user_per_hour: int = 60
    ask_queries_per_workspace_per_hour: int = 600
    run_token_budget: int = 400_000
    run_cost_budget_usd: float = 2.0
    workspace_monthly_cost_budget_usd: float = 50.0
    expensive_model_approval_usd: float = 1.0
    allowed_models: list[str] = Field(default_factory=list)  # empty = platform allowlist
    # Provider names from the models config; "internal" matches every provider declared `egress: internal`
    # (a local model endpoint inside the installation, P4-S04).
    allowed_providers: list[str] = Field(default_factory=lambda: ["openrouter", "typesafe", "internal"])
    data_residency: str | None = None  # informational until providers expose region metadata
    send_data_samples_to_models: bool = False  # only aggregates/stats/schema leave the platform by default
    # An extra review of every finding by a model of another family (spec v3 §4.2). Off by default: the
    # deterministic REV checks decide verification; high-stakes workspaces may opt in.
    independent_model_verification: bool = False
    restricted_columns: list[str] = Field(default_factory=list)  # "schema.table.column" or "*.column"
    pii_columns: list[str] = Field(default_factory=list)
    pii_access: Literal["none", "restricted", "allowed"] = "restricted"
    tool_denylist: list[str] = Field(default_factory=list)
    attribute_rules: list[AttributeRule] = Field(default_factory=list)  # ABAC; applied in resolve_scope
    # S01-T6: deny rules over {classification, purpose, region, aggregation_grain, audience}, decided on every
    # tool call, action and query (governance/abac.py); the platform rules there always apply as well.
    abac_rules: list[AbacRule] = Field(default_factory=list)
    row_filters: list[RowFilter] = Field(default_factory=list)  # row-level security; rendered in resolve_scope
    publish_destinations: list[str] = Field(default_factory=lambda: ["superset"])
    publish_requires_approval: bool = True  # cannot be disabled in the MVP (enforced in code)
    separation_of_duties: bool = False
    approval_ttl_hours: int = 72
    max_iterations: int = 3
    alpha: float = 0.05
    # Domain packs (spec v3 §3.6) whose templates and knowledge runs use: None = every installed pack
    # whose applies_when matches the selected catalog; a list (even empty) = exactly these pack ids.
    domain_packs: list[str] | None = None
    # Where knowledge comes from (P4-K09, spec v3 §6.6): the workspace's own packs, imported OKF/Atlas
    # bundles, and Atlas `get_knowledge_context` over an allowlisted MCP server. Data, never authority.
    context_providers: list[ContextProviderConfig] = Field(default_factory=lambda: [ContextProviderConfig(kind="local")])
    # P4-K03: publication refuses a KPI that is not an approved metric of the workspace semantic model.
    # False here keeps workspaces created before the semantic layer working; create_workspace sets True.
    require_approved_metrics: bool = False
    # S04: conversation summaries go to the ledger as workspace knowledge (else personal to the participant).
    share_conversation_summaries: bool = False
    # S08: a dashboard export (CSV/XLSX/PDF) of data at or above this sensitivity needs a hash-bound approval;
    # below it the export is still watermarked and logged.
    export_approval_min_sensitivity: Literal["public", "internal", "confidential", "restricted"] = "confidential"


class DataScope(BaseModel):
    """The resolved, server-side authorized scope carried through a run (§12.2 invariants).

    assets: fully qualified "schema.table" names the caller may reference.
    denied_columns: "schema.table.column" never readable (restricted + PII without clearance).
    """

    workspace_id: str
    user_id: str
    role: str
    source_ids: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    asset_sources: dict[str, str] = Field(default_factory=dict)  # "schema.table" -> source_id
    denied_columns: list[str] = Field(default_factory=list)
    # "schema.table" -> ordered column names. The gateway uses it to expand stars and resolve
    # every column reference before execution; unknown columns are rejected.
    columns: dict[str, list[str]] = Field(default_factory=dict)
    source_dialects: dict[str, str] = Field(default_factory=dict)  # source_id -> sqlglot dialect
    max_rows: int = 50_000
    timeout_seconds: int = 30
    policy_version: int = 1
    # Row filters rendered for this caller ("schema.table" -> predicates in the asset's dialect; RowFilter).
    # Every read of the asset is wrapped in `SELECT <permitted columns> FROM t WHERE <predicates>`.
    row_filters: dict[str, list[str]] = Field(default_factory=dict)
    # Assets a row filter withheld from this caller (a missing attribute: fail closed) -> the reason.
    withheld_assets: dict[str, str] = Field(default_factory=dict)
    # S01-T6: decision inputs for every query (AbacScope); never part of the hash.
    abac: AbacScope | None = None

    def scope_hash(self) -> str:
        from analystos.core.ids import stable_hash

        # Empty row-security fields are left out, so a scope without row filters keeps its earlier hash.
        empty = {k for k in ("row_filters", "withheld_assets") if not getattr(self, k)}
        return stable_hash(self.model_dump(exclude={*empty, "abac"}))


class PolicyDecision(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    risk_tier: Literal["low", "medium", "high"] = "low"
    policy_version: int = 1
    effective_autonomy: int = 0
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class ExecutionIdentity(BaseModel):
    """Who is acting, for what (§46 example execution identity)."""

    user_id: str
    workspace_id: str
    agent_id: str | None = None
    purpose: str = "analysis"
    tool_id: str | None = None
    source_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None


class ApprovalSubject(BaseModel):
    """What an approval is about: the run it belongs to (if any), where the action lands, what it touches."""

    run_id: str | None = None
    destination: str | None = None
    affected_assets: list[str] = Field(default_factory=list)


class ApprovalView(BaseModel):
    """GET /api/approvals/{id}: one approval's state, read only (decisions are the approve/reject POSTs).
    `payload_hash` is the hash the approval binds to; the proposal itself stays on the inbox list."""

    id: str
    workspace_id: str
    kind: str
    status: str
    risk_tier: str
    subject: ApprovalSubject
    payload_hash: str
    plan_hash: str | None = None
    policy_version: int
    requested_by: str
    decided_by: str | None = None
    reason: str | None = None
    created_at: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None

"""Platform settings: every runtime knob an administrator can change without a redeploy (§52.8).

Stored as append-only versions (platform_setting); the effective document is the latest version
merged over these defaults. Workspace policy (contracts/policy.py) may only tighten what is set here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

LLMMode = Literal["off", "auto", "always"]
# Cheap first, escalate: which tier answers a chat purpose. never = the small tier only;
# on_validation_failure = small first, the large tier only after a deterministic validation failure
# (recorded as model_call.escalated_from / escalation_reason); always_large = the large tier first.
EscalationPolicy = Literal["never", "on_validation_failure", "always_large"]
# Execution ladder (spec v3 §4.1, ADR-0012): what may answer a purpose, cheapest first. cache = exact
# response cache (L0), registry = verified query/hypothesis/metric (L1), rules = deterministic code
# (L2), decision = typed decision model (L3), llm_small / llm_large = low_cost / strong chat profiles
# (L4/L5). `answered_by` on every model_call row is one of these.
Rung = Literal["cache", "registry", "rules", "decision", "llm_small", "llm_large"]
MODEL_RUNGS: tuple[str, ...] = ("decision", "llm_small", "llm_large")

# Purposes where a deterministic path produces an equivalent result (auto = deterministic first).
DETERMINISTIC_CAPABLE = {"planning", "hypothesis_generation", "follow_up_generation", "insight_narrative", "summarization",
                         "semantic_modeling", "dashboard_design", "dashboard_plan", "feedback_interpretation", "metadata_enrichment",
                         "hypothesis_priority", "chart_selection", "feedback_classification", "stop_check", "agent_actions",
                         "pipeline_proposal",
                         "ask_route", "clarify_needed", "metric_match", "join_path_choice", "semantic_query",
                         "ml_spec_proposal", "analyst_planning", "analyst_synthesis", "glossary_suggestion", "data_shape_proposal",
                         "request_triage"}


class LLMSettings(BaseModel):
    """How and when models are called.

    Each purpose runs a ladder (config/models.yaml `ladders`, spec v3 §4.1). `ladders` here replaces a
    purpose's ladder outright; `purpose_modes` (the increment-3 modes, what presets set) derive one
    from the default: off = no model rung, auto = deterministic rungs first and the model only when
    they are insufficient, always = model first. Precedence: ladders > purpose_modes > default.
    """

    purpose_modes: dict[str, LLMMode] = Field(default_factory=dict)  # missing purpose -> its default ladder
    ladders: dict[str, list[Rung]] = Field(default_factory=dict)  # purpose -> ordered rungs (admin override)
    # Per-run caps for one purpose, on top of the workspace run budget: {"calls": n, "tokens": n, "usd": x}.
    purpose_run_caps: dict[str, dict[str, float]] = Field(default_factory=dict)
    routing_overrides: dict[str, str] = Field(default_factory=dict)  # purpose -> profile
    profile_models: dict[str, list[str]] = Field(default_factory=dict)  # profile -> models (fallback order)
    profile_escalation_models: dict[str, list[str]] = Field(default_factory=dict)  # profile -> large tier override
    escalation: dict[str, EscalationPolicy] = Field(default_factory=dict)  # purpose -> policy (else models.yaml)
    # Hard spend caps: every billable call reserves its estimated cost atomically before it is sent
    # (runtime/budget_counters.py). Platform-wide per UTC day; None = no daily cap. The per-workspace
    # monthly cap is the workspace policy's workspace_monthly_cost_budget_usd.
    daily_spend_cap_usd: float | None = Field(2.0, ge=0.0, le=100_000.0)
    spend_alert_fraction: float = Field(0.8, ge=0.05, le=1.0)  # event + admin notification once per cap period
    disabled_models: list[str] = Field(default_factory=list)  # removed from the allowlist at runtime
    cache_enabled: bool = True
    cache_ttl_hours: int = Field(168, ge=0, le=24 * 90)
    cacheable_purposes: list[str] = Field(default_factory=lambda: [
        "planning", "hypothesis_generation", "semantic_modeling", "sql_generation", "feedback_interpretation",
        "metadata_enrichment", "insight_narrative", "summarization", "hypothesis_priority", "chart_selection",
        "rev_second_opinion", "risk_check", "feedback_classification", "alert_triage",
        # Deterministic-input purposes (Stream B): the request text holds everything the answer depends on.
        "sql_repair",  # the failing SQL + the gateway's error + the catalog; the fix is a function of them
        "semantic_query",  # question + the approved metric catalog (names only); the compiler validates the choice
        "verification",  # the claim, its statistics and the deterministic checks; the reviewer only judges them
        "follow_up_generation",  # objective + tested results + catalog; the same round asks the same question
        "glossary_suggestion",  # screened candidate names, types and profile shape; the same scan asks the same
        "data_shape_proposal",  # column names, types and distinct counts only; the same catalog asks the same
        "request_triage",  # the request text and the R1-R9 options; the same request is classified the same
    ])
    max_prompt_tokens: int = Field(16000, ge=500, le=400_000)  # estimated input tokens per call; larger prompts are refused (deterministic fallback)
    downgrade_below_budget_fraction: float = Field(0.25, ge=0.0, le=1.0)  # when a run has less budget left, chat purposes use the low_cost profile
    compact_prompts: bool = True
    catalog_max_columns_per_table: int = Field(30, ge=5, le=500)
    catalog_max_tables: int = Field(12, ge=1, le=200)


ContextSection = Literal["catalog", "glossary", "business_rules", "metrics", "prior_findings", "negative_knowledge", "episodes",
                         "external"]
CatalogDetail = Literal["names", "columns", "stats", "profile"]


class PurposeProfile(BaseModel):
    """What the context compiler (P4-T03) gives one model purpose, and how much of it.

    `sections` are filled in order after the mandatory part (the caller's required inputs); each
    section is cut in whole items and whatever does not fit is listed as omitted, never truncated.
    `referenced_only` restricts the catalog to the tables the objective/question/SQL names (SQL
    generation and repair). Budgets are characters of compact JSON (≈ 3.6 chars per token)."""

    sections: list[ContextSection] = Field(default_factory=list)
    catalog_detail: CatalogDetail = "columns"
    referenced_only: bool = False
    drop_semantic_types: list[str] = Field(default_factory=list)  # e.g. raw ids, which hypothesis specs cannot use
    max_columns_per_table: int = Field(30, ge=1, le=500)
    max_items_per_section: int = Field(8, ge=0, le=100)
    item_chars: int = Field(240, ge=40, le=4000)  # excerpt length of one knowledge item
    max_chars: int = Field(40_000, ge=1_000, le=1_500_000)
    # P4-K05: each supplementary section (external, prior_findings, negative_knowledge, episodes) is
    # filled after the primary ones and may take at most this share of the budget
    supplementary_share: float = Field(0.12, ge=0.0, le=1.0)


def _default_profiles() -> dict[str, PurposeProfile]:
    knowledge: list[ContextSection] = ["glossary", "business_rules", "metrics"]
    return {
        "planning": PurposeProfile(sections=["catalog", "glossary", "business_rules", "external", "episodes"],
                                   catalog_detail="names", drop_semantic_types=["id"], max_columns_per_table=20,
                                   max_items_per_section=4, max_chars=16_000),
        "hypothesis_generation": PurposeProfile(sections=["catalog", *knowledge, "external", "prior_findings",
                                                          "negative_knowledge", "episodes"],
                                                catalog_detail="stats", drop_semantic_types=["id"],
                                                max_columns_per_table=30, max_chars=48_000),
        # Same catalog rendering as hypothesis_generation (stats, 30 columns), so the run's catalog text repeats.
        "follow_up_generation": PurposeProfile(sections=["catalog", "negative_knowledge"], catalog_detail="stats",
                                               drop_semantic_types=["id"], max_columns_per_table=30,
                                               max_items_per_section=6, max_chars=40_000),
        "sql_generation": PurposeProfile(sections=["catalog", "glossary", "metrics"], catalog_detail="profile",
                                         referenced_only=True, max_columns_per_table=40, max_items_per_section=6,
                                         max_chars=24_000),
        "sql_repair": PurposeProfile(sections=["catalog"], catalog_detail="columns", referenced_only=True,
                                     max_columns_per_table=60, max_chars=16_000),
        "semantic_modeling": PurposeProfile(sections=["metrics", "glossary", "business_rules"], max_items_per_section=5,
                                            max_chars=20_000),
        "feedback_interpretation": PurposeProfile(sections=["catalog", "glossary"], catalog_detail="names",
                                                  max_columns_per_table=60, max_items_per_section=5, max_chars=12_000),
    }


class ContextSettings(BaseModel):
    """Context compiler (P4-T03): per-purpose profiles; purposes without one get only their
    mandatory inputs. `min_relevance` is the share of objective terms a knowledge item must match
    (or a mapped in-scope column) before it may reach a prompt; below it the section says NO_MATCH."""

    compiler_enabled: bool = True
    min_relevance: float = Field(0.15, ge=0.0, le=1.0)
    # Shared context cache (Stream B): compiled contexts, knowledge retrieval and the rendered catalog,
    # reused across steps and workers for this long (Redis when configured, else in-process).
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(900, ge=0, le=86_400)
    profiles: dict[str, PurposeProfile] = Field(default_factory=_default_profiles)


class AnalysisSettings(BaseModel):
    max_round1_hypotheses: int = Field(8, ge=1, le=30)
    max_followups_per_round: int = Field(3, ge=0, le=10)
    # Statistical safeguards can only be tightened: REV uses max(critic.MIN_N, this value).
    min_sample_size: int = Field(100, ge=30, le=1_000_000)
    sample_rows: int = Field(50000, ge=1000, le=5_000_000)
    # P8-15: share of each analysed table's rows held out (by a stable hash of the row key) and read only
    # once, after REV locked a verified claim, to confirm it. 0 turns held-out confirmation off: every
    # finding then stays exploratory unless a fresh-snapshot replication confirms it.
    holdout_fraction: float = Field(0.3, ge=0.0, le=0.5)
    heuristic_hypotheses_sufficient: int = Field(6, ge=1, le=30)  # auto mode: skip the model if rules produce this many
    max_charts: int = Field(16, ge=1, le=60)


class CrawlSettings(BaseModel):
    default_mode: Literal["full", "incremental"] = "incremental"
    max_tables: int = Field(2000, ge=1, le=100_000)
    profile_changed_only: bool = True
    profile_sample_rows: int = Field(100000, ge=0, le=10_000_000)
    llm_enrichment: bool = False  # deterministic semantics always run; the model only fills placeholders when enabled
    enrichment_batch_tables: int = Field(25, ge=1, le=100)
    enrichment_max_columns: int = Field(12, ge=1, le=60)
    pii_value_sampling: bool = True  # classify PII from a small sample of values (values never leave the platform)
    profile_reuse_hours: int = Field(24, ge=0, le=24 * 365)  # a run reuses a stored profile of the same shape this young
    profile_on_select: bool = True  # selecting tables starts a background crawl that profiles the newly selected ones


class MonitorSettings(BaseModel):
    default_lookback: int = Field(8, ge=3, le=520)
    default_z_threshold: float = Field(3.0, ge=1.0, le=10.0)
    triage_escalate_probability: float = Field(0.8, ge=0.5, le=1.0)
    auto_investigation_enabled: bool = True
    # alert_triage materiality rules (ADR-0015): below these a signal keeps its severity and is not
    # escalated or auto-investigated; it is never suppressed (the alert is still raised).
    min_material_effect: float = Field(0.0, ge=0.0, le=10.0)  # |relative change|, 0 = no minimum
    min_material_points: int = Field(3, ge=2, le=520)


class DecisionSettings(BaseModel):
    """DecisionService (ADR-0015) knobs an administrator can change without a redeploy."""

    backends: dict[str, list[str]] = Field(default_factory=dict)  # purpose -> backend order override (rules always kept)
    calibration_window_days: int = Field(30, ge=1, le=365)
    calibration_min_outcomes: int = Field(20, ge=1, le=100_000)  # fewer labelled outcomes = no verdict, no change
    max_brier: float = Field(0.2, ge=0.0, le=1.0)  # spec v3 §5: route/bounded_stop need Brier <= 0.2
    max_ece: float = Field(0.15, ge=0.0, le=1.0)
    purpose_max_brier: dict[str, float] = Field(default_factory=dict)  # per-purpose override
    auto_downgrade: bool = True  # a backend below threshold drops out of that purpose's chain (recorded, reversible)
    pinned: list[str] = Field(default_factory=list)  # "purpose:backend" pairs never downgraded automatically


class SourceSettings(BaseModel):
    enabled_kinds: list[str] = Field(default_factory=list)  # empty = every kind in the catalog
    allow_pushdown: bool = True
    staged_max_rows: int = Field(1_000_000, ge=1000, le=100_000_000)


class OutboundSettings(BaseModel):
    """Where workspace-registered endpoints may point (P4-X05). Platform-level on purpose: a workspace
    owner decides *whether* to use a server, only an administrator decides which hosts exist at all.
    Added to ANALYSTOS_MCP_HOST_ALLOWLIST; entry syntax in tools/http.py (`*`, `*.example.com`,
    `host[:port]`, IP or CIDR). Only an exact name, IP or CIDR lets a server resolve to a private address."""

    mcp_host_allowlist: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("mcp_host_allowlist")
    @classmethod
    def _entries(cls, v: list[str]) -> list[str]:
        from analystos.core.errors import InvalidInput
        from analystos.tools.http import parse_entry

        out: list[str] = []
        for e in v:
            try:
                parse_entry(e)
            except InvalidInput as exc:
                raise ValueError(exc.message) from None
            if e.strip().lower() not in out:
                out.append(e.strip().lower())
        return out


class FeatureFlags(BaseModel):
    jev_decisions: bool = True
    superset_publishing: bool = True
    python_sandbox: bool = True
    reports: bool = True


class PlatformSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    monitors: MonitorSettings = Field(default_factory=MonitorSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    outbound: OutboundSettings = Field(default_factory=OutboundSettings)
    decisions: DecisionSettings = Field(default_factory=DecisionSettings)


PRESETS: dict[str, dict[str, LLMMode]] = {
    # The default ladders (P4-T02): deterministic first wherever a rule path exists.
    "balanced": {},
    # Fewest tokens without losing analysis quality: rules first everywhere a rule path exists.
    "token_saver": {p: "auto" for p in DETERMINISTIC_CAPABLE} | {"verification": "always"},
    # Model first for every purpose (the pre-increment-4 default).
    "max_quality": {p: "always" for p in DETERMINISTIC_CAPABLE | {"verification", "sql_generation", "sql_repair",
                                                                 "rev_second_opinion", "risk_check", "alert_triage",
                                                                 "statistical_interpretation"}},
    # No model calls at all: the platform runs entirely on deterministic paths.
    "offline": {p: "off" for p in DETERMINISTIC_CAPABLE | {"verification", "sql_generation", "sql_repair",
                                                          "rev_second_opinion", "risk_check", "alert_triage",
                                                          "statistical_interpretation", "decision_structured", "health_probe"}},
    # Air-gapped (P4-S04): the offline preset plus a local OpenAI-compatible model as the last rung.
    # Deterministic rungs answer first wherever they exist; decision-model purposes stay on rules (the
    # DecisionService keeps rules / local_classifier only). Egress itself is enforced by the deployment
    # flag ANALYSTOS_AIR_GAPPED, not by this preset: settings are data, the guard is code.
    "air_gapped": {p: "auto" for p in DETERMINISTIC_CAPABLE} | {
        "rev_second_opinion": "off", "risk_check": "off", "alert_triage": "off", "decision_structured": "off",
        "hypothesis_priority": "off", "chart_selection": "off", "feedback_classification": "off", "stop_check": "off",
        "ask_route": "off", "clarify_needed": "off", "metric_match": "off", "join_path_choice": "off"},
}

# Escalation policy a preset sets (replaces llm.escalation wholesale; {} = the models.yaml defaults).
# max_quality keeps the large tier (Sonnet) first wherever it was first before cheap-first routing (the
# reasoning_strong / analytical_reasoning / coding purposes); low_cost purposes stay small-first.
LARGE_FIRST_PURPOSES = ("planning", "hypothesis_generation", "follow_up_generation", "statistical_interpretation",
                        "sql_generation", "sql_repair", "semantic_modeling", "feedback_interpretation")
PRESET_ESCALATION: dict[str, dict[str, EscalationPolicy]] = {
    "max_quality": {p: "always_large" for p in LARGE_FIRST_PURPOSES},
}

# Settings a preset changes besides the purpose modes (deep-merged by platform_settings.apply_preset).
PRESET_PATCHES: dict[str, dict] = {
    "air_gapped": {"features": {"jev_decisions": False}},
}

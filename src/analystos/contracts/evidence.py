"""Evidence contracts for findings (P4-03, target spec §4, ADR-0011 "Evidence migration").

One overloaded `verified` badge is replaced by dimensions on a versioned `EvidenceBundle`:

* **Claim** — typed `Fact`s (value, unit, group, baseline, direction, query/result hashes, method) and the
  `binding` of every number in the narrative to one of them. A sentence whose numbers, groups, units or
  directions do not match the facts does not bind, and an unbound finding cannot be verified.
* **Data** — the run's `DataManifest` entries for the analysed asset (snapshot version, rows, content
  fingerprint, sampling, time window) plus the governed query receipts.
* **Method** — effect size, uncertainty, sample sizes, multiple-testing family, selection procedure,
  assumptions and power at the practical threshold where the method can state it.
* **Validation** — `state` (exploratory | replicated | confirmed | inconclusive | invalid |
  insufficient_evidence | legacy) with check-level pass / fail / not_applicable, and the confirmation rule
  that promoted a discovery, if any. `confirmed` cannot be constructed without a passing rule. Since P8-15
  it also carries the finding's `strength` and its `holdout` confirmation record.
* **Limits** — population, stale inputs, confounding, untested slices.

The legacy review score stays, labelled uncalibrated: it is not a probability that the claim is true.
Models never write any of this; `analystos.evidence` computes it from the deterministic path.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUNDLE_VERSION = "evidence.v1"
MANIFEST_VERSION = "data_manifest.v1"

Unit = Literal["fraction", "percent", "points", "ratio", "count", "hours", "days", "minutes", "seconds", "value",
               "coefficient", "probability"]
FactKind = Literal["level", "comparison", "change", "share", "count", "statistic"]
Direction = Literal["higher", "lower", "increase", "decrease", "equal"]
ValidationState = Literal["exploratory", "replicated", "confirmed", "inconclusive", "invalid",
                          "insufficient_evidence", "legacy"]
Label = Literal["discovery", "confirmation", "legacy"]
CheckOutcome = Literal["pass", "fail", "not_applicable"]
StrengthLabel = Literal["weak", "moderate", "strong"]
CONFIRMATION_RULES = ("holdout_partition", "fresh_snapshot_replication")


class Fact(BaseModel):
    """One number a finding may quote, bound to what it measures. `value` is in `unit` exactly as the
    statistic produced it (a rate is a `fraction` 0..1; its percent rendering is derived)."""

    model_config = ConfigDict(extra="forbid")
    id: str
    role: str  # the statistic key it came from (top_rate, rate_ratio, pct_change, n, ...)
    kind: FactKind
    metric: str | None = None  # what is measured (outcome label, "records", "p-value")
    value: float
    unit: Unit
    subject: str | None = None  # the group / cohort / period the value belongs to
    dimension: str | None = None  # what `subject` is a value of (the segment's label)
    baseline: str | None = None  # comparison and change facts: the other side
    direction: Direction | None = None
    window: str | None = None  # time window / period the value covers, when stated
    method: str | None = None
    query_ids: list[str] = Field(default_factory=list)
    result_hashes: list[str] = Field(default_factory=list)
    primary: bool = False  # the claim's headline comparison / change (direction checks without a number use it)


class Mention(BaseModel):
    """A number in the narrative and the fact it was bound to (None: unbound)."""

    model_config = ConfigDict(extra="forbid")
    text: str
    value: float
    unit: str | None = None
    fact_id: str | None = None
    problem: str | None = None


class Binding(BaseModel):
    ok: bool
    mentions: list[Mention] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class ManifestEntry(BaseModel):
    """What one analysed asset was when the run read it."""

    model_config = ConfigDict(extra="forbid")
    asset: str  # schema.table as the gateway sees it
    source_id: str | None = None
    mode: Literal["staged", "pushdown", "unknown"] = "unknown"
    load_id: str | None = None
    rows: int | None = None
    source_total_rows: int | None = None
    content_fingerprint: str | None = None  # order-independent hash of the staged rows
    structure_fingerprint: str | None = None  # the crawler's column-structure fingerprint
    sampling_method: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    staged_at: str | None = None
    observed_at: str | None = None  # pushdown: when the run read the live source (best-effort replay only)
    immutable: bool = False  # True when the version names a fixed staged snapshot
    version: str | None = None  # sha256 of the fields that define the data (None when there is no fixed version)
    version_basis: Literal["content", "load_metadata", "none"] = "none"


class DataManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = MANIFEST_VERSION
    version: str  # sha256 over the entries' versions: identical data -> identical manifest version
    built_at: str
    entries: list[ManifestEntry] = Field(default_factory=list)

    def entry(self, asset: str | None) -> ManifestEntry | None:
        return next((e for e in self.entries if e.asset == asset), None)


class Check(BaseModel):
    model_config = ConfigDict(extra="allow")
    check: str
    outcome: CheckOutcome
    reason: str = ""


class Confirmation(BaseModel):
    """Result of evaluating the confirmation rules for one finding."""

    model_config = ConfigDict(extra="forbid")
    rule: str | None = None  # the rule that passed (one of CONFIRMATION_RULES)
    passed: bool = False
    evaluated: list[dict[str, Any]] = Field(default_factory=list)  # every rule tried, with its reason


class Strength(BaseModel):
    """How far a supported finding clears its method's bars (P8-15, `evidence.strength`): `weak` when the
    effect is under 1.5x the method's minimum effect or the adjusted p-value is within 10x of alpha. Computed
    from the statistic alone; no model is involved."""

    model_config = ConfigDict(extra="forbid")
    label: StrengthLabel
    effect: float | None = None
    effect_label: str | None = None
    threshold: float | None = None  # the method's minimum effect for this effect measure
    margin: float | None = None  # how many times the threshold the effect is (log scale for ratios)
    q: float | None = None  # the adjusted p-value (raw p when there was no adjustment)
    alpha: float | None = None
    reasons: list[str] = Field(default_factory=list)
    rule: str = "strength.v1"


class HoldoutCheck(BaseModel):
    """The held-out confirmation of one claim (P8-15, `evidence.holdout`): the claim was locked, then the same
    test ran once on rows the discovery never read. `evaluated` False says why it did not run."""

    model_config = ConfigDict(extra="forbid")
    evaluated: bool
    reason: str | None = None
    partition: str | None = None  # plain description, e.g. "30% of rows held out by Order ID"
    partition_spec: dict[str, Any] | None = None
    claim: dict[str, Any] | None = None  # method, spec hash, top group, baseline, direction, asset
    claim_hash: str | None = None
    claim_locked_at: str | None = None
    partition_accessed_at: str | None = None
    supported: bool | None = None
    top: str | None = None
    direction: Direction | None = None
    test: str | None = None
    p_value: float | None = None
    p_one_sided: float | None = None
    alpha: float | None = None
    effect_size: float | None = None
    effect_label: str | None = None
    n: int | None = None
    contrast: list[str] | None = None  # the two groups compared when the claim was tested as top vs baseline
    experiment_id: str | None = None
    confirmed: bool | None = None  # the holdout rule's verdict (same top group and direction, locked first)


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: ValidationState
    label: Label
    checks: list[Check] = Field(default_factory=list)
    confirmation: Confirmation = Field(default_factory=Confirmation)
    missing_evidence: list[str] = Field(default_factory=list)
    reproducible: bool | None = None
    predictive_evaluated: bool = False  # a model evaluated on unseen rows (e.g. holdout AUC), not a confirmation
    strength: Strength | None = None  # P8-15: supported findings only
    holdout: HoldoutCheck | None = None  # P8-15: the held-out confirmation attempt, if one was considered

    @model_validator(mode="after")
    def _confirmed_needs_a_rule(self) -> Validation:
        if self.state == "confirmed" and not (self.confirmation.passed and self.confirmation.rule in CONFIRMATION_RULES):
            raise ValueError("a finding is 'confirmed' only when a confirmation rule passed")
        if self.state == "confirmed" and self.label != "confirmation":
            raise ValueError("a confirmed finding carries the 'confirmation' label")
        if self.label == "confirmation" and self.state != "confirmed":
            raise ValueError("the 'confirmation' label is reserved for confirmed findings")
        return self


class Freshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["current", "stale", "unknown"] = "current"
    since: str | None = None
    reason: str | None = None
    assets: list[str] = Field(default_factory=list)


class ReviewScore(BaseModel):
    """The REV confidence number. Hand-built, not calibrated: never a probability of truth."""

    model_config = ConfigDict(extra="forbid")
    value: float | None = None
    calibrated: Literal[False] = False
    note: str = "heuristic review score from deterministic checks; not a calibrated probability"


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = BUNDLE_VERSION
    verifier_version: str = "rev.v2"
    data: dict[str, Any] = Field(default_factory=dict)
    claim: dict[str, Any] = Field(default_factory=dict)
    method: dict[str, Any] = Field(default_factory=dict)
    validation: Validation
    limits: dict[str, Any] = Field(default_factory=dict)
    freshness: Freshness = Field(default_factory=Freshness)
    review_score: ReviewScore = Field(default_factory=ReviewScore)
    legacy: dict[str, Any] | None = None  # pre-P4-03 badge values, kept with their verifier version


# ------------------------------------------------------------------------------------ Evidence API v2 (S07-T4)
class EvidenceEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | None = None
    unit: str | None = None  # the effect's measure (cramers_v, rate_ratio, pct_change_fitted, mase ...)
    ci: dict[str, float | None] | None = None  # {low, high} when the method reports an interval


class EvidenceSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 0
    coverage: float | None = None  # rows read / rows in the source (None when the source total is unknown)


class EvidenceDatasetVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str
    source_id: str | None = None
    version: str | None = None
    mode: str = "unknown"
    rows: int | None = None
    staged_at: str | None = None


class EvidenceMethod(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    version: str | None = None  # the method manifest version the run bound
    test: str | None = None
    claim_kind: Literal["observation", "association"] = "association"


class EvidenceV2(BaseModel):
    """The API shape of one piece of evidence (v1 §7.3): the existing `Insight` + its `VerificationRecord` +
    citations, read through one contract. A VOID record always carries `void_reason`."""

    model_config = ConfigDict(extra="forbid")
    v: Literal[2] = 2
    evidence_id: str  # the finding (insight) id
    run_id: str
    claim_id: str  # stable across runs: the claim's identity (what was tested, which group came out on top)
    code: str | None = None
    label: Literal["observed", "supported_association", "possible_contributor", "rejected", "unknown"]
    title: str = ""
    description: str
    dataset_versions: list[EvidenceDatasetVersion] = Field(default_factory=list)
    metric_version: dict[str, str] | None = None  # {metric, version} of the semantic metric measured, if any
    query_execution_ids: list[str] = Field(default_factory=list)
    method: EvidenceMethod
    effect: EvidenceEffect
    sample: EvidenceSample
    limitations: list[str] = Field(default_factory=list)
    review_status: str  # verified | failed | void | pending | superseded | legacy | unverified
    validation: str | None = None  # exploratory | replicated | confirmed | ... (P4-03)
    fingerprint: str | None = None
    verification_record_id: str | None = None
    void_reason: str | None = None
    citations: dict[str, Any] | None = None  # counts by kind (N-8); the full set at /api/insights/{id}/citations

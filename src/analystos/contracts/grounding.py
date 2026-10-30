"""Human-owned business meaning, grounded model suggestions and repeatable evaluation."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PopulationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not_in"]
    value: str | int | float | bool | list[str | int | float]

    @model_validator(mode="after")
    def comparison_value(self):
        if self.op in {"in", "not_in"} and (not isinstance(self.value, list) or not self.value or len(self.value) > 100):
            raise ValueError("Membership rules need 1 to 100 literal values")
        if self.op not in {"in", "not_in"} and isinstance(self.value, list):
            raise ValueError("This comparison needs one literal value")
        return self


class MetricMeaning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome_column: str
    numerator: str = Field(min_length=3, max_length=500)
    denominator: str = Field(min_length=3, max_length=500)
    grain: str = Field(min_length=2, max_length=200)
    unit: str = Field(min_length=1, max_length=80)
    time_column: str | None = None
    exclusions: str = Field(default="", max_length=1000)
    true_value: str | int | bool | None = None
    calculation: Literal["auto", "numeric_mean", "flag_rate", "duration_hours"] = "auto"
    end_column: str | None = None

    @model_validator(mode="after")
    def duration_contract(self):
        if self.calculation == "duration_hours" and not self.end_column:
            raise ValueError("Elapsed time needs both start and end columns")
        return self


class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: str = Field(min_length=2, max_length=200)
    definition: str = Field(min_length=5, max_length=3000)
    kind: Literal["definition", "metric", "population_rule", "quality_rule", "policy"] = "definition"
    columns: list[str] = Field(default_factory=list, max_length=30)
    domain: str = Field(default="", max_length=80)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    owner_id: str | None = None
    effective_from: date | None = None
    expires_on: date | None = None
    state: Literal["draft", "approved", "retired"] = "draft"
    evidence: str = Field(default="", max_length=2000)
    examples: str = Field(default="", max_length=2000)
    metric: MetricMeaning | None = None
    predicate: PopulationRule | None = None

    @model_validator(mode="after")
    def complete(self):
        if self.effective_from and self.expires_on and self.effective_from > self.expires_on:
            raise ValueError("expiry must be on or after the effective date")
        if self.state == "approved" and (not self.owner_id or not self.evidence or not self.examples):
            raise ValueError("approval needs an owner, supporting evidence and validation examples")
        if self.kind == "metric" and self.state == "approved" and not self.metric:
            raise ValueError("approved metric meaning needs numerator, denominator, grain and unit")
        if self.kind in {"population_rule", "quality_rule"} and self.state == "approved" and not self.predicate:
            raise ValueError("an executable rule needs a typed predicate")
        return self


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=5, max_length=4000)
    source_ids: list[str] | None = None
    use_model: bool = True


class GoldenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=5, max_length=2000)
    expected_columns: list[str] = Field(default_factory=list, max_length=30)
    expect_clarification: bool = False
    notes: str = Field(default="", max_length=2000)


class GoldenEvaluateIn(BaseModel):
    use_model: bool = False


class ModelResolution(BaseModel):
    term: str = Field(max_length=200)
    evidence_ids: list[str] = Field(max_length=20)
    columns: list[str] = Field(max_length=30)
    reason: str = Field(default="", max_length=1000)


class MetricDraft(BaseModel):
    term: str = Field(max_length=200)
    definition: str = Field(max_length=2000)
    columns: list[str] = Field(default_factory=list, max_length=30)
    missing_contract_fields: list[str] = Field(default_factory=list, max_length=20)


class ResolutionAnswer(BaseModel):
    resolutions: list[ModelResolution] = Field(default_factory=list, max_length=25)
    missing: list[str] = Field(default_factory=list, max_length=10)
    metric_proposals: list[MetricDraft] = Field(default_factory=list, max_length=5)
    filters: list["MeaningFilter"] = Field(default_factory=list, max_length=20)
    open_terms: list["OpenTerm"] = Field(default_factory=list, max_length=25)


class MeaningFilter(PopulationRule):
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class OpenTerm(BaseModel):
    """A term the question or plan depends on that the context does not resolve (PILOT-FIX-04). A model only
    proposes it: code checks the columns and whether the catalog could hold it before anything is asked."""

    term: str = Field(max_length=200)
    question: str = Field(default="", max_length=500)
    availability: str = Field(default="unknown", max_length=20)  # in_data | derivable | absent | unknown
    columns: list[str] = Field(default_factory=list, max_length=30)



class MeaningQuestion(BaseModel):
    key: str
    term: str
    question: str
    kind: Literal["review", "rule", "model", "metric"]
    state: str | None = None
    columns: list[str] = Field(default_factory=list)
    binding_id: str | None = None
    reason: str | None = None


class MeaningLimitation(BaseModel):
    key: str
    term: str
    kind: Literal["absent_data", "unreviewed"]
    statement: str


class MeaningReview(BaseModel):
    """One round of the meaning review: the one list asked, limitations the run carries, the pinned definitions."""

    round: int = Field(ge=1)
    questions: list[MeaningQuestion] = Field(default_factory=list)
    limitations: list[MeaningLimitation] = Field(default_factory=list)
    not_asked: list[MeaningQuestion] = Field(default_factory=list)
    resolved_without_asking: list[dict] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    pinned: list[dict] = Field(default_factory=list)
    binding_revisions: dict[str, int] = Field(default_factory=dict)

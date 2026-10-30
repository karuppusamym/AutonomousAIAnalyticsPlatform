"""Guided questions, accountable follow-up and self-reported pilot measurements."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DateRange(BaseModel):
    """Inclusive calendar dates; `label` is the reader's own name for it ("Q3 2026"), shown, never parsed."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    start: date
    end: date
    label: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _ordered(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("a date range ends on or after its start")
        if (self.end - self.start).days > 3660:
            raise ValueError("a date range spans at most ten years")
        return self


ComparisonMode = Literal["previous_period", "same_period_last_year", "custom", "none"]


class GuidedInvestigationIn(BaseModel):
    """PILOT-FIX-01: the period and its baseline are typed date ranges, applied as filters and compared by code
    (`services.periods`); free text named them before and the run analysed all history."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_id: str
    period: DateRange
    comparison_mode: ComparisonMode = "previous_period"
    baseline: DateRange | None = None  # only with comparison_mode "custom"
    focus: str = Field(default="All records in the selected source", min_length=3, max_length=500)
    decision: str = Field(min_length=5, max_length=500)
    source_ids: list[str] | None = None

    @model_validator(mode="after")
    def _baseline_mode(self) -> "GuidedInvestigationIn":
        if self.comparison_mode == "custom" and self.baseline is None:
            raise ValueError("a custom comparison needs the baseline dates")
        if self.comparison_mode != "custom" and self.baseline is not None:
            raise ValueError("baseline dates are given only with comparison_mode 'custom'")
        return self


class InvestigationActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=5, max_length=300)
    next_step: str = Field(min_length=5, max_length=2000)
    owner_id: str | None = None
    due_date: date | None = None
    impact_assessment: str = Field(default="", max_length=2000)
    success_measure: str = Field(min_length=3, max_length=1000)
    open_questions: str = Field(default="", max_length=2000)
    status: Literal["proposed", "in_progress", "done", "dismissed"] = "proposed"
    resolution_note: str = Field(default="", max_length=2000)


# What a pilot assessment measures: an investigation run, or a conversational deliverable (VALUE-05).
PILOT_SUBJECTS = ("investigation", "ask", "dashboard", "report", "digest")


class PilotReviewIn(BaseModel):
    """An investigation is named by `run_id`; an Ask conversation, builder dashboard, report document or
    scheduled digest by `subject_id`. `assisted_minutes` includes `review_minutes` (checking and correcting)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject_type: Literal["investigation", "ask", "dashboard", "report", "digest"] = "investigation"
    run_id: str | None = None
    subject_id: str | None = None
    evidence_kind: Literal["rehearsal", "real_pilot"]
    task: str = Field(min_length=5, max_length=500)
    baseline_minutes: float = Field(gt=0, le=100000, allow_inf_nan=False)
    assisted_minutes: float = Field(ge=0, le=100000, allow_inf_nan=False)
    accuracy: Literal["correct", "partly_correct", "incorrect", "not_reviewed"]
    usefulness: int = Field(ge=1, le=5)
    would_use_again: bool
    notes: str = Field(default="", max_length=3000)
    review_minutes: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    interventions: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def _one_subject(self) -> "PilotReviewIn":
        if self.subject_type == "investigation":
            if not self.run_id or self.subject_id not in (None, self.run_id):
                raise ValueError("an investigation assessment names its run_id")
            self.subject_id = self.run_id
        elif not self.subject_id or self.run_id is not None:
            raise ValueError(f"a {self.subject_type} assessment names its subject_id, not a run_id")
        if self.review_minutes is not None and self.review_minutes > self.assisted_minutes:
            raise ValueError("review minutes are part of the assisted time and cannot exceed it")
        return self

"""Guided questions, accountable follow-up and self-reported pilot measurements."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GuidedInvestigationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_id: str
    period: str = Field(min_length=3, max_length=200)
    comparison: str = Field(min_length=3, max_length=200)
    focus: str = Field(default="All records in the selected source", min_length=3, max_length=500)
    decision: str = Field(min_length=5, max_length=500)
    source_ids: list[str] | None = None


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


class PilotReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    run_id: str
    evidence_kind: Literal["rehearsal", "real_pilot"]
    task: str = Field(min_length=5, max_length=500)
    baseline_minutes: float = Field(gt=0, le=100000, allow_inf_nan=False)
    assisted_minutes: float = Field(ge=0, le=100000, allow_inf_nan=False)
    accuracy: Literal["correct", "partly_correct", "incorrect", "not_reviewed"]
    usefulness: int = Field(ge=1, le=5)
    would_use_again: bool
    notes: str = Field(default="", max_length=3000)

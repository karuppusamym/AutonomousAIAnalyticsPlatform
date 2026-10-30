"""Controlled-pilot readiness (P4-09, SEC-001..003, OPS-001..005).

A pilot workspace and each of its sources name two accountable people: the business owner (accepts the
decisions the outputs support) and the technical owner (keeps the source and its access working). They are
named people, not platform roles: an owner may have no AnalystOS account at all. Readiness lists what a
controlled pilot still lacks — a missing owner, an uncertified connector, a source in error, no single
sign-on mapping, no recovery drill — each with the reason and what to do; it never averages them away.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OWNER_ROLES = ("business", "technical")


class NamedOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    user_id: str | None = Field(default=None, max_length=40)  # set when the owner is also a platform user


class OwnersIn(BaseModel):
    """A full replacement of the named owners: an omitted or null role clears it."""

    model_config = ConfigDict(extra="forbid")
    business: NamedOwner | None = None
    technical: NamedOwner | None = None


class PilotCheck(BaseModel):
    check: str  # owner.business | owner.technical | connector.certified | connector.health | identity.sso | recovery.drill
    subject: str  # "workspace" or "source:<id> (<name>)" or "platform"
    status: Literal["pass", "missing", "fail"]
    reason: str
    remediation: str | None = None  # the technical step (paths, scripts, settings): expert detail
    evidence: str | None = None  # a file path or record id backing a pass
    # Plain wording for the page (PILOT-FIX readiness friction): what to do, who can do it, and the screen for it.
    action: str | None = None
    who: Literal["workspace_owner", "editor", "analyst", "platform_admin"] | None = None
    screen: Literal["members_policy", "sources", "metrics", "scorecard"] | None = None


class PilotReadiness(BaseModel):
    workspace_id: str
    workspace_name: str
    verdict: Literal["ready", "not_ready"]
    missing: int
    failing: int
    checks: list[PilotCheck]
    generated_at: datetime

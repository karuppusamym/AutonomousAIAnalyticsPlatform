"""Model usage sink: every model call is persisted (MOD-003) and counted against budgets (§51).

Budget checks read Redis counters (runtime/budget_counters.py, P4-T07); the database aggregates are
only the seed of a missing counter and the fallback when Redis is unavailable. Hard spend caps
(platform daily, workspace monthly) are reservations: `reserve` before a billable call, settled by
`record` to the actual cost; they fail closed without Redis."""
from __future__ import annotations

import time
from datetime import datetime
from threading import Lock
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from analystos.core.errors import BudgetExceeded, SpendCapReached, SpendCountersUnavailable
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, ModelCall, ModelPayload, User, Workspace
from analystos.governance.policy import load_policy
from analystos.llm.replay import encode_payload
from analystos.llm.router import CallContext, cached_prompt_tokens
from analystos.runtime import budget_counters
from analystos.runtime.budget_counters import (
    DAY_TTL_SECONDS,
    MONTH_TTL_SECONDS,
    RUN_TTL_SECONDS,
    BudgetCounters,
    CapExceeded,
    CapSpec,
    CountersUnavailable,
    SpendReservation,
    day_start,
    month_start,
    next_day_start,
    next_month_start,
)

log = get_logger(__name__)

BILLABLE = ("ok", "error")


def _store_payload(s, kind: str, value: dict | None) -> str | None:
    """Content-addressed insert (idempotent): a retry re-sending the same request stores nothing new."""
    if value is None:
        return None
    digest, body, size, truncated = encode_payload(value)
    s.execute(pg_insert(ModelPayload).values(hash=digest, kind=kind, size_bytes=size, truncated=truncated, body=body)
              .on_conflict_do_nothing(index_elements=["hash"]))
    return digest


def _month_cost_sql(s, workspace_id: str, now: datetime | None = None) -> float:
    return float(s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(
        ModelCall.workspace_id == workspace_id, ModelCall.created_at >= month_start(now))) or 0.0)


def day_cost_sql(s, now: datetime | None = None) -> float:
    """Platform-wide model spend of the current UTC day (the seed of the daily cap counter)."""
    return float(s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(
        ModelCall.created_at >= day_start(now))) or 0.0)


CAP_REMEDY = {
    "platform_daily": ("The platform-wide daily model spend cap is reached. Model calls resume at 00:00 UTC; a platform "
                       "administrator can raise it in Admin > Settings (llm.daily_spend_cap_usd). Deterministic paths "
                       "continue meanwhile."),
    "workspace_monthly": ("This workspace's monthly model spend cap is reached. Model calls resume on the 1st of next month "
                          "(UTC); a workspace owner can raise workspace_monthly_cost_budget_usd in the workspace policy. "
                          "Deterministic paths continue meanwhile."),
}


def _purpose_sql(s, run_id: str, purpose: str, what: str) -> float:
    column = {"calls": func.count().filter(ModelCall.status.in_(BILLABLE)),
              "tokens": func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0),
              "usd": func.coalesce(func.sum(ModelCall.cost_usd), 0.0)}[what]
    return float(s.scalar(select(column).where(ModelCall.run_id == run_id, ModelCall.purpose == purpose)) or 0)


class DbUsageSink:
    def __init__(self, counters: BudgetCounters | None = None) -> None:
        self._counters = counters
        self._ws_caps: dict[str, tuple[float, float]] = {}  # workspace -> (monotonic time read, monthly cap)
        self._lock = Lock()

    @property
    def counters(self) -> BudgetCounters:
        return self._counters if self._counters is not None else budget_counters.default_budget_counters()

    def record(self, *, ctx: CallContext, purpose, profile, provider, model, status, attempt, latency_ms,
               input_tokens, output_tokens, cost_usd, request_hash, error, tokens_saved: int = 0,
               request: dict | None = None, response: dict | None = None, answered_by: str | None = None,
               cost_source: str | None = None, reservation: SpendReservation | None = None,
               escalated_from: str | None = None, escalation_reason: str | None = None) -> None:
        try:
            self._insert(ctx=ctx, purpose=purpose, profile=profile, provider=provider, model=model, status=status,
                         attempt=attempt, latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens,
                         cost_usd=cost_usd, request_hash=request_hash, error=error, tokens_saved=tokens_saved,
                         request=request, response=response, answered_by=answered_by, cost_source=cost_source,
                         escalated_from=escalated_from, escalation_reason=escalation_reason)
        finally:
            if reservation is not None:
                # Settle even when the row could not be written: the provider was (or was not) paid either
                # way. An unknown price keeps the reserved estimate instead of counting the call as free.
                actual = reservation.estimate if cost_source == "missing_price" else float(cost_usd or 0.0)
                self.counters.settle(reservation, actual)
        # After the commit, and only on counters that exist: a missing counter is seeded from the
        # database on its next read, which already includes this row. (A seed landing between the
        # commit and this increment counts the call twice: conservative, never under-counted.)
        if status in BILLABLE or cost_usd:
            self.counters.record_model_usage(workspace_id=ctx.workspace_id, run_id=ctx.run_id, purpose=purpose,
                                             tokens=int(input_tokens or 0) + int(output_tokens or 0), cost_usd=float(cost_usd or 0.0),
                                             billable=status in BILLABLE, capped=reservation is not None)

    def _insert(self, *, ctx: CallContext, purpose, profile, provider, model, status, attempt, latency_ms, input_tokens,
                output_tokens, cost_usd, request_hash, error, tokens_saved, request, response, answered_by, cost_source,
                escalated_from, escalation_reason) -> None:
        with session_scope() as s:
            s.add(ModelCall(workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=ctx.task_id, agent_id=ctx.agent_id,
                            purpose=purpose, profile=profile, provider=provider, model=model, prompt_version=ctx.prompt_version,
                            status=status, attempt=attempt, latency_ms=latency_ms, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=cost_usd, request_hash=request_hash, error=error,
                            tokens_saved=tokens_saved, request_ref=_store_payload(s, "request", request),
                            response_ref=_store_payload(s, "response", response),
                            answered_by=answered_by or ("rules" if status == "skipped" else "llm_large"),
                            cost_source=cost_source,
                            cached_input_tokens=cached_prompt_tokens((response or {}).get("usage")),
                            context_receipts=ctx.context_receipts or None, escalated_from=escalated_from,
                            escalation_reason=escalation_reason[:200] if escalation_reason else None))
            if ctx.run_id and (input_tokens or output_tokens or cost_usd):
                s.execute(update(AnalysisRun).where(AnalysisRun.id == ctx.run_id).values(
                    tokens=AnalysisRun.tokens + input_tokens + output_tokens, cost_usd=AnalysisRun.cost_usd + cost_usd))

    # ------------------------------------------------------------------ hard spend caps
    def _workspace_cap(self, workspace_id: str) -> float | None:
        now = time.monotonic()
        with self._lock:
            hit = self._ws_caps.get(workspace_id)
            if hit and now - hit[0] < 5.0:
                return hit[1]
        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if ws is None:
                return None
            limit = float(load_policy(s, ws).workspace_monthly_cost_budget_usd)
        with self._lock:
            self._ws_caps[workspace_id] = (now, limit)
        return limit

    def cap_specs(self, workspace_id: str | None) -> list[CapSpec]:
        """The caps a billable call of this workspace must fit under (platform day; workspace month)."""
        from analystos.services.platform_settings import get as platform

        c = self.counters
        now = c.clock()

        def seed_day() -> float:
            with session_scope() as s:
                return day_cost_sql(s, now)

        caps = [CapSpec("platform_daily", c.platform_day_key(), platform().llm.daily_spend_cap_usd, DAY_TTL_SECONDS,
                        seed_day, next_day_start(now))]
        if workspace_id:
            def seed_month() -> float:
                with session_scope() as s:
                    return _month_cost_sql(s, workspace_id, now)

            caps.append(CapSpec("workspace_monthly", c.workspace_month_key(workspace_id), self._workspace_cap(workspace_id),
                                MONTH_TTL_SECONDS, seed_month, next_month_start(now)))
        return caps

    def reserve(self, *, ctx: CallContext, purpose: str, model: str, estimate_usd: float) -> SpendReservation:
        """Reserve this call's estimated cost under every cap, atomically, or refuse it visibly."""
        caps = self.cap_specs(ctx.workspace_id)
        try:
            reservation = self.counters.reserve(caps, estimate_usd)
        except CapExceeded as exc:
            cap = exc.cap
            details = {"cap": cap.name, "spent_usd": round(exc.spent, 6), "limit_usd": cap.limit,
                       "estimate_usd": round(exc.estimate, 6), "purpose": purpose, "model": model,
                       "resets_at": cap.resets_at.isoformat() if cap.resets_at else None, "remedy": CAP_REMEDY[cap.name]}
            self._cap_reached(ctx, cap, details)
            raise SpendCapReached(f"{cap.name.replace('_', ' ')} model spend cap ${cap.limit:.2f} reached "
                                  f"(spent ${exc.spent:.4f}, this call needs up to ${exc.estimate:.4f}); {purpose} was not "
                                  f"sent to {model}", details=details) from None
        except CountersUnavailable as exc:
            if getattr(self.counters, "spend_store", "redis") == "postgres":
                raise SpendCountersUnavailable(
                    f"spend counters unavailable ({exc}); billable model calls are paused until the spend_counter table "
                    "is reachable (fail closed)",
                    details={"purpose": purpose, "model": model, "store": "postgres",
                             "remedy": "Check Postgres and run `analystos migrate`; deterministic paths continue meanwhile."}
                ) from None
            raise SpendCountersUnavailable(
                f"spend counters unavailable ({exc}); billable model calls are paused until Redis is back (fail closed)",
                details={"purpose": purpose, "model": model,
                         "remedy": "Restore Redis (ANALYSTOS_REDIS_URL); deterministic paths continue meanwhile."}) from None
        self._alert(ctx, reservation)
        return reservation

    def _admins_notify(self, s, workspace_id: str, *, title: str, body: str) -> None:
        from analystos.services.notifications import notify

        for admin in s.scalars(select(User).where(User.is_admin.is_(True))):
            notify(s, workspace_id, kind="budget", title=title, body=body, link={"type": "admin", "id": "models"},
                   user_id=admin.id)

    def _alert(self, ctx: CallContext, reservation: SpendReservation) -> None:
        """Once per cap period: event `budget.warning` and an admin notification at the alert fraction."""
        from analystos.events.bus import emit
        from analystos.services.platform_settings import get as platform

        fraction = platform().llm.spend_alert_fraction
        for cap in reservation.caps:
            spent = reservation.after.get(cap.name)
            if not cap.limit or spent is None or spent < fraction * cap.limit:
                continue
            if not self.counters.mark_once(cap.key + ":alert", cap.ttl):
                continue
            workspace = ctx.workspace_id or "platform"
            payload = {"cap": cap.name, "spent_usd": round(spent, 4), "limit_usd": cap.limit, "fraction": fraction,
                       "resets_at": cap.resets_at.isoformat() if cap.resets_at else None}
            try:
                with session_scope() as s:
                    emit(workspace, "budget.warning", payload, session=s)
                    self._admins_notify(s, workspace, title=(
                        f"Model spend at {spent / cap.limit:.0%} of the {cap.name.replace('_', ' ')} cap "
                        f"(${spent:.2f} of ${cap.limit:.2f})"), body=CAP_REMEDY[cap.name])
            except Exception as exc:  # an alert must never fail the call it rides on
                log.warning("spend alert not delivered: %s", exc)

    def _cap_reached(self, ctx: CallContext, cap: CapSpec, details: dict[str, Any]) -> None:
        from analystos.events.bus import emit

        if not self.counters.mark_once(cap.key + ":reached", cap.ttl):
            return
        workspace = ctx.workspace_id or "platform"
        try:
            with session_scope() as s:
                emit(workspace, "budget.cap_reached", {k: v for k, v in details.items() if k != "remedy"}, session=s)
                self._admins_notify(s, workspace, title=(
                    f"Model calls refused: {cap.name.replace('_', ' ')} spend cap ${cap.limit:.2f} reached"),
                    body=CAP_REMEDY[cap.name])
        except Exception as exc:
            log.warning("spend cap notification not delivered: %s", exc)

    def note_cooldown(self, provider: str, seconds: float, reason: str) -> None:
        self.counters.note_cooldown(provider, seconds, reason)

    # ------------------------------------------------------------------ budgets
    def _run_usage(self, s, run_id: str) -> tuple[float, float] | None:
        c = self.counters
        run: list[AnalysisRun | None] = []

        def row() -> AnalysisRun | None:
            if not run:
                run.append(s.get(AnalysisRun, run_id))
            return run[0]

        tokens = c.read(c.run_key(run_id, "tokens"), lambda: row().tokens if row() else 0, RUN_TTL_SECONDS)
        usd = c.read(c.run_key(run_id, "usd"), lambda: row().cost_usd if row() else 0.0, RUN_TTL_SECONDS)
        if tokens is None or usd is None:
            if row() is None:
                return None
            return float(row().tokens), float(row().cost_usd)
        return tokens, usd

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        if not ctx.workspace_id:
            return
        c = self.counters
        with session_scope() as s:
            ws = s.get(Workspace, ctx.workspace_id)
            if ws is None:
                return
            policy = load_policy(s, ws)
            if ctx.run_id:
                usage = self._run_usage(s, ctx.run_id)
                if usage is not None:
                    tokens, usd = usage
                    from analystos.autonomy.budget import run_limits

                    goal_cap = run_limits(s, ctx.run_id).get("max_cost_usd")  # a goal may only tighten (S06 §3.4)
                    if goal_cap is not None and usd >= float(goal_cap) and float(goal_cap) < policy.run_cost_budget_usd:
                        raise BudgetExceeded(f"run cost budget ${float(goal_cap):.2f} exhausted (goal limit)",
                                             details={"cap": "max_cost_usd", "used": usd, "limit": goal_cap})
                    if usd >= policy.run_cost_budget_usd:
                        raise BudgetExceeded(f"run cost budget ${policy.run_cost_budget_usd:.2f} exhausted")
                    if tokens >= policy.run_token_budget:
                        raise BudgetExceeded(f"run token budget {policy.run_token_budget} exhausted")
                self._check_purpose_caps(s, ctx.run_id, purpose)
            month_cost = c.read(c.workspace_month_key(ctx.workspace_id),
                                lambda: _month_cost_sql(s, ctx.workspace_id, c.clock()), MONTH_TTL_SECONDS)
            if month_cost is None:
                month_cost = _month_cost_sql(s, ctx.workspace_id, c.clock())
            if month_cost >= policy.workspace_monthly_cost_budget_usd:
                raise BudgetExceeded(f"workspace monthly model budget ${policy.workspace_monthly_cost_budget_usd:.2f} exhausted")

    def _check_purpose_caps(self, s, run_id: str, purpose: str) -> None:
        from analystos.services.platform_settings import get as platform

        caps = platform().llm.purpose_run_caps.get(purpose) or {}
        c = self.counters
        for what, limit in caps.items():
            if limit is None:
                continue
            used = c.read(c.purpose_key(run_id, purpose, what), lambda what=what: _purpose_sql(s, run_id, purpose, what),
                          RUN_TTL_SECONDS)
            if used is None:
                used = _purpose_sql(s, run_id, purpose, what)
            if used >= limit:
                raise BudgetExceeded(f"per-run cap for {purpose} exhausted: {what} {used:g} of {limit:g}",
                                     details={"purpose": purpose, "cap": what, "used": used, "limit": limit})

    def remaining_fraction(self, ctx: CallContext) -> float:
        if not (ctx.workspace_id and ctx.run_id):
            return 1.0
        with session_scope() as s:
            ws = s.get(Workspace, ctx.workspace_id)
            if ws is None:
                return 1.0
            usage = self._run_usage(s, ctx.run_id)
            if usage is None:
                return 1.0
            tokens, usd = usage
            policy = load_policy(s, ws)
            spent = max(usd / policy.run_cost_budget_usd if policy.run_cost_budget_usd else 0,
                        tokens / policy.run_token_budget if policy.run_token_budget else 0)
            return max(0.0, 1.0 - spent)

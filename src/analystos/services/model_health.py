"""Model provider health for administrators (`GET /api/admin/models/health`).

Answers "why are no model calls happening?" without a live call: is the provider's key present in
THIS process's environment (never the key itself), when did a call last succeed, is the provider
cooling down (e.g. after HTTP 402, shared across processes through Redis), and how much of today's
platform spend cap is used. `probe=True` sends one tiny billable request (purpose `health_probe`,
through the router: allowlists, spend reservation and the call record all apply) to verify credits."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.core.config import secret_env
from analystos.core.errors import AnalystOSError
from analystos.db.models import ModelCall
from analystos.llm import router as router_module
from analystos.llm.config import load_models_config
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.budget_counters import BudgetCounters, day_start, next_day_start
from analystos.runtime.usage import day_cost_sql

PROBE_PURPOSE = "health_probe"
LOOKBACK = timedelta(days=30)


def _missing_key_message(env: str) -> str:
    return f"No API key in this process — set {env} for the api and worker containers and restart them."


def _probe(router: ModelRouter, provider: str, counters: BudgetCounters) -> dict[str, Any]:
    cfg = router.config
    profile = cfg.profiles.get(cfg.routing.get(PROBE_PURPOSE, ""))
    if profile is None or profile.provider != provider:
        return {"probed": False, "detail": f"no probe route for provider {provider} (purpose {PROBE_PURPOSE} uses "
                                           f"{profile.provider if profile else 'nothing'})"}
    # An explicit probe is how an administrator checks that topped-up credits work: it goes out even
    # while the provider cools down, and a success ends the cooldown.
    router_module._PROVIDER_COOLDOWN.pop(provider, None)
    started = time.perf_counter()
    try:
        r = router.complete(PROBE_PURPOSE, [{"role": "user", "content": "Reply with the single word OK."}],
                            ctx=CallContext(agent_id="admin.health_probe"), max_tokens=5)
    except AnalystOSError as exc:
        return {"probed": True, "ok": False, "code": exc.code, "error": exc.message[:300],
                "latency_ms": round((time.perf_counter() - started) * 1000),
                **({"remedy": exc.details["remedy"]} if isinstance(exc.details, dict) and exc.details.get("remedy") else {})}
    counters.clear_cooldown(provider)
    return {"probed": True, "ok": True, "model": r.model, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}


def health(session: Session, *, probe: bool = False, router: ModelRouter | None = None,
           counters: BudgetCounters | None = None) -> dict[str, Any]:
    from analystos.runtime import budget_counters
    from analystos.runtime.context import default_router
    from analystos.services.platform_settings import get as platform

    cfg = load_models_config()
    counters = counters or budget_counters.default_budget_counters()
    now = counters.clock()
    llm = platform().llm
    spent = counters.value(counters.platform_day_key())
    source = "counter"
    if spent is None:
        spent, source = day_cost_sql(session, now), "database"
    since = now - LOOKBACK
    last_ok = dict(session.execute(select(ModelCall.provider, func.max(ModelCall.created_at))
                                   .where(ModelCall.created_at >= since, ModelCall.status == "ok")
                                   .group_by(ModelCall.provider)).all())
    today = dict(session.execute(select(ModelCall.provider, func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
                                 .where(ModelCall.created_at >= day_start(now)).group_by(ModelCall.provider)).all())
    calls_today = dict(session.execute(select(ModelCall.provider, func.count())
                                       .where(ModelCall.created_at >= day_start(now), ModelCall.status.in_(("ok", "error")))
                                       .group_by(ModelCall.provider)).all())
    providers = []
    for name, p in cfg.providers.items():
        key_present = p.api_key_env is None or bool(secret_env(p.api_key_env))
        last_error = session.execute(select(ModelCall.created_at, ModelCall.model, ModelCall.error)
                                     .where(ModelCall.created_at >= since, ModelCall.provider == name,
                                            ModelCall.status == "error")
                                     .order_by(ModelCall.created_at.desc()).limit(1)).first()
        local_until = router_module._PROVIDER_COOLDOWN.get(name)
        local_left = max(0.0, local_until - time.monotonic()) if local_until else 0.0
        shared = counters.cooldown(name)
        cooldown = None
        if local_left > 0 or shared:
            cooldown = {"remaining_seconds": round(max(local_left, shared[0] if shared else 0.0)),
                        "reason": shared[1] if shared else "provider refused for credits (HTTP 402) in this process"}
        ok_at = last_ok.get(name)
        entry: dict[str, Any] = {
            "provider": name, "type": p.type, "kind": p.kind, "base_url": p.url(), "api_key_env": p.api_key_env,
            "key_required": p.api_key_env is not None, "key_present": key_present,
            "last_success_at": ok_at.isoformat() if ok_at else None,
            "last_error": {"at": last_error[0].isoformat(), "model": last_error[1], "error": (last_error[2] or "")[:300]}
            if last_error else None,
            "cooldown": cooldown, "spend_today_usd": round(float(today.get(name) or 0.0), 6),
            "calls_today": int(calls_today.get(name) or 0),
            "message": _missing_key_message(p.api_key_env) if not key_present and p.api_key_env else None,
        }
        providers.append(entry)
    if probe:
        router = router or default_router()
        for entry in providers:
            if not entry["key_present"]:
                entry["probe"] = {"probed": False, "ok": False, "detail": entry["message"]}
            else:
                entry["probe"] = _probe(router, entry["provider"], counters)
    cap = llm.daily_spend_cap_usd
    return {
        "checked_at": now.isoformat(),
        "counters_available": counters.available,
        # Hard caps can be proven (Redis, or Postgres in the lite profile); false = billable calls are refused.
        "spend_counters": {"store": counters.spend_store, "available": counters.spend_available},
        "spend_today": {"usd": round(float(spent), 6), "cap_usd": cap, "source": source,
                        "fraction": round(float(spent) / cap, 4) if cap else None,
                        "alert_fraction": llm.spend_alert_fraction, "resets_at": next_day_start(now).isoformat()},
        "providers": providers,
    }


STATS_WINDOW = timedelta(hours=24)


def _provider_stats(session: Session, since: Any) -> dict[str, dict[str, Any]]:
    """Per provider over the window: answered calls, error rate, latency p50/p95 of successful calls
    (aggregated in the database; no rows are held)."""
    status = select(ModelCall.provider, func.count().label("calls"),
                    func.count().filter(ModelCall.status == "error").label("errors"))         .where(ModelCall.created_at >= since, ModelCall.status.in_(("ok", "error"))).group_by(ModelCall.provider)
    out: dict[str, dict[str, Any]] = {}
    for provider, calls, errors in session.execute(status).all():
        out[provider] = {"calls": int(calls), "errors": int(errors),
                         "error_rate": round(int(errors) / int(calls), 4) if calls else None,
                         "latency_ms_p50": None, "latency_ms_p95": None}
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        latency = select(ModelCall.provider,
                         func.percentile_cont(0.5).within_group(ModelCall.latency_ms),
                         func.percentile_cont(0.95).within_group(ModelCall.latency_ms))             .where(ModelCall.created_at >= since, ModelCall.status == "ok").group_by(ModelCall.provider)
        for provider, p50, p95 in session.execute(latency).all():
            entry = out.setdefault(provider, {"calls": 0, "errors": 0, "error_rate": None})
            entry["latency_ms_p50"] = round(float(p50)) if p50 is not None else None
            entry["latency_ms_p95"] = round(float(p95)) if p95 is not None else None
    return out


def providers_view(session: Session) -> dict[str, Any]:
    """Providers for `GET /api/admin/models/providers`: configuration (no secrets: the key is named by
    its environment variable only), the profiles that use each one, and health."""
    cfg = load_models_config()
    report = health(session)
    stats = _provider_stats(session, report_time(report) - STATS_WINDOW)
    out = []
    for entry in report["providers"]:
        name = entry["provider"]
        p = cfg.providers[name]
        out.append({**entry, "egress": p.egress, "region": p.region, "base_url_env": p.base_url_env,
                    "capabilities": p.capabilities.model_dump(exclude_none=True),
                    "models": sorted(p.model_map) or None,
                    "profiles": sorted(n for n, prof in cfg.profiles.items() if prof.provider == name),
                    "stats_24h": stats.get(name, {"calls": 0, "errors": 0, "error_rate": None,
                                                  "latency_ms_p50": None, "latency_ms_p95": None})})
    return {"checked_at": report["checked_at"], "spend_today": report["spend_today"], "providers": out}


def report_time(report: dict[str, Any]) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(report["checked_at"])


TEST_PROMPT = "Reply with the single word OK."


def test_provider(provider_id: str, *, router: ModelRouter | None = None) -> dict[str, Any]:
    """`POST /api/admin/models/providers/{id}/test`: one live ping with a fixed prompt through the router
    (purpose `health_probe` routed to a chat profile of this provider), so allowlists, spend caps and the
    model_call record apply as to any call."""
    from analystos.core.errors import NotFound
    from analystos.runtime.context import default_router
    from analystos.services.platform_settings import get as platform

    base = router or default_router()
    cfg = base.config
    provider = cfg.providers.get(provider_id)
    if provider is None:
        raise NotFound(f"provider {provider_id} is not configured")
    if provider.kind == "decision":
        return {"provider": provider_id, "probed": False,
                "detail": "a decision provider answers typed questions only; its purposes show in GET /api/admin/models"}
    profiles = [n for n, p in cfg.profiles.items() if p.provider == provider_id and cfg.model_rung(n) != "decision"]
    if not profiles:
        return {"provider": provider_id, "probed": False, "detail": f"no chat profile uses provider {provider_id}"}
    probe_profile = cfg.routing.get(PROBE_PURPOSE)
    profile = probe_profile if probe_profile in profiles else profiles[0]
    settings = platform()
    llm = settings.llm.model_copy(update={"routing_overrides": {**settings.llm.routing_overrides, PROBE_PURPOSE: profile},
                                          "purpose_modes": {**settings.llm.purpose_modes, PROBE_PURPOSE: "always"},
                                          "ladders": {k: v for k, v in settings.llm.ladders.items() if k != PROBE_PURPOSE}})
    patched = settings.model_copy(update={"llm": llm})
    pinger = ModelRouter(cfg, sink=base.sink, transport=base.transport, api_key_lookup=base.api_key_lookup, max_retries=0,
                         settings_provider=lambda: patched, cache=base.cache, air_gapped=base.air_gapped)
    started = time.perf_counter()
    try:
        r = pinger.complete(PROBE_PURPOSE, [{"role": "user", "content": TEST_PROMPT}],
                            ctx=CallContext(agent_id="admin.provider_test"), max_tokens=5)
    except AnalystOSError as exc:
        return {"provider": provider_id, "profile": profile, "probed": True, "ok": False, "code": exc.code,
                "error": exc.message[:300], "latency_ms": round((time.perf_counter() - started) * 1000)}
    return {"provider": provider_id, "profile": profile, "probed": True, "ok": True, "model": r.model,
            "latency_ms": r.latency_ms, "cost_usd": r.cost_usd, "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens, "answer": (r.text or "")[:40]}


__all__ = ["PROBE_PURPOSE", "health", "providers_view", "test_provider"]

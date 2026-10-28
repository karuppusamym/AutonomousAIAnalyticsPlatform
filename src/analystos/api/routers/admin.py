from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import rows
from analystos.context import service as ctx_svc
from analystos.contracts.registry import AgentSpec, SkillSpec, ToolSpec
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import (
    AgentDefinition,
    AuditEvent,
    ContextEntry,
    KnowledgeDocument,
    ModelCall,
    QueryExecution,
    SkillDefinition,
    ToolDefinition,
    User,
)
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role
from analystos.llm.config import load_models_config
from analystos.tools.registry import list_tools

router = APIRouter(prefix="/api", tags=["admin"])


class SearchIn(BaseModel):
    workspace_id: str
    query: str
    limit: int = 8


class ContextIn(BaseModel):
    kind: str = "term"
    name: str
    body: str
    synonyms: list[str] = []
    mapped_columns: list[str] = []


class EnabledPatch(BaseModel):
    enabled: bool | None = None
    spec: dict | None = None


@router.post("/context/search")
def context_search(body: SearchIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, body.workspace_id, "viewer")
    return ctx_svc.search(session, body.workspace_id, body.query, limit=min(body.limit, 50))


@router.get("/context/entities/{entry_id}")
def context_entity(entry_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.knowledge.entries import get_entry

    e = session.get(ContextEntry, entry_id)
    doc = session.get(KnowledgeDocument, entry_id) if e is None else None
    owner = e.workspace_id if e is not None else (doc.workspace_id if doc is not None else None)
    if e is None and doc is None:
        raise NotFound("context entry not found")
    if owner:  # a workspace's entry or document: the caller's role there decides (404 otherwise)
        load_in_workspace(session, ContextEntry if e is not None else KnowledgeDocument, entry_id, user=user,
                          label="context entry")
    view = get_entry(session, owner, entry_id) if owner else None
    if view is None:  # a platform-pack document: visible to every workspace
        from analystos.knowledge.entries import doc_entry

        view = doc_entry(doc, "platform")
    return {**view.as_dict(), "workspace_id": owner}


@router.post("/workspaces/{workspace_id}/context")
def add_context(workspace_id: str, body: ContextIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    e = ctx_svc.add_entry(session, workspace_id=workspace_id, **body.model_dump(), origin="user")
    return {"id": e.id}


@router.get("/workspaces/{workspace_id}/context")
def list_context(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.knowledge.entries import visible_entries

    require_role(session, user, workspace_id, "viewer")
    return [e.as_dict() for e in sorted(visible_entries(session, workspace_id, exclude_kinds=["episode"]),
                                        key=lambda e: (e.kind, e.name, e.id))]


@router.get("/tools")
def tools(_: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return list_tools(session)


@router.post("/tools")
def register_tool(body: dict, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    spec = ToolSpec.model_validate(body)
    session.merge(ToolDefinition(id=spec.tool_id, spec=spec.model_dump(), enabled=False))
    audit(f"user:{admin.id}", "tool.registered", target=spec.tool_id, session=session)
    return {"tool_id": spec.tool_id, "enabled": False, "note": "new tools start disabled; enable after review"}


@router.patch("/tools/{tool_id}")
def patch_tool(tool_id: str, body: EnabledPatch, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    t = session.get(ToolDefinition, tool_id)
    if t is None:
        raise NotFound("tool not found")
    if body.enabled is not None:
        t.enabled = body.enabled
    if body.spec:
        t.spec = ToolSpec.model_validate({**t.spec, **body.spec}).model_dump()
    audit(f"user:{admin.id}", "tool.updated", target=tool_id, details=body.model_dump(), session=session)
    return {**t.spec, "enabled": t.enabled}


_MANIFEST_ONLY = ("an agent's contract is its kind: Agent capability manifest (config/agents/*.yaml or a pack); "
                  "edit or add the manifest and POST /api/admin/capabilities/reload")


def _agent_view(spec: AgentSpec, row: AgentDefinition | None) -> dict:
    """What the admin API shows for an agent (FND-006): the manifest-derived AgentSpec, plus the row's
    platform-wide `enabled` switch (no row = enabled, as the runtime treats it)."""
    return {**spec.model_dump(mode="json", by_alias=True), "enabled": row.enabled if row is not None else True}


@router.get("/agents")
def agents(_: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Every registered agent, derived from its current manifest (never from the cached row spec).
    A row left from an agent whose manifest is gone is listed as `source: orphan` (it cannot run)."""
    from analystos.capabilities import registry
    from analystos.capabilities.agents import to_agent_spec

    rows = {a.id: a for a in session.scalars(select(AgentDefinition))}
    specs = {s.id: s for s in (to_agent_spec(m) for m in registry.current().list("Agent"))}
    out = [_agent_view(specs[i], rows.get(i)) for i in sorted(specs)]
    for i in sorted(set(rows) - set(specs)):
        out.append({**AgentSpec.model_validate(rows[i].spec).model_dump(mode="json", by_alias=True), "source": "orphan",
                    "enabled": rows[i].enabled})
    return out


@router.post("/agents")
def register_agent(body: dict, admin: User = Depends(admin_user)):
    """Refused: an AgentSpec is a view of a manifest, so registering one on its own would fork the contract."""
    raise InvalidInput(_MANIFEST_ONLY, details={"reason": "manifest_only"})


@router.patch("/agents/{agent_id}")
def patch_agent(agent_id: str, body: EnabledPatch, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    """Only the platform-wide `enabled` switch is editable here; the contract comes from the manifest."""
    from analystos.capabilities.agents import contract_for

    if body.spec:
        raise InvalidInput(_MANIFEST_ONLY, details={"reason": "manifest_only"})
    spec = contract_for(None, agent_id)
    a = session.get(AgentDefinition, agent_id)
    if spec is None:
        raise NotFound("agent not found")
    if a is None:  # a pack agent has no row until an admin sets its switch
        a = AgentDefinition(id=agent_id, version=spec.version, spec=spec.model_dump(mode="json", by_alias=True), enabled=True)
        session.add(a)
    if body.enabled is not None:
        a.enabled = body.enabled
    audit(f"user:{admin.id}", "agent.updated", target=agent_id, details=body.model_dump(), session=session)
    return _agent_view(spec, a)


@router.get("/skills")
def skills(_: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [{**s.spec, **SkillSpec.model_validate(s.spec).model_dump(), "enabled": s.enabled}
            for s in session.scalars(select(SkillDefinition).order_by(SkillDefinition.id))]


@router.get("/admin/models")
def models(_: User = Depends(current_user)):
    """Effective routing after admin overrides: purpose -> mode, profile, candidate models, availability."""
    from analystos.contracts.platform import DETERMINISTIC_CAPABLE
    from analystos.llm.router import CallContext
    from analystos.runtime.context import default_router

    cfg = load_models_config()
    router_ = default_router()
    effective = {}
    for purpose in cfg.routing:
        profile, profile_cfg, models = router_.candidates(purpose, CallContext())
        unavailable = router_.unavailable(purpose)
        effective[purpose] = {"profile": profile, "models": models, "mode": router_.mode(purpose), "ladder": router_.ladder(purpose),
                              "available": router_.available(purpose), "deterministic_path": purpose in DETERMINISTIC_CAPABLE,
                              "unavailable_reason": unavailable.message if unavailable else None,
                              "unavailable_code": unavailable.code if unavailable else None,
                              "decision_model": cfg.profiles[cfg.routing[purpose]].provider == "typesafe",
                              "escalation": router_.escalation_policy(purpose),
                              "escalation_models": router_.escalation_tier(purpose, CallContext(), profile, profile_cfg, models)}
    return {**cfg.public_view(), "effective": effective, "available": {p: v["available"] for p, v in effective.items()}}


@router.get("/admin/models/health")
def models_health(probe: bool = False, _: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    """Per provider: key present in this process (never the key), last success, cooldown, today's spend vs
    the daily cap. `?probe=1` sends one tiny billable request per provider to verify credits."""
    from analystos.services.model_health import health

    return health(session, probe=probe)


class SettingsPatch(BaseModel):
    patch: dict
    note: str = ""
    switch_override_reason: str | None = None  # S05-T5: a platform admin's reason to activate a blocked model switch


class PresetIn(BaseModel):
    preset: str
    switch_override_reason: str | None = None


class RollbackIn(BaseModel):
    version: int
    switch_override_reason: str | None = None


@router.get("/admin/settings")
def get_settings_doc(_: User = Depends(admin_user)):
    from analystos.contracts.platform import PRESETS, PlatformSettings
    from analystos.services import platform_settings

    version, settings = platform_settings.current()
    return {"version": version, "settings": settings.model_dump(), "defaults": PlatformSettings().model_dump(),
            "presets": {k: v for k, v in PRESETS.items()}, "schema": PlatformSettings.model_json_schema()}


@router.put("/admin/settings")
def put_settings(body: SettingsPatch, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    from analystos.services import platform_settings

    return platform_settings.update(session, session.merge(admin), body.patch, note=body.note,
                                    gate=platform_settings.SwitchGate(body.switch_override_reason))


@router.post("/admin/settings/preset")
def preset(body: PresetIn, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    from analystos.services import platform_settings

    return platform_settings.apply_preset(session, session.merge(admin), body.preset,
                                          gate=platform_settings.SwitchGate(body.switch_override_reason))


@router.get("/admin/settings/history")
def settings_history(_: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    from analystos.services import platform_settings

    return platform_settings.history(session)


@router.post("/admin/settings/rollback")
def settings_rollback(body: RollbackIn, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    from analystos.services import platform_settings

    return platform_settings.rollback(session, session.merge(admin), body.version,
                                      gate=platform_settings.SwitchGate(body.switch_override_reason))


@router.get("/admin/prompts")
def prompts(_: User = Depends(admin_user)):
    from analystos.agents.prompts import PROMPTS

    return [{"name": k, "version": k.rsplit(".", 1)[-1], "text": v} for k, v in sorted(PROMPTS.items())]


@router.get("/admin/token-savings")
def token_savings(days: int = 30, _: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    """Tokens spent vs avoided (cache hits, deterministic skips, refused oversize prompts)."""
    # By purpose and by the ladder rung that answered (P4-T01), plus calls whose cost is unknown (P4-T07).
    from datetime import timedelta

    from analystos.core.ids import utcnow
    from analystos.llm.config import load_models_config

    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    rows = session.execute(select(ModelCall.purpose, ModelCall.status, ModelCall.answered_by, func.count(),
                                  func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0),
                                  func.coalesce(func.sum(ModelCall.tokens_saved), 0), func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
                           .where(ModelCall.created_at >= since)
                           .group_by(ModelCall.purpose, ModelCall.status, ModelCall.answered_by)).all()

    def bucket() -> dict:
        return {"calls": 0, "answered": 0, "tokens_used": 0, "tokens_saved": 0, "cost_usd": 0.0}

    # Cheap first, escalate: calls the large tier made because a small-tier answer failed validation,
    # by purpose and reason, and the small-tier calls whose answers were rejected (the cost of escalating).
    escalations: dict[str, dict] = {}
    for purpose, model, reason, n, cost in session.execute(
            select(ModelCall.purpose, ModelCall.model, ModelCall.escalation_reason, func.count(),
                   func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
            .where(ModelCall.created_at >= since, ModelCall.escalated_from.is_not(None), ModelCall.status.in_(("ok", "error")))
            .group_by(ModelCall.purpose, ModelCall.model, ModelCall.escalation_reason)).all():
        e = escalations.setdefault(purpose, {"calls": 0, "cost_usd": 0.0, "by_model": {}, "by_reason": {}})
        e["calls"] += n
        e["cost_usd"] += float(cost)
        e["by_model"][model] = e["by_model"].get(model, 0) + n
        kind = (reason or "unknown").split(":", 1)[0]
        e["by_reason"][kind] = e["by_reason"].get(kind, 0) + n

    def add(b: dict, status: str, n: int, used: int, saved: int, cost: float) -> None:
        b["calls"] += n if status in ("ok", "error") else 0  # requests sent to a provider
        b["answered"] += n if status != "error" else 0  # the rung produced the answer
        b["tokens_used"] += int(used)
        b["tokens_saved"] += int(saved)
        b["cost_usd"] += float(cost)

    by_purpose: dict[str, dict] = {}
    by_rung: dict[str, dict] = {}
    totals = {"calls": 0, "tokens_used": 0, "tokens_saved": 0, "cost_usd": 0.0, "cache_hits": 0, "deterministic_skips": 0, "refused": 0}
    for purpose, status, rung, n, used, saved, cost in rows:
        rung = rung or "llm_large"
        p = by_purpose.setdefault(purpose, {**bucket(), "by_status": {}, "by_rung": {}})
        p["by_status"][status] = p["by_status"].get(status, 0) + n
        add(p, status, n, used, saved, cost)
        add(p["by_rung"].setdefault(rung, bucket()), status, n, used, saved, cost)
        add(by_rung.setdefault(rung, bucket()), status, n, used, saved, cost)
        totals["calls"] += n if status in ("ok", "error") else 0
        totals["tokens_used"] += int(used)
        totals["tokens_saved"] += int(saved)
        totals["cost_usd"] += float(cost)
        totals["cache_hits"] += n if status == "cache_hit" else 0
        totals["deterministic_skips"] += n if status == "skipped" else 0
        totals["refused"] += n if status == "refused" else 0
    # By model: spend of calls a provider served; avoided calls (skips) carry no model and stay out.
    by_model: dict[str, dict] = {}
    for model, status, n, used, saved, cost in session.execute(
            select(ModelCall.model, ModelCall.status, func.count(),
                   func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0),
                   func.coalesce(func.sum(ModelCall.tokens_saved), 0), func.coalesce(func.sum(ModelCall.cost_usd), 0.0))
            .where(ModelCall.created_at >= since, ModelCall.status.in_(("ok", "error", "cache_hit")))
            .group_by(ModelCall.model, ModelCall.status)).all():
        add(by_model.setdefault(model or "unknown", bucket()), status, n, used, saved, cost)
    denom = totals["tokens_used"] + totals["tokens_saved"]
    totals["saved_share"] = round(totals["tokens_saved"] / denom, 4) if denom else 0.0
    _provider_cache(session, since, totals, by_purpose, by_model)
    # A model with no price and no provider-reported cost: its spend is unknown, not $0.
    unpriced = session.execute(select(ModelCall.model, ModelCall.purpose, ModelCall.answered_by, func.count(), func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0))
                               .where(ModelCall.created_at >= since, ModelCall.cost_source == "missing_price")
                               .group_by(ModelCall.model, ModelCall.purpose, ModelCall.answered_by)).all()
    missing_models: dict[str, dict] = {}
    for model, purpose, rung, n, tokens in unpriced:
        entry = missing_models.setdefault(model, {"model": model, "calls": 0, "tokens": 0})
        entry["calls"] += n
        entry["tokens"] += int(tokens)
        # Mark every breakdown that includes unknown spend, so a row cannot imply a known $0.
        for group, key in ((by_model, model or "unknown"), (by_purpose, purpose), (by_rung, rung or "llm_large")):
            if key in group:
                group[key]["cost_complete"] = False
    missing = list(missing_models.values())
    totals["missing_price_calls"] = sum(m["calls"] for m in missing)
    totals["escalations"] = sum(e["calls"] for e in escalations.values())
    totals["escalation_cost_usd"] = round(sum(e["cost_usd"] for e in escalations.values()), 6)
    return {"days": days, "totals": totals, "by_purpose": by_purpose, "by_rung": by_rung, "by_model": by_model,
            "escalations": escalations, "missing_price": missing, "prices_version": load_models_config().prices_version,
            "cost_complete": not missing, "context_cache": _context_cache_view()}


def _provider_cache(session: Session, since: object, totals: dict, by_purpose: dict, by_model: dict) -> None:
    """Provider prompt caching (P4-T04): prompt tokens the provider served from its cache, their share
    of all prompt tokens sent, and the estimated USD they saved (input price × the provider's cache-read
    discount, `llm.config.cached_discount`). Unpriced models count tokens but add no USD estimate."""
    config = load_models_config()
    totals.update({"input_tokens": 0, "cached_input_tokens": 0, "cached_input_share": 0.0, "cached_input_saved_usd": 0.0})
    for group in (by_purpose, by_model):
        for row in group.values():
            row.update({"cached_input_tokens": 0, "cached_input_saved_usd": 0.0})
    for purpose, model, sent, cached in session.execute(
            select(ModelCall.purpose, ModelCall.model, func.coalesce(func.sum(ModelCall.input_tokens), 0),
                   func.coalesce(func.sum(ModelCall.cached_input_tokens), 0))
            .where(ModelCall.created_at >= since, ModelCall.status.in_(("ok", "error")))
            .group_by(ModelCall.purpose, ModelCall.model)).all():
        saved = config.cached_saving(model or "", int(cached)) if cached else 0.0
        totals["input_tokens"] += int(sent)
        totals["cached_input_tokens"] += int(cached)
        totals["cached_input_saved_usd"] += saved or 0.0
        for group, key in ((by_purpose, purpose), (by_model, model or "unknown")):
            if key in group:
                group[key]["cached_input_tokens"] += int(cached)
                group[key]["cached_input_saved_usd"] = round(group[key]["cached_input_saved_usd"] + (saved or 0.0), 6)
    totals["cached_input_saved_usd"] = round(totals["cached_input_saved_usd"], 6)
    if totals["input_tokens"]:
        totals["cached_input_share"] = round(totals["cached_input_tokens"] / totals["input_tokens"], 4)


def _context_cache_view() -> dict:
    """Context reuse (Stream B): compiled contexts and knowledge retrievals served from the shared
    context cache. Work avoided, not provider tokens: never counted in tokens_saved."""
    from analystos.context import cache as context_cache

    try:
        return {"shared": context_cache.store().shared, "by_kind": context_cache.stats()}
    except Exception:  # visibility only
        return {"shared": False, "by_kind": {}}


@router.get("/admin/usage")
def usage(_: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    by_model = session.execute(select(ModelCall.purpose, ModelCall.model, ModelCall.provider, func.count(), func.sum(ModelCall.cost_usd),
                                      func.avg(ModelCall.latency_ms), func.count().filter(ModelCall.status == "error"),
                                      func.count().filter(ModelCall.cost_source == "missing_price"))
                               .where(ModelCall.status.in_(("ok", "error")))  # skips/cache hits/refusals: see /admin/token-savings
                               .group_by(ModelCall.purpose, ModelCall.model, ModelCall.provider)).all()
    q = session.execute(select(QueryExecution.status, func.count(), func.avg(QueryExecution.duration_ms)).group_by(QueryExecution.status)).all()
    return {"models": [{"purpose": p, "model": m, "provider": pr, "calls": c, "cost_usd": float(cost or 0), "avg_latency_ms": float(lat or 0),
                        "failed": int(f or 0), "cost_complete": not missing, "missing_price_calls": int(missing)} for p, m, pr, c, cost, lat, f, missing in by_model],
            "queries": [{"status": s, "count": c, "avg_ms": float(a or 0)} for s, c, a in q]}


@router.get("/admin/audit")
def admin_audit(limit: int = 300, _: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    return rows(session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 2000))))

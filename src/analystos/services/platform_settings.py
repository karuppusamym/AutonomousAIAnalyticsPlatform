"""Admin control plane: the effective PlatformSettings document, versioned and audited.

Reads are cached for a few seconds so hot paths (the model router, the gateway) can consult
settings on every call; a write bumps the version and clears the cache in this process — other
processes pick it up within the TTL."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.platform import PRESET_ESCALATION, PRESET_PATCHES, PRESETS, PlatformSettings
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.db.base import session_scope
from analystos.db.models import PlatformSetting, User
from analystos.governance.audit import audit

_TTL = 5.0
_lock = Lock()
_cache: tuple[float, int, PlatformSettings] | None = None
_last_good: tuple[int, PlatformSettings] | None = None


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in (
            "purpose_modes", "ladders", "purpose_run_caps", "routing_overrides", "profile_models", "profile_escalation_models",
            "escalation") else v
    return out


def current() -> tuple[int, PlatformSettings]:
    global _cache, _last_good
    with _lock:
        if _cache and time.monotonic() - _cache[0] < _TTL:
            return _cache[1], _cache[2]
    try:
        with session_scope() as s:
            row = s.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
            version, doc = (row.version, row.document) if row else (0, {})
    except Exception:
        # Never fail open: a read error keeps the last-known-good document (disabled models stay
        # disabled, "off" stays off). Defaults only when nothing was ever loaded in this process.
        with _lock:
            if _last_good is not None:
                return _last_good
        version, doc = 0, {}
    settings = PlatformSettings.model_validate(_deep_merge(PlatformSettings().model_dump(), doc))
    with _lock:
        _cache = (time.monotonic(), version, settings)
        if version:
            _last_good = (version, settings)
    return version, settings


def get() -> PlatformSettings:
    return current()[1]


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _diff(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        return out
    return [] if a == b else [{"path": path, "from": a, "to": b}]


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise Forbidden("platform administrator only")


def _latest(session: Session) -> tuple[int, PlatformSettings]:
    """Uncached read inside the writer's transaction: a write must never merge onto a stale copy."""
    row = session.scalar(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(1))
    version, doc = (row.version, row.document) if row else (0, {})
    return version, PlatformSettings.model_validate(_deep_merge(PlatformSettings().model_dump(), doc))


class SwitchGate:
    """Ask the model-switch evaluation gate (S05-T5) before a save. The admin API passes one; internal callers
    (seeding, tests of other features) do not."""

    def __init__(self, override_reason: str | None = None, runner: Any = None) -> None:
        self.override_reason, self.runner = override_reason, runner

    def check(self, session: Session, user: User, before: PlatformSettings, after: PlatformSettings) -> dict[str, Any]:
        from analystos.llm.config import load_models_config
        from analystos.llm.layers import resolve
        from analystos.services import model_switch

        cfg = load_models_config()
        changes = model_switch.changed_primaries(resolve(cfg, before, []), resolve(cfg, after, []))
        return model_switch.check(session, user, changes, override_reason=self.override_reason, runner=self.runner)


def update(session: Session, user: User, patch: dict[str, Any], *, note: str = "",
           gate: SwitchGate | None = None) -> dict[str, Any]:
    """Deep-merge `patch` into the latest version. The maps purpose_modes, ladders, purpose_run_caps,
    routing_overrides, profile_models, profile_escalation_models and escalation are replaced wholesale
    (the only way to remove a key). With `gate`, a changed primary model must pass the model-switch gate."""
    _require_admin(user)
    version, before = _latest(session)
    merged = _deep_merge(before.model_dump(), patch)
    try:
        after = PlatformSettings.model_validate(merged)
    except Exception as exc:
        raise InvalidInput(f"invalid settings: {exc}") from exc
    _validate_references(after)
    changes = _diff(before.model_dump(), after.model_dump())
    if not changes:
        return {"version": version, "changes": []}
    gated = gate.check(session, user, before, after) if gate is not None else None
    new_version = version + 1  # a concurrent writer with the same base fails on the primary key instead of overwriting
    session.add(PlatformSetting(version=new_version, document=after.model_dump(), note=note[:500], created_by=user.id))
    audit(f"user:{user.id}", "platform.settings.updated", target=f"v{new_version}", decision="allow",
          details={"changes": changes[:100], "note": note}, session=session)
    session.flush()
    invalidate()
    return {"version": new_version, "changes": changes, **({"model_switch_gate": gated} if gated else {})}


def apply_preset(session: Session, user: User, preset: str, *, gate: SwitchGate | None = None) -> dict[str, Any]:
    if preset not in PRESETS:
        raise InvalidInput(f"preset must be one of {sorted(PRESETS)}")
    # A preset is expressed as modes; per-purpose ladder overrides would shadow it, so it clears them.
    patch = _deep_merge(PRESET_PATCHES.get(preset, {}), {"llm": {"purpose_modes": PRESETS[preset], "ladders": {},
                                                                 "escalation": PRESET_ESCALATION.get(preset, {})}})
    return update(session, user, patch, note=f"preset {preset}", gate=gate)


def history(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(PlatformSetting).order_by(PlatformSetting.version.desc()).limit(limit)))
    return [{"version": r.version, "note": r.note, "created_by": r.created_by, "created_at": r.created_at.isoformat()} for r in rows]


def rollback(session: Session, user: User, version: int, *, gate: SwitchGate | None = None) -> dict[str, Any]:
    _require_admin(user)
    row = session.get(PlatformSetting, version)
    if row is None:
        raise NotFound(f"settings version {version} not found")
    base = PlatformSettings().model_dump()
    # Replace wholesale: rolling back must also undo keys added after `version`.
    target_settings = PlatformSettings.model_validate(_deep_merge(base, row.document))
    _validate_references(target_settings)  # an old version must not revive models/purposes removed from models.yaml
    target = target_settings.model_dump()
    latest, current_settings = _latest(session)
    current_doc = current_settings.model_dump()
    gated = gate.check(session, user, current_settings, target_settings) if gate is not None else None
    new_version = latest + 1
    session.add(PlatformSetting(version=new_version, document=target, note=f"rollback to v{version}", created_by=user.id))
    audit(f"user:{user.id}", "platform.settings.rolled_back", target=f"v{new_version}", decision="allow",
          details={"to_version": version, "changes": _diff(current_doc, target)[:100]}, session=session)
    session.flush()
    invalidate()
    return {"version": new_version, "rolled_back_to": version, **({"model_switch_gate": gated} if gated else {})}


def _validate_references(settings: PlatformSettings) -> None:
    from analystos.llm.config import load_models_config

    cfg = load_models_config()
    for purpose, profile in settings.llm.routing_overrides.items():
        if profile not in cfg.profiles:
            raise InvalidInput(f"routing override {purpose} -> unknown profile {profile}")
    for profile, models in [*settings.llm.profile_models.items(), *settings.llm.profile_escalation_models.items()]:
        if profile not in cfg.profiles:
            raise InvalidInput(f"unknown profile {profile}")
        unknown = [m for m in models if m not in cfg.allowlist]
        if unknown:
            raise InvalidInput(f"models not on the platform allowlist: {', '.join(unknown)} (edit config/models.yaml to extend it)")
    for purpose in [*settings.llm.purpose_modes, *settings.llm.ladders, *settings.llm.purpose_run_caps, *settings.llm.escalation]:
        if purpose not in cfg.routing:
            raise InvalidInput(f"unknown model purpose {purpose}")
    for purpose, rungs in settings.llm.ladders.items():
        if not rungs or len(set(rungs)) != len(rungs):
            raise InvalidInput(f"ladder for {purpose} must list each rung once and not be empty")
    for purpose, caps in settings.llm.purpose_run_caps.items():
        unknown = set(caps) - {"calls", "tokens", "usd"}
        if unknown:
            raise InvalidInput(f"purpose_run_caps.{purpose}: unknown cap {', '.join(sorted(unknown))} (calls, tokens, usd)")

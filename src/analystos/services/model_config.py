"""Tenant and workspace model configuration overrides (spec S05-T3): versioned, audited, narrowing only.

The file and platform layers stay where they are (`config/models.yaml`, `services/platform_settings.py`).
This module stores the two lower layers as append-only versions in `model_config_override` and resolves
the four into one effective configuration (`analystos.llm.layers.resolve`).

Writes are refused when the document would widen the layer above (`InvalidInput` listing each
violation) or when `If-Match` names a version other than the latest (`PreconditionFailed`). A rollback
writes the old document as a new version, re-checked against today's upper layers.

The router reads a workspace's effective layers through `narrowing_for`, cached for a few seconds in a
bounded map. That read is a deliberate, keyed cross-tenant lookup (`system_scope`): a worker without a
bound tenant must still apply the workspace's narrowing. A read error keeps the last-known-good value;
with none, the call fails closed (ModelRouteUnavailable)."""
from __future__ import annotations

import time
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, Forbidden, InvalidInput, ModelRouteUnavailable, NotFound, PreconditionFailed
from analystos.core.ids import new_id
from analystos.db.models import ModelConfigOverride, TenantMember, User, Workspace
from analystos.governance.audit import audit
from analystos.llm.layers import EffectiveModels, ModelOverride, resolve

SCOPES = ("tenant", "workspace")
TENANT_ADMIN_ROLE = "tenant_admin"
_TTL = 5.0
_MAX_ENTRIES = 2048
_lock = Lock()
_cache: dict[str, tuple[float, EffectiveModels]] = {}
_last_good: dict[str, EffectiveModels] = {}


def scope_key(scope: str, tenant_id: str, workspace_id: str | None) -> str:
    if scope == "tenant":
        return f"tenant:{tenant_id}"
    if not workspace_id:
        raise InvalidInput("scope=workspace needs a workspace id")
    return f"workspace:{workspace_id}"


def _latest_row(session: Session, key: str) -> ModelConfigOverride | None:
    return session.scalar(select(ModelConfigOverride).where(ModelConfigOverride.scope_key == key)
                          .order_by(ModelConfigOverride.version.desc()).limit(1))


def _parse(document: dict[str, Any] | None) -> ModelOverride | None:
    if document is None:
        return None
    try:
        return ModelOverride.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, or our own ValueError from a widening key
        raise InvalidInput(f"invalid model configuration override: {exc}") from exc


def _layers(session: Session, tenant_id: str, workspace_id: str | None, *, upto: str = "workspace",
            replace: tuple[str, ModelOverride] | None = None) -> list[tuple[str, int, ModelOverride | None]]:
    """(layer, version, override) for the tenant and (when given) the workspace, in order. `upto` stops
    after that layer; `replace` substitutes a candidate document for one layer (write validation)."""
    out: list[tuple[str, int, ModelOverride | None]] = []
    for layer in SCOPES:
        if layer == "workspace" and not workspace_id:
            break
        row = _latest_row(session, scope_key(layer, tenant_id, workspace_id))
        doc = _stored(row)
        version = row.version if row else 0
        if replace is not None and replace[0] == layer:
            doc = replace[1]
        out.append((layer, version, doc))
        if layer == upto:
            break
    return out


def _stored(row: ModelConfigOverride | None) -> ModelOverride | None:
    """A stored document parses (it was validated on write); if a later contract change broke it, the
    layer narrows everything rather than nothing."""
    if row is None:
        return None
    try:
        return ModelOverride.model_validate(row.document)
    except Exception:
        return ModelOverride(allowlist=[])


def _upper(config: Any = None, platform: Any = None) -> tuple[Any, Any]:
    from analystos.llm.config import load_models_config
    from analystos.services.platform_settings import get as platform_get

    return config or load_models_config(), platform or platform_get()


def effective(session: Session, *, tenant_id: str, workspace_id: str | None = None, config: Any = None,
              platform: Any = None) -> EffectiveModels:
    cfg, plat = _upper(config, platform)
    return resolve(cfg, plat, _layers(session, tenant_id, workspace_id))


def workspace_tenant(session: Session, workspace_id: str) -> str:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.deleted_at is not None:
        raise NotFound(f"workspace {workspace_id} not found")
    if not ws.tenant_id:
        from analystos.tenancy import DEFAULT_TENANT_ID

        return DEFAULT_TENANT_ID
    return ws.tenant_id


def can_administer(session: Session, user: User, tenant_id: str, workspace_id: str | None = None) -> bool:
    """Platform administrators, the tenant's administrators, and (workspace scope) the workspace's owners."""
    if user.is_admin:
        return True
    member = session.get(TenantMember, (tenant_id, user.id))
    if member is not None and member.role == TENANT_ADMIN_ROLE:
        return True
    if workspace_id:
        from analystos.governance.policy import member_role
        from analystos.security.auth import role_at_least

        role = member_role(session, user, workspace_id)
        return role is not None and role_at_least(role, "owner")
    return False


def require_access(session: Session, user: User, tenant_id: str, workspace_id: str | None, *, what: str) -> None:
    """Forbidden unless `can_administer`; NotFound for a workspace the caller has no role in (its
    existence is not revealed)."""
    if can_administer(session, user, tenant_id, workspace_id):
        return
    if workspace_id:
        from analystos.governance.policy import member_role

        if member_role(session, user, workspace_id) is None:
            raise NotFound(f"workspace {workspace_id} not found")
    raise Forbidden(f"{what} needs a platform or tenant administrator" + (" or a workspace owner" if workspace_id else ""))


def _require(session: Session, user: User, scope: str, tenant_id: str, workspace_id: str | None) -> None:
    if scope not in SCOPES:
        raise InvalidInput(f"scope must be one of {', '.join(SCOPES)}")
    require_access(session, user, tenant_id, workspace_id if scope == "workspace" else None,
                   what=f"changing the {scope} model configuration")


def _check_narrowing(session: Session, scope: str, tenant_id: str, workspace_id: str | None,
                     doc: ModelOverride) -> EffectiveModels:
    cfg, plat = _upper()
    resolved = resolve(cfg, plat, _layers(session, tenant_id, workspace_id, upto=scope, replace=(scope, doc)))
    mine = [v for v in resolved.violations if v["layer"] == scope]
    if mine:
        raise InvalidInput(f"the {scope} override would widen the layer above: "
                           + "; ".join(f"{v['path']}: {v['message']}" for v in mine), details={"violations": mine})
    return resolved


def _write(session: Session, user: User, *, scope: str, tenant_id: str, workspace_id: str | None, version: int,
           document: dict[str, Any], note: str, action: str, details: dict[str, Any]) -> None:
    session.add(ModelConfigOverride(id=new_id("mco"), scope_key=scope_key(scope, tenant_id, workspace_id), scope=scope,
                                    tenant_id=tenant_id, workspace_id=workspace_id if scope == "workspace" else None,
                                    version=version, document=document, note=note[:500], created_by=user.id))
    try:
        session.flush()
    except IntegrityError as exc:  # a concurrent writer took this version first
        raise Conflict(f"the {scope} model configuration changed concurrently; reload and retry") from exc
    audit(f"user:{user.id}", action, target=f"{scope_key(scope, tenant_id, workspace_id)}@v{version}", decision="allow",
          workspace_id=workspace_id if scope == "workspace" else None, details=details, session=session)
    invalidate()


def _switch_gate(session: Session, user: User, scope: str, tenant_id: str, workspace_id: str | None,
                 after: EffectiveModels, override_reason: str | None, runner: Any) -> dict[str, Any]:
    """S05-T5: a save that changes a purpose's primary model passes the model-switch evaluation gate first."""
    from analystos.services import model_switch

    cfg, plat = _upper()
    before = resolve(cfg, plat, _layers(session, tenant_id, workspace_id, upto=scope))
    return model_switch.check(session, user, model_switch.changed_primaries(before, after),
                              override_reason=override_reason, runner=runner)


def put(session: Session, user: User, *, scope: str, tenant_id: str, workspace_id: str | None,
        document: dict[str, Any], expected_version: int | None, note: str = "",
        switch_override: str | None = None, switch_runner: Any = None) -> dict[str, Any]:
    """Replace the scope's override with `document` as a new version. `expected_version` is the
    If-Match version (0 = no override yet; None = `*`, any version). A changed primary model must pass
    the model-switch gate (409 otherwise) unless a platform administrator gives `switch_override`."""
    _require(session, user, scope, tenant_id, workspace_id)
    key = scope_key(scope, tenant_id, workspace_id)
    latest = _latest_row(session, key)
    current = latest.version if latest else 0
    if expected_version is not None and expected_version != current:
        raise PreconditionFailed(f"the {scope} model configuration is at version {current}, not {expected_version}",
                                 details={"version": current})
    doc = _parse(document)
    assert doc is not None
    resolved = _check_narrowing(session, scope, tenant_id, workspace_id, doc)
    stored = doc.model_dump(exclude_defaults=True)
    before = latest.document if latest else {}
    if stored == before and latest is not None:
        return {"scope": scope, "version": current, "changed": False}
    gate = _switch_gate(session, user, scope, tenant_id, workspace_id, resolved, switch_override, switch_runner)
    _write(session, user, scope=scope, tenant_id=tenant_id, workspace_id=workspace_id, version=current + 1, document=stored,
           note=note, action="models.config.updated", details={"from_version": current, "note": note, "document": stored})
    return {"scope": scope, "version": current + 1, "changed": True, "effective": resolved.view(), "model_switch_gate": gate}


def rollback(session: Session, user: User, *, scope: str, tenant_id: str, workspace_id: str | None,
             version: int, switch_override: str | None = None, switch_runner: Any = None) -> dict[str, Any]:
    _require(session, user, scope, tenant_id, workspace_id)
    key = scope_key(scope, tenant_id, workspace_id)
    target = session.scalar(select(ModelConfigOverride).where(ModelConfigOverride.scope_key == key,
                                                              ModelConfigOverride.version == version))
    if target is None:
        raise NotFound(f"{scope} model configuration version {version} not found")
    doc = _parse(target.document)
    assert doc is not None
    resolved = _check_narrowing(session, scope, tenant_id, workspace_id, doc)  # the upper layers may have narrowed since
    gate = _switch_gate(session, user, scope, tenant_id, workspace_id, resolved, switch_override, switch_runner)
    latest = _latest_row(session, key)
    new_version = (latest.version if latest else 0) + 1
    _write(session, user, scope=scope, tenant_id=tenant_id, workspace_id=workspace_id, version=new_version,
           document=dict(target.document), note=f"rollback to v{version}", action="models.config.rolled_back",
           details={"to_version": version})
    return {"scope": scope, "version": new_version, "rolled_back_to": version, "model_switch_gate": gate}


def history(session: Session, *, scope: str, tenant_id: str, workspace_id: str | None, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(select(ModelConfigOverride).where(ModelConfigOverride.scope_key == scope_key(scope, tenant_id, workspace_id))
                           .order_by(ModelConfigOverride.version.desc()).limit(min(limit, 200)))
    return [{"version": r.version, "note": r.note, "created_by": r.created_by, "created_at": r.created_at.isoformat(),
             "document": r.document} for r in rows]


def current_version(session: Session, *, scope: str, tenant_id: str, workspace_id: str | None) -> int:
    row = _latest_row(session, scope_key(scope, tenant_id, workspace_id))
    return row.version if row else 0


# ------------------------------------------------------------------------------ router lookup
def narrowing_for(workspace_id: str) -> EffectiveModels:
    """The workspace's effective layers for the router (cached, bounded; fail closed without any good read)."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(workspace_id)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    try:
        with system_scope(), session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if ws is None:
                raise LookupError(workspace_id)
            resolved = effective(s, tenant_id=workspace_tenant(s, workspace_id), workspace_id=workspace_id)
    except Exception as exc:
        with _lock:
            good = _last_good.get(workspace_id)
        if good is not None:
            return good
        if isinstance(exc, (LookupError, NotFound)):
            # No such workspace in the control plane (tests, platform pseudo-workspaces): only the
            # file and platform layers exist for it.
            resolved = effective_without_overrides()
        else:
            raise ModelRouteUnavailable(f"model configuration for workspace {workspace_id} could not be read: {exc}"[:300],
                                        details={"workspace_id": workspace_id}) from exc
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            _cache.clear()
        if len(_last_good) >= _MAX_ENTRIES:
            _last_good.clear()
        _cache[workspace_id] = (now, resolved)
        _last_good[workspace_id] = resolved
    return resolved


def effective_without_overrides() -> EffectiveModels:
    cfg, plat = _upper()
    return resolve(cfg, plat, [])


def invalidate() -> None:
    with _lock:
        _cache.clear()


__all__ = ["SCOPES", "can_administer", "current_version", "require_access", "effective", "history", "invalidate", "narrowing_for", "put",
           "rollback", "scope_key", "workspace_tenant"]

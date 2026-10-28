"""Per-tenant Superset isolation, access and embedding (spec S08 §5, S08-T4/T5).

Isolation model, as built:
* every Superset database connection belongs to one workspace of one tenant and connects as that
  workspace's own BI login (P4-02). Its `extra` is stamped with `aos_tenant` / `aos_workspace`; a
  publisher bound to tenant A refuses to reuse a connection stamped for tenant B (or one it cannot
  attribute), so a name collision or a hand-made connection can never route tenant A's datasets
  through tenant B's identity. A tenant's connections are therefore one reader identity per workspace,
  strictly narrower than the spec's one-per-tenant (recorded in the spec's As built).
* roles `ws_{slug}_viewer` (data access on the workspace connection) and `ws_{slug}_editor` (the same
  plus write on charts and dashboards) are managed here; platform membership and audiences map onto them.
* row-level security: each workspace policy row filter becomes one Superset RLS rule on the workspace's
  datasets that read a matching asset, for both workspace roles. A predicate that needs the viewer's
  attributes cannot be a static rule, so the role rule is `1 = 0` (fail closed) and embedding renders it
  per viewer into the guest token instead.
"""
from __future__ import annotations

import json
from typing import Any

from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.logging import get_logger

log = get_logger(__name__)

EDITOR_PERMISSIONS = (("can_write", "Dashboard"), ("can_write", "Chart"))
FAIL_CLOSED = "1 = 0"


class SupersetAccess:
    """Mixed into `SupersetPublisher` (uses its `client`, `_security_api`, `_role_id`, naming helpers)."""

    client: Any

    # -- tenant stamp -------------------------------------------------------------------------------------
    def tenant_for(self, workspace_id: str) -> str | None:
        resolver = getattr(self, "_tenant_of", None)
        if resolver is not None:
            return resolver(workspace_id)
        from analystos.tenancy.background import workspace_tenant

        return workspace_tenant(workspace_id)

    def _stamp(self, extra: dict[str, Any], workspace_id: str) -> dict[str, Any]:
        extra["aos_workspace"] = workspace_id
        tenant = self.tenant_for(workspace_id)
        if tenant:
            extra["aos_tenant"] = tenant
        return extra

    def check_database_tenant(self, database_id: int, workspace_id: str) -> None:
        """Refuse a connection stamped for another tenant or workspace (`test_cross_tenant_superset_denied`)."""
        body = self.client.get(f"/api/v1/database/{int(database_id)}", allow_404=True) or {}
        try:
            extra = json.loads((body.get("result") or {}).get("extra") or "{}")
        except (TypeError, ValueError):
            extra = {}
        tenant = self.tenant_for(workspace_id)
        stamped_tenant, stamped_ws = extra.get("aos_tenant"), extra.get("aos_workspace")
        if stamped_ws not in (None, workspace_id) or (stamped_tenant is not None and tenant is not None
                                                        and stamped_tenant != tenant):
            log.warning("superset database %s is stamped for %s/%s, not %s/%s", database_id, stamped_tenant, stamped_ws,
                        tenant, workspace_id)
            raise Forbidden("this Superset connection belongs to another tenant or workspace; it is never reused",
                            details={"database_id": database_id})

    # -- roles --------------------------------------------------------------------------------------------
    @staticmethod
    def editor_role_name(workspace_id: str) -> str:
        from analystos.publishing.base import workspace_slug

        return f"ws_{workspace_slug(workspace_id)}_editor"

    def _pvm(self, permission: str, resource: str) -> int | None:
        vms = [v for v in self._security_api("resources", [{"col": "name", "opr": "eq", "value": resource}])  # type: ignore[attr-defined]
               if v.get("name") == resource]
        perms = [p for p in self._security_api("permissions", [{"col": "name", "opr": "eq", "value": permission}])  # type: ignore[attr-defined]
                 if p.get("name") == permission]
        if not vms or not perms:
            return None
        pvms = self._security_api("permissions-resources", [  # type: ignore[attr-defined]
            {"col": "view_menu", "opr": "rel_o_m", "value": vms[0]["id"]},
            {"col": "permission", "opr": "rel_o_m", "value": perms[0]["id"]}])
        return int(pvms[0]["id"]) if pvms else None

    def ensure_editor_role(self, workspace_id: str) -> int:
        """`ws_{slug}_editor`: the viewer role's data access plus write on charts and dashboards."""
        database_id, _ = self.ensure_database(workspace_id)  # type: ignore[attr-defined]
        role_id = self._role_id(self.editor_role_name(workspace_id), create=True)  # type: ignore[attr-defined]
        pvms = [self._database_access_pvm(database_id)]  # type: ignore[attr-defined]
        for permission, resource in EDITOR_PERMISSIONS:
            pvm = self._pvm(permission, resource)
            if pvm is None:
                raise NotFound(f"Superset has no {permission} on {resource}")
            pvms.append(pvm)
        self.client.post(f"/api/v1/security/roles/{role_id}/permissions", {"permission_view_menu_ids": pvms})
        return int(role_id)

    def set_dashboard_roles(self, dashboard_id: int, role_ids: list[int]) -> None:
        """Dashboard RBAC: only these roles list and open the dashboard in Superset."""
        self.client.put(f"/api/v1/dashboard/{int(dashboard_id)}", {"roles": sorted(set(role_ids))})

    def ensure_workspace_roles(self, workspace_id: str) -> dict[str, int]:
        return {"viewer": self.ensure_workspace_role(workspace_id),  # type: ignore[attr-defined]
                "editor": self.ensure_editor_role(workspace_id)}

    # -- row-level security --------------------------------------------------------------------------------
    @staticmethod
    def rls_rule_name(workspace_id: str, filter_id: str) -> str:
        from analystos.publishing.base import workspace_slug

        return f"aos_{workspace_slug(workspace_id)}_{filter_id}"[:255]

    def sync_rls(self, workspace_id: str, rules: list[dict[str, Any]], role_ids: list[int]) -> dict[str, int]:
        """Upsert one Superset RLS rule per `{filter_id, clause, tables: [dataset ids]}`; rules of this
        workspace no longer wanted are deleted. Returns {rule name: id}."""
        prefix = self.rls_rule_name(workspace_id, "")
        existing = {r.get("name"): r for r in self.client.list("rowlevelsecurity", [
            {"col": "name", "opr": "sw", "value": prefix}]) if str(r.get("name", "")).startswith(prefix)}
        out: dict[str, int] = {}
        for rule in rules:
            if not rule["tables"]:
                continue
            name = self.rls_rule_name(workspace_id, rule["filter_id"])
            body = {"name": name, "filter_type": "Regular", "tables": sorted(int(t) for t in rule["tables"]),
                    "roles": sorted(role_ids), "clause": rule["clause"], "group_key": "",
                    "description": f"AnalystOS workspace policy row filter {rule['filter_id']}"}
            if name in existing:
                rid = int(existing.pop(name)["id"])
                self.client.put(f"/api/v1/rowlevelsecurity/{rid}", body)
            else:
                rid = int(self.client.post("/api/v1/rowlevelsecurity/", body)["id"])
            out[name] = rid
        for stale in existing.values():
            self.client.delete(f"/api/v1/rowlevelsecurity/{int(stale['id'])}")
        return out

    # -- embedding ------------------------------------------------------------------------------------------
    def ensure_embedded(self, dashboard_id: int, allowed_domains: list[str]) -> str:
        """The dashboard's embedded uuid (created once), its allowed domains kept equal to `allowed_domains`
        (updated in place; Superset keeps the uuid on an upsert). Superset reads an empty list as "any site may
        embed this", so an empty list is refused rather than written."""
        wanted = sorted(set(allowed_domains or []))
        if not wanted:
            raise InvalidInput("an embedded dashboard needs at least one allowed origin")
        body = self.client.get(f"/api/v1/dashboard/{int(dashboard_id)}/embedded", allow_404=True)
        current = (body or {}).get("result") or {}
        if current.get("uuid") and sorted(current.get("allowed_domains") or []) == wanted:
            return str(current["uuid"])
        created = self.client.post(f"/api/v1/dashboard/{int(dashboard_id)}/embedded", {"allowed_domains": wanted})
        return str((created.get("result") or created).get("uuid"))

    def dashboard_dataset_ids(self, dashboard_id: int) -> list[int]:
        """Every dataset the dashboard's charts read, as Superset holds them now (not as published)."""
        body = self.client.get(f"/api/v1/dashboard/{int(dashboard_id)}/datasets") or {}
        return sorted({int(d["id"]) for d in body.get("result") or [] if d.get("id") is not None})

    def guest_token(self, *, user: dict[str, str], dashboard_uuid: str, rls: list[dict[str, Any]]) -> str:
        body = {"user": user, "resources": [{"type": "dashboard", "id": dashboard_uuid}], "rls": rls}
        return str(self.client.post("/api/v1/security/guest_token/", body)["token"])

    # -- reconcile ------------------------------------------------------------------------------------------
    def object_state(self, resource: str, oid: Any) -> dict[str, Any] | None:
        """The object as Superset holds it, or None when it no longer exists."""
        try:
            body = self.client.get(f"/api/v1/{resource}/{int(oid)}", allow_404=True)
        except (TypeError, ValueError):
            return None
        return (body or {}).get("result") if body else None

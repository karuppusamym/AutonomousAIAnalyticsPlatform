"""Superset access from workspace membership (P4-02, SEC-007).

Each AnalystOS workspace has one Superset role (``database_access`` on that workspace's database,
which connects as the workspace's BI login). A member's Superset user gets exactly Gamma, the roles
of the workspaces they belong to and, for members who author analysis, sql_lab; platform admins are
not made Superset admins. Run by ``analystos bi-sync``; idempotent.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.db.models import User, Workspace, WorkspaceMember

SQL_LAB_ROLES = frozenset({"owner", "editor", "analyst"})
EDITOR_ROLES = SQL_LAB_ROLES  # S08: authors get `ws_{slug}_editor`; viewers and approvers `ws_{slug}_viewer` only


def membership_plan(session: Session, workspace_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{email: {workspaces, sql_lab, active, name}} for every member of the (active) workspaces."""
    q = (select(User, WorkspaceMember.workspace_id, WorkspaceMember.role)
         .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
         .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
         .where(Workspace.deleted_at.is_(None)))
    plan: dict[str, dict[str, Any]] = defaultdict(lambda: {"workspaces": set(), "editor_workspaces": set(), "sql_lab": False})
    for user, ws, role in session.execute(q):
        entry = plan[user.email]
        entry.update(active=bool(user.active), name=user.name)
        entry["workspaces"].add(ws)
        if role in EDITOR_ROLES:
            entry["editor_workspaces"].add(ws)
        entry["sql_lab"] = entry["sql_lab"] or role in SQL_LAB_ROLES
    if workspace_ids is not None:
        wanted = set(workspace_ids)
        plan = {e: p for e, p in plan.items() if p["workspaces"] & wanted}
    return {e: {**p, "workspaces": sorted(p["workspaces"]), "editor_workspaces": sorted(p["editor_workspaces"])}
            for e, p in plan.items()}


def sync_bi_access(session: Session, publisher: Any, workspace_ids: list[str] | None = None) -> dict[str, Any]:
    plan = membership_plan(session, workspace_ids)
    roles = {ws: publisher.ensure_workspace_role(ws) for ws in sorted({w for p in plan.values() for w in p["workspaces"]})}
    users = {}
    for email, p in sorted(plan.items()):
        first, _, last = (p.get("name") or email).partition(" ")
        users[email] = publisher.sync_user(username=email, email=email, workspace_ids=p["workspaces"], first_name=first,
                                           last_name=last, sql_lab=p["sql_lab"], active=p["active"],
                                           editor_workspace_ids=p["editor_workspaces"])
    return {"workspace_roles": roles, "users": users}

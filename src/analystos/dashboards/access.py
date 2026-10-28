"""Superset access for a published dashboard (S08-T4) and per-viewer embedding (S08-T5).

After a publish: the workspace's `ws_{slug}_viewer|editor` roles exist, the dashboard is visible to the
roles its approved audience names, and the workspace policy's row filters are Superset RLS rules on the
datasets that read a filtered asset. A filter Superset cannot express statically (it needs the viewer's
attributes, or its columns are not in the dataset) is the rule `1 = 0`: closed for direct Superset use,
rendered per viewer only in a guest token.

A guest token is minted only after re-checking, at request time: membership, workspace state, that the
dashboard is published from a succeeded publication, the approved audience, and the viewer's own resolved
scope (withheld assets refuse; row filters become the token's RLS clauses).
"""
from __future__ import annotations

from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.contracts.bi import PublishBundle
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.db.models import Approval, Publication, User
from analystos.publishing.superset_access import FAIL_CLOSED


def _columns(predicate: str) -> set[str]:
    try:
        return {c.name.lower() for c in sqlglot.parse_one(predicate, read="postgres").find_all(exp.Column)}
    except sqlglot.errors.ParseError:
        return {"\x00unparseable"}


def static_rls_rules(policy: Any, bundle: PublishBundle, dataset_ids: dict[str, Any]) -> list[dict[str, Any]]:
    """One rule per policy row filter that applies to a published dataset: {filter_id, clause, tables, closed}."""
    from analystos.governance.row_filters import MissingAttribute, applies, render

    rules = []
    for flt in policy.row_filters:
        tables, closed = [], False
        for ds in bundle.datasets:
            if ds.name not in dataset_ids or not any(applies(flt.assets, a) for a in ds.source_assets):
                continue
            tables.append(int(dataset_ids[ds.name]))
        if not tables:
            continue
        try:
            clause, cols = render(flt.predicate, {}, "postgres", filter_id=flt.id)
            names = {str(c.get("name")).lower() for ds in bundle.datasets for c in ds.columns}
            if not set(map(str.lower, cols)) <= names:
                clause, closed = FAIL_CLOSED, True
        except (MissingAttribute, ValueError):
            clause, closed = FAIL_CLOSED, True
        rules.append({"filter_id": flt.id, "clause": clause, "tables": tables, "closed": closed})
    return rules


def apply_access(session: Session, workspace_id: str, publisher: Any, bundle: PublishBundle, external_ids: dict[str, Any],
                 audience: list[str]) -> dict[str, Any]:
    from analystos.governance.policy import get_workspace, load_policy

    roles = publisher.ensure_workspace_roles(workspace_id)
    dash_roles = [roles["editor"], *([roles["viewer"]] if "role:viewer" in audience else [])]
    for dash_id in (external_ids.get("dashboards") or {}).values():
        publisher.set_dashboard_roles(int(dash_id), dash_roles)
    policy = load_policy(session, get_workspace(session, workspace_id))
    rules = static_rls_rules(policy, bundle, external_ids.get("datasets") or {})
    synced = publisher.sync_rls(workspace_id, rules, [roles["viewer"], roles["editor"]])
    return {"roles": roles, "dashboard_roles": sorted(dash_roles), "rls_rules": sorted(synced),
            "fail_closed": [r["filter_id"] for r in rules if r["closed"]]}


def _audience_allows(session: Session, user: User, workspace_id: str, role: str, audience: list[str]) -> bool:
    from analystos.db.models import UserIdentity
    from analystos.security.auth import role_at_least

    if role_at_least(role, "editor"):
        return True
    if "role:viewer" in audience:
        return True
    wanted = {a.split(":", 1)[1] for a in audience if a.startswith("group:")}
    if not wanted:
        return False
    groups = {g for (gs,) in session.execute(select(UserIdentity.groups).where(UserIdentity.user_id == user.id))
              for g in (gs or [])}
    return bool(groups & wanted)


def viewer_rls(scope: Any, bundle: PublishBundle, dataset_ids: dict[str, Any]) -> list[dict[str, Any]]:
    """The viewer's own row filters as guest-token RLS clauses; refuses what the viewer may not see.

    A guest holds no workspace role, so the static RLS rules never reach it: anything this function does not
    turn into a clause is unfiltered. So it refuses unless every source asset of every published dataset is in
    the viewer's current scope (an asset deselected, or of a source no longer ready, since publish), and a
    dataset that names no source asset cannot be checked at all."""
    in_scope = {a.lower() for a in (scope.assets or [])}
    withheld_by = {a.lower(): r for a, r in (scope.withheld_assets or {}).items()}
    filters_by = {}
    for a, preds in (scope.row_filters or {}).items():
        filters_by.setdefault(a.lower(), []).extend(preds)
    out = []
    for ds in bundle.datasets:
        if ds.name not in dataset_ids:
            continue
        if not ds.source_assets:
            raise Forbidden(f"dataset {ds.name} names no source asset, so your row filters cannot be checked for it")
        withheld = [a for a in ds.source_assets if a.lower() in withheld_by]
        if withheld:
            raise Forbidden(f"dashboard data from {', '.join(withheld)} is withheld from you by a row filter")
        outside = [a for a in ds.source_assets if a.lower() not in in_scope]
        if outside:
            raise Forbidden(f"dashboard data from {', '.join(outside)} is no longer in your data scope")
        denied = {c.split(".")[-1].lower() for c in scope.denied_columns
                  if any(c.lower().startswith(a.lower() + ".") for a in ds.source_assets)}
        names = {str(c.get("name")).lower() for c in ds.columns}
        if denied & names:
            raise Forbidden(f"dataset {ds.name} shows columns you may not read: {', '.join(sorted(denied & names))}")
        preds = [p for a in ds.source_assets for p in filters_by.get(a.lower(), [])]
        if not preds:
            continue
        if any(not _columns(p) <= names for p in preds):
            raise Forbidden(f"a row filter on {ds.name} uses columns the published dataset does not carry; "
                            "it cannot be enforced in Superset, so the dashboard is not shown")
        out.append({"dataset": int(dataset_ids[ds.name]), "clause": " AND ".join(f"({p})" for p in preds)})
    return out


def embed_domains(settings: Any) -> list[str]:
    """Where an embedded dashboard may be framed: the configured origins (comma or space separated), else the
    UI's own origin. Never empty: Superset reads an empty allowed-domains list as "any site"."""
    from urllib.parse import urlsplit

    raw = (getattr(settings, "superset_embed_domains", "") or "").replace(",", " ").split()
    listed = raw or [getattr(settings, "web_url", "") or ""]
    out = []
    for entry in listed:
        url = urlsplit(entry.strip())
        if url.scheme not in ("http", "https") or not url.hostname or "*" in entry:
            raise InvalidInput(f"embed origin {entry!r} is not an http(s) origin; set ANALYSTOS_SUPERSET_EMBED_DOMAINS "
                               "or ANALYSTOS_WEB_URL to the UI's address")
        out.append(f"{url.scheme}://{url.netloc}")
    return sorted(set(out))


def lock_unpublished_datasets(publisher: Any, dashboard_id: int, published: dict[str, Any]) -> list[dict[str, Any]]:
    """`1 = 0` for every dataset on the live dashboard that the approved publication does not name. Editors can add
    charts in Superset itself; a guest may read every chart on its dashboard, so such a dataset gets no rows."""
    known = {int(i) for i in published.values()}
    return [{"dataset": ds, "clause": FAIL_CLOSED} for ds in sorted(set(publisher.dashboard_dataset_ids(dashboard_id)) - known)]


def guest_token(session: Session, user: User, artifact_id: str, *, publisher: Any = None) -> dict[str, Any]:
    """Mint a Superset guest token for one viewer after re-checking authorization (`test_guest_token_rechecks_auth`)."""
    from analystos.core.config import get_settings
    from analystos.dashboards import service
    from analystos.governance.audit import audit
    from analystos.governance.policy import get_workspace, member_role, resolve_scope
    from analystos.publishing.base import get_publisher

    art = service.load(session, user, artifact_id, minimum="viewer")  # 404 outside the caller's workspaces and tenant
    current = session.get(User, user.id)
    if current is None or not current.active:
        raise Forbidden("your account is not active")
    get_workspace(session, art.workspace_id)  # disabled or archived: refused
    if art.status != "published" or art.platform != "superset" or not art.external_id:
        raise InvalidInput("this dashboard is not published to Superset")
    pub = session.scalar(select(Publication).where(Publication.artifact_id == art.id, Publication.status == "succeeded",
                                                   Publication.artifact_version == art.version)
                         .order_by(Publication.created_at.desc()).limit(1))
    approval = session.get(Approval, pub.approval_id) if pub and pub.approval_id else None
    if pub is None or approval is None or approval.status != "executed":
        raise NotFound("no succeeded publication for this dashboard version")
    role = member_role(session, current, art.workspace_id)
    audience = list((approval.payload or {}).get("audience") or [])
    if role is None or not _audience_allows(session, current, art.workspace_id, role, audience):
        audit(f"user:{user.id}", "dashboard.embed_denied", workspace_id=art.workspace_id, target=art.id, decision="deny",
              reasons=["audience"])  # its own transaction: the refusal below rolls the request back
        raise Forbidden("this dashboard was not published for your audience")
    bundle = PublishBundle.model_validate(approval.payload["bundle"])
    published = pub.external_ids.get("datasets") or {}
    rls = viewer_rls(resolve_scope(session, current, art.workspace_id, minimum_role="viewer"), bundle, published)
    settings = get_settings()
    publisher = publisher or get_publisher("superset", settings)
    rls += lock_unpublished_datasets(publisher, int(art.external_id), published)
    uuid = publisher.ensure_embedded(int(art.external_id), allowed_domains=embed_domains(settings))
    first, _, last = (current.name or current.email).partition(" ")
    token = publisher.guest_token(user={"username": current.email, "first_name": first or current.email,
                                        "last_name": last or "-"}, dashboard_uuid=uuid, rls=rls)
    audit(f"user:{user.id}", "dashboard.embed_token", workspace_id=art.workspace_id, target=art.id, decision="allow",
          details={"publication_id": pub.id, "rls_clauses": len(rls)}, session=session)
    return {"token": token, "dashboard_uuid": uuid, "superset_url": getattr(publisher, "public_url", None),
            "rls_clauses": len(rls)}

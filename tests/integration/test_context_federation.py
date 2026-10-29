"""Spec S03-T7 (federation subscription) on Postgres with row-level security and strict tenancy:

* a workspace in tenant A follows a workspace in tenant B through REST; copies land in A's ledger as `approved`,
  read-only, filled with A's tenant by the trigger; tenant B sees none of A's subscription rows;
* a source version change refreshes the copy; redelivery from cursor 0 changes nothing;
* a user who does not belong to tenant B cannot subscribe to it (404), and the external feed applies the caller's
  access and tenant;
* the subject leaving tenant B suspends the subscription and retires the copies;
* the scheduler pass (platform loop) syncs due subscriptions of every tenant, each as its own tenant."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.context.service import add_entry
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, TenantMember, User, Workspace, WorkspaceMember
    from analystos.security.auth import hash_password
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope
    from analystos.tenancy.resolve import invalidate

    tb = new_id("tnt")
    ids = {"tenant_a": DEFAULT_TENANT_ID, "tenant_b": tb}
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"fed-{tb[-6:]}", name="Federation source tenant"))
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        for key in ("owner", "a_only"):
            u = User(id=new_id("usr"), email=f"fed-{key}-{tb[-6:]}@analystos.local", name=key,
                     password_hash=hash_password(PASSWORD), is_admin=False, active=True, attributes={})
            s.add(u)
            s.flush()
            ids[key], ids[f"{key}_email"] = u.id, u.email
        for key in ("ws_a", "ws_a2"):
            w = Workspace(id=new_id("ws"), name=f"federation {key}", created_by=ids["owner"])
            s.add(w)
            s.flush()
            ids[key] = w.id
        s.add_all([WorkspaceMember(workspace_id=ids["ws_a"], user_id=ids["owner"], role="owner"),
                   WorkspaceMember(workspace_id=ids["ws_a2"], user_id=ids["a_only"], role="owner")])
    with system_scope(), session_scope() as s:  # the owner also belongs to tenant B
        s.add(TenantMember(tenant_id=tb, user_id=ids["owner"], role="member"))
    invalidate()
    with tenant_scope(tb), session_scope() as s:
        w = Workspace(id=new_id("ws"), name="federation source", created_by=ids["owner"])
        s.add(w)
        s.flush()
        ids["ws_b"] = w.id
        s.add(WorkspaceMember(workspace_id=w.id, user_id=ids["owner"], role="owner"))
        s.flush()
        ids["term"] = add_entry(s, workspace_id=w.id, kind="term", name="Backlog",
                                body="Incidents still open at the end of a day.", synonyms=["Open queue"]).id
        add_entry(s, workspace_id=w.id, kind="definition", name="Churn guess", body="Unreviewed.", origin="model",
                  trusted=False)
    return ids


@pytest.fixture(scope="module")
def api(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _open_titles(tenant: str, ws: str) -> dict[str, str]:
    from analystos.context.models import ContextLedgerEntry
    from analystos.db.base import session_scope
    from analystos.tenancy import tenant_scope

    with tenant_scope(tenant), session_scope() as s:
        return {r.title: r.trust for r in s.scalars(select(ContextLedgerEntry).where(
            ContextLedgerEntry.workspace_id == ws, ContextLedgerEntry.valid_to.is_(None),
            ContextLedgerEntry.source == "federation"))}


def test_cross_tenant_subscription_follows_the_feed_under_rls(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import ContextEntry
    from analystos.tenancy import tenant_scope

    owner, a_only = _login(api, world["owner_email"]), _login(api, world["a_only_email"])
    # a user outside tenant B cannot reach its workspace, so cannot subscribe to it
    r = api.post("/api/v2/context/subscriptions", headers=a_only,
                 json={"workspace": world["ws_a2"], "source_workspace": world["ws_b"]})
    assert r.status_code == 404, r.text
    r = api.post("/api/v2/context/subscriptions", headers=owner,
                 json={"workspace": world["ws_a"], "source_workspace": world["ws_b"]})
    assert r.status_code == 201, r.text
    body = r.json()
    sid = body["subscription"]["id"]
    assert body["subscription"]["source_tenant_id"] == world["tenant_b"] and body["sync"]["added"] == 1
    assert _open_titles(world["tenant_a"], world["ws_a"]) == {"Backlog": "approved"}  # the draft stays home
    hits = api.get(f"/api/v2/context/search?workspace={world['ws_a']}&q=open queue", headers=owner).json()
    assert hits[0]["title"] == "Backlog" and hits[0]["trust"] == "approved"
    entry = api.get(f"/api/v2/context/entries/{hits[0]['id']}", headers=owner).json()
    assert entry["body"]["read_only"] is True and entry["provenance"]["source_workspace_id"] == world["ws_b"]

    with tenant_scope(world["tenant_a"]), session_scope() as s:  # the trigger filled tenant A on both tables
        assert s.execute(text("SELECT DISTINCT tenant_id FROM context_subscription")).scalars().all() == [world["tenant_a"]]
        assert s.execute(text("SELECT DISTINCT tenant_id FROM context_federated_entry")).scalars().all() == [world["tenant_a"]]
    with tenant_scope(world["tenant_b"]), session_scope() as s:  # RLS: tenant B sees none of A's rows
        assert s.scalar(text("SELECT count(*) FROM context_subscription")) == 0
        assert s.scalar(text("SELECT count(*) FROM context_federated_entry")) == 0
        s.get(ContextEntry, world["term"]).body = "Incidents open for more than a day."

    synced = api.post(f"/api/v2/context/subscriptions/{sid}/sync", headers=owner).json()
    assert synced["updated"] == 1
    with tenant_scope(world["tenant_a"]), session_scope() as s:
        from analystos.context.models import ContextLedgerEntry

        copy = s.scalar(select(ContextLedgerEntry).where(ContextLedgerEntry.workspace_id == world["ws_a"],
                                                         ContextLedgerEntry.title == "Backlog",
                                                         ContextLedgerEntry.valid_to.is_(None)))
        assert copy.version == 2 and copy.summary == "Incidents open for more than a day."
    replay = api.post(f"/api/v2/context/subscriptions/{sid}/resync", headers=owner).json()
    assert (replay["added"], replay["updated"], replay["removed"]) == (0, 0, 0)  # redelivery changes nothing

    # the external feed: the caller's tenant and access apply
    feed = api.get(f"/api/v2/context/feed?workspace={world['ws_b']}&since=0",
                   headers={**owner, "X-Tenant-Id": world["tenant_b"]}).json()
    assert [(i["op"], i["title"], i["version"]) for i in feed["items"]] == [("upsert", "Backlog", 1), ("upsert", "Backlog", 2)]
    assert api.get(f"/api/v2/context/feed?workspace={world['ws_b']}&since=0", headers=owner).status_code == 404
    listed = api.get(f"/api/v2/context/subscriptions?workspace={world['ws_a']}", headers=owner).json()
    assert [(x["id"], x["copies"]) for x in listed] == [(sid, 1)]


def test_scheduler_pass_syncs_due_subscriptions_and_suspends_on_lost_access(api, world):
    from analystos.context import federation
    from analystos.context.models import ContextSubscription
    from analystos.db.base import session_scope
    from analystos.db.models import TenantMember
    from analystos.tenancy import system_scope, tenant_scope

    with tenant_scope(world["tenant_a"]), session_scope() as s:
        sub = s.scalar(select(ContextSubscription).where(ContextSubscription.workspace_id == world["ws_a"]))
        sid = sub.id
        sub.last_synced_at = sub.last_synced_at - timedelta(hours=1)
    out = federation.sync_due()
    assert out["synced"] >= 1 and out["failed"] == 0
    with system_scope(), session_scope() as s:  # the owner leaves tenant B
        s.delete(s.get(TenantMember, (world["tenant_b"], world["owner"])))
    out = federation.sync_subscription(sid)  # called unbound: @owned_by binds the subscriber's tenant
    assert out["status"] == "suspended" and out["copies_retired"] == 1
    assert _open_titles(world["tenant_a"], world["ws_a"]) == {}

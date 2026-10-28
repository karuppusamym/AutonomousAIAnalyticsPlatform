"""S08 on Postgres under strict tenancy: migration 0066, builder dashboards over REST (typed ops, If-Match,
versions with op diffs, revert, conversation), fork-on-edit of a published dashboard, the hash-bound publish
flow into an in-memory Superset (roles, dashboard RBAC, RLS rules), guest tokens that re-check authorization,
cross-tenant denial, reconcile with a verified repair, and export approvals with a watermark.

Superset is the stateful fake from tests/unit (respx); no object is created in any running Superset."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import respx
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import ApprovalRequired, Forbidden, NotFound  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from analystos.db.base import session_scope  # noqa: E402
from analystos.db.models import (  # noqa: E402
    Approval,
    Artifact,
    AuditEvent,
    Publication,
    QueryExecution,
    User,
    VerifiedQuery,
    WorkspaceMember,
)

pytestmark = pytest.mark.integration


def _user(s, email: str, *, admin: bool = False) -> User:
    from analystos.security.auth import hash_password

    u = User(id=new_id("usr"), email=email, name=email.split("@")[0], password_hash=hash_password("x"), is_admin=admin)
    s.add(u)
    s.flush()
    return u


def _auth(user: User) -> dict[str, str]:
    from analystos.security.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(user.id, user.email)}"}


def U(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        people = {r: _user(s, f"{r}-{new_id('x')}@t") for r in ("approver", "editor", "viewer")}
        ws = create_workspace(s, owner, name="Dashboards", objective="Build and publish dashboards", autonomy_level=3)
        s.flush()
        for role, u in people.items():
            add_member(s, owner, ws.id, u.email, role)
        qx = QueryExecution(id=new_id("qx"), workspace_id=ws.id, actor=f"user:{owner.id}", sql=(
            "SELECT priority, COUNT(*) AS incidents, region FROM itsm.incidents GROUP BY priority, region"),
            status="ok", referenced_assets=["itsm.incidents"])
        s.add(qx)
        s.add(VerifiedQuery(id=new_id("vq"), workspace_id=ws.id, name="weekly_incidents", sql_template=(
            "SELECT date_trunc('week', opened_at) AS week, COUNT(*) AS incidents, region FROM itsm.incidents "
            "GROUP BY 1, 3"), parameters=[], dialect="postgres", status="active", created_by=owner.id,
            origin={"assets": ["itsm.incidents"]}))
        ids = {"ws": ws.id, "owner": owner.id, "qx": qx.id, **{r: u.id for r, u in people.items()},
               "headers": {k: _auth(u) for k, u in {"owner": owner, **people}.items()}}
    return ids


@pytest.fixture(scope="module")
def api(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def document(world, did: str | None = None) -> dict:
    from tests.unit.test_dashboard_dsl import sample

    raw = copy.deepcopy(sample())
    raw["id"] = did or f"d_{new_id('x')[-8:].lower()}"
    raw["kpis"] = []  # metric datasets need an approved semantic model; covered by the compiler's own tests
    raw["layout"]["items"] = [i for i in raw["layout"]["items"] if i["ref"] != "kpi_open"]
    raw["text_blocks"][0]["markdown"] = "Incidents rose to 135 in the week of 12 October."
    raw["charts"][1]["dataset_ref"]["query_id"] = world["qx"]
    return raw


@pytest.fixture()
def superset():
    from tests.unit.test_publishing_superset import BASE, bi_login
    from tests.unit.test_superset_access import SecureSuperset

    from analystos.publishing.superset import SupersetPublisher
    from analystos.tenancy import DEFAULT_TENANT_ID

    fake = SecureSuperset()
    with respx.mock(base_url=BASE, assert_all_called=False, assert_all_mocked=False) as mock:
        mock.route(name="api").mock(side_effect=fake.handle)
        pub = SupersetPublisher(base_url=BASE, username="admin", password="admin", bi_login=bi_login,
                                analytics_sqlalchemy_uri="postgresql+psycopg2://r:r@postgres:5432/analytics",
                                tenant_of=lambda ws: DEFAULT_TENANT_ID)
        yield fake, pub


def _create(world, raw=None) -> str:
    from analystos.dashboards import service

    with session_scope() as s:
        return service.create(s, U(world["editor"]), world["ws"], raw or document(world)).id


def _approve(world, approval_id: str) -> None:
    from analystos.governance.approvals import decide

    with session_scope() as s:
        decide(s, approval_id, s.get(User, world["approver"]), approve=True)


def _publish(world, pub, art_id: str, audience=None) -> dict:
    from analystos.dashboards import service

    with session_scope() as s:
        approval = service.request_publish(s, U(world["editor"]), art_id, audience=audience or ["role:viewer"]).id
    _approve(world, approval)
    with session_scope() as s:
        out = service.execute_publish(s, U(world["editor"]), art_id, approval, publisher=pub)
    return {**out, "approval_id": approval}


# ------------------------------------------------------------------------------------ migration
def test_migration_0066_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from analystos.core.config import REPO_ROOT
    from analystos.db import vectors

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig66"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0066_dashboards_publishing")
        insp = inspect(engine)
        assert "diff" in {c["name"] for c in insp.get_columns("artifact_version")}
        assert {"artifact_id", "artifact_version", "drift", "reconciled_at"} <= {c["name"] for c in insp.get_columns("publication")}
        command.downgrade(cfg, "0065_tool_registry")
        insp = inspect(engine)
        assert "diff" not in {c["name"] for c in insp.get_columns("artifact_version")}
        assert "drift" not in {c["name"] for c in insp.get_columns("publication")}
        command.upgrade(cfg, "0066_dashboards_publishing")
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db} WITH (FORCE)"))
        admin.dispose()


# ------------------------------------------------------------------------------------ T2 over REST
def test_patch_ops_versioning(api, world):
    h = world["headers"]
    raw = document(world)
    assert api.post(f"/api/v2/workspaces/{world['ws']}/dashboards", json={"document": raw}, headers=h["viewer"]).status_code == 403
    bad = copy.deepcopy(raw)
    bad["charts"][0].pop("provenance")
    r = api.post(f"/api/v2/workspaces/{world['ws']}/dashboards", json={"document": bad}, headers=h["editor"])
    assert r.status_code == 422 and r.json()["error"]["details"]["problems"][0]["code"] == "schema"
    created = api.post(f"/api/v2/workspaces/{world['ws']}/dashboards", json={"document": raw}, headers=h["editor"])
    assert created.status_code == 201, created.text
    did = created.json()["id"]
    assert created.json()["version"] == 1 and created.json()["status"] == "draft"

    op = {"op": "chart.set_type", "chart_id": "trend", "type": "area"}
    r = api.post(f"/api/v2/dashboards/{did}/ops", json={"ops": [op]}, headers={**h["editor"], "If-Match": "1"})
    assert r.status_code == 200 and r.json()["status"] == "applied" and r.json()["dashboard"]["version"] == 2
    stale = api.post(f"/api/v2/dashboards/{did}/ops", json={"ops": [op]}, headers={**h["editor"], "If-Match": "1"})
    assert stale.status_code == 409
    refused = api.post(f"/api/v2/dashboards/{did}/ops", json={"ops": [{"op": "chart.set_type", "chart_id": "by_priority",
                                                                    "type": "line"}], "base_version": 2}, headers=h["editor"])
    assert refused.json()["status"] == "refused" and refused.json()["dashboard"]["version"] == 2
    assert refused.json()["refused"][0]["suggestion"]["type"] == "bar"
    assert api.post(f"/api/v2/dashboards/{did}/ops", json={"ops": [op]}, headers=h["viewer"]).status_code == 403

    said = api.post(f"/api/v2/dashboards/{did}/messages", json={"text": "compare year over year", "focus": "trend"},
                    headers=h["editor"])
    assert said.status_code == 200, said.text
    body = said.json()
    assert body["proposal"]["answered_by"] == "rules" and body["dashboard"]["version"] == 3
    chart = next(c for c in body["dashboard"]["document"]["charts"] if c["id"] == "trend")
    assert chart["query"] == {"version": 2, "compare": "yoy", "period": None}

    history = api.get(f"/api/v2/dashboards/{did}/versions", headers=h["viewer"]).json()
    assert [v["version"] for v in history] == [3, 2, 1]
    assert history[0]["diff"][0]["op"] == "query.extend_period" and history[0]["diff"][0]["origin"]["answered_by"] == "rules"
    assert history[1]["diff"] == [op] and history[2]["diff"] == [{"op": "create"}]

    undo = api.post(f"/api/v2/dashboards/{did}/revert", json={"to_version": 1}, headers=h["editor"]).json()
    v1 = api.get(f"/api/v2/dashboards/{did}/versions/1", headers=h["viewer"]).json()
    assert undo["dashboard"]["version"] == 4 and undo["dashboard"]["content_hash"] == v1["content_hash"]
    assert api.get(f"/api/v2/dashboards/{did}/versions", headers=h["viewer"]).json()[0]["diff"][0]["op"] == "revert"
    schema = api.get("/api/v2/schemas/dashboard", headers=h["viewer"]).json()
    assert "charts" in schema["properties"]


# ------------------------------------------------------------------------------------ publish flow
def test_publish_is_hash_bound_single_use_and_applies_access(world, superset):
    from analystos.dashboards import service

    fake, pub = superset
    art_id = _create(world)
    with session_scope() as s:
        approval = service.request_publish(s, U(world["editor"]), art_id, audience=["role:viewer"])
        aid = approval.id
        assert s.get(Artifact, art_id).status == "approval_required" and approval.risk_tier == "medium"
    with pytest.raises(ApprovalRequired), session_scope() as s:  # pending: not yet decided
        service.execute_publish(s, U(world["editor"]), art_id, aid, publisher=pub)
    _approve(world, aid)
    with session_scope() as s:  # an edit after approval invalidates it: the payload no longer matches
        service.apply(s, U(world["editor"]), art_id, [{"op": "chart.set_type", "chart_id": "trend", "type": "area"}])
    with pytest.raises(ApprovalRequired), session_scope() as s:
        service.execute_publish(s, U(world["editor"]), art_id, aid, publisher=pub)
    assert not fake.dashboards
    out = _publish(world, pub, art_id)
    assert out["status"] == "published" and out["url"]
    with session_scope() as s:
        art, p = s.get(Artifact, art_id), s.get(Publication, out["publication_id"])
        assert art.status == "published" and art.platform == "superset"
        assert p.idempotency_key == service.idempotency_key(art_id, art.version, "superset")
        assert p.artifact_id == art_id and p.artifact_version == art.version and p.drift["status"] == "in_sync"
        assert s.get(Approval, out["approval_id"]).status == "executed"
    with pytest.raises(ApprovalRequired), session_scope() as s:  # consumed: never replayable
        service.execute_publish(s, U(world["editor"]), art_id, out["approval_id"], publisher=pub)
    dash = int(next(iter(p.external_ids["dashboards"].values())))
    roles = {r["name"]: r["id"] for r in fake.roles.values()}
    slug = world["ws"].lower()
    assert set(fake.dashboards[dash]["roles"]) == {roles[f"ws_{slug}_viewer"], roles[f"ws_{slug}_editor"]}
    assert "bundle=" in fake.dashboards[dash]["css"]


def test_failed_publish_keeps_the_approval_for_a_reconciling_retry(world, superset):
    from analystos.core.errors import UpstreamUnavailable
    from analystos.dashboards import service

    fake, pub = superset
    art_id = _create(world)
    with session_scope() as s:
        aid = service.request_publish(s, U(world["editor"]), art_id).id
    _approve(world, aid)
    fake.fail[("POST", r"/api/v1/chart/")] = (500, {"message": "boom"})
    with pytest.raises(UpstreamUnavailable), session_scope() as s:
        try:
            service.execute_publish(s, U(world["editor"]), art_id, aid, publisher=pub)
        finally:
            s.commit()  # what the route does: keep the FAILED record
    with session_scope() as s:
        assert s.get(Artifact, art_id).status == "failed" and s.get(Approval, aid).status == "approved"
        assert s.scalar(select(Publication).where(Publication.artifact_id == art_id)).status == "partial"
    fake.fail.clear()
    with session_scope() as s:
        out = service.execute_publish(s, U(world["editor"]), art_id, aid, publisher=pub)
    assert out["status"] == "published"
    assert len(fake.datasets) == len({d["table_name"] for d in fake.datasets.values()})  # reconciled, no duplicates


def test_edit_published_forks_draft(api, world, superset):
    _, pub = superset
    art_id = _create(world)
    _publish(world, pub, art_id)
    with session_scope() as s:
        published_version = s.get(Artifact, art_id).version
    r = api.post(f"/api/v2/dashboards/{art_id}/ops", json={"ops": [{"op": "chart.set_type", "chart_id": "trend", "type": "area"}]},
                 headers=world["headers"]["editor"])
    body = r.json()
    assert r.status_code == 200 and body["forked_from"] == art_id and body["dashboard"]["id"] != art_id
    assert body["dashboard"]["status"] == "draft" and body["dashboard"]["version"] == 2
    with session_scope() as s:
        live = s.get(Artifact, art_id)
        assert live.status == "published" and live.version == published_version  # viewers see the approved version
    fork_history = api.get(f"/api/v2/dashboards/{body['dashboard']['id']}/versions", headers=world["headers"]["viewer"]).json()
    assert fork_history[-1]["diff"][0] == {"op": "fork", "from": {"artifact_id": art_id, "version": published_version}}


# ------------------------------------------------------------------------------------ T5 embedding
def test_guest_token_rechecks_auth(api, world, superset, monkeypatch):
    from analystos.dashboards import access, service

    fake, pub = superset
    monkeypatch.setattr("analystos.publishing.base.get_publisher", lambda *a, **k: pub)
    art_id = _create(world)
    _publish(world, pub, art_id, audience=["role:editor"])
    h = world["headers"]
    assert api.post(f"/api/v2/dashboards/{art_id}/embed-token", headers=h["viewer"]).status_code == 403  # not its audience
    ok = api.post(f"/api/v2/dashboards/{art_id}/embed-token", headers=h["editor"])
    assert ok.status_code == 200 and ok.json()["token"].startswith("guest-")

    art_b = _create(world)
    _publish(world, pub, art_b, audience=["role:viewer"])
    first = api.post(f"/api/v2/dashboards/{art_b}/embed-token", headers=h["viewer"])
    assert first.status_code == 200, first.text

    class Scope:  # the viewer's resolved scope: a row filter rendered with their attributes
        row_filters = {"itsm.incidents": ["region = 'EMEA'"]}
        withheld_assets: dict = {}
        denied_columns: list = []

    monkeypatch.setattr("analystos.governance.policy.resolve_scope", lambda *a, **k: Scope())
    with session_scope() as s:
        out = access.guest_token(s, U(world["viewer"]), art_b, publisher=pub)
    assert out["rls_clauses"] == 2 and all(r["clause"] == "(region = 'EMEA')" for r in fake.guest_requests[-1]["rls"])
    Scope.withheld_assets = {"itsm.incidents": "missing attribute region"}
    with pytest.raises(Forbidden), session_scope() as s:
        access.guest_token(s, U(world["viewer"]), art_b, publisher=pub)
    monkeypatch.undo()
    monkeypatch.setattr("analystos.publishing.base.get_publisher", lambda *a, **k: pub)

    with session_scope() as s:  # membership removed: rechecked at request time, not at publish time
        m = s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == world["ws"],
                                                   WorkspaceMember.user_id == world["viewer"]))
        role = m.role
        s.delete(m)
    try:
        assert api.post(f"/api/v2/dashboards/{art_b}/embed-token", headers=h["viewer"]).status_code == 404
    finally:
        with session_scope() as s:
            s.add(WorkspaceMember(workspace_id=world["ws"], user_id=world["viewer"], role=role))
    with session_scope() as s:  # rolled back: no token for an unpublished dashboard
        s.get(Artifact, art_b).status = "rolled_back"
    assert api.post(f"/api/v2/dashboards/{art_b}/embed-token", headers=h["viewer"]).status_code == 422
    with session_scope() as s:
        tokens = s.scalars(select(AuditEvent.action).where(AuditEvent.workspace_id == world["ws"],
                                                           AuditEvent.action.like("dashboard.embed%"))).all()
    assert "dashboard.embed_token" in tokens and "dashboard.embed_denied" in tokens
    assert service.APPROVAL_ACTION == "dashboard.publish"


def test_cross_tenant_superset_denied(api, world, superset):
    """A tenant-B administrator cannot read, edit, publish, embed or export tenant A's dashboards (404), and
    tenant-B work cannot load them at all (RLS), whatever ids it is given."""
    from analystos.dashboards import access, service
    from analystos.db.models import Tenant, Workspace
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope, tenant_scope

    _, pub = superset
    art_id = _create(world)
    out = _publish(world, pub, art_id)
    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"s08-{tb[-6:]}", name="Tenant B"))
    with tenant_scope(tb), session_scope() as s:
        admin_b = User(id=new_id("usr"), email=f"admin-{tb}@b.test", name="B admin", password_hash=hash_password("x"),
                       is_admin=True)
        s.add(admin_b)
        s.flush()
        s.add(Workspace(id=new_id("ws"), name="B workspace", created_by=admin_b.id))
        s.expunge(admin_b)
    hb = _auth(admin_b)
    for method, path, body in (("get", f"/api/v2/dashboards/{art_id}", None),
                               ("post", f"/api/v2/dashboards/{art_id}/ops", {"ops": [{"op": "chart.remove", "chart_id": "trend"}]}),
                               ("post", f"/api/v2/dashboards/{art_id}/embed-token", None),
                               ("post", f"/api/v2/dashboards/{art_id}/publish", {"approval_id": out["approval_id"]}),
                               ("get", f"/api/v2/dashboards/{art_id}/exports/csv", None)):
        r = getattr(api, method)(path, headers=hb, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (path, r.status_code, r.text)
    with tenant_scope(tb), session_scope() as s:
        assert s.get(Artifact, art_id) is None and s.get(Publication, out["publication_id"]) is None
        with pytest.raises(NotFound):
            access.guest_token(s, admin_b, art_id, publisher=pub)
        with pytest.raises(NotFound):
            service.load(s, admin_b, art_id)


# ------------------------------------------------------------------------------------ reconcile
def test_reconcile_flags_drift_and_repairs_only_under_the_approval(world, superset):
    from analystos.dashboards.reconcile import reconcile_publication

    fake, pub = superset
    art_id = _create(world)
    out = _publish(world, pub, art_id)
    with session_scope() as s:
        p = s.get(Publication, out["publication_id"])
        dash = int(next(iter(p.external_ids["dashboards"].values())))
        assert reconcile_publication(s, p, publisher=pub)["status"] == "in_sync"
    del fake.dashboards[dash]
    with session_scope() as s:
        p = s.get(Publication, out["publication_id"])
        flagged = reconcile_publication(s, p, publisher=pub)
        assert flagged["status"] == "drifted" and flagged["items"][0]["drift"] == "missing"
        assert p.drift["status"] == "drifted" and p.reconciled_at is not None
    with session_scope() as s:
        repaired = reconcile_publication(s, s.get(Publication, out["publication_id"]), publisher=pub, repair=True)
    assert repaired["status"] == "repaired" and fake.dashboards
    victim = next(iter(fake.dashboards))
    del fake.dashboards[victim]
    with session_scope() as s:  # the approver lost approval rights: drift is flagged, never repaired
        m = s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == world["ws"],
                                                   WorkspaceMember.user_id == world["approver"]))
        m.role = "viewer"
    try:
        with session_scope() as s:
            refused = reconcile_publication(s, s.get(Publication, out["publication_id"]), publisher=pub, repair=True)
        assert refused["status"] == "drifted" and refused["repair"]["status"] == "refused"
        assert victim not in fake.dashboards
    finally:
        with session_scope() as s:
            s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == world["ws"],
                                                   WorkspaceMember.user_id == world["approver"])).role = "approver"


# ------------------------------------------------------------------------------------ T6 exports
def test_export_needs_an_approval_by_classification_and_is_watermarked(api, world):
    h = world["headers"]
    internal = _create(world)
    r = api.post(f"/api/v2/dashboards/{internal}/exports", json={"format": "csv"}, headers=h["viewer"])
    assert r.json() == {"status": "allowed", "sensitivity": "internal"}
    csv = api.get(f"/api/v2/dashboards/{internal}/exports/csv", headers=h["viewer"])
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    first_line = csv.content.decode("utf-8-sig").splitlines()[0]
    assert U(world["viewer"]).email in first_line and "INTERNAL" in first_line

    raw = document(world)
    raw["charts"][0]["fields"][1]["sensitivity"] = "confidential"
    secret = _create(world, raw)
    assert api.get(f"/api/v2/dashboards/{secret}/exports/pdf", headers=h["editor"]).status_code in (403, 409, 428)
    viewer_req = api.post(f"/api/v2/dashboards/{secret}/exports", json={"format": "pdf"}, headers=h["viewer"])
    assert viewer_req.status_code == 403  # publish-class: an editor requests it
    req = api.post(f"/api/v2/dashboards/{secret}/exports", json={"format": "pdf"}, headers=h["editor"]).json()
    assert req["status"] == "approval_required" and req["sensitivity"] == "confidential"
    aid = req["approval"]["id"]
    _approve(world, aid)
    other = api.get(f"/api/v2/dashboards/{secret}/exports/pdf", params={"approval_id": aid}, headers=h["owner"])
    assert other.status_code == 403  # granted to the requester only
    wrong_format = api.get(f"/api/v2/dashboards/{secret}/exports/csv", params={"approval_id": aid}, headers=h["editor"])
    assert wrong_format.status_code != 200  # bound to the format it approved
    with session_scope() as s:
        s.get(Approval, aid).status = "approved"  # the mismatch invalidated it; restore to show the exact match works
        s.get(Approval, aid).reason = None
    pdf = api.get(f"/api/v2/dashboards/{secret}/exports/pdf", params={"approval_id": aid}, headers=h["editor"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    again = api.get(f"/api/v2/dashboards/{secret}/exports/pdf", params={"approval_id": aid}, headers=h["editor"])
    assert again.status_code != 200  # single use
    with session_scope() as s:
        audits = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == world["ws"],
                                                    AuditEvent.action == "export.created")).all()
    assert {a.details["format"] for a in audits} >= {"csv", "pdf"} and all(a.details["sha256"] for a in audits)

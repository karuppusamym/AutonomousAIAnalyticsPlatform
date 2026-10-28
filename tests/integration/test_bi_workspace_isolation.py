"""P4-02 (SEC-007, GOV-002): BI workspace isolation, live against the compose Superset + Postgres.

Two workspaces stage a table each. Each workspace's Superset database connects as that workspace's
BI login; each Superset user holds Gamma (+ sql_lab) and only their workspace's role. Proven here:

  * direct DB: workspace A's BI login reads A, not B - not by name, not after ``SET ROLE`` or
    ``set_config('role', ...)`` (the path the shared reader login left open), and never writes;
  * Superset SQL Lab: a user of A cannot run SQL on B's database, and SQL on A's database that
    names B's schema (or switches role first) is refused by Postgres;
  * Superset API: B's database, dataset and chart data are invisible or 403 to A's user;
  * a synced user never keeps an all-data role (Admin/Alpha).

Skips when Superset is down. The user-scoping tests also skip when Superset runs without
FAB_ADD_SECURITY_API (a running instance needs a restart after this change); point
ANALYSTOS_SUPERSET_URL at an instance started from deploy/superset.
"""
from __future__ import annotations

import contextlib
import os
import secrets

import httpx
import psycopg
import pytest

from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Forbidden, NotFound
from analystos.publishing.superset import SupersetClient, SupersetPublisher

pytestmark = pytest.mark.integration

_SUFFIX = os.environ.get("ANALYSTOS_TEST_DP_DB", "analystos_test_dp").removeprefix("analystos_test_dp").strip("_") or "x"
WS = {"a": f"ws_biiso_a_{_SUFFIX}", "b": f"ws_biiso_b_{_SUFFIX}"}
SCHEMA = {"a": f"src_biiso_a_{_SUFFIX}", "b": f"src_biiso_b_{_SUFFIX}"}
PASSWORD = "Bi-" + secrets.token_urlsafe(12)


def _pg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="module")
def settings(analytics_plane):
    s = get_settings()
    try:
        if httpx.get(f"{s.superset_url}/health", timeout=5).status_code != 200:
            raise httpx.HTTPError("unhealthy")
    except httpx.HTTPError:
        pytest.skip(f"Superset not reachable at {s.superset_url}/health")
    return s


def _security_api(pub: SupersetPublisher) -> bool:
    try:
        pub.client.get("/api/v1/security/roles/")
        return True
    except NotFound:
        return False


@pytest.fixture()
def users(world):
    if not world["security_api"]:
        pytest.skip("Superset runs without FAB_ADD_SECURITY_API (restart it with deploy/superset/superset_config.py)")
    return world


@pytest.fixture(scope="module")
def world(settings):
    """Staged tables, Superset databases/datasets/users for A and B; everything removed afterwards."""
    from analystos.staging.roles import ensure_workspace_role, grant_schema, reader_login, role_for

    with psycopg.connect(_pg(settings.analytics_loader_url), autocommit=True) as conn:
        for k in ("a", "b"):
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA[k]} CASCADE")
            conn.execute(f"CREATE SCHEMA {SCHEMA[k]}")
            conn.execute(f"CREATE TABLE {SCHEMA[k]}.orders (id int, secret text)")
            conn.execute(f"INSERT INTO {SCHEMA[k]}.orders VALUES (1, 'secret-{k}-1'), (2, 'secret-{k}-2')")
            with conn.transaction(), conn.cursor() as cur:
                ensure_workspace_role(cur, role_for(settings, WS[k]), reader_login(settings))
                grant_schema(cur, SCHEMA[k], role_for(settings, WS[k]), reader_login(settings))
    pub = SupersetPublisher(settings)
    made: dict = {"db": {}, "ds": {}, "user": {}, "role": {}, "security_api": _security_api(pub)}
    try:
        for k in ("a", "b"):
            db, _ = pub.ensure_database(WS[k])
            made["db"][k] = db
            # An admin exposing the database in SQL Lab must not widen what it can read.
            pub.client.put(f"/api/v1/database/{db}", {"expose_in_sqllab": True})
            made["ds"][k] = int(pub.client.post("/api/v1/dataset/", {
                "database": db, "schema": SCHEMA[k], "table_name": f"aos_biiso_{k}_{_SUFFIX}",
                "sql": f"SELECT id, secret FROM {SCHEMA[k]}.orders"})["id"])
            if not made["security_api"]:
                continue
            made["user"][k] = pub.sync_user(username=f"biiso_{k}_{_SUFFIX}", email=f"biiso_{k}_{_SUFFIX}@test.local",
                                            workspace_ids=[WS[k]], password=PASSWORD, sql_lab=True)
            made["role"][k] = pub._role_id(pub.workspace_role_name(WS[k]))
        yield {"pub": pub, **made}
    finally:
        for uid in made["user"].values():
            _quiet(pub.client.delete, f"/api/v1/security/users/{uid}")
        for rid in made["role"].values():
            _quiet(pub.client.delete, f"/api/v1/security/roles/{rid}")
        for ds in made["ds"].values():
            _quiet(pub.client.delete, f"/api/v1/dataset/{ds}")
        for db in made["db"].values():
            _quiet(pub.client.delete, f"/api/v1/database/{db}")
        pub.close()


def _quiet(fn, *a):
    with contextlib.suppress(AnalystOSError):
        fn(*a)


def _as_user(settings, k: str) -> SupersetClient:
    return SupersetClient(settings.superset_url, f"biiso_{k}_{_SUFFIX}", PASSWORD)


def _bi_dsn(settings, k: str) -> str:
    from sqlalchemy.engine import make_url

    from analystos.staging.roles import bi_login_for, bi_password

    login = bi_login_for(settings, WS[k])
    return make_url(_pg(settings.analytics_loader_url)).set(username=login, password=bi_password(settings, login)) \
        .render_as_string(hide_password=False)


# -- direct DB ------------------------------------------------------------------------------------

def test_bi_login_reads_its_workspace_only_even_with_role_switching(settings, world):
    from analystos.staging.roles import role_for

    other = role_for(settings, WS["b"])
    with psycopg.connect(_bi_dsn(settings, "a"), autocommit=True) as conn:
        assert conn.execute(f"SELECT count(*) FROM {SCHEMA['a']}.orders").fetchone()[0] == 2
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {SCHEMA['b']}.orders")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f'SET ROLE "{other}"')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT set_config('role', %s, false)", (other,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # not even into its own role: grants inherit only
            conn.execute(f'SET ROLE "{role_for(settings, WS["a"])}"')
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            conn.execute(f"CREATE TABLE {SCHEMA['a']}.x (i int)")
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            conn.execute(f"DELETE FROM {SCHEMA['a']}.orders")


def test_superset_databases_use_workspace_bi_logins_not_the_shared_reader(settings, world):
    from sqlalchemy.engine import make_url

    from analystos.staging.roles import bi_login_for, reader_login

    for k in ("a", "b"):
        uri = make_url(world["pub"].client.get(f"/api/v1/database/{world['db'][k]}/connection")["result"]["sqlalchemy_uri"])
        assert uri.username == bi_login_for(settings, WS[k]) != reader_login(settings)
        assert "options" not in uri.query


def test_legacy_reader_database_is_moved_to_the_bi_login(settings, world):
    """A database created before P4-02 (shared reader + ``-c role=``) is rewritten on the next publish."""
    from sqlalchemy.engine import make_url

    from analystos.staging.roles import bi_login_for, role_for

    pub, db = world["pub"], world["db"]["a"]
    legacy = make_url(settings.superset_analytics_sqlalchemy_uri).update_query_dict(
        {"options": f"-c role={role_for(settings, WS['a'])}"}).render_as_string(hide_password=False)
    pub.client.put(f"/api/v1/database/{db}", {"sqlalchemy_uri": legacy})
    assert pub.ensure_database(WS["a"]) == (db, False)
    uri = make_url(pub.client.get(f"/api/v1/database/{db}/connection")["result"]["sqlalchemy_uri"])
    assert uri.username == bi_login_for(settings, WS["a"]) and "options" not in uri.query


# -- Superset SQL Lab and API ----------------------------------------------------------------------

def _sqllab(client: SupersetClient, database_id: int, sql: str):
    return client.request("POST", "/api/v1/sqllab/execute/", raw=True, json_body={
        "database_id": database_id, "sql": sql, "runAsync": False, "schema": None, "tab": "p402",
        "client_id": secrets.token_hex(5), "queryLimit": 100, "select_as_cta": False, "tmp_table_name": "",
        "expand_data": True})


def _sqllab_result(client, database_id, sql) -> tuple[int, str]:
    try:
        resp = _sqllab(client, database_id, sql)
        return resp.status_code, resp.text
    except AnalystOSError as exc:
        return int(exc.details.get("status", 0)), str(exc.details.get("superset_message", exc.message))


def test_sql_lab_user_of_a_cannot_query_workspace_b(settings, users):
    world = users
    from analystos.staging.roles import role_for

    ua = _as_user(settings, "a")
    try:
        status, body = _sqllab_result(ua, world["db"]["a"], f"SELECT secret FROM {SCHEMA['a']}.orders ORDER BY id")
        assert status == 200 and "secret-a-1" in body, body[:500]
        # B's database: filtered out by Superset's database_access check before any SQL runs (4.1 answers
        # "database ... not found" with HTTP 500).
        status, body = _sqllab_result(ua, world["db"]["b"], f"SELECT secret FROM {SCHEMA['b']}.orders")
        assert status >= 400 and "secret-b" not in body and "not found" in body.lower(), (status, body[:500])
        # B's schema through A's database: Postgres refuses (A's BI login holds no grant on it) ...
        status, body = _sqllab_result(ua, world["db"]["a"], f"SELECT secret FROM {SCHEMA['b']}.orders")
        assert status >= 400 and "secret-b" not in body and "permission denied" in body.lower(), (status, body[:500])
        # ... also after trying to switch role inside a SELECT (worked with the shared reader login).
        other = role_for(settings, WS["b"])
        status, body = _sqllab_result(ua, world["db"]["a"], f"SELECT set_config('role', '{other}', false)")
        assert status >= 400 and "permission denied" in body.lower(), (status, body[:500])
        status, body = _sqllab_result(
            ua, world["db"]["a"], f"SELECT set_config('role', '{other}', false);\nSELECT secret FROM {SCHEMA['b']}.orders")
        assert status >= 400 and "secret-b" not in body, (status, body[:500])
    finally:
        ua.close()


def test_superset_api_hides_workspace_b_from_a_user(settings, users):
    world = users
    ua = _as_user(settings, "a")
    try:
        dbs = {d["id"] for d in ua.list("database", [])}
        assert world["db"]["a"] in dbs and world["db"]["b"] not in dbs
        with pytest.raises((NotFound, Forbidden)):
            ua.get(f"/api/v1/database/{world['db']['b']}")
        datasets = {d["id"] for d in ua.list("dataset", [])}
        assert world["ds"]["a"] in datasets and world["ds"]["b"] not in datasets
        with pytest.raises((NotFound, Forbidden)):
            ua.get(f"/api/v1/dataset/{world['ds']['b']}")

        def chart_data(ds: int):
            return ua.post("/api/v1/chart/data", {
                "datasource": {"id": ds, "type": "table"}, "result_format": "json", "result_type": "full",
                "queries": [{"columns": ["secret"], "metrics": [], "row_limit": 10}]})

        rows = chart_data(world["ds"]["a"])["result"][0]["data"]
        assert {r["secret"] for r in rows} == {"secret-a-1", "secret-a-2"}
        with pytest.raises((Forbidden, NotFound)):
            chart_data(world["ds"]["b"])
    finally:
        ua.close()


def test_sync_user_strips_all_data_roles(settings, users):
    world = users
    pub = world["pub"]
    uid = world["user"]["a"]
    admin_role = pub._role_id("Admin")
    user = pub.client.get(f"/api/v1/security/users/{uid}")["result"]
    pub.client.put(f"/api/v1/security/users/{uid}", {"roles": [admin_role, *(r["id"] for r in user["roles"])]})
    pub.sync_user(username=f"biiso_a_{_SUFFIX}", email=f"biiso_a_{_SUFFIX}@test.local", workspace_ids=[WS["a"]],
                  sql_lab=True)
    roles = {r["name"] for r in pub.client.get(f"/api/v1/security/users/{uid}")["result"]["roles"]}
    assert roles == {"Gamma", "sql_lab", pub.workspace_role_name(WS["a"])}
    perms = pub.client.get(f"/api/v1/security/roles/{world['role']['a']}/permissions/")["result"]
    assert [p["permission_name"] for p in perms] == ["database_access"]
    assert f"(id:{world['db']['a']})" in perms[0]["view_menu_name"]


def test_bi_sync_maps_workspace_membership_to_superset_roles(settings, users, control_db):
    world = users
    """``analystos bi-sync``: a viewer of A gets Gamma + A's viewer role (no SQL Lab); an analyst of both gets both
    workspaces' viewer and editor roles (S08)."""
    from sqlalchemy import select

    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User, Workspace, WorkspaceMember
    from analystos.services.bi_access import sync_bi_access

    pub = world["pub"]
    emails = {"viewer": f"biiso_viewer_{_SUFFIX}@test.local", "analyst": f"biiso_analyst_{_SUFFIX}@test.local"}
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == settings.bootstrap_admin_email))
        for k in ("a", "b"):
            if s.get(Workspace, WS[k]) is None:
                s.add(Workspace(id=WS[k], name=f"BI isolation {k}", created_by=owner.id))
        s.flush()
        ids = {}
        for who, email in emails.items():
            u = s.scalar(select(User).where(User.email == email))
            if u is None:
                u = User(id=new_id("usr"), email=email, name=f"Bi {who.title()}", password_hash="!")
                s.add(u)
                s.flush()
            ids[who] = u.id
        s.add(WorkspaceMember(workspace_id=WS["a"], user_id=ids["viewer"], role="viewer"))
        s.add(WorkspaceMember(workspace_id=WS["a"], user_id=ids["analyst"], role="analyst"))
        s.add(WorkspaceMember(workspace_id=WS["b"], user_id=ids["analyst"], role="analyst"))
    made: dict = {}
    try:
        with session_scope() as s:
            made = sync_bi_access(s, pub, [WS["a"], WS["b"]])["users"]
        roles = {who: {r["name"] for r in pub.client.get(f"/api/v1/security/users/{made[email]}")["result"]["roles"]}
                 for who, email in emails.items()}
        assert roles["viewer"] == {"Gamma", pub.workspace_role_name(WS["a"])}
        assert roles["analyst"] == {"Gamma", "sql_lab", pub.workspace_role_name(WS["a"]), pub.workspace_role_name(WS["b"]),
                                    pub.editor_role_name(WS["a"]), pub.editor_role_name(WS["b"])}  # S08: authors edit
    finally:
        for uid in made.values():
            _quiet(pub.client.delete, f"/api/v1/security/users/{uid}")
        with session_scope() as s:
            for m in s.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(list(WS.values())))):
                s.delete(m)

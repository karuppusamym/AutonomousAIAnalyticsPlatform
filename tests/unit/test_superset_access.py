"""S08-T4/T5 against an in-memory Superset: tenant-stamped connections (cross-tenant reuse refused),
viewer/editor roles, RLS rules from workspace policy, dashboard RBAC, embedding and guest tokens, drift."""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from tests.unit.test_publishing_superset import BASE, FakeSuperset, bi_login, make_bundle

from analystos.contracts.policy import RowFilter, WorkspacePolicyDoc
from analystos.core.errors import Forbidden, InvalidInput
from analystos.dashboards.access import static_rls_rules, viewer_rls
from analystos.dashboards.reconcile import inspect
from analystos.publishing.base import bundle_hash
from analystos.publishing.superset import SupersetPublisher
from analystos.publishing.superset_access import FAIL_CLOSED

PERMS = ["database_access", "can_write"]


def _output_columns(sql: str) -> list[str]:
    """Output column names of a SELECT, looking through `*` into subqueries and CTEs (None-safe)."""
    import sqlglot
    from sqlglot import exp

    def cols(node) -> list[str]:
        if isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.Select):
            return []
        out: list[str] = []
        with_ = node.args.get("with_") or node.args.get("with")
        from_ = node.args.get("from_") or node.args.get("from")
        ctes = {c.alias: c.this for c in (with_.expressions if with_ else [])}
        for proj in node.expressions:
            if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
                src = from_.this if from_ else None
                if isinstance(src, exp.Table) and src.name in ctes:
                    out += cols(ctes[src.name])
                elif src is not None:
                    out += cols(src)
            else:
                out.append(proj.alias_or_name)
        return out

    try:
        return [c for c in cols(sqlglot.parse_one(sql, read="postgres")) if c]
    except sqlglot.errors.ParseError:
        return []


class SecureSuperset(FakeSuperset):
    """FakeSuperset plus the security, RLS and embedding APIs (FAB_ADD_SECURITY_API, EMBEDDED_SUPERSET)."""

    def __init__(self) -> None:
        super().__init__()
        self.roles: dict[int, dict] = {}
        self.permissions = {i: {"id": i, "name": n} for i, n in enumerate(PERMS, start=1)}
        self.resources: dict[int, dict] = {50: {"id": 50, "name": "Dashboard"}, 51: {"id": 51, "name": "Chart"}}
        self.pvms: dict[int, dict] = {500: {"id": 500, "permission": 2, "view_menu": 50},
                                      501: {"id": 501, "permission": 2, "view_menu": 51}}
        self.rls: dict[int, dict] = {}
        self.embedded: dict[int, str] = {}
        self.embed_domains: dict[int, list[str]] = {}
        self.embed_writes = 0
        self.guest_requests: list[dict] = []

    def create(self, res, body):  # a new database gets its database_access resource and pvm, as in Superset
        resp = super().create(res, body)
        if res == "dataset" and resp.status_code == 201:  # columns come from the SQL, as Superset derives them
            names = _output_columns(body.get("sql") or "")
            if names:
                ds = self.datasets[resp.json()["id"]]
                ds["columns"] = [{"id": self._id(), "column_name": c, "type": "TEXT", "is_dttm": False,
                                  "changed_on": "2026-01-01", "type_generic": 1} for c in names]
        if res == "database":
            oid = resp.json()["id"]
            vm = self._id()
            self.resources[vm] = {"id": vm, "name": f"[{body['database_name']}].(id:{oid})"}
            pvm = self._id()
            self.pvms[pvm] = {"id": pvm, "permission": 1, "view_menu": vm}
        return resp

    def route(self, method, path, body, q):
        parts = path.strip("/").split("/")[2:]
        if parts[:1] == ["security"]:
            return self.security(method, parts[1:], body, q)
        if parts[:1] == ["rowlevelsecurity"]:
            rest = [p for p in parts[1:] if p]
            if not rest and method == "GET":
                rows = [r for r in self.rls.values() if all(
                    str(r.get(f["col"], "")).startswith(f["value"]) for f in q.get("filters", []) if f["opr"] == "sw")]
                return httpx.Response(200, json={"count": len(rows), "result": rows})
            if not rest and method == "POST":
                oid = self._id()
                self.rls[oid] = {"id": oid, **body}
                return httpx.Response(201, json={"id": oid, "result": body})
            oid = int(rest[0])
            if method == "PUT":
                self.rls[oid].update(body)
                return httpx.Response(200, json={"id": oid, "result": body})
            if method == "DELETE":
                self.rls.pop(oid, None)
                return httpx.Response(200, json={"message": "OK"})
        if parts[:1] == ["dashboard"] and len(parts) == 3 and parts[2] == "embedded":
            did = int(parts[1])
            if did not in self.dashboards:
                return httpx.Response(404, json={"message": "Not found"})
            if method == "GET":
                if did not in self.embedded:
                    return httpx.Response(404, json={"message": "Not found"})
                return httpx.Response(200, json={"result": {"uuid": self.embedded[did],
                                                            "allowed_domains": self.embed_domains.get(did, [])}})
            self.embedded[did] = f"uuid-{did}"  # an upsert keeps the uuid
            self.embed_domains[did] = list((body or {}).get("allowed_domains") or [])
            self.embed_writes += 1
            return httpx.Response(200, json={"result": {"uuid": self.embedded[did]}})
        return super().route(method, path, body, q)

    def security(self, method, parts, body, q):
        kind = parts[0]
        if kind == "guest_token":
            self.guest_requests.append(body)
            return httpx.Response(200, json={"token": f"guest-{len(self.guest_requests)}"})
        stores = {"roles": self.roles, "permissions": self.permissions, "resources": self.resources,
                  "permissions-resources": self.pvms, "users": {}}
        store = stores[kind]
        rest = [p for p in parts[1:] if p]
        if kind == "roles" and len(rest) == 2 and rest[1] == "permissions":
            self.roles[int(rest[0])]["pvms"] = body["permission_view_menu_ids"]
            return httpx.Response(200, json={"result": body})
        if method == "POST" and not rest:
            oid = self._id()
            store[oid] = {"id": oid, **body}
            return httpx.Response(201, json={"id": oid, "result": body})
        rows = []
        for o in store.values():
            ok = True
            for f in q.get("filters", []):
                if f["opr"] == "eq" and o.get(f["col"]) != f["value"]:
                    ok = False
                if f["opr"] == "rel_o_m" and o.get(f["col"]) != f["value"]:
                    ok = False
            if ok:
                rows.append(o)
        return httpx.Response(200, json={"count": len(rows), "result": rows})


@pytest.fixture
def fake() -> SecureSuperset:
    return SecureSuperset()


@pytest.fixture
def router(fake):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.route(name="api").mock(side_effect=fake.handle)
        yield mock


def publisher(tenant: str = "t_a") -> SupersetPublisher:
    return SupersetPublisher(base_url=BASE, username="admin", password="admin", bi_login=bi_login,
                             analytics_sqlalchemy_uri="postgresql+psycopg2://r:r@postgres:5432/analytics",
                             tenant_of=lambda ws: tenant)


def test_connections_are_stamped_and_another_tenants_is_never_reused(router, fake):
    """test_cross_tenant_superset_denied (publisher half): a connection named for the workspace but stamped for
    another tenant (or another workspace) is refused, never reconfigured or reused."""
    db_id, created = publisher("t_a").ensure_database("ws_unit")
    extra = json.loads(fake.databases[db_id]["extra"])
    assert created and extra["aos_tenant"] == "t_a" and extra["aos_workspace"] == "ws_unit"
    assert publisher("t_a").ensure_database("ws_unit") == (db_id, False)
    with pytest.raises(Forbidden):
        publisher("t_b").ensure_database("ws_unit")  # same name, other tenant
    fake.databases[db_id]["extra"] = json.dumps({**extra, "aos_workspace": "ws_other"})
    with pytest.raises(Forbidden):
        publisher("t_a").ensure_database("ws_unit")
    result = publisher("t_b").publish(make_bundle("ws_unit"), idempotency_key="k")
    assert result.status == "failed" and "another tenant" in result.errors[0]
    assert not fake.datasets  # nothing was created through the foreign connection


def test_unstamped_legacy_connection_gets_stamped(router, fake):
    pub = publisher("t_a")
    db_id, _ = pub.ensure_database("ws_unit")
    fake.databases[db_id]["extra"] = json.dumps({"aos_bi_login": "old"})
    pub.ensure_database("ws_unit")
    assert json.loads(fake.databases[db_id]["extra"])["aos_tenant"] == "t_a"


def test_viewer_and_editor_roles(router, fake):
    roles = publisher().ensure_workspace_roles("ws_unit")
    viewer, editor = fake.roles[roles["viewer"]], fake.roles[roles["editor"]]
    assert viewer["name"] == "ws_ws_unit_viewer" and editor["name"] == "ws_ws_unit_editor"
    assert len(viewer["pvms"]) == 1 and set(editor["pvms"]) == {viewer["pvms"][0], 500, 501}


def test_rls_rules_from_policy_and_fail_closed(router, fake):
    pub = publisher()
    result = pub.publish(make_bundle("ws_unit"), idempotency_key="k")
    datasets = result.external_ids["datasets"]
    policy = WorkspacePolicyDoc(row_filters=[
        RowFilter(id="emea_only", assets=["src_publishtest.incidents"], predicate="priority <> 'P5'"),
        RowFilter(id="own_group", assets=["*.incidents"], predicate="assignment_group = {{user.group}}"),
        RowFilter(id="other", assets=["hr.salaries"], predicate="1 = 1"),
    ])
    rules = static_rls_rules(policy, make_bundle("ws_unit"), datasets)
    by_id = {r["filter_id"]: r for r in rules}
    assert set(by_id) == {"emea_only", "own_group"}
    assert by_id["emea_only"]["clause"] == "priority <> 'P5'" and not by_id["emea_only"]["closed"]
    assert by_id["own_group"]["clause"] == FAIL_CLOSED and by_id["own_group"]["closed"]  # needs the viewer
    roles = pub.ensure_workspace_roles("ws_unit")
    synced = pub.sync_rls("ws_unit", rules, [roles["viewer"], roles["editor"]])
    assert len(fake.rls) == 2 and all(r["roles"] == sorted(roles.values()) for r in fake.rls.values())
    pub.sync_rls("ws_unit", rules[:1], [roles["viewer"]])  # a removed policy filter removes its rule
    assert [r["name"] for r in fake.rls.values()] == [sorted(synced)[0]]


def test_dashboard_rbac_embedding_and_guest_token(router, fake):
    pub = publisher()
    result = pub.publish(make_bundle("ws_unit"), idempotency_key="k")
    dash = int(result.external_ids["dashboards"]["executive"])
    pub.set_dashboard_roles(dash, [7, 3, 7])
    assert fake.dashboards[dash]["roles"] == [3, 7]
    uuid = pub.ensure_embedded(dash, ["http://ui.test"])
    assert uuid == pub.ensure_embedded(dash, ["http://ui.test"]) == f"uuid-{dash}" and fake.embed_writes == 1
    # a changed origin updates the embed config in place, same uuid
    assert pub.ensure_embedded(dash, ["http://ui2.test"]) == uuid and fake.embed_domains[dash] == ["http://ui2.test"]
    assert fake.embed_writes == 2
    for empty in ([], None):  # Superset reads [] as "any site": never written, never replaces a pinned list
        with pytest.raises(InvalidInput):
            pub.ensure_embedded(dash, empty)
    assert fake.embed_domains[dash] == ["http://ui2.test"] and fake.embed_writes == 2
    assert pub.dashboard_dataset_ids(dash) == sorted(int(i) for i in result.external_ids["datasets"].values())
    token = pub.guest_token(user={"username": "a@x"}, dashboard_uuid=uuid, rls=[{"dataset": 1, "clause": "x = 1"}])
    assert token == "guest-1" and fake.guest_requests[0]["resources"] == [{"type": "dashboard", "id": uuid}]


class Scope:
    def __init__(self, row_filters=None, withheld=None, denied=None, assets=("src_publishtest.incidents",)):
        self.row_filters, self.withheld_assets, self.denied_columns = row_filters or {}, withheld or {}, denied or []
        self.assets = list(assets)


def test_viewer_rls_renders_their_filters_and_refuses_what_they_cannot_see():
    bundle = make_bundle("ws_unit")
    ids = {"incidents": 9}
    assert viewer_rls(Scope(), bundle, ids) == []
    rls = viewer_rls(Scope({"src_publishtest.incidents": ["assignment_group = 'Network'"]}), bundle, ids)
    assert rls == [{"dataset": 9, "clause": "(assignment_group = 'Network')"}]
    with pytest.raises(Forbidden):
        viewer_rls(Scope(withheld={"src_publishtest.incidents": "missing attribute group"}), bundle, ids)
    with pytest.raises(Forbidden):
        viewer_rls(Scope(denied=["src_publishtest.incidents.priority"]), bundle, ids)
    with pytest.raises(Forbidden):  # a filter on a column the dataset does not carry cannot be enforced
        viewer_rls(Scope({"src_publishtest.incidents": ["region = 'EMEA'"]}), bundle, ids)
    # names that differ only in case still match: the filter is carried, the asset counts as in scope
    rls = viewer_rls(Scope({"SRC_PUBLISHTEST.Incidents": ["assignment_group = 'Network'"]},
                           assets=["SRC_publishtest.INCIDENTS"]), bundle, ids)
    assert rls == [{"dataset": 9, "clause": "(assignment_group = 'Network')"}]


def test_viewer_rls_refuses_an_asset_that_left_the_viewers_scope():
    """A guest holds no workspace role, so static rules never reach it: an asset deselected (or of a source no
    longer ready) after publish has no filter in scope and would show unfiltered. It is refused instead."""
    bundle = make_bundle("ws_unit")
    ids = {"incidents": 9}
    with pytest.raises(Forbidden, match="no longer in your data scope"):
        viewer_rls(Scope(assets=[]), bundle, ids)
    with pytest.raises(Forbidden, match="no longer in your data scope"):
        viewer_rls(Scope(assets=["src_publishtest.other"]), bundle, ids)
    bundle.datasets[0].source_assets = []
    with pytest.raises(Forbidden, match="names no source asset"):
        viewer_rls(Scope(), bundle, ids)


def test_datasets_added_in_superset_are_locked_for_guests(router, fake):
    from analystos.dashboards.access import lock_unpublished_datasets

    pub = publisher()
    result = pub.publish(make_bundle("ws_unit"), idempotency_key="k")
    dash = int(result.external_ids["dashboards"]["executive"])
    published = dict(result.external_ids["datasets"])
    assert lock_unpublished_datasets(pub, dash, published) == []
    # an editor adds a chart over another dataset by hand in Superset: the guest gets no rows from it
    on_dash = pub.dashboard_dataset_ids(dash)
    held_back = {k: v for k, v in published.items() if int(v) != on_dash[0]}
    assert lock_unpublished_datasets(pub, dash, held_back) == [{"dataset": on_dash[0], "clause": "1 = 0"}]


def test_inspect_reports_missing_changed_and_unpublished(router, fake):
    pub = publisher()
    bundle = make_bundle("ws_unit")
    result = pub.publish(bundle, idempotency_key="k")
    expected = bundle_hash(bundle)
    assert inspect(pub, result.external_ids, expected) == []
    ids = result.external_ids
    del fake.charts[int(ids["charts"]["trend"])]
    fake.dashboards[int(ids["dashboards"]["executive"])]["css"] = "/* edited by hand */"
    fake.dashboards[int(ids["dashboards"]["operational"])]["published"] = False
    drift = {(d["kind"], d["key"]): d["drift"] for d in inspect(pub, ids, expected)}
    assert drift == {("chart", "trend"): "missing", ("dashboard", "executive"): "changed",
                     ("dashboard", "operational"): "unpublished"}
    repaired = pub.publish(bundle, idempotency_key="k", previous=ids)  # what a verified repair runs
    assert repaired.status == "succeeded" and inspect(pub, repaired.external_ids, expected) == []


def test_embed_domains_default_to_the_ui_origin():
    from types import SimpleNamespace

    from analystos.dashboards.access import embed_domains

    assert embed_domains(SimpleNamespace(superset_embed_domains="", web_url="http://localhost:5173/app")) == ["http://localhost:5173"]
    assert embed_domains(SimpleNamespace(superset_embed_domains="https://a.test/, https://b.test",
                                         web_url="http://localhost:5173")) == ["https://a.test", "https://b.test"]
    assert embed_domains(SimpleNamespace(superset_embed_domains="https://a.test https://b.test:8443",
                                         web_url="")) == ["https://a.test", "https://b.test:8443"]
    # never an empty list (Superset reads it as "any site") and never a wildcard or a bare scheme
    for bad, web in (("", ""), ("", "localhost:5173"), ("*", ""), ("https:", ""), ("https://*.example.com", "")):
        with pytest.raises(InvalidInput):
            embed_domains(SimpleNamespace(superset_embed_domains=bad, web_url=web))

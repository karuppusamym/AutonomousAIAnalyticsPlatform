"""SupersetPublisher against a stateful in-memory Superset served through respx."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import respx

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle
from analystos.core.errors import Forbidden, InvalidInput, NotFound, UpstreamUnavailable
from analystos.publishing import _rison as rison
from analystos.publishing.superset import SupersetClient, SupersetPublisher

BASE = "http://superset.test"
COLUMNS = ["opened_at", "priority", "assignment_group", "reassignment_count", "made_sla", "resolution_hours"]


def make_bundle(workspace_id: str = "ws_unit") -> PublishBundle:
    cols = [{"name": c} for c in COLUMNS]
    return PublishBundle(
        workspace_id=workspace_id,
        datasets=[
            DatasetDef(
                name="incidents",
                sql="SELECT * FROM src_publishtest.incidents",
                columns=cols,
                time_column="opened_at",
                source_assets=["src_publishtest.incidents"],
            )
        ],
        metrics=[
            MetricDef(name="incident_count", display_name="Incidents", definition="count", sql_expression="COUNT(*)"),
            MetricDef(name="sla_rate", display_name="SLA %", definition="made sla",
                      sql_expression="AVG(CASE WHEN made_sla THEN 1.0 ELSE 0 END)", format="percent"),
            MetricDef(name="avg_resolution_hours", display_name="Avg resolution (h)", definition="avg",
                      sql_expression="AVG(resolution_hours)", format="hours", source_columns=["resolution_hours"]),
        ],
        charts=[
            ChartSpec(key="kpi_count", title="Incidents", chart_type="kpi", intent="kpi", dataset="incidents",
                      metric="incident_count"),
            ChartSpec(key="trend", title="Weekly incidents", chart_type="line", intent="trend", dataset="incidents",
                      metric="incident_count", time_grain="week"),
            ChartSpec(key="by_priority", title="Resolution by priority", chart_type="bar", intent="comparison",
                      dataset="incidents", metric="avg_resolution_hours", dimension="priority"),
            ChartSpec(key="res_hist", title="Resolution distribution", chart_type="histogram", intent="distribution",
                      dataset="incidents", dimension="resolution_hours"),
            ChartSpec(key="groups", title="By group", chart_type="table", intent="detail", dataset="incidents",
                      metrics=["incident_count", "sla_rate"], dimension="assignment_group"),
        ],
        dashboards=[
            DashboardSpec(key="executive", title="Executive overview", audience="executive",
                          charts=["kpi_count", "trend", "by_priority"],
                          layout=[{"chart": "kpi_count", "row": 0, "col": 0, "width": 4},
                                  {"chart": "trend", "row": 0, "col": 1, "width": 8},
                                  {"chart": "by_priority", "row": 1, "col": 0, "width": 12}],
                          native_filters=["priority"], summary_markdown="**Incidents are up.**"),
            DashboardSpec(key="operational", title="Operations", audience="operational",
                          charts=["trend", "res_hist", "groups"], native_filters=["assignment_group", "priority"]),
        ],
    )


class FakeSuperset:
    """Just enough Superset 4.x REST semantics to exercise reconcile, metrics PUT and linking."""

    def __init__(self) -> None:
        self.next_id = 100
        self.tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.csrf = "csrf-token-1"
        self.databases: dict[int, dict] = {}
        self.datasets: dict[int, dict] = {}
        self.charts: dict[int, dict] = {}
        self.dashboards: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], tuple[int, dict]] = {}  # (method, path regex) -> response
        self.logins = 0

    # helpers
    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def posts(self, resource: str | None = None) -> list[str]:
        return [p for m, p in self.calls if m == "POST" and "/security/" not in p and (resource is None or resource in p)]

    def expire_tokens(self) -> None:
        self.tokens.clear()

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        for (m, rx), (status, body) in list(self.fail.items()):
            if m == method and re.fullmatch(rx, path):
                return httpx.Response(status, json=body)
        if path == "/api/v1/security/login":
            data = json.loads(request.content)
            if data != {"username": "admin", "password": "admin", "provider": "db", "refresh": True}:
                return httpx.Response(401, json={"message": "Not authorized"})
            self.logins += 1
            tok, ref = f"access-{self._id()}", f"refresh-{self._id()}"
            self.tokens.add(tok)
            self.refresh_tokens.add(ref)
            return httpx.Response(200, json={"access_token": tok, "refresh_token": ref})
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        if path == "/api/v1/security/refresh":
            if auth not in self.refresh_tokens:
                return httpx.Response(401, json={"msg": "bad refresh"})
            tok = f"access-{self._id()}"
            self.tokens.add(tok)
            return httpx.Response(200, json={"access_token": tok})
        if auth not in self.tokens:
            return httpx.Response(401, json={"msg": "Token has expired"})
        if path == "/api/v1/security/csrf_token/":
            return httpx.Response(200, json={"result": self.csrf}, headers={"set-cookie": "session=sess-abc; Path=/"})
        if method != "GET":
            if request.headers.get("x-csrftoken") != self.csrf or "session=sess-abc" not in request.headers.get("cookie", ""):
                return httpx.Response(400, json={"message": "400 Bad Request: The CSRF session token is missing."})
            if request.headers.get("referer") != BASE:
                return httpx.Response(400, json={"message": "The referrer header is missing."})
        body = json.loads(request.content) if request.content else {}
        q = rison.loads(request.url.params["q"]) if "q" in request.url.params else {}
        return self.route(method, path, body, q)

    def route(self, method: str, path: str, body: dict, q: Any) -> httpx.Response:
        parts = path.strip("/").split("/")[2:]  # after api/v1
        res = parts[0]
        rest = parts[1:] if len(parts) > 1 and parts[1] else []
        store = {"database": self.databases, "dataset": self.datasets, "chart": self.charts, "dashboard": self.dashboards}.get(res)
        if res == "me":  # real 4.1: session-cookie auth only, JWT gets 401
            return httpx.Response(401, json={"message": "Not authorized"})
        if store is None:
            return httpx.Response(404, json={"message": "Not found"})
        if not rest:
            if method == "GET":
                rows = [self.render(res, o) for o in store.values() if self.matches(res, o, q.get("filters", []))]
                page, size = q.get("page", 0), q.get("page_size", 25)
                return httpx.Response(200, json={"count": len(rows), "result": rows[page * size:(page + 1) * size]})
            if method == "POST":
                return self.create(res, body)
        if rest == ["export"]:
            return httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"})
        key = rest[0]
        obj = self.lookup(store, res, key)
        if obj is None:
            return httpx.Response(404, json={"message": "Not found"})
        if len(rest) == 2 and rest[1] == "refresh" and method == "PUT":
            return httpx.Response(200, json={"message": "OK"})
        if len(rest) == 2 and res == "dashboard" and rest[1] == "charts":
            charts = [c for c in self.charts.values() if obj["id"] in c["dashboards"]]
            return httpx.Response(200, json={"result": [
                {"id": c["id"], "slice_name": c["slice_name"], "form_data": json.loads(c["params"])} for c in charts]})
        if len(rest) == 2 and res == "dashboard" and rest[1] == "datasets":
            ds_ids = {c["datasource_id"] for c in self.charts.values() if obj["id"] in c["dashboards"]}
            return httpx.Response(200, json={"result": [self.render("dataset", self.datasets[i]) for i in ds_ids]})
        if method == "GET":
            return httpx.Response(200, json={"id": obj["id"], "result": self.render(res, obj)})
        if method == "DELETE":
            if res == "database" and any(d["database"] == obj["id"] for d in self.datasets.values()):
                return httpx.Response(422, json={"message": "There are associated datasets"})
            del store[obj["id"]]
            return httpx.Response(200, json={"message": "OK"})
        if method == "PUT":
            return self.update(res, obj, body)
        return httpx.Response(405, json={"message": "method not allowed"})

    def lookup(self, store: dict, res: str, key: str) -> dict | None:
        if key.isdigit():
            return store.get(int(key))
        if res == "dashboard":
            return next((d for d in store.values() if d.get("slug") == key), None)
        return None

    def matches(self, res: str, obj: dict, filters: list[dict]) -> bool:
        for f in filters:
            col, opr, val = f["col"], f["opr"], f["value"]
            if col == "database" and opr == "rel_o_m":
                if obj.get("database") != val:
                    return False
                continue
            have = obj.get(col)
            if opr == "eq" and have != val:
                return False
            if opr == "sw" and not str(have or "").startswith(val):
                return False
        return True

    def render(self, res: str, obj: dict) -> dict:
        out = dict(obj)
        if res == "dataset":
            db = self.databases.get(obj["database"], {})
            out["database"] = {"id": obj["database"], "database_name": db.get("database_name")}
        if res == "chart":
            out["dashboards"] = [{"id": d, "dashboard_title": self.dashboards[d]["dashboard_title"]}
                                 for d in obj["dashboards"] if d in self.dashboards]
        return out

    def create(self, res: str, body: dict) -> httpx.Response:
        oid = self._id()
        obj = {"id": oid, **body}
        if res == "database" and any(d["database_name"] == body["database_name"] for d in self.databases.values()):
            return httpx.Response(422, json={"message": {"database_name": "A database with the same name already exists."}})
        if res == "dataset":
            if any(d["table_name"] == body["table_name"] and d["database"] == body["database"] for d in self.datasets.values()):
                return httpx.Response(422, json={"message": {"table_name": ["Dataset already exists"]}})
            obj["columns"] = [{"id": self._id(), "column_name": c, "type": "TEXT", "is_dttm": False,
                               "changed_on": "2026-01-01", "type_generic": 1} for c in COLUMNS]
            obj["metrics"] = [{"id": self._id(), "metric_name": "count", "expression": "COUNT(*)", "changed_on": "x"}]
        if res == "chart":
            obj.setdefault("dashboards", [])
        if res == "dashboard":
            obj.setdefault("published", False)
        {"database": self.databases, "dataset": self.datasets, "chart": self.charts, "dashboard": self.dashboards}[res][oid] = obj
        return httpx.Response(201, json={"id": oid, "result": body})

    def update(self, res: str, obj: dict, body: dict) -> httpx.Response:
        if res == "dataset" and "columns" in body:
            for c in body["columns"]:
                bad = set(c) - {"id", "column_name", "type", "advanced_data_type", "verbose_name", "description",
                                "expression", "extra", "filterable", "groupby", "is_active", "is_dttm",
                                "python_date_format", "uuid"}
                if bad:
                    return httpx.Response(400, json={"message": {"columns": {k: ["Unknown field."] for k in bad}}})
            obj["columns"] = body["columns"]
        if res == "dataset" and "metrics" in body:
            names = [m["metric_name"] for m in body["metrics"]]
            if len(names) != len(set(names)):
                return httpx.Response(422, json={"message": {"metrics": ["One or more metrics are duplicated"]}})
            existing = {m["metric_name"] for m in obj["metrics"]}
            if any("id" not in m and m["metric_name"] in existing for m in body["metrics"]):
                return httpx.Response(422, json={"message": {"metrics": ["One or more metrics already exist"]}})
            obj["metrics"] = [m if "id" in m else {**m, "id": self._id()} for m in body["metrics"]]
        obj.update({k: v for k, v in body.items() if k not in {"columns", "metrics"}})
        return httpx.Response(200, json={"id": obj["id"], "result": body})


@pytest.fixture
def fake() -> FakeSuperset:
    return FakeSuperset()


@pytest.fixture
def router(fake: FakeSuperset):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/v1/security/login", name="login").mock(side_effect=fake.handle)
        mock.get("/api/v1/security/csrf_token/", name="csrf").mock(side_effect=fake.handle)
        mock.route(name="api").mock(side_effect=fake.handle)
        yield mock


def bi_login(workspace_id: str) -> tuple[str, str]:
    return f"analystos_bi_{workspace_id}", f"pw-{workspace_id}"


def publisher(login=bi_login) -> SupersetPublisher:
    return SupersetPublisher(base_url=BASE, username="admin", password="admin", bi_login=login,
                             analytics_sqlalchemy_uri="postgresql+psycopg2://analystos_reader:reader@postgres:5432/analytics")


# -- auth ---------------------------------------------------------------------------------------

def test_login_and_csrf_are_used_for_mutations(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    assert res.status == "succeeded", res.errors
    assert router["login"].call_count == 1
    assert router["csrf"].call_count == 1
    mutation = next(c.request for c in router["api"].calls if c.request.method == "POST")
    assert mutation.headers["x-csrftoken"] == fake.csrf
    assert mutation.headers["authorization"].startswith("Bearer access-")
    assert "session=sess-abc" in mutation.headers["cookie"]


def test_expired_token_is_refreshed_and_request_retried(router, fake):
    client = SupersetClient(BASE, "admin", "admin")
    client.login()
    fake.expire_tokens()
    assert client.get("/api/v1/database/")["count"] == 0
    assert ("POST", "/api/v1/security/refresh") in fake.calls
    assert fake.logins == 1


def test_bad_credentials_map_to_forbidden(router, fake):
    with pytest.raises(Forbidden):
        SupersetClient(BASE, "admin", "wrong").login()
    assert SupersetPublisher(base_url=BASE, username="admin", password="nope").test_connection()["ok"] is False


def test_test_connection_ok(router):
    info = publisher().test_connection()
    assert info == {"ok": True, "destination": "superset", "url": BASE, "user": "admin", "error": None}


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (500, {"message": "boom"}, UpstreamUnavailable),
        (503, {"message": "down"}, UpstreamUnavailable),
        (403, {"message": "Forbidden"}, Forbidden),
        (404, {"message": "Not found"}, NotFound),
        (422, {"message": {"sql": ["bad sql"]}}, InvalidInput),
        (400, {"errors": [{"message": "Invalid viz"}]}, InvalidInput),
    ],
)
def test_error_mapping(router, fake, status, body, exc):
    fake.fail[("GET", "/api/v1/chart/1")] = (status, body)
    client = SupersetClient(BASE, "admin", "admin")
    with pytest.raises(exc) as info:
        client.get("/api/v1/chart/1")
    if status == 422:
        assert "bad sql" in info.value.message
    if status == 400:
        assert "Invalid viz" in info.value.message


def test_network_error_maps_to_upstream_unavailable():
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/v1/security/login").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(UpstreamUnavailable):
            SupersetClient(BASE, "admin", "admin").login()
        res = publisher().publish(make_bundle(), idempotency_key="k")
    assert res.status == "failed" and "unreachable" in res.errors[0]


# -- publish ------------------------------------------------------------------------------------

def test_database_connects_as_the_workspace_bi_login(router, fake):
    """P4-02: each workspace's Superset database connects as that workspace's BI login (a member of
    its reader role only), never as the shared reader that can SET ROLE into every workspace; legacy
    databases (shared reader + ``-c role=``) and rotated passwords are moved over on the next publish."""
    from sqlalchemy.engine import make_url

    res = publisher().publish(make_bundle(), idempotency_key="k-role")
    db = fake.databases[res.external_ids["database"]]
    url = make_url(db["sqlalchemy_uri"])
    assert (url.username, url.password, url.host, url.database) == ("analystos_bi_ws_unit", "pw-ws_unit", "postgres", "analytics")
    assert "options" not in url.query
    assert json.loads(db["extra"])["aos_bi_login"] and json.loads(db["extra"])["allows_virtual_table_explore"]
    puts = len([c for c in fake.calls if c[0] == "PUT" and c[1].endswith(f"/database/{db['id']}")])
    assert publisher().ensure_database("ws_unit") == (db["id"], False)  # unchanged: no rewrite
    assert len([c for c in fake.calls if c[0] == "PUT" and c[1].endswith(f"/database/{db['id']}")]) == puts
    db["sqlalchemy_uri"] = "postgresql+psycopg2://analystos_reader:XXXXXXXXXX@postgres:5432/analytics?options=-c%20role%3Dr"
    assert publisher().ensure_database("ws_unit") == (db["id"], False)
    assert make_url(db["sqlalchemy_uri"]).username == "analystos_bi_ws_unit" and "options" not in make_url(db["sqlalchemy_uri"]).query
    publisher(lambda ws: (f"analystos_bi_{ws}", "rotated")).ensure_database("ws_unit")
    assert make_url(db["sqlalchemy_uri"]).password == "rotated"


def test_a_database_create_that_times_out_but_lands_is_reused(router, fake, monkeypatch):
    """Superset syncs per-schema permissions while it creates a connection; on a large analytics database that
    outlasts the request. The publisher takes the connection Superset made instead of failing or doubling it."""
    p = publisher()
    original = p._create_database

    def slow(workspace_id, uri):
        original(workspace_id, uri)  # Superset finishes the create...
        raise UpstreamUnavailable("Superset POST /api/v1/database/ timed out")  # ...after the client gave up

    monkeypatch.setattr(p, "_create_database", slow)
    monkeypatch.setattr(p, "DATABASE_WAIT_POLL", 0)
    db_id, created = p.ensure_database("ws_unit")
    assert created and db_id in fake.databases and len(fake.databases) == 1


def test_a_database_create_that_times_out_and_never_lands_still_fails(router, fake, monkeypatch):
    p = publisher()

    def lost(workspace_id, uri):
        raise UpstreamUnavailable("Superset POST /api/v1/database/ timed out")

    monkeypatch.setattr(p, "_create_database", lost)
    monkeypatch.setattr(p, "DATABASE_WAIT_SECONDS", 0)
    with pytest.raises(UpstreamUnavailable, match="timed out"):
        p.ensure_database("ws_unit")
    assert not fake.databases


def test_publisher_without_a_bi_login_refuses_to_create_a_database(router, fake):
    p = SupersetPublisher(base_url=BASE, username="admin", password="admin",
                          analytics_sqlalchemy_uri="postgresql+psycopg2://analystos_reader:reader@postgres:5432/analytics")
    with pytest.raises(InvalidInput, match="BI login"):
        p.ensure_database("ws_unit")
    assert not fake.databases


def test_publish_creates_everything(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    assert res.status == "succeeded", res.errors
    ids = res.external_ids
    assert ids["database_created"] is True
    db = fake.databases[ids["database"]]
    assert db["database_name"] == "AnalystOS Analytics (ws_unit)"
    assert db["expose_in_sqllab"] is False and db["allow_dml"] is False
    assert db["sqlalchemy_uri"].startswith("postgresql+psycopg2://analystos_bi_ws_unit")

    ds = fake.datasets[ids["datasets"]["incidents"]]
    assert ds["table_name"] == "aos_ws_unit_incidents" and ds["schema"] == "src_publishtest"
    assert ds["sql"].startswith("SELECT") and ds["main_dttm_col"] == "opened_at"
    assert next(c for c in ds["columns"] if c["column_name"] == "opened_at")["is_dttm"] is True
    metrics = {m["metric_name"]: m for m in ds["metrics"]}
    assert set(metrics) == {"count", "incident_count", "sla_rate", "avg_resolution_hours"}  # pre-existing kept
    assert metrics["sla_rate"]["d3format"] == ".1%" and metrics["avg_resolution_hours"]["d3format"] == ",.1f"
    assert metrics["sla_rate"]["verbose_name"] == "SLA %"

    assert set(ids["charts"]) == {"kpi_count", "trend", "by_priority", "res_hist", "groups"}
    trend = fake.charts[ids["charts"]["trend"]]
    assert trend["slice_name"] == "aos_ws_unit_trend: Weekly incidents"
    assert json.loads(trend["params"])["aos_key"] == "trend"
    assert json.loads(trend["query_context"])["datasource"] == {"id": ds["id"], "type": "table"}
    # trend is on both dashboards; charts linked to exactly the dashboards that use them
    assert sorted(trend["dashboards"]) == sorted(ids["dashboards"].values())
    assert fake.charts[ids["charts"]["kpi_count"]]["dashboards"] == [ids["dashboards"]["executive"]]

    for key, did in ids["dashboards"].items():
        d = fake.dashboards[did]
        assert d["published"] is True
        assert d["slug"] == f"aos-ws-unit-{key}"
        assert res.urls[key] == f"{BASE}/superset/dashboard/{did}/"
        pos = json.loads(d["position_json"])
        chart_ids = sorted(n["meta"]["chartId"] for n in pos.values() if isinstance(n, dict) and n.get("type") == "CHART")
        assert chart_ids == sorted(ids["charts"][k] for k in make_bundle().dashboards[0 if key == "executive" else 1].charts)
    meta = json.loads(fake.dashboards[ids["dashboards"]["operational"]]["json_metadata"])
    nf = meta["native_filter_configuration"]
    assert [f["targets"][0]["column"]["name"] for f in nf] == ["assignment_group", "priority"]
    assert all(f["filterType"] == "filter_select" and f["targets"][0]["datasetId"] == ds["id"] for f in nf)


def test_republish_reconciles_without_duplicates(router, fake):
    first = publisher().publish(make_bundle(), idempotency_key="k1")
    n_posts = len(fake.posts())
    second = publisher().publish(make_bundle(), idempotency_key="k1")  # fresh process, no `previous`
    assert second.status == "succeeded", second.errors
    assert len(fake.posts()) == n_posts, "reconcile must not POST again"
    assert second.external_ids["database"] == first.external_ids["database"]
    for kind in ("datasets", "charts", "dashboards"):
        assert second.external_ids[kind] == first.external_ids[kind]
    assert second.external_ids["database_created"] is False
    assert len(fake.charts) == 5 and len(fake.dashboards) == 2 and len(fake.datasets) == 1


def test_republish_with_changed_bundle_updates_in_place(router, fake):
    first = publisher().publish(make_bundle(), idempotency_key="k1")
    b = make_bundle()
    b.charts[1].title = "Incidents per week"
    b.dashboards[0].title = "Exec overview v2"
    second = publisher().publish(b, idempotency_key="k2", previous=first.external_ids)
    assert second.external_ids["charts"] == first.external_ids["charts"]
    assert fake.charts[first.external_ids["charts"]["trend"]]["slice_name"].endswith("Incidents per week")
    assert fake.dashboards[first.external_ids["dashboards"]["executive"]]["dashboard_title"] == "Exec overview v2"
    assert len(fake.charts) == 5


def test_stale_previous_ids_fall_back_to_name_lookup(router, fake):
    first = publisher().publish(make_bundle(), idempotency_key="k1")
    stale = {"datasets": {"incidents": 99999}, "charts": {"trend": 99998}, "dashboards": {"executive": 99997}}
    second = publisher().publish(make_bundle(), idempotency_key="k1", previous=stale)
    assert second.external_ids["charts"] == first.external_ids["charts"]
    assert second.external_ids["dashboards"] == first.external_ids["dashboards"]


def test_partial_failure_reports_created_ids_and_retry_converges(router, fake):
    fake.fail[("POST", "/api/v1/dashboard/")] = (500, {"message": "db locked"})
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    assert res.status == "partial"
    assert "dashboard executive" in res.errors[0] and "db locked" in res.errors[0]
    assert set(res.external_ids["charts"]) == {"kpi_count", "trend", "by_priority", "res_hist", "groups"}
    assert res.external_ids["datasets"]["incidents"] in fake.datasets
    assert res.external_ids["dashboards"] == {}

    del fake.fail[("POST", "/api/v1/dashboard/")]
    dataset_posts, chart_posts = len(fake.posts("/dataset/")), len(fake.posts("/chart/"))
    retry = publisher().publish(make_bundle(), idempotency_key="k1", previous=res.external_ids)
    assert retry.status == "succeeded", retry.errors
    assert len(fake.posts("/dataset/")) == dataset_posts and len(fake.posts("/chart/")) == chart_posts
    assert retry.external_ids["charts"] == res.external_ids["charts"]
    assert len(fake.dashboards) == 2


def test_validation_error_from_superset_is_partial_with_message(router, fake):
    fake.fail[("POST", "/api/v1/chart/")] = (422, {"message": {"params": ["Invalid JSON"]}})
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    assert res.status == "partial"
    assert "Invalid JSON" in res.errors[0] and res.errors[0].startswith("chart kpi_count")


def test_failure_before_anything_created_is_failed(router, fake):
    fake.fail[("GET", "/api/v1/database/")] = (503, {"message": "starting"})
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    assert res.status == "failed" and res.external_ids["datasets"] == {}


def test_invalid_bundle_makes_no_http_calls(router, fake):
    b = make_bundle()
    b.charts[0].metric = "nope"
    res = publisher().publish(b, idempotency_key="k1")
    assert res.status == "failed"
    assert any("unknown metric 'nope'" in e for e in res.errors)
    assert fake.calls == []


# -- rollback / inspect / misc ------------------------------------------------------------------

def test_rollback_deletes_created_objects_and_owned_database(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    deleted = publisher().rollback(res.external_ids)
    assert not fake.dashboards and not fake.charts and not fake.datasets and not fake.databases
    assert deleted[0].startswith("dashboard:") and deleted[-1] == f"database:{res.external_ids['database']}"
    assert len(deleted) == 2 + 5 + 1 + 1
    assert publisher().rollback(res.external_ids) == []  # idempotent


def test_rollback_keeps_database_it_did_not_create_or_still_in_use(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    publisher().rollback({**res.external_ids, "database_created": False})
    assert fake.databases

    fake2 = publisher().publish(make_bundle(), idempotency_key="k2")  # recreates dataset in same db
    other = {"id": 555, "table_name": "someone_else", "database": fake2.external_ids["database"], "columns": [], "metrics": []}
    fake.datasets[555] = other
    ids = {**fake2.external_ids, "database_created": True}
    deleted = publisher().rollback(ids)
    assert not any(d.startswith("database:") for d in deleted)
    assert fake.databases and 555 in fake.datasets


def test_rollback_reports_failures(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    fake.fail[("DELETE", r"/api/v1/chart/\d+")] = (500, {"message": "nope"})
    with pytest.raises(UpstreamUnavailable) as info:
        publisher().rollback(res.external_ids)
    assert len(info.value.details["failed"]) == 5
    assert len([d for d in info.value.details["deleted"] if d.startswith("dashboard:")]) == 2
    assert fake.databases  # never drop the database while charts could not be removed


def test_inspect_dashboard(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    info = publisher().inspect_dashboard(res.external_ids["dashboards"]["executive"])
    assert info["title"] == "Executive overview" and info["published"] is True
    assert {c["aos_key"] for c in info["charts"]} == {"kpi_count", "trend", "by_priority"}
    assert info["datasets"][0]["name"] == "aos_ws_unit_incidents"
    assert {m["name"] for m in info["metrics"]} == {"incident_count", "avg_resolution_hours"}
    assert info["filters"][0]["targets"][0]["column"] == "priority"
    assert len(info["layout"]) == 3 and {c["width"] for c in info["layout"]} == {4, 8, 12}


def test_export_and_phase3_stubs(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    p = publisher()
    assert p.export_artifact("dashboard", res.external_ids["dashboards"]["executive"]).startswith(b"PK")
    with pytest.raises(InvalidInput):
        p.export_artifact("report", 1)
    with pytest.raises(NotImplementedError, match="MVP"):
        p.create_report(1, recipients=["a@b.c"])
    with pytest.raises(NotImplementedError, match="Phase 3"):
        p.schedule_report(1, cron="0 8 * * 1")
    with pytest.raises(NotImplementedError, match="Phase 3"):
        p.create_alert(1, condition={}, recipients=[])

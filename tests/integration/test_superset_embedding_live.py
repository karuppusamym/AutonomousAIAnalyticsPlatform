"""S08 AT-07 / AT-08 live: guest-token embedding and row-level security against a running Superset configured by
deploy/superset (EMBEDDED_SUPERSET, the read-only `aos_embed_guest` role, frame-ancestors), over the staged
ServiceNow mock. Every number is compared with a count the analytics database computes itself.

AT-08 (static RLS): workspace policy row filters become Superset RLS rules on the workspace roles; a Superset
user holding only the workspace viewer role sees exactly the rows the filters allow (filters AND, never OR), and
a filter that needs viewer attributes is closed (`1 = 0`) for direct Superset use.
AT-07 (embedding): a guest token minted for one dashboard reads that dashboard's charts with the viewer's own
RLS clauses applied, cannot read another dashboard's chart, and the embedded page loads only from the allowed
origin. Skips when Superset or the data plane is down."""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403
from dataplane_fixtures import reader_as  # noqa: E402

pytestmark = pytest.mark.integration

UI_ORIGIN = "http://localhost:5173"


@pytest.fixture()
def superset(dp_settings):
    try:
        if httpx.get(f"{dp_settings.superset_url}/health", timeout=5).status_code != 200:
            raise httpx.HTTPError("unhealthy")
    except httpx.HTTPError:
        pytest.skip(f"Superset not reachable at {dp_settings.superset_url}/health")
    from analystos.publishing.superset import SupersetPublisher

    pub = SupersetPublisher(dp_settings)
    if not (pub.client.get("/api/v1/security/roles/", params={"q": "(page_size:100)"}) or {}):
        pytest.skip("Superset security API is off")
    yield pub
    pub.close()


def _bundle(ws: str, schema: str) -> PublishBundle:
    return PublishBundle(
        workspace_id=ws,
        datasets=[DatasetDef(name="incidents_at07", sql=f"SELECT priority, state, opened_at FROM {schema}.incident",
                             columns=[{"name": c} for c in ("priority", "state", "opened_at")], time_column="opened_at",
                             source_assets=[f"{schema}.incident"]),
                  DatasetDef(name="changes_at07", sql=f"SELECT risk, type, start_date FROM {schema}.change_request",
                             columns=[{"name": c} for c in ("risk", "type", "start_date")], time_column="start_date",
                             source_assets=[f"{schema}.change_request"])],
        metrics=[MetricDef(name="n", display_name="Rows", definition="count", sql_expression="COUNT(*)")],
        charts=[ChartSpec(key="at07_incidents", title="Incidents", chart_type="kpi", intent="kpi", dataset="incidents_at07",
                          metric="n"),
                ChartSpec(key="at07_changes", title="Changes", chart_type="kpi", intent="kpi", dataset="changes_at07",
                          metric="n")],
        dashboards=[DashboardSpec(key="embed", title="AT-07 embedded (test)", audience="operational", charts=["at07_incidents"]),
                    DashboardSpec(key="other", title="AT-07 other (test)", audience="operational", charts=["at07_changes"])],
    )


def _count(settings, ws: str, sql: str) -> int:
    with reader_as(settings, ws) as conn:
        return int(conn.execute(text(sql)).scalar_one())


def _value(body: dict) -> int:
    (result,) = body["result"]
    (row,) = result["data"]
    return int(next(iter(row.values())))


def _guest_chart_data(base: str, token: str, chart: dict, dashboard_id: int) -> httpx.Response:
    """What the embedded dashboard's frontend sends: the chart's saved query context, under the guest token."""
    qc = json.loads(chart["query_context"])
    qc["form_data"] = {**(qc.get("form_data") or {}), "slice_id": chart["id"], "dashboardId": dashboard_id}
    return httpx.post(f"{base}/api/v1/chart/data", json=qc, headers={"X-GuestToken": token}, timeout=30)


def test_at07_at08_live_guest_token_and_rls(superset, dp_settings, staged_servicenow, dp_workspace):
    from analystos.dashboards.access import FAIL_CLOSED, static_rls_rules

    ws, schema = dp_workspace["workspace_id"], staged_servicenow["schema"]
    base = superset.base_url
    published = superset.publish(_bundle(ws, schema), idempotency_key=f"it-at07-{ws}")
    assert published.status == "succeeded", published.errors
    ids = published.external_ids
    dash, other = int(ids["dashboards"]["embed"]), int(ids["dashboards"]["other"])
    ds_inc, ds_chg = int(ids["datasets"]["incidents_at07"]), int(ids["datasets"]["changes_at07"])
    chart_inc = superset.client.get(f"/api/v1/chart/{int(ids['charts']['at07_incidents'])}")["result"]
    chart_chg = superset.client.get(f"/api/v1/chart/{int(ids['charts']['at07_changes'])}")["result"]
    chart_inc["id"], chart_chg["id"] = int(ids["charts"]["at07_incidents"]), int(ids["charts"]["at07_changes"])
    total = _count(dp_settings, ws, f"SELECT COUNT(*) FROM {schema}.incident")
    p2 = _count(dp_settings, ws, f"SELECT COUNT(*) FROM {schema}.incident WHERE priority = 2")
    p3 = _count(dp_settings, ws, f"SELECT COUNT(*) FROM {schema}.incident WHERE priority = 3")
    assert 0 < p2 < total and 0 < p3 < total

    # ---------------------------------------------------------------- AT-08: static RLS on the workspace roles
    roles = superset.ensure_workspace_roles(ws)
    superset.set_dashboard_roles(dash, [roles["viewer"], roles["editor"]])
    superset.set_dashboard_roles(other, [roles["viewer"], roles["editor"]])
    policy = SimpleNamespace(row_filters=[
        SimpleNamespace(id="f_low_priority", assets=[f"{schema}.incident"], predicate="priority IN (1, 2)"),
        SimpleNamespace(id="f_p2", assets=[f"{schema}.incident"], predicate="priority = 2"),
        SimpleNamespace(id="f_team", assets=[f"{schema}.change_request"], predicate="type = {{user.change_type}}"),
    ])
    rules = static_rls_rules(policy, _bundle(ws, schema), ids["datasets"])
    assert {r["filter_id"]: r["closed"] for r in rules} == {"f_low_priority": False, "f_p2": False, "f_team": True}
    synced = superset.sync_rls(ws, rules, [roles["viewer"], roles["editor"]])
    held = {r["name"]: r for r in superset.client.list("rowlevelsecurity", [])}
    for name, rid in synced.items():
        rule = superset.client.get(f"/api/v1/rowlevelsecurity/{rid}")["result"]
        assert name in held and {t["id"] for t in rule["tables"]} <= {ds_inc, ds_chg}
        assert {r["id"] for r in rule["roles"]} == {roles["viewer"], roles["editor"]}
    assert superset.client.get(f"/api/v1/rowlevelsecurity/{synced[superset.rls_rule_name(ws, 'f_team')]}")["result"][
        "clause"] == FAIL_CLOSED

    user, password = f"at08_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    superset.sync_user(username=user, email=f"{user}@test.local", workspace_ids=[ws], password=password)
    from analystos.publishing.superset import SupersetClient

    viewer = SupersetClient(base, user, password)
    try:
        # two filters on one dataset AND together (1,2 ∩ 2 = 2): a second policy filter only ever narrows
        assert _value(viewer.get(f"/api/v1/chart/{chart_inc['id']}/data/", params={"format": "json"})) == p2
        # the attribute-bound filter cannot be a static rule: closed for direct Superset use
        assert _value(viewer.get(f"/api/v1/chart/{chart_chg['id']}/data/", params={"format": "json"})) == 0
    finally:
        viewer.close()

    # ---------------------------------------------------------------- AT-07: guest tokens and the embedded page
    uuid = superset.ensure_embedded(dash, [UI_ORIGIN])
    assert superset.ensure_embedded(dash, [UI_ORIGIN]) == uuid
    assert superset.client.get(f"/api/v1/dashboard/{dash}/embedded")["result"]["allowed_domains"] == [UI_ORIGIN]
    person = {"username": "viewer@test.local", "first_name": "Viewer", "last_name": "Test"}
    token = superset.guest_token(user=person, dashboard_uuid=uuid, rls=[{"dataset": ds_inc, "clause": "(priority = 3)"}])
    assert _value(_guest_chart_data(base, token, chart_inc, dash).raise_for_status().json()) == p3  # the viewer's clause
    unfiltered = superset.guest_token(user=person, dashboard_uuid=uuid, rls=[])
    # a guest holds only aos_embed_guest: the role rules are not in force, the token's clauses are
    assert _value(_guest_chart_data(base, unfiltered, chart_inc, dash).raise_for_status().json()) == total
    # a token for one dashboard reads nothing of another dashboard
    assert _guest_chart_data(base, token, chart_chg, other).status_code in (401, 403)
    assert _guest_chart_data(base, token, chart_chg, dash).status_code in (401, 403)
    assert httpx.post(f"{base}/api/v1/chart/data", json=json.loads(chart_inc["query_context"]),
                      headers={"X-GuestToken": "not-a-token"}, timeout=30).status_code in (401, 403)
    # the guest cannot change what the chart asks for (raw rows, other columns): Superset compares the payload
    # with the saved chart, and the legacy explore_json endpoint that skips that check is not in the guest role
    tampered = json.loads(chart_inc["query_context"])
    tampered["form_data"] = {**(tampered.get("form_data") or {}), "slice_id": chart_inc["id"], "dashboardId": dash}
    for q in tampered["queries"]:
        q.update({"columns": ["priority", "state", "opened_at"], "metrics": [], "row_limit": 100000})
    assert httpx.post(f"{base}/api/v1/chart/data", json=tampered, headers={"X-GuestToken": token},
                      timeout=30).status_code in (401, 403)
    # Superset 4.1.1 skips that comparison for guests (its chart lookup is permission-filtered, so it finds no
    # chart); deploy/superset/aos_security.py resolves the chart itself. It also pins the saved WHERE SQL and
    # refuses a request that names no chart.
    with_sql = json.loads(chart_inc["query_context"])
    with_sql["form_data"] = {**(with_sql.get("form_data") or {}), "slice_id": chart_inc["id"], "dashboardId": dash}
    for q in with_sql["queries"]:
        q["extras"] = {**(q.get("extras") or {}), "where": "priority > 0"}
    assert httpx.post(f"{base}/api/v1/chart/data", json=with_sql, headers={"X-GuestToken": token},
                      timeout=30).status_code in (401, 403)
    unnamed = json.loads(chart_inc["query_context"])
    unnamed["form_data"] = {**(unnamed.get("form_data") or {}), "slice_id": None, "dashboardId": dash}
    assert httpx.post(f"{base}/api/v1/chart/data", json=unnamed, headers={"X-GuestToken": token},
                      timeout=30).status_code in (401, 403)
    legacy = httpx.post(f"{base}/superset/explore_json/", params={"form_data": json.dumps({"slice_id": chart_inc["id"]})},
                        headers={"X-GuestToken": token}, timeout=30, follow_redirects=False)
    assert legacy.status_code in (302, 401, 403), legacy.text[:300]
    listed = httpx.get(f"{base}/api/v1/dashboard/", headers={"X-GuestToken": token}, timeout=30)
    assert listed.status_code in (401, 403) or {d["id"] for d in listed.json()["result"]} <= {dash}

    page = httpx.get(f"{base}/embedded/{uuid}", headers={"Referer": f"{UI_ORIGIN}/studio"}, timeout=30)
    assert page.status_code == 200
    assert f"frame-ancestors 'self' {UI_ORIGIN}" in page.headers.get("content-security-policy", "")
    assert httpx.get(f"{base}/embedded/{uuid}", headers={"Referer": "http://evil.test/"}, timeout=30).status_code == 403

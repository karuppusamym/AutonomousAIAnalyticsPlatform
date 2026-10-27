"""Synthetic ServiceNow data, the mock Table API and the ServiceNow connector."""
from __future__ import annotations

import time
import warnings

import httpx
import polars as pl
import pyarrow as pa
import pytest

warnings.filterwarnings("ignore", message=".*starlette.testclient.*")
warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from analystos.connectors.servicenow import ServiceNowConnector, normalize_internal_type  # noqa: E402
from analystos.connectors.servicenow_mock import app  # noqa: E402
from analystos.connectors.synthetic_servicenow import (  # noqa: E402
    DATA_QUALITY,
    GROUND_TRUTH,
    PERIOD_END,
    generate_servicenow_data,
)
from analystos.core.errors import Forbidden, UpstreamUnavailable  # noqa: E402

BASE = "http://sn.test"


@pytest.fixture(scope="module")
def data() -> dict[str, pl.DataFrame]:
    return {k: pl.from_arrow(v) for k, v in generate_servicenow_data().items()}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, base_url=BASE)


def connector(client: httpx.Client, **config) -> ServiceNowConnector:
    password = config.pop("password", "admin")
    cfg = {"instance_url": BASE, "username": "admin", "page_size": 5000, **config}
    return ServiceNowConnector(cfg, http_client=client, password=password, sleep=lambda s: None)


# ------------------------------------------------------------------------------ generator


def test_generator_is_fast_deterministic_and_sized() -> None:
    generate_servicenow_data.cache_clear()
    started = time.perf_counter()
    a = generate_servicenow_data()
    assert time.perf_counter() - started < 3.0
    b = generate_servicenow_data.__wrapped__()
    assert a["incident"].equals(b["incident"])
    assert a["incident"].num_rows == 20_000
    assert a["change_request"].num_rows == 3_000
    assert a["sys_user_group"].num_rows == 12
    assert a["cmdb_ci"].num_rows == 40
    for field in ("sys_id", "number", "opened_at", "resolved_at", "closed_at", "priority", "impact", "urgency", "state",
                  "category", "assignment_group", "cmdb_ci", "caused_by", "reassignment_count", "reopen_count",
                  "made_sla", "contact_type", "short_description", "caller_id", "sys_updated_on"):
        assert field in a["incident"].column_names
    assert a["incident"].schema.field("made_sla").type == pa.bool_()


def test_ground_truth_reassignment_sla(data) -> None:
    inc = data["incident"]
    le1 = (~inc.filter(pl.col("reassignment_count") <= 1)["made_sla"]).mean()
    ge3 = (~inc.filter(pl.col("reassignment_count") >= 3)["made_sla"]).mean()
    gt = GROUND_TRUTH["reassignment_sla_breach"]
    assert abs(ge3 / le1 - gt["expected"]) <= gt["tolerance"]


def test_ground_truth_after_hours_resolution(data) -> None:
    inc = data["incident"].filter(pl.col("resolved_at").is_not_null() & (pl.col("resolved_at") >= pl.col("opened_at")))
    inc = inc.with_columns(
        hours=(pl.col("resolved_at") - pl.col("opened_at")).dt.total_seconds() / 3600,
        after=(pl.col("opened_at").dt.hour() < 8) | (pl.col("opened_at").dt.hour() >= 18) | (pl.col("opened_at").dt.weekday() >= 6),
    )
    ratio = inc.filter(pl.col("after"))["hours"].mean() / inc.filter(~pl.col("after"))["hours"].mean()
    gt = GROUND_TRUTH["after_hours_resolution"]
    assert abs(ratio - gt["expected"]) <= gt["tolerance"]


def test_ground_truth_payments_gateway_p1(data) -> None:
    inc, ci = data["incident"], data["cmdb_ci"]
    pg = ci.filter(pl.col("name") == GROUND_TRUTH["payments_gateway_p1"]["ci_name"])["sys_id"][0]
    p1 = inc.filter(pl.col("priority") == 1)
    p1_share = (p1["cmdb_ci"] == pg).mean()
    all_share = (inc["cmdb_ci"] == pg).mean()
    gt = GROUND_TRUTH["payments_gateway_p1"]
    assert abs(p1_share - gt["expected"]["p1_share"]) <= gt["tolerance"]["p1_share"]
    assert abs(all_share - gt["expected"]["all_share"]) <= gt["tolerance"]["all_share"]


def test_ground_truth_emergency_changes(data) -> None:
    j = data["incident"].filter(pl.col("caused_by").is_not_null()).join(
        data["change_request"], left_on="caused_by", right_on="sys_id", suffix="_chg"
    )
    j = j.with_columns(lag=(pl.col("opened_at") - pl.col("end_date")).dt.total_hours(), emergency=pl.col("type") == "emergency")
    within = j.group_by("emergency").agg(((pl.col("lag") >= 0) & (pl.col("lag") < 48)).mean().alias("share"))
    share = dict(zip(within["emergency"], within["share"], strict=True))
    gt = GROUND_TRUTH["emergency_change_incidents"]
    assert share[True] >= gt["expected"]["within_48h_share_emergency"] - gt["tolerance"]["within_48h_share_emergency"]
    assert share[False] <= gt["expected"]["within_48h_share_other"] + gt["tolerance"]["within_48h_share_other"]
    per_change = j.group_by("emergency").len().join(
        data["change_request"].group_by(emergency=pl.col("type") == "emergency").len(), on="emergency"
    ).with_columns(rate=pl.col("len") / pl.col("len_right"))
    rate = dict(zip(per_change["emergency"], per_change["rate"], strict=True))
    assert rate[True] / rate[False] >= gt["expected"]["incidents_per_change_ratio_min"]


def test_ground_truth_network_ops_reassignment(data) -> None:
    means = (
        data["incident"].join(data["sys_user_group"], left_on="assignment_group", right_on="sys_id")
        .group_by("name").agg(pl.col("reassignment_count").mean().alias("m")).sort("m", descending=True)
    )
    assert means["name"][0] == GROUND_TRUTH["network_ops_reassignment"]["expected"]


def test_injected_data_quality_issues(data) -> None:
    inc = data["incident"]
    resolved = inc.filter(pl.col("resolved_at").is_not_null())
    bad = (resolved["resolved_at"] < resolved["opened_at"]).mean()
    assert 0.005 <= bad <= 0.015
    assert 0.02 <= inc["assignment_group"].is_null().mean() <= 0.04
    assert inc["number"].is_duplicated().sum() == 2 * DATA_QUALITY["duplicate_numbers"]["count"]
    assert (inc["opened_at"] > PERIOD_END).sum() == DATA_QUALITY["future_opened_at"]["count"]


# ------------------------------------------------------------------------------ mock API


def test_mock_requires_basic_auth(client: TestClient) -> None:
    assert client.get("/api/now/table/incident").status_code == 401
    assert client.get("/api/now/table/incident", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/now/table/incident", params={"sysparm_limit": 1}, auth=("admin", "admin")).status_code == 200


def test_mock_env_credentials(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SERVICENOW_MOCK_USER", "svc")
    monkeypatch.setenv("SERVICENOW_MOCK_PASSWORD", "s3cret")
    assert client.get("/api/now/table/incident", auth=("admin", "admin")).status_code == 401
    assert client.get("/api/now/table/incident", params={"sysparm_limit": 1}, auth=("svc", "s3cret")).status_code == 200


def test_mock_table_api_wire_format(client: TestClient) -> None:
    r = client.get(
        "/api/now/table/incident",
        params={"sysparm_limit": 3, "sysparm_offset": 2, "sysparm_fields": "number,made_sla,opened_at,assignment_group,priority"},
        auth=("admin", "admin"),
    )
    assert r.status_code == 200
    assert r.headers["X-Total-Count"] == "20000"
    assert 'rel="next"' in r.headers["Link"]
    rows = r.json()["result"]
    assert len(rows) == 3
    row = rows[0]
    assert set(row) == {"number", "made_sla", "opened_at", "assignment_group", "priority"}
    assert row["made_sla"] in ("true", "false")
    assert len(row["opened_at"]) == 19 and row["opened_at"][10] == " "
    assert isinstance(row["priority"], str)
    ag = row["assignment_group"]
    assert ag == "" or (isinstance(ag, dict) and set(ag) == {"link", "value"})


def test_mock_query_filters_and_ordering(client: TestClient, data) -> None:
    r = client.get(
        "/api/now/table/incident",
        params={"sysparm_query": "priority=1^reassignment_count>2^ORDERBYDESCopened_at", "sysparm_limit": 50,
                "sysparm_exclude_reference_link": "true"},
        auth=("admin", "admin"),
    )
    rows = r.json()["result"]
    expected = data["incident"].filter((pl.col("priority") == 1) & (pl.col("reassignment_count") > 2)).height
    assert int(r.headers["X-Total-Count"]) == expected
    assert all(x["priority"] == "1" and int(x["reassignment_count"]) > 2 for x in rows)
    opened = [x["opened_at"] for x in rows]
    assert opened == sorted(opened, reverse=True)
    assert all(isinstance(x["assignment_group"], str) for x in rows)


def test_mock_display_value_all(client: TestClient) -> None:
    r = client.get(
        "/api/now/table/incident",
        params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true", "sysparm_limit": 200,
                "sysparm_fields": "assignment_group,priority,made_sla", "sysparm_query": "assignment_group!="},
        auth=("admin", "admin"),
    )
    row = r.json()["result"][0]
    assert set(row["assignment_group"]) == {"display_value", "value"}
    assert len(row["assignment_group"]["value"]) == 32
    assert row["assignment_group"]["display_value"] and row["assignment_group"]["display_value"] != row["assignment_group"]["value"]
    assert row["priority"]["display_value"].startswith(row["priority"]["value"] + " - ")


def test_mock_metadata_tables(client: TestClient) -> None:
    objs = client.get("/api/now/table/sys_db_object", params={"sysparm_query": "name=incident"}, auth=("admin", "admin")).json()["result"]
    assert objs[0]["label"] == "Incident"
    dic = client.get("/api/now/table/sys_dictionary", params={"sysparm_query": "name=incident"}, auth=("admin", "admin")).json()["result"]
    by = {d["element"]: d for d in dic}
    assert by["sys_id"]["internal_type"] == "GUID" and by["sys_id"]["primary"] == "true"
    assert by["assignment_group"]["internal_type"] == "reference" and by["assignment_group"]["reference"] == "sys_user_group"
    assert by["opened_at"]["internal_type"] == "glide_date_time"
    assert by["made_sla"]["internal_type"] == "boolean"


def test_mock_unknown_table(client: TestClient) -> None:
    assert client.get("/api/now/table/nope", auth=("admin", "admin")).status_code == 400


# ------------------------------------------------------------------------------ connector


def test_type_mapping() -> None:
    assert normalize_internal_type("integer") == "integer"
    assert normalize_internal_type("glide_date_time") == "timestamp"
    assert normalize_internal_type("boolean") == "boolean"
    assert normalize_internal_type("reference") == "text"
    assert normalize_internal_type("GUID") == "text"
    assert normalize_internal_type("decimal") == "numeric"
    assert normalize_internal_type("something_new") == "text"


def test_connector_discover(client: TestClient) -> None:
    con = connector(client)
    assert con.test().ok
    assets = {a.name: a for a in con.discover()}
    assert set(assets) == {"incident", "change_request", "sys_user_group", "cmdb_ci"}
    inc = assets["incident"]
    assert inc.row_count == 20_000 and inc.kind == "api_table" and inc.business_name == "Incident"
    assert inc.freshness_at is not None
    cols = {c.name: c for c in inc.columns}
    assert cols["sys_id"].is_key and not cols["sys_id"].nullable
    assert cols["opened_at"].data_type == "timestamp"
    assert cols["priority"].data_type == "integer"
    assert cols["made_sla"].data_type == "boolean"
    assert cols["assignment_group"].references == "sys_user_group.sys_id"
    assert cols["caused_by"].references == "change_request.sys_id"
    for ref in ("assignment_group", "cmdb_ci", "caused_by", "caller_id"):
        derived = cols[f"{ref}_name"]
        assert derived.data_type == "text" and derived.description == f"Display value of {ref}"
    assert [c.name for c in inc.columns].index("assignment_group_name") == [c.name for c in inc.columns].index("assignment_group") + 1


def test_connector_extract_paginates_and_types(client: TestClient) -> None:
    con = connector(client, page_size=700, tables=["change_request", "sys_user_group", "cmdb_ci"])
    assets = {a.name: a for a in con.discover()}
    batches = list(con.extract(assets["change_request"], max_rows=2_500))
    assert len(batches) == 4  # 700 * 3 + 400
    table = pa.Table.from_batches(batches)
    assert table.num_rows == 2_500
    assert table.schema.field("start_date").type == pa.timestamp("us")
    assert table.schema.field("risk").type == pa.int64()
    assert len(set(table.column("sys_id").to_pylist())) == 2_500
    snap = con.last_snapshot  # P4-C12: the cap cut the table, and the snapshot says so
    assert (snap["rows_staged"], snap["truncated"], snap["sampling_method"]) == (2_500, True, "undeclared")
    assert snap["source_total_rows"] > 2_500 and snap["total_rows_basis"] == "x-total-count"
    names = set(table.column("assignment_group_name").to_pylist())
    assert "Network Operations" in names or len(names) > 5
    ci = pa.Table.from_batches(list(con.extract(assets["cmdb_ci"], max_rows=1000)))
    assert "Payments Gateway" in ci.column("name").to_pylist()
    assert all(v for v in ci.column("support_group_name").to_pylist())


def test_connector_extract_incident_display_values(client: TestClient) -> None:
    con = connector(client, tables=["incident"])
    (asset,) = con.discover()
    t = pa.Table.from_batches(list(con.extract(asset, max_rows=20_000)))
    assert t.num_rows == 20_000
    assert con.last_snapshot["truncated"] is False and con.last_snapshot["source_total_rows"] == 20_000
    df = pl.from_arrow(t)
    assert "Payments Gateway" in df["cmdb_ci_name"].to_list()
    caused = df.filter(pl.col("caused_by").is_not_null())
    assert caused.height > 0 and caused["caused_by_name"].str.starts_with("CHG").all()
    assert df["made_sla"].dtype == pl.Boolean


def test_connector_bad_credentials_raise_forbidden(client: TestClient) -> None:
    con = connector(client, password="nope")
    with pytest.raises(Forbidden):
        con.discover()
    assert not con.test().ok


def test_connector_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503 if calls["n"] == 1 else 429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"result": [{"name": "incident"}]}, headers={"X-Total-Count": "1"})

    slept: list[float] = []
    con = ServiceNowConnector(
        {"instance_url": BASE, "username": "u", "max_retries": 3, "backoff_seconds": 0.01},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        password="p",
        sleep=slept.append,
    )
    assert con.test().ok
    assert calls["n"] == 3 and len(slept) == 2


def test_connector_gives_up_with_upstream_unavailable() -> None:
    con = ServiceNowConnector(
        {"instance_url": BASE, "username": "u", "max_retries": 2, "backoff_seconds": 0},
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        password="p",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamUnavailable):
        con.discover()


def test_connector_transport_errors_are_upstream_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    con = ServiceNowConnector(
        {"instance_url": BASE, "username": "u", "max_retries": 1, "backoff_seconds": 0},
        http_client=httpx.Client(transport=httpx.MockTransport(boom)),
        password="p",
        sleep=lambda s: None,
    )
    with pytest.raises(UpstreamUnavailable):
        con.discover()


def test_connector_password_comes_from_secret_ref(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SN_TEST", "admin")
    con = ServiceNowConnector({"instance_url": BASE, "username": "admin", "tables": ["cmdb_ci"]}, "env:ANALYSTOS_SOURCE_SECRET_SN_TEST",
                              http_client=client)
    assert con.discover()[0].row_count == 40

"""Governed REST connector (S02-T8): read-only by construction, typed params, pagination, response -> table
mapping, rate limit, credential only from secret_ref, every request through the outbound SSRF guard."""
from __future__ import annotations

import ipaddress
import json
from types import SimpleNamespace

import httpx
import pytest

from analystos.connectors.rest import RESTConnector, parse_spec
from analystos.core.errors import Forbidden, InvalidInput, PolicyDenied

PUBLIC = [ipaddress.ip_address("93.184.216.34")]


def _settings(**over):
    base = dict(rest_safe_post_allowlist="", outbound_private_hosts="", air_gapped=False,
                secret_env_prefixes="ANALYSTOS_SOURCE_SECRET_", secret_env_names="", secret_file_dirs="",
                process_role="all", credential_roles="all,query,crawl,elt")
    base.update(over)
    return SimpleNamespace(**base)


TICKETS = {
    "base_url": "https://api.example.com/v2",
    "auth": {"type": "bearer"},
    "rate_limit": {"requests_per_second": 2},
    "endpoints": [{
        "name": "Open Tickets", "path": "/tickets",
        "params": {"status": {"type": "string", "value": "open", "allowed": ["open", "closed"]}},
        "pagination": {"type": "page", "page_param": "page", "size_param": "per_page", "size": 2},
        "records_path": "data.items",
        "columns": [{"name": "id", "path": "id", "type": "integer", "key": True},
                    {"name": "title", "path": "fields.title"},
                    {"name": "hours", "path": "hours", "type": "numeric"},
                    {"name": "tags", "path": "tags", "type": "json"}],
    }],
}
ITEMS = [{"id": i, "fields": {"title": f"t{i}"}, "hours": str(i * 1.5), "tags": ["a"]} for i in range(1, 6)]


def _api(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("per_page", "2"))
        return httpx.Response(200, json={"data": {"items": ITEMS[(page - 1) * size: page * size]}})

    return httpx.MockTransport(handler)


def _connector(calls, config=TICKETS, **kw):
    sleeps: list[float] = []
    clock = [100.0]

    def sleep(seconds: float) -> None:  # a fake clock that the throttle's sleeps advance
        sleeps.append(seconds)
        clock[0] += seconds

    con = RESTConnector(config, "env:ANALYSTOS_SOURCE_SECRET_TICKETS", settings=_settings(**kw.pop("settings", {})),
                        http_transport=_api(calls), resolver=lambda h, p: PUBLIC, sleep=sleep,
                        clock=lambda: clock[0], **kw)
    return con, sleeps


def test_discover_maps_endpoints_to_tables_without_a_request():
    calls: list = []
    con, _ = _connector(calls)
    (asset,) = con.discover()
    assert asset.name == "open_tickets" and asset.kind == "api_table"
    assert [(c.name, c.data_type, c.is_key) for c in asset.columns] == [
        ("id", "integer", True), ("title", "text", False), ("hours", "numeric", False), ("tags", "json", False)]
    assert calls == []


def test_extract_pages_types_and_the_credential(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_TICKETS", "tok-123")
    calls: list = []
    con, sleeps = _connector(calls)
    batches = list(con.extract(con.discover()[0], max_rows=100))
    rows = [r for b in batches for r in b.to_pylist()]
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5] and rows[0]["title"] == "t1" and rows[2]["hours"] == 4.5
    assert json.loads(rows[0]["tags"]) == ["a"]
    assert len(calls) == 3  # two full pages, then a short one ends the paging
    first = calls[0]
    assert first.method == "GET" and first.headers["Authorization"] == "Bearer tok-123"
    assert first.headers["Host"] == "api.example.com" and first.url.host == "93.184.216.34"  # pinned address
    assert first.url.path == "/v2/tickets" and first.url.params["status"] == "open"
    assert sleeps and all(abs(s - 0.5) < 1e-9 for s in sleeps)  # 2 requests per second


def test_max_rows_stops_paging():
    calls: list = []
    con, _ = _connector(calls, password="x")
    rows = [r for b in con.extract(con.discover()[0], max_rows=3) for r in b.to_pylist()]
    assert [r["id"] for r in rows] == [1, 2, 3] and len(calls) == 2


@pytest.mark.parametrize("mutate,err", [
    (lambda c: c["endpoints"][0].update(method="DELETE"), InvalidInput),
    (lambda c: c["endpoints"][0].update(path="https://evil.example/x"), InvalidInput),
    (lambda c: c["endpoints"][0].update(path="/../admin"), InvalidInput),
    (lambda c: c["endpoints"][0].update(path="/tickets?all=1"), InvalidInput),
    (lambda c: c.update(base_url="ftp://api.example.com"), InvalidInput),
    (lambda c: c.update(base_url="https://user:pw@api.example.com"), InvalidInput),
    (lambda c: c["endpoints"][0]["params"]["status"].update(value="deleted"), InvalidInput),
    (lambda c: c["endpoints"][0]["params"].update(limit={"type": "integer", "value": "10; DROP"}), InvalidInput),
    (lambda c: c["endpoints"][0]["params"].update(api_key={"type": "string", "value": "sk-live"}), InvalidInput),
    (lambda c: c["endpoints"][0].update(method="POST"), Forbidden),  # not safe, not allowlisted
])
def test_the_description_is_read_only_by_construction(mutate, err):
    config = json.loads(json.dumps(TICKETS))
    mutate(config)
    with pytest.raises(err):
        parse_spec(config, _settings())


def test_post_needs_safe_and_the_admin_allowlist():
    config = json.loads(json.dumps(TICKETS))
    config["endpoints"][0].update(method="POST", safe=True)
    with pytest.raises(Forbidden):
        parse_spec(config, _settings())  # safe but not allowlisted
    assert parse_spec(config, _settings(rest_safe_post_allowlist="api.example.com/v2/tickets")).endpoints[0].method == "POST"
    config["endpoints"][0]["safe"] = False
    with pytest.raises(Forbidden):  # allowlisted but not marked safe
        parse_spec(config, _settings(rest_safe_post_allowlist="api.example.com/v2/tickets"))


def test_next_url_must_stay_on_the_base_url():
    config = json.loads(json.dumps(TICKETS))
    config["endpoints"][0]["pagination"] = {"type": "next_url", "cursor_path": "next"}
    config["auth"] = {"type": "none"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": ITEMS[:1]}, "next": "https://evil.example/steal"})

    con = RESTConnector(config, None, settings=_settings(), http_transport=httpx.MockTransport(handler),
                        resolver=lambda h, p: PUBLIC, sleep=lambda s: None)
    with pytest.raises(Forbidden, match="base URL"):
        list(con.extract(con.discover()[0], max_rows=100))


def test_private_addresses_are_refused_by_the_outbound_guard():
    calls: list = []
    con = RESTConnector(TICKETS, None, settings=_settings(), http_transport=_api(calls), password="x",
                        resolver=lambda h, p: [ipaddress.ip_address("169.254.169.254")], sleep=lambda s: None)
    with pytest.raises(PolicyDenied):
        list(con.extract(con.discover()[0], max_rows=10))
    assert calls == []
    assert not con.test().ok


def test_credential_comes_only_from_an_allowed_secret_ref(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    con = RESTConnector(TICKETS, "env:OPENROUTER_API_KEY", settings=_settings(), http_transport=_api([]),
                        resolver=lambda h, p: PUBLIC, sleep=lambda s: None)
    with pytest.raises(Forbidden):
        list(con.extract(con.discover()[0], max_rows=10))


def test_registration_accepts_the_auth_block_but_not_credentials():
    from analystos.services.sources import _credentials_in

    assert not _credentials_in("rest", dict(TICKETS, auth={"type": "header", "header": "X-Api-Key"}))
    assert _credentials_in("rest", dict(TICKETS, auth={"type": "bearer", "token": "sk-live"}))
    assert _credentials_in("postgres", {"auth": {"type": "bearer"}})  # other kinds keep the strict rule


def test_catalog_and_factory_know_the_kind():
    from analystos.connectors import kinds
    from analystos.connectors.registry import build_connector

    spec = kinds.get_kind("rest")
    assert spec.category == "api" and kinds.execution_mode_for("rest", None) == "staged" and not spec.pushdown_allowed
    con = build_connector(SimpleNamespace(kind="rest", config=TICKETS, secret_ref=None), _settings())
    assert isinstance(con, RESTConnector) and con.execution_mode == "staged"

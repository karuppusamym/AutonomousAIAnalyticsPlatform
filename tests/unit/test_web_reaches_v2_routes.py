"""The web client sends every call under /api (nginx and the dev proxy forward nothing else). A route it names as
`/v2/...` must therefore also be served at `/api/v2/...`; the Data tools and MCP servers screens showed "Not Found"
when the tool registry was mounted at /v2 only."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client():
    from analystos.api.app import app

    return TestClient(app, raise_server_exceptions=False)  # no lifespan: only routing and auth run


def _calls() -> set[tuple[str, str]]:
    source = (ROOT / "web" / "src" / "api.ts").read_text(encoding="utf-8")
    return {(m.upper(), p) for m, p in re.findall(r'\b(get|post|put|patch|del)\(\s*"(/v2/[^"]+)"', source)}


def _concrete(path: str) -> str:
    return re.sub(r"\{\w+\}", "x_probe", path)


def test_every_v2_path_the_web_client_calls_is_served_under_api(client):
    calls = _calls()
    assert calls, "the web client names no /v2 path any more: drop this test"
    missing = []
    for method, path in sorted(calls):
        method = "DELETE" if method == "DEL" else method
        status = client.request(method, "/api" + _concrete(path)).status_code
        if status == 404:  # unauthenticated: an existing route answers 401/403/405/422, an absent one 404
            missing.append(f"{method} /api{path}")
    assert not missing, f"served only at /v2, so the browser gets 404: {missing}"


def test_external_callers_keep_the_v2_paths_and_the_schema_has_one_entry_per_route(client):
    from analystos.api.app import app

    assert client.get("/v2/tools").status_code != 404
    schema = app.openapi()["paths"]
    assert "/v2/tools" in schema and "/api/v2/tools" not in schema

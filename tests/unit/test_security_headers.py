"""Security self-check 2026-10-30 (ZAP baseline/API scan): every API response carries browser hardening headers, error
responses and streams included; a route's own Cache-Control wins; the interactive docs keep only the anti-framing
policy. The web server (nginx, compose image and Helm chart) sends its own set on every location."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from analystos.api.security_headers import API_CSP, DOCS_CSP, SecurityHeadersMiddleware

ROOT = Path(__file__).resolve().parents[2]
WEB_HEADERS = ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy",
               "Permissions-Policy")


def _assert_hardened(headers, csp: str = API_CSP) -> None:
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == csp
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert "camera=()" in headers["permissions-policy"]


@pytest.fixture
def client(sqlite_db):
    from analystos.api.app import app

    return TestClient(app)  # no lifespan: no scheduler or runtime is started


def test_api_success_and_error_responses_are_hardened(client):
    ok = client.get("/api/health")
    _assert_hardened(ok.headers)
    assert ok.headers["cache-control"] == "no-store"
    denied = client.get("/api/ask/threads/ask_nope")
    assert denied.status_code == 401
    _assert_hardened(denied.headers)
    missing = client.get("/api/no-such-route")
    assert missing.status_code == 404
    _assert_hardened(missing.headers)


def test_the_docs_keep_only_anti_framing(client):
    r = client.get("/docs")
    assert r.status_code == 200
    _assert_hardened(r.headers, DOCS_CSP)


def test_a_routes_own_headers_win_and_streams_pass_through():
    app = FastAPI()

    @app.get("/stream")
    def stream():
        return StreamingResponse(iter([b"data: 1\n\n", b"data: 2\n\n"]), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    app.add_middleware(SecurityHeadersMiddleware)
    r = TestClient(app).get("/stream")
    assert r.text == "data: 1\n\ndata: 2\n\n"
    assert r.headers["cache-control"] == "no-cache"  # not overwritten, not duplicated
    assert r.headers.get_list("cache-control") == ["no-cache"]
    _assert_hardened(r.headers)


def _locations(conf: str) -> dict[str, str]:
    """`location <match> { ... }` bodies (one level deep, as both configs are written)."""
    return {m.group(1).strip(): m.group(2) for m in re.finditer(r"^\s*location\s+([^{\n]+)\{([^{}]*)\}", conf, re.M)}


@pytest.mark.parametrize("path", ["web/nginx.conf", "deploy/helm/analystos/templates/scheduler-web.yaml"])
def test_every_web_location_that_sets_headers_repeats_the_security_set(path):
    """nginx drops server-level add_header in a location that has its own, so each such location must carry the set,
    with `always` so error pages get it too."""
    conf = (ROOT / path).read_text(encoding="utf-8")
    locations = _locations(conf)
    assert {"/", "/assets/"} <= set(locations)
    for match, body in locations.items():
        if "add_header" not in body or match.startswith("/api/") or match == "= /healthz":
            continue
        for header in WEB_HEADERS:
            assert re.search(rf"add_header\s+{header}\s+.+\s+always;", body), (path, match, header)
        csp = re.search(r'add_header\s+Content-Security-Policy\s+"([^"]+)"', body).group(1)
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
        assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp

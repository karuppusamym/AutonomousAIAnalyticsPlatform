"""Browser hardening headers on every API response (security self-check 2026-10-30, ZAP 10020/10021/10038/10049).

The API returns JSON, event streams and export files, never a page, so a browser gets: no MIME sniffing (a JSON body
holding user text cannot be read as HTML), no framing, a policy that lets nothing it returns load or run anything, no
referrer, no shared or disk caching of authenticated data (unless the route set its own Cache-Control), and no
cross-origin no-cors reads (CORS requests from the web origin are unaffected). The interactive API docs load Swagger
UI from a CDN, so they keep only the anti-framing part of the policy. HSTS belongs to the TLS terminator (ingress).
"""
from __future__ import annotations

from typing import Any

DOCS_PATHS = ("/docs", "/redoc")
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
DOCS_CSP = "frame-ancestors 'none'"
HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
)


def _is_docs(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in DOCS_PATHS)


class SecurityHeadersMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware): streaming responses pass through untouched. A header the route already
    set wins, so a route can still choose its own caching or policy."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = (DOCS_CSP if _is_docs(scope.get("path") or "") else API_CSP).encode("latin-1")
        extra = (*HEADERS, (b"content-security-policy", csp), (b"cache-control", b"no-store"))

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                message = {**message, "headers": [*headers, *((k, v) for k, v in extra if k not in present)]}
            await send(message)

        await self.app(scope, receive, send_with_headers)

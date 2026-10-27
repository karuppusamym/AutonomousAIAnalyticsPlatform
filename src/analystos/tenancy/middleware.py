"""Bind the tenant of each API request before any route code runs.

Pure ASGI (no BaseHTTPMiddleware): the context variable set here is copied into every thread and
task the request starts, so the request's DB session, extra ``session_scope`` blocks and SSE reads
all act for the same tenant. A token that does not decode binds nothing; the route's own
authentication then answers 401 as before.
"""
from __future__ import annotations

import json
from typing import Any

from analystos.core.errors import Forbidden
from analystos.tenancy.context import _system, _tenant

TENANT_HEADER = b"x-tenant-id"


def _user_id(headers: dict[bytes, bytes]) -> str | None:
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if not auth.lower().startswith("bearer "):
        return None
    from analystos.core.errors import Unauthenticated
    from analystos.security.auth import decode_token

    try:
        return str(decode_token(auth[7:].strip())["sub"])
    except (Unauthenticated, KeyError):
        return None


class TenantMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        user_id = _user_id(headers)
        tenant = None
        if user_id is not None:
            import anyio

            from analystos.tenancy.resolve import resolve_tenant

            requested = headers.get(TENANT_HEADER, b"").decode("latin-1").strip() or None
            try:
                tenant = await anyio.to_thread.run_sync(resolve_tenant, user_id, requested)
            except Forbidden as exc:
                if scope["type"] == "http":
                    body = json.dumps({"error": exc.to_dict()}).encode()
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": body})
                return
        t, s = _tenant.set(tenant), _system.set(False)
        try:
            await self.app(scope, receive, send)
        finally:
            _system.reset(s)
            _tenant.reset(t)

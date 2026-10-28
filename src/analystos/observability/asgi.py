"""Request metrics and the request span (pure ASGI: streaming responses pass through untouched).

Latency is measured to the response start, so an SSE stream counts the time until it opens, not its life.
The route class comes from the matched route template (bounded by the app's routes; unmatched paths are one
class), never from the raw path.
"""
from __future__ import annotations

import time
from typing import Any

from analystos.observability import metrics, telemetry

# Template segments that name an SLO class wherever they appear; otherwise the first literal segment.
CLASS_KEYWORDS = ("ask", "desk", "goals", "conversations", "runs", "analysis", "spend", "evaluations", "admin",
                  "auth", "mcp", "tools", "context", "evidence", "catalog", "health", "metrics")


def route_class(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not template:
        return "unmatched"
    parts = [p for p in template.split("/") if p and not p.startswith("{")]
    for keyword in CLASS_KEYWORDS:
        if keyword in parts:
            return keyword
    literal = [p for p in parts if p not in ("api", "v2")]
    return literal[0][:24] if literal else "root"


class MetricsMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        method = scope.get("method", "GET")
        state = {"status": 500, "recorded": False}

        def record() -> None:
            if state["recorded"]:
                return
            state["recorded"] = True
            cls = route_class(scope)
            metrics.observe("analystos_http_request_duration_seconds", time.perf_counter() - started,
                            **{"class": cls, "method": method})
            metrics.inc("analystos_http_requests_total", **{"class": cls, "method": method, "status": str(state["status"])})

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", 500))
                record()
            await send(message)

        with telemetry.span("http.request", method=method) as attrs:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                record()
                attrs.update({"class": route_class(scope), "http.status_code": state["status"]})

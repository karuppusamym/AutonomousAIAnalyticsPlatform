"""`/metrics` for processes without an HTTP server (Temporal workers, the scheduler): a daemon thread on
`ANALYSTOS_METRICS_PORT` (unset or 0 = off). Same exposition and optional bearer token as the API's route."""
from __future__ import annotations

import hmac
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from analystos.observability import metrics, telemetry

log = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        token = os.environ.get("ANALYSTOS_METRICS_TOKEN", "")
        given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            self.send_error(401)
            return
        body = metrics.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:  # scrapes are not log lines
        return


def serve_metrics(component: str, port: int | None = None) -> ThreadingHTTPServer | None:
    telemetry.setup_tracing(component)
    metrics.set_gauge("analystos_build_info", 1, version=_version(), component=component)
    port = port if port is not None else int(os.environ.get("ANALYSTOS_METRICS_PORT", "0") or 0)
    if not port:
        return None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        log.warning("metrics endpoint not started on :%s: %s", port, exc)
        return None
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    log.info("metrics on :%s/metrics (%s)", server.server_address[1], component)
    return server


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("analystos")
    except Exception:  # noqa: BLE001
        return "unknown"

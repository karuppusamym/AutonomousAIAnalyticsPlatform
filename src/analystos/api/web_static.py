"""The built web app served by the API (`ANALYSTOS_WEB_DIST`, set by `analystos local`): one port, no Node at run time.

Registered after every API route, so it only answers paths nothing else claimed. API-shaped paths that matched no
route keep their JSON 404 instead of receiving the app shell. Files are served from inside the dist folder only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, JSONResponse

# The web server policy of web/nginx.conf (and the Helm web template): the API default (`default-src 'none'`,
# api/security_headers.py) suits JSON, not a page. A route-set header wins over the middleware default.
WEB_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "font-src 'self' data:; connect-src 'self'; frame-src 'self' https: http:; worker-src 'self' blob:; "
           "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
IMMUTABLE = "public, max-age=31536000, immutable"  # Vite file names carry a content hash
API_PREFIXES = ("api/", "v2/", "mcp", "metrics", "health", "docs", "redoc", "openapi.json", "auth/")


def mount(app: Any, dist: Path | None) -> bool:
    if dist is None:
        return False
    root = Path(dist).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    async def web(path: str) -> Any:
        if path.startswith(API_PREFIXES) or path in ("api", "v2"):
            return JSONResponse({"error": {"code": "not_found", "message": "Not Found"}}, status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            cache = IMMUTABLE if path.startswith("assets/") else "no-cache"
            return FileResponse(candidate, headers={"Content-Security-Policy": WEB_CSP, "Cache-Control": cache})
        # client-side routes get the app shell
        return FileResponse(index, headers={"Content-Security-Policy": WEB_CSP, "Cache-Control": "no-cache"})

    app.add_api_route("/{path:path}", web, methods=["GET", "HEAD"], include_in_schema=False)
    return True

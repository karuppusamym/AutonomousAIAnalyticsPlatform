"""Governed REST connector (S02-T8, FR-029): a declarative description of a read-only HTTP API, staged into
the analytics DB like any API source and then queried through the gateway.

    base_url: https://api.example.com/v1
    auth: {type: bearer}                     # bearer | basic (username in config) | header (name) | none
    rate_limit: {requests_per_second: 5}
    endpoints:
      - name: tickets                        # the staged table
        path: /tickets
        params: {status: {type: string, value: open, allowed: [open, closed]}, limit: {type: integer, value: 100}}
        pagination: {type: page, page_param: page, size_param: per_page, size: 100}
        records_path: data.items
        columns:
          - {name: id, path: id, type: integer, key: true}
          - {name: created, path: created_at, type: timestamp}

Read-only by construction: an endpoint is GET, or POST only when it is marked `safe: true` AND its
`host/path` is on the operator's allowlist (`ANALYSTOS_REST_SAFE_POST_ALLOWLIST`); there is no other verb.
Paths are relative to `base_url` (no scheme, host, `..` or query string); a `next_url` from the API must stay on
the base URL. Every request goes through the outbound SSRF guard (`tools/http.call`: the host is resolved and
pinned once, private and metadata addresses refused, no redirects, a response size cap). The credential comes
from `secret_ref` (connectors/secrets.py policy) and is sent only to the base URL's host. Parameter values are
fixed in the description and type-checked; the API's text never changes what is called (context is data).
"""
from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from analystos.connectors.base import ConnectionTest, DiscoveredAsset, DiscoveredColumn
from analystos.connectors.naming import sanitize_identifier
from analystos.core.errors import Forbidden, InvalidInput, UpstreamUnavailable

ParamType = Literal["string", "integer", "number", "boolean", "date"]
ColumnType = Literal["text", "integer", "bigint", "numeric", "double", "boolean", "date", "timestamp", "json"]
MAX_PAGES = 1000


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ParamType = "string"
    value: Any = None
    allowed: list[Any] | None = None

    @model_validator(mode="after")
    def _typed(self) -> ParamSpec:
        if self.value is None:
            return self
        v = self.value
        ok = {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
              "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
              "date": isinstance(v, str) and _is_date(v)}[self.type]
        if not ok:
            raise ValueError(f"parameter value {v!r} is not a {self.type}")
        if self.allowed is not None and v not in self.allowed:
            raise ValueError(f"parameter value {v!r} is not one of {self.allowed}")
        return self


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none", "page", "offset", "cursor", "next_url"] = "none"
    page_param: str = "page"
    offset_param: str = "offset"
    size_param: str | None = None
    size: int = Field(default=100, ge=1, le=10_000)
    start: int = 1
    cursor_param: str = "cursor"
    cursor_path: str | None = None  # where the next cursor / next URL sits in the response body


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    type: ColumnType = "text"
    key: bool = False
    description: str | None = None


class EndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    method: Literal["GET", "POST"] = "GET"
    safe: bool = False
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    pagination: Pagination = Field(default_factory=Pagination)
    records_path: str | None = None
    columns: list[ColumnSpec] = Field(min_length=1, max_length=500)
    description: str | None = None

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or ".." in value.split("/") or "\\" in value:
            raise ValueError("endpoint path must be a plain path relative to base_url (no host, query or '..')")
        return "/" + value.lstrip("/")


class AuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none", "bearer", "basic", "header"] = "none"
    header: str | None = None  # `header` auth: the header name (e.g. X-Api-Key)
    username: str | None = None  # `basic` auth: the user; the password is the secret


class RestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    auth: AuthSpec = Field(default_factory=AuthSpec)
    rate_limit: dict[str, float] = Field(default_factory=lambda: {"requests_per_second": 5.0})
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    max_response_bytes: int = Field(default=5_000_000, ge=1_000, le=50_000_000)
    endpoints: list[EndpointSpec] = Field(min_length=1, max_length=50)

    @field_validator("base_url")
    @classmethod
    def _base(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password \
                or parts.query or parts.fragment:
            raise ValueError("base_url must be an http(s) URL without credentials, query or fragment")
        return value.strip().rstrip("/")


def _is_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _csv(value: Any) -> set[str]:
    return {p.strip().lower().rstrip("/") for p in str(value or "").split(",") if p.strip()}


def parse_spec(config: dict[str, Any], settings: Any | None = None) -> RestSpec:
    """Validate a source's REST description; POST endpoints must be `safe` and on the operator's allowlist."""
    body = {k: v for k, v in (config or {}).items() if k not in ("schemas", "max_rows", "sampling")}
    try:
        spec = RestSpec.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise InvalidInput(f"REST source description is invalid at {where or 'top level'}: {first.get('msg')}") from None
    names = [sanitize_identifier(e.name) for e in spec.endpoints]
    if len(set(names)) != len(names):
        raise InvalidInput("REST endpoint names must be unique")
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    allowed = _csv(getattr(settings, "rest_safe_post_allowlist", ""))
    host = (urlsplit(spec.base_url).hostname or "").lower()
    base_path = urlsplit(spec.base_url).path.rstrip("/")
    for e in spec.endpoints:
        if e.method == "POST":
            entry = f"{host}{base_path}{e.path}".lower().rstrip("/")
            if not e.safe or entry not in allowed:
                raise Forbidden(f"REST endpoint {e.name} uses POST: only an endpoint marked safe: true whose "
                                f"host/path ({entry}) a platform administrator allowlisted may be called")
    from analystos.services.sources import _CREDENTIAL_KEY

    for e in spec.endpoints:
        leaked = [k for k in e.params if _CREDENTIAL_KEY.search(k)]
        if leaked:
            raise InvalidInput(f"REST endpoint {e.name}: parameter {leaked[0]} looks like a credential; put it in the "
                               "source's secret_ref and use auth (bearer, basic or header) instead")
    if spec.auth.type == "header" and not spec.auth.header:
        raise InvalidInput("header auth needs auth.header (the header name)")
    if spec.auth.type == "basic" and not spec.auth.username:
        raise InvalidInput("basic auth needs auth.username")
    return spec


def _dig(doc: Any, path: str | None) -> Any:
    if not path:
        return doc
    for part in path.split("."):
        if isinstance(doc, dict):
            doc = doc.get(part)
        elif isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        else:
            return None
    return doc


_ARROW = {"text": pa.string(), "integer": pa.int64(), "bigint": pa.int64(), "numeric": pa.float64(),
          "double": pa.float64(), "boolean": pa.bool_(), "date": pa.string(), "timestamp": pa.string(), "json": pa.string()}


def _coerce(value: Any, kind: str) -> Any:
    import json

    if value is None:
        return None
    try:
        if kind in ("integer", "bigint"):
            return int(value)
        if kind in ("numeric", "double"):
            return float(value)
        if kind == "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")
        if kind == "json":
            return json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, str) else str(value)


class RESTConnector:
    kind = "rest"
    execution_mode = "staged"
    dialect = "postgres"

    def __init__(self, config: dict[str, Any], secret_ref: str | None, *, settings: Any | None = None,
                 http_transport: Any | None = None, resolver: Any | None = None, password: str | None = None,
                 sleep: Any = time.sleep, clock: Any = time.monotonic, **_: Any) -> None:
        self.config = dict(config or {})
        self.spec = parse_spec(self.config, settings)
        self._settings = settings
        self._secret_ref = secret_ref
        self._password = password
        self._transport = http_transport
        self._resolver = resolver
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.requests = 0

    # -- plumbing ------------------------------------------------------------------------------------
    def _settings_obj(self) -> Any:
        if self._settings is None:
            from analystos.core.config import get_settings

            self._settings = get_settings()
        return self._settings

    def _headers(self) -> dict[str, str]:
        auth = self.spec.auth
        if auth.type == "none":
            return {"Accept": "application/json"}
        from analystos.connectors.secrets import resolve_secret

        secret = self._password if self._password is not None else resolve_secret(self._secret_ref)
        if not secret:
            raise InvalidInput("this REST source needs a secret_ref for its credential")
        if auth.type == "bearer":
            return {"Accept": "application/json", "Authorization": f"Bearer {secret}"}
        if auth.type == "basic":
            token = base64.b64encode(f"{auth.username}:{secret}".encode()).decode()
            return {"Accept": "application/json", "Authorization": f"Basic {token}"}
        return {"Accept": "application/json", str(auth.header): secret}

    def _throttle(self) -> None:
        rps = float(self.spec.rate_limit.get("requests_per_second") or 0)
        if rps <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + 1.0 / rps
        if wait > 0:
            self._sleep(wait)

    def _call(self, url: str, method: str, params: dict[str, Any]) -> Any:
        from analystos.tools import http as outbound

        base = urlsplit(self.spec.base_url)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or not target.path.startswith(base.path):
            raise Forbidden("a REST request may only go to the source's base URL")
        settings = self._settings_obj()
        self._throttle()
        self.requests += 1
        result = outbound.call(url, method=method, parameters=params, allowlist=None,
                               private_hosts=outbound.split_hosts(getattr(settings, "outbound_private_hosts", "")),
                               timeout=self.spec.timeout_seconds, max_bytes=self.spec.max_response_bytes,
                               headers=self._headers(), resolver=self._resolver, transport=self._transport,
                               internal_only=bool(getattr(settings, "air_gapped", False)))
        return result["content"]

    def _endpoint(self, name: str) -> EndpointSpec:
        for e in self.spec.endpoints:
            if sanitize_identifier(e.name) == name or e.name == name:
                return e
        raise InvalidInput(f"no REST endpoint {name}")

    def _pages(self, e: EndpointSpec, max_rows: int) -> Iterator[list[Any]]:
        """Records page by page, never more than max_rows in total and never more than MAX_PAGES requests."""
        p = e.pagination
        params = {k: v.value for k, v in e.params.items() if v.value is not None}
        url = self.spec.base_url + e.path
        page, offset, cursor, seen = p.start, 0, None, 0
        for _ in range(MAX_PAGES):
            q = dict(params)
            if p.size_param and p.type != "none":
                q[p.size_param] = p.size
            if p.type == "page":
                q[p.page_param] = page
            elif p.type == "offset":
                q[p.offset_param] = offset
            elif p.type == "cursor" and cursor:
                q[p.cursor_param] = cursor
            body = self._call(url, e.method, q)
            records = _dig(body, e.records_path)
            if records is None:
                records = []
            if not isinstance(records, list):
                raise UpstreamUnavailable(f"REST endpoint {e.name}: records_path does not point at a list")
            if records:
                room = max_rows - seen
                yield records[:room]
                seen += min(len(records), room)
            if seen >= max_rows or not records or p.type == "none":
                return
            if p.type in ("page", "offset") and p.size_param and len(records) < p.size:
                return  # a short page is the last one
            if p.type == "page":
                page += 1
            elif p.type == "offset":
                offset += len(records)
            else:
                nxt = _dig(body, p.cursor_path)
                if not nxt:
                    return
                if p.type == "cursor":
                    cursor = str(nxt)
                else:
                    url = str(nxt)  # checked against the base URL in _call
                    params = {}
        raise UpstreamUnavailable(f"REST endpoint {e.name} returned more than {MAX_PAGES} pages")

    # -- Connector protocol -------------------------------------------------------------------------
    def test(self) -> ConnectionTest:
        started = time.monotonic()
        try:
            e = self.spec.endpoints[0]
            next(self._pages(e, 1), [])
        except (InvalidInput, Forbidden, UpstreamUnavailable) as exc:
            return ConnectionTest(ok=False, message=exc.message, latency_ms=int((time.monotonic() - started) * 1000))
        return ConnectionTest(ok=True, message=f"{self.spec.base_url} answered", details={"endpoints": len(self.spec.endpoints)},
                              latency_ms=int((time.monotonic() - started) * 1000))

    def discover(self) -> list[DiscoveredAsset]:
        """The description is the catalogue: one table per endpoint (no request is needed)."""
        return [DiscoveredAsset(
            source_name=e.path, name=sanitize_identifier(e.name), kind="api_table", description=e.description,
            columns=[DiscoveredColumn(name=sanitize_identifier(c.name), data_type=c.type, is_key=c.key,
                                      description=c.description) for c in e.columns])
            for e in self.spec.endpoints]

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        e = self._endpoint(asset.name)
        names = [sanitize_identifier(c.name) for c in e.columns]
        schema = pa.schema([pa.field(n, _ARROW[c.type]) for n, c in zip(names, e.columns, strict=True)])
        for records in self._pages(e, max_rows):
            cols = [[_coerce(_dig(r, c.path), c.type) for r in records] for c in e.columns]
            yield pa.record_batch([pa.array(v, type=schema.field(i).type) for i, v in enumerate(cols)], schema=schema)

    def sqlalchemy_url(self) -> str:
        raise InvalidInput("a REST source is staged; it has no SQL connection")

    def close(self) -> None:
        return None

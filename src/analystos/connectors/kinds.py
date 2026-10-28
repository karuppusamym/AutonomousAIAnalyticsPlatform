"""Source-kind catalog: which systems AnalystOS can connect to and how (config/source_kinds.yaml).

The catalog is the single place that decides, per kind, the SQLAlchemy URL shape, the credential
field (always through ``secret_ref``), the driver and its pip extra, the source's native sqlglot
dialect, whether pushdown is allowed, the read-only session statements and the discovery
strategies. ``governance.policy`` and ``services.sources`` read it through ``dialect_for`` and
``execution_mode_for`` so a new kind is a catalog entry, not code in several places.

Pushdown (the gateway queries the source in place) is allowed only when three things hold (P4-E01):
the gateway validator has a security suite for the kind's native dialect (``gateway/dialects.py``),
the analysis compiler (``skills/sqlbuild``) emits that dialect, and the session can be made
read-only (catalog ``session_sql.readonly`` statements, or a ``read_only`` connect argument).
SQL Server predates the last rule: it pushes down with a least-privilege login instead
(``LEGACY_IDENTITY_PUSHDOWN``). Everything else is staged into the analytics DB and queried there in
the postgres dialect. Newly allowed kinds (DuckDB files) keep ``staged`` as their default mode and
push down only when a source asks for it (``pushdown_default: false``).
"""
from __future__ import annotations

import base64
import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field

from analystos.core.config import DATA_ROOT
from analystos.core.errors import InvalidInput

CATALOG_PATH = DATA_ROOT / "config" / "source_kinds.yaml"
# Validator dialects (gateway/dialects.py) that the analysis compiler also emits (skills/sqlbuild.DIALECTS);
# tests/unit/test_source_kinds.py keeps the three lists in step.
PUSHDOWN_DIALECTS = frozenset({"postgres", "tsql", "duckdb"})
# Kinds allowed to push down with a least-privilege identity and no session read-only switch (increment 3).
LEGACY_IDENTITY_PUSHDOWN = frozenset({"sqlserver"})
STAGED_DIALECT = "postgres"  # staged data lives in the analytics DB
KIND_ALIASES = {"file": "csv", "postgresql": "postgres", "mssql": "sqlserver"}
PLACEHOLDERS = ("username", "password", "host", "port", "database", "account", "warehouse", "role", "project",
                "http_path", "catalog", "schema", "path", "sslmode", "http_scheme", "connect_timeout")
DEFAULT_CONNECT_TIMEOUT = 10

Category = Literal["database", "warehouse", "lakehouse", "engine", "file", "api"]
Mode = Literal["pushdown", "staged"]


class SecretSpec(BaseModel):
    field: str = "password"
    required: bool = True
    encoding: Literal["plain", "base64"] = "plain"


class DriverSpec(BaseModel):
    modules: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    extra: str | None = None


class SessionSQL(BaseModel):
    readonly: list[str] = Field(default_factory=list)
    timeout: list[str] = Field(default_factory=list)


class KindCapabilities(BaseModel):
    # S02-T5: statements that hand the end user to the source's own row security (driver-bound parameters
    # %(user)s, %(user_email)s, %(workspace)s), run on pushdown connections before the query.
    session_identity: list[str] = Field(default_factory=list)


class SourceKind(BaseModel):
    kind: str
    label: str
    category: Category
    url_template: str | None = None
    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    secret: SecretSpec | None = None
    default_port: int | None = None
    driver: DriverSpec = Field(default_factory=DriverSpec)
    sqlglot_dialect: str = STAGED_DIALECT
    session_sql: SessionSQL = Field(default_factory=SessionSQL)
    connect_args: dict[str, Any] = Field(default_factory=dict)
    connect_timeout_arg: str | None = None
    timeout_url_param: str | None = None
    row_estimate: str = "none"
    catalog: Literal["inspector", "duckdb", "none"] = "inspector"
    system_schemas: list[str] = Field(default_factory=list)
    default_schemas: list[str] | None = None
    pushdown_default: bool = True  # when pushdown is allowed: is it the default mode, or opt-in?
    capabilities: KindCapabilities = Field(default_factory=KindCapabilities)
    docs: str = ""

    @property
    def is_sql(self) -> bool:
        return self.url_template is not None

    @property
    def readonly_enforcement(self) -> Literal["session", "connect", "identity"]:
        """How a pushdown session is kept read-only: catalog statements, a connect argument, or only
        the configured least-privilege identity."""
        if self.session_sql.readonly:
            return "session"
        if self.connect_args.get("read_only") is True:
            return "connect"
        return "identity"

    @property
    def pushdown_allowed(self) -> bool:
        if not self.is_sql or self.sqlglot_dialect not in PUSHDOWN_DIALECTS:
            return False
        return self.readonly_enforcement != "identity" or self.kind in LEGACY_IDENTITY_PUSHDOWN

    @property
    def pip_install_hint(self) -> str:
        if self.driver.extra:
            return f"pip install 'analystos[{self.driver.extra}]'"
        return f"pip install {' '.join(self.driver.packages)}" if self.driver.packages else "(bundled)"

    def public(self) -> dict[str, Any]:
        """Catalog entry for UIs/APIs (no templates with credentials, just what a form needs)."""
        return {
            "kind": self.kind, "label": self.label, "category": self.category, "required": self.required,
            "optional": self.optional, "secret_field": self.secret.field if self.secret else None,
            "secret_required": bool(self.secret and self.secret.required), "default_port": self.default_port,
            "sqlglot_dialect": self.sqlglot_dialect, "pushdown_allowed": self.pushdown_allowed,
            "default_execution_mode": execution_mode_for(self.kind, None), "pip_extra": self.driver.extra,
            "driver_available": driver_available(self.kind), "readonly_session": bool(self.session_sql.readonly),
            "readonly_enforcement": self.readonly_enforcement, "docs": self.docs,
            "session_identity": bool(self.capabilities.session_identity),
        }


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, SourceKind]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, SourceKind] = {}
    for kind, body in (raw.get("kinds") or {}).items():
        out[kind] = SourceKind(kind=kind, **(body or {}))
    return out


def _catalog() -> dict[str, SourceKind]:
    return _load(str(CATALOG_PATH))


def list_kinds() -> list[SourceKind]:
    return list(_catalog().values())


def kind_ids() -> list[str]:
    return list(_catalog())


def get_kind(kind: str) -> SourceKind:
    key = (kind or "").strip().lower()
    key = KIND_ALIASES.get(key, key)
    found = _catalog().get(key)
    if found is None:
        raise InvalidInput(f"Unknown source kind {kind!r}; expected one of {', '.join(kind_ids())}")
    return found


def execution_mode_for(kind: str, requested: str | None = None) -> Mode:
    """``pushdown`` only when requested (or defaulted) AND the kind allows it; otherwise ``staged``.

    A pushdown request for a kind that cannot be pushed down degrades to ``staged`` (the data is
    still reachable, through a governed snapshot); an unknown mode string is an error."""
    spec = get_kind(kind)
    if requested not in (None, "", "pushdown", "staged"):
        raise InvalidInput(f"execution_mode must be 'pushdown' or 'staged', not {requested!r}")
    if requested == "staged" or not spec.pushdown_allowed:
        return "staged"
    if requested is None or requested == "":
        return "pushdown" if spec.pushdown_default else "staged"
    return "pushdown"


def dialect_for(kind: str, execution_mode: str | None = None) -> str:
    """The sqlglot dialect the *gateway* validates this source's SQL in.

    Pushdown sources: their native dialect (postgres | tsql | duckdb). Staged sources: ``postgres`` (the
    analytics DB), whatever the origin system speaks. ``execution_mode`` defaults to the kind's
    default mode; pass the Source row's mode when it is known."""
    mode = execution_mode_for(kind, execution_mode)
    return get_kind(kind).sqlglot_dialect if mode == "pushdown" else STAGED_DIALECT


def driver_available(kind: str) -> bool:
    for module in get_kind(kind).driver.modules:
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except (ImportError, ValueError):  # parent package missing
            return False
    return True


def require_driver(kind: str) -> None:
    spec = get_kind(kind)
    if not driver_available(kind):
        raise InvalidInput(
            f"The {spec.label} driver is not installed ({', '.join(spec.driver.modules)}): {spec.pip_install_hint}"
        )


def missing_config(kind: str, config: dict[str, Any]) -> list[str]:
    return [f for f in get_kind(kind).required if config.get(f) in (None, "")]


def _q(value: Any) -> str:
    return quote(str(value), safe="")


def build_url(kind: str, config: dict[str, Any], password: str | None) -> str:
    """Render the kind's SQLAlchemy URL. Credentials and placeholder values are URL-escaped; the
    result contains the secret, so it must never be logged (use ``redact_url`` for messages)."""
    spec = get_kind(kind)
    if not spec.is_sql or spec.url_template is None:
        raise InvalidInput(f"{spec.label} sources have no SQL endpoint")
    missing = missing_config(kind, config)
    if missing:
        raise InvalidInput(f"{spec.label} source needs config.{', config.'.join(missing)}")
    if spec.secret and spec.secret.required and not password:
        raise InvalidInput(f"{spec.label} source needs its {spec.secret.field} through secret_ref")
    template = spec.url_template
    if not password:
        template = template.replace(":{password}@", "@")
    if password and spec.secret and spec.secret.encoding == "base64":
        password = base64.urlsafe_b64encode(password.encode("utf-8")).decode("ascii")
    values: dict[str, str] = {}
    for name in PLACEHOLDERS:
        raw = password if name == "password" else config.get(name)
        if name == "port" and raw in (None, ""):
            raw = spec.default_port
        if name == "connect_timeout" and raw in (None, ""):
            raw = DEFAULT_CONNECT_TIMEOUT
        if raw in (None, ""):
            values[name] = ""
        elif name == "path":
            values[name] = _path_value(kind, str(raw))
        elif name == "host":
            host = str(raw).strip()
            if any(ch in host for ch in "@/?#\\ ") or not host:
                raise InvalidInput(f"config.host {host!r} is not a host name")
            values[name] = host
        else:
            values[name] = _q(raw)
    url = template.format(**values)
    base, sep, query = url.partition("?")
    if base.endswith("/") and not base.endswith("//"):  # an optional trailing path segment was empty
        base = base[:-1]
    kept = [p for p in query.split("&") if p and not p.endswith("=")] if sep else []
    return base + ("?" + "&".join(kept) if kept else "")


def _path_value(kind: str, path: str) -> str:
    if any(ch in path for ch in "?#\x00"):
        raise InvalidInput("config.path must not contain '?', '#' or NUL")
    if kind == "sqlite":  # a file: URI; percent-encode everything but the separators
        return quote(path, safe="/")
    return path


def redact_url(url: str) -> str:
    """A URL safe for messages: password and credential query parameters masked."""
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
        query = {k: ("***" if "credential" in k.lower() or "token" in k.lower() else v) for k, v in parsed.query.items()}
        return parsed.set(query=query).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - never echo an unparsable URL
        return "<unparsable url>"


def session_statements(kind: str, timeout_seconds: int | None = None, *, readonly: bool = True) -> list[str]:
    """Read-only (and optionally timeout) statements to run on a fresh connection of ``kind``."""
    spec = get_kind(kind)
    out = list(spec.session_sql.readonly) if readonly else []
    if timeout_seconds:
        seconds = max(1, int(timeout_seconds))
        out = [s.format(timeout_ms=seconds * 1000, timeout_s=seconds) for s in spec.session_sql.timeout] + out
    return out

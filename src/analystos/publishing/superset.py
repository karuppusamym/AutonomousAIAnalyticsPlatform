"""Apache Superset (4.x) publisher over the REST API v1.

Idempotency: every object carries a deterministic, workspace-scoped identity and is looked up
before it is created (reconcile-before-create), so a publish retried after a crash converges on
the same objects:

    database   database_name  "AnalystOS Analytics (<workspace_id>)"
    dataset    table_name     "aos_<ws>_<dataset name>"   (virtual dataset, scoped to the database)
    chart      slice_name     "aos_<ws>_<chart key>: <title>"  + params.aos_workspace / aos_key
    dashboard  slug           "aos-<ws>-<dashboard key>"

``previous`` (a prior PublishResult.external_ids) is used as a first hint; ids that no longer
exist fall back to name lookup. ``update_*`` PUTs in place, so external ids stay stable.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle, PublishResult
from analystos.core.errors import AnalystOSError, Forbidden, InvalidInput, NotFound, UpstreamUnavailable
from analystos.publishing import _rison as rison
from analystos.publishing.base import ExternalId, PublishContext, workspace_slug
from analystos.publishing.layout import build_position_json, parse_position_json
from analystos.publishing.preview import chart_metric_names, validate_bundle
from analystos.publishing.superset_access import SupersetAccess
from analystos.publishing.superset_charts import build_chart, metric_d3format

if TYPE_CHECKING:
    from analystos.core.config import Settings

log = logging.getLogger(__name__)

PAGE_SIZE = 100
_COLUMN_PUT_FIELDS = {
    "id", "column_name", "type", "advanced_data_type", "verbose_name", "description", "expression", "extra",
    "filterable", "groupby", "is_active", "is_dttm", "python_date_format", "uuid",
}
_METRIC_PUT_FIELDS = {
    "id", "metric_name", "expression", "description", "extra", "metric_type", "d3format", "verbose_name",
    "warning_text", "uuid",
}


def superset_metric(m: MetricDef) -> dict[str, Any]:
    """A KPI as a Superset dataset metric. An approved semantic-layer metric (P4-K03) is marked certified,
    so Superset users can tell the governed definitions from ad-hoc ones."""
    item: dict[str, Any] = {
        "metric_name": m.name,
        "expression": m.sql_expression,
        "verbose_name": m.display_name,
        "description": m.definition,
        "d3format": metric_d3format(m),
    }
    if m.status == "approved":
        item["extra"] = json.dumps({"certification": {
            "certified_by": "AnalystOS semantic layer",
            "details": "Approved metric of the workspace semantic model" + (f" (owner {m.owner})" if m.owner else "")}})
    return item


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if isinstance(body, dict):
        msg = body.get("message") or body.get("msg")
        if msg is None and body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in body["errors"] if isinstance(e, dict)) or body["errors"]
        if msg is not None:
            return msg if isinstance(msg, str) else json.dumps(msg, default=str)[:1000]
    return json.dumps(body, default=str)[:1000]


def map_error(resp: httpx.Response, what: str) -> AnalystOSError:
    status = resp.status_code
    msg = f"Superset {what}: HTTP {status}: {_message(resp)}"
    details = {"status": status, "superset_message": _message(resp)}
    if status >= 500:
        return UpstreamUnavailable(msg, details=details)
    if status in (401, 403):
        return Forbidden(msg, details=details)
    if status == 404:
        return NotFound(msg, details=details)
    return InvalidInput(msg, details=details)


class SupersetClient:
    """Thin authenticated JSON client: JWT login (refresh on 401), CSRF token + session cookie."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._access: str | None = None
        self._refresh: str | None = None
        self._csrf: str | None = None

    def close(self) -> None:
        self._http.close()

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable(f"Superset {method} {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"Superset unreachable at {self.base_url}: {exc}") from exc

    def login(self) -> None:
        resp = self._send(
            "POST",
            "/api/v1/security/login",
            json={"username": self.username, "password": self._password, "provider": "db", "refresh": True},
        )
        if resp.status_code != 200:
            raise map_error(resp, "login")
        body = resp.json()
        self._access, self._refresh = body["access_token"], body.get("refresh_token")
        self._csrf = None

    def _refresh_access(self) -> None:
        if self._refresh:
            resp = self._send(
                "POST", "/api/v1/security/refresh", headers={"Authorization": f"Bearer {self._refresh}"}
            )
            if resp.status_code == 200 and resp.json().get("access_token"):
                self._access = resp.json()["access_token"]
                self._csrf = None
                return
        self.login()

    def csrf_token(self) -> str:
        if self._csrf is None:
            if self._access is None:
                self.login()
            resp = self._send(
                "GET", "/api/v1/security/csrf_token/", headers={"Authorization": f"Bearer {self._access}"}
            )
            if resp.status_code == 401:
                self._refresh_access()
                resp = self._send(
                    "GET", "/api/v1/security/csrf_token/", headers={"Authorization": f"Bearer {self._access}"}
                )
            if resp.status_code != 200:
                raise map_error(resp, "csrf token")
            self._csrf = resp.json()["result"]
        return self._csrf

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        if self._access is None:
            self.login()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access}", "Referer": self.base_url}
            if method != "GET":
                headers["X-CSRFToken"] = self.csrf_token()
            extra = {"timeout": timeout} if timeout is not None else {}
            resp = self._send(method, path, json=json_body, params=params, headers=headers, **extra)
            if attempt == 0 and resp.status_code == 401:
                self._refresh_access()
                continue
            if attempt == 0 and resp.status_code == 400 and "csrf" in resp.text.lower():
                self._csrf = None
                continue
            break
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 400:
            raise map_error(resp, f"{method} {path}")
        if raw:
            return resp
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.text}

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any, *, timeout: float | None = None) -> Any:
        return self.request("POST", path, json_body=body, timeout=timeout)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, json_body=body)

    def delete(self, path: str) -> bool:
        return self.request("DELETE", path, allow_404=True) is not None

    def list(self, resource: str, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            q = rison.dumps({"filters": filters, "page": page, "page_size": PAGE_SIZE})
            body = self.get(f"/api/v1/{resource}/", params={"q": q})
            rows = body.get("result") or []
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                return out
            page += 1


def _schema_of(dataset: DatasetDef) -> str | None:
    for asset in dataset.source_assets:
        parts = asset.split(".")
        if len(parts) >= 2:
            return parts[-2]
    m = re.search(r'\bfrom\s+"?([A-Za-z_][\w$]*)"?\s*\.\s*"?[A-Za-z_]', dataset.sql, re.IGNORECASE)
    return m.group(1) if m else None


class SupersetPublisher(SupersetAccess):
    destination = "superset"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        analytics_sqlalchemy_uri: str | None = None,
        public_url: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        bi_login: Callable[[str], tuple[str, str]] | None = None,
        tenant_of: Callable[[str], str | None] | None = None,
    ) -> None:
        if settings is None and base_url is None:
            from analystos.core.config import get_settings

            settings = get_settings()
        self.base_url = (base_url or settings.superset_url).rstrip("/")  # type: ignore[union-attr]
        configured_public_url = settings.superset_public_url if settings else None
        self.public_url = (public_url or configured_public_url or self.base_url).rstrip("/")
        self.analytics_uri = analytics_sqlalchemy_uri or (settings.superset_analytics_sqlalchemy_uri if settings else "")
        self.role_prefix = getattr(settings, "analytics_workspace_role_prefix", "analystos_r_") if settings else "analystos_r_"
        if bi_login is None and settings is not None:
            from analystos.staging.roles import ensure_bi_login

            def bi_login(workspace_id: str) -> tuple[str, str]:
                return ensure_bi_login(settings, workspace_id)
        self._bi_login = bi_login
        if tenant_of is None and settings is not None:
            from analystos.tenancy.background import workspace_tenant as tenant_of
        self._tenant_of = tenant_of or (lambda _workspace_id: None)
        self.client = SupersetClient(
            self.base_url,
            username or (settings.superset_username if settings else "admin"),
            password or (settings.superset_password if settings else ""),
            timeout=timeout,
            transport=transport,
        )

    # -- naming ---------------------------------------------------------------------------------
    @staticmethod
    def database_name(workspace_id: str) -> str:
        return f"AnalystOS Analytics ({workspace_id})"

    @staticmethod
    def dataset_table_name(workspace_id: str, dataset_name: str) -> str:
        return f"aos_{workspace_slug(workspace_id)}_{re.sub(r'[^A-Za-z0-9_]+', '_', dataset_name)}"[:250]

    @staticmethod
    def chart_prefix(workspace_id: str) -> str:
        return f"aos_{workspace_slug(workspace_id)}_"

    @classmethod
    def chart_name(cls, workspace_id: str, chart: ChartSpec) -> str:
        return f"{cls.chart_prefix(workspace_id)}{chart.key}: {chart.title}"[:250]

    @staticmethod
    def dashboard_slug(workspace_id: str, key: str) -> str:
        return f"aos-{workspace_slug(workspace_id).replace('_', '-')}-{re.sub(r'[^A-Za-z0-9-]+', '-', key)}".lower()[:255]

    def dashboard_url(self, dashboard_id: ExternalId) -> str:
        return f"{self.public_url}/superset/dashboard/{dashboard_id}/"

    # -- connection -----------------------------------------------------------------------------
    def test_connection(self) -> dict[str, Any]:
        try:
            self.client.login()
            # /api/v1/me/ is session-cookie only in 4.1 (401 for JWT); the CSRF endpoint is JWT-protected.
            self.client.csrf_token()
            return {"ok": True, "destination": self.destination, "url": self.public_url,
                    "user": self.client.username, "error": None}
        except AnalystOSError as exc:
            return {"ok": False, "destination": self.destination, "url": self.public_url, "error": exc.message}

    # -- database -------------------------------------------------------------------------------
    def find_database(self, workspace_id: str) -> int | None:
        name = self.database_name(workspace_id)
        rows = self.client.list("database", [{"col": "database_name", "opr": "eq", "value": name}])
        for r in rows:
            if r.get("database_name") == name:
                return int(r["id"])
        return None

    def analytics_uri_for(self, workspace_id: str) -> str:
        """The workspace's own BI login (P4-02): a member of this workspace's reader role only. The shared
        reader login is never given to Superset, because it may SET ROLE into every workspace and SQL Lab
        or a virtual dataset could run ``SELECT set_config('role', <other workspace>, false)``."""
        from sqlalchemy.engine import make_url

        if self._bi_login is None:
            raise InvalidInput("no per-workspace BI login is configured for Superset (P4-02)")
        login, password = self._bi_login(workspace_id)
        url = make_url(self.analytics_uri).set(username=login, password=password)
        query = {k: v for k, v in url.query.items() if k != "options"}  # no role switching at connect time
        return url.set(query=query).render_as_string(hide_password=False)

    @staticmethod
    def _login_fingerprint(uri: str) -> str:
        import hashlib

        return hashlib.sha256(uri.encode()).hexdigest()[:16]

    def _database_extra(self, uri: str, current: str | None = None, workspace_id: str | None = None) -> str:
        try:
            extra = json.loads(current or "{}")
        except ValueError:
            extra = {}
        extra.setdefault("allows_virtual_table_explore", True)
        extra["aos_bi_login"] = self._login_fingerprint(uri)
        if workspace_id:
            self._stamp(extra, workspace_id)
        return json.dumps(extra)

    def _reconcile_database_uri(self, database_id: int, workspace_id: str) -> None:
        """Databases created before BI logins connect as the shared reader (with ``-c role=``), and a
        rotated secret changes the password: move them to the workspace's login. Fails loudly: a
        database left on the shared reader is the cross-workspace path this closes."""
        from sqlalchemy.engine import make_url

        wanted = self.analytics_uri_for(workspace_id)
        # 4.1 returns the (password-masked) URI only from /connection; older builds from the item itself.
        body = self.client.get(f"/api/v1/database/{database_id}/connection", allow_404=True) \
            or self.client.get(f"/api/v1/database/{database_id}") or {}
        current = body.get("result") or {}
        try:
            url = make_url(str(current.get("sqlalchemy_uri") or ""))
            same_login = url.username == make_url(wanted).username and "options" not in url.query
        except Exception:  # noqa: BLE001 - unparseable: rewrite it
            same_login = False
        try:
            stamps = json.loads(current.get("extra") or "{}")
        except ValueError:
            stamps = {}
        wanted_extra = self._database_extra(wanted, current.get("extra"), workspace_id)
        if same_login and stamps.get("aos_bi_login") == self._login_fingerprint(wanted) and                 all(stamps.get(k) == v for k, v in json.loads(wanted_extra).items() if k.startswith("aos_")):
            return
        self.client.put(f"/api/v1/database/{database_id}", {"sqlalchemy_uri": wanted, "extra": wanted_extra})

    def ensure_database(self, workspace_id: str) -> tuple[int, bool]:
        """(id, created). Read-only analytics identity bound to the workspace's reader role; not
        exposed in SQL Lab; no DML."""
        existing = self.find_database(workspace_id)
        if existing is not None:
            self.check_database_tenant(existing, workspace_id)  # never reuse another tenant's connection
            if self.analytics_uri:
                self._reconcile_database_uri(existing, workspace_id)
            return existing, False
        if not self.analytics_uri:
            raise InvalidInput("superset_analytics_sqlalchemy_uri is not configured")
        uri = self.analytics_uri_for(workspace_id)
        try:
            body = self._create_database(workspace_id, uri)
        except UpstreamUnavailable as exc:
            # Superset builds permissions for every schema of the analytics database while it creates the
            # connection, which outlasts a request timeout on a database with hundreds of schemas. It usually
            # finishes anyway: take the connection it made rather than failing (or creating a second one).
            if "timed out" not in exc.message:
                raise
            made = self._await_database(workspace_id)
            if made is None:
                raise
            self.check_database_tenant(made, workspace_id)
            return made, True
        return int(body["id"]), True

    DATABASE_CREATE_TIMEOUT = 180.0  # seconds; creation syncs per-schema permissions synchronously
    DATABASE_WAIT_SECONDS = 120.0
    DATABASE_WAIT_POLL = 5.0

    def _await_database(self, workspace_id: str) -> int | None:
        deadline = time.monotonic() + self.DATABASE_WAIT_SECONDS
        while True:
            found = self.find_database(workspace_id)
            if found is not None or time.monotonic() >= deadline:
                return found
            time.sleep(self.DATABASE_WAIT_POLL)

    def _create_database(self, workspace_id: str, uri: str) -> dict[str, Any]:
        return self.client.post(
            "/api/v1/database/",
            {
                "database_name": self.database_name(workspace_id),
                "sqlalchemy_uri": uri,
                "expose_in_sqllab": False,
                "allow_dml": False,
                "allow_ctas": False,
                "allow_cvas": False,
                "allow_run_async": False,
                "allow_file_upload": False,
                "impersonate_user": False,
                "extra": self._database_extra(uri, None, workspace_id),
            },
            timeout=self.DATABASE_CREATE_TIMEOUT,
        )

    # -- workspace access (P4-02) ---------------------------------------------------------------
    BASE_USER_ROLES = ("Gamma",)  # Superset's read-only application role: no data access of its own

    @staticmethod
    def workspace_role_name(workspace_id: str) -> str:
        """The viewer role (S08: `ws_{slug}_viewer`; `ws_{slug}_editor` adds write, superset_access.py)."""
        return f"ws_{workspace_slug(workspace_id)}_viewer"

    def _security_api(self, resource: str, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return self.client.list(f"security/{resource}", filters)
        except NotFound as exc:
            raise UpstreamUnavailable("Superset security API is disabled; set FAB_ADD_SECURITY_API = True "
                                      "(deploy/superset/superset_config.py) to scope users per workspace") from exc

    def _role_id(self, name: str, *, create: bool = False) -> int | None:
        for r in self._security_api("roles", [{"col": "name", "opr": "eq", "value": name}]):
            if r.get("name") == name:
                return int(r["id"])
        if not create:
            return None
        return int(self.client.post("/api/v1/security/roles/", {"name": name})["id"])

    def _database_access_pvm(self, database_id: int) -> int:
        """The ``database_access`` permission on this database's resource: every dataset and SQL Lab
        query on that database, and nothing on any other."""
        db = self.client.get(f"/api/v1/database/{database_id}")["result"]
        resource = f"[{db['database_name']}].(id:{database_id})"
        vms = [v for v in self._security_api("resources", [{"col": "name", "opr": "eq", "value": resource}])
               if v.get("name") == resource]
        perms = [p for p in self._security_api("permissions", [{"col": "name", "opr": "eq", "value": "database_access"}])
                 if p.get("name") == "database_access"]
        if not vms or not perms:
            raise NotFound(f"Superset has no database_access permission for {resource}")
        pvms = self._security_api("permissions-resources", [{"col": "view_menu", "opr": "rel_o_m", "value": vms[0]["id"]},
                                                             {"col": "permission", "opr": "rel_o_m", "value": perms[0]["id"]}])
        if not pvms:
            raise NotFound(f"Superset has no database_access permission for {resource}")
        return int(pvms[0]["id"])

    def ensure_workspace_role(self, workspace_id: str) -> int:
        """One Superset role per workspace holding exactly ``database_access`` on that workspace's
        database (which itself connects as the workspace's BI login). Replaces whatever else the role
        held, so a hand-added grant on another workspace's database does not survive a sync."""
        database_id, _ = self.ensure_database(workspace_id)
        role_id = self._role_id(self.workspace_role_name(workspace_id), create=True)
        assert role_id is not None
        pvm = self._database_access_pvm(database_id)
        self.client.post(f"/api/v1/security/roles/{role_id}/permissions", {"permission_view_menu_ids": [pvm]})
        return role_id

    def sync_user(self, *, username: str, email: str, workspace_ids: list[str], first_name: str = "", last_name: str = "",
                  password: str | None = None, sql_lab: bool = False, active: bool = True,
                  editor_workspace_ids: list[str] | None = None) -> int:
        """Make a Superset user's roles exactly: Gamma (+ sql_lab) + the viewer roles of ``workspace_ids`` + the
        editor roles of ``editor_workspace_ids`` (a subset: authors of those workspaces).
        Admin/Alpha (all-data roles) are never granted here, and are removed from a synced user."""
        import secrets

        role_ids = [rid for rid in (self._role_id(n) for n in (*self.BASE_USER_ROLES, *(["sql_lab"] if sql_lab else [])))
                    if rid is not None]
        role_ids += [self.ensure_workspace_role(ws) for ws in sorted(set(workspace_ids))]
        role_ids += [self.ensure_editor_role(ws) for ws in sorted(set(editor_workspace_ids or []) & set(workspace_ids))]
        body = {"username": username, "email": email, "first_name": first_name or username,
                "last_name": last_name or "-", "active": active, "roles": role_ids}
        existing = [u for u in self._security_api("users", [{"col": "username", "opr": "eq", "value": username}])
                    if u.get("username") == username]
        if existing:
            uid = int(existing[0]["id"])
            if password:
                body["password"] = password
            self.client.put(f"/api/v1/security/users/{uid}", body)
            return uid
        # No password given: an unusable random one; the person signs in through SSO (P4-09) or an admin reset.
        body["password"] = password or secrets.token_urlsafe(32)
        return int(self.client.post("/api/v1/security/users/", body)["id"])

    # -- datasets -------------------------------------------------------------------------------
    def find_dataset(self, database_id: int, table_name: str) -> int | None:
        rows = self.client.list(
            "dataset",
            [{"col": "table_name", "opr": "eq", "value": table_name}, {"col": "database", "opr": "rel_o_m", "value": database_id}],
        )
        for r in rows:
            db = r.get("database") or {}
            if r.get("table_name") == table_name and (not db or int(db.get("id", database_id)) == database_id):
                return int(r["id"])
        return None

    def _database_for(self, ctx: PublishContext) -> int:
        db = ctx.external_ids.get("database")
        if db is None:
            db, created = self.ensure_database(ctx.workspace_id)
            ctx.external_ids["database"] = db
            ctx.external_ids["database_created"] = created
        return int(db)

    def create_dataset(self, dataset: DatasetDef, ctx: PublishContext) -> ExternalId:
        body = self.client.post(
            "/api/v1/dataset/",
            {
                "database": self._database_for(ctx),
                "schema": _schema_of(dataset),
                "table_name": self.dataset_table_name(ctx.workspace_id, dataset.name),
                "sql": dataset.sql,
                "normalize_columns": False,
                "always_filter_main_dttm": False,
            },
        )
        ds_id = int(body["id"])
        self._configure_dataset(ds_id, dataset, ctx)
        return ds_id

    def update_dataset(self, external_id: ExternalId, dataset: DatasetDef, ctx: PublishContext) -> ExternalId:
        ds_id = int(external_id)
        self.client.put(
            f"/api/v1/dataset/{ds_id}",
            {"sql": dataset.sql, "schema": _schema_of(dataset), "description": dataset.description or None},
        )
        self._configure_dataset(ds_id, dataset, ctx)
        return ds_id

    def _configure_dataset(self, ds_id: int, dataset: DatasetDef, ctx: PublishContext) -> None:
        """Refresh columns from the SQL, flag the temporal column, apply business names."""
        self.client.put(f"/api/v1/dataset/{ds_id}/refresh", {})
        current = self.client.get(f"/api/v1/dataset/{ds_id}")["result"]
        business = {c.get("name"): c for c in dataset.columns}
        columns = []
        for col in current.get("columns") or []:
            c = {k: v for k, v in col.items() if k in _COLUMN_PUT_FIELDS and v is not None}
            if dataset.time_column and c.get("column_name") == dataset.time_column:
                c["is_dttm"] = True
            spec = business.get(c.get("column_name"))
            if spec and spec.get("business_name"):
                c["verbose_name"] = spec["business_name"]
            columns.append(c)
        if dataset.time_column and not any(c.get("column_name") == dataset.time_column for c in columns):
            raise InvalidInput(f"dataset {dataset.name!r}: time_column {dataset.time_column!r} not returned by its SQL")
        body: dict[str, Any] = {"columns": columns, "description": dataset.description or None}
        if dataset.time_column:
            body["main_dttm_col"] = dataset.time_column
        self.client.put(f"/api/v1/dataset/{ds_id}", body)

    def upsert_metrics(self, dataset_id: ExternalId, metrics: list[MetricDef]) -> dict[str, int]:
        """One PUT with the dataset's full metric list (Superset deletes metrics missing from the list)."""
        current = self.client.get(f"/api/v1/dataset/{int(dataset_id)}")["result"]
        existing = {m["metric_name"]: m for m in current.get("metrics") or []}
        ours = {m.name: m for m in metrics}
        payload = []
        for name, m in existing.items():
            if name not in ours:
                payload.append({k: v for k, v in m.items() if k in _METRIC_PUT_FIELDS and v is not None})
        for name, m in ours.items():
            item = superset_metric(m)
            if name in existing:
                item["id"] = existing[name]["id"]
            payload.append({k: v for k, v in item.items() if v is not None})
        self.client.put(f"/api/v1/dataset/{int(dataset_id)}", {"metrics": payload})
        after = self.client.get(f"/api/v1/dataset/{int(dataset_id)}")["result"]
        return {m["metric_name"]: int(m["id"]) for m in after.get("metrics") or [] if m["metric_name"] in ours}

    def create_metric(self, dataset_external_id: ExternalId, metric: MetricDef, ctx: PublishContext) -> ExternalId:
        return self.upsert_metrics(dataset_external_id, [metric])[metric.name]

    # -- charts ---------------------------------------------------------------------------------
    def chart_index(self, workspace_id: str) -> dict[str, int]:
        """{ChartSpec.key: chart id} for every chart this workspace published."""
        prefix = self.chart_prefix(workspace_id)
        ws = workspace_slug(workspace_id)
        out: dict[str, int] = {}
        for r in self.client.list("chart", [{"col": "slice_name", "opr": "sw", "value": prefix}]):
            name = r.get("slice_name") or ""
            if not name.startswith(prefix):
                continue
            try:
                params = json.loads(r.get("params") or "{}")
            except ValueError:
                params = {}
            if params.get("aos_workspace") not in (None, ws):
                continue
            key = params.get("aos_key") or name[len(prefix):].split(":", 1)[0]
            out.setdefault(key, int(r["id"]))
        return out

    def _chart_body(self, chart: ChartSpec, ctx: PublishContext) -> dict[str, Any]:
        ds_id = ctx.external_ids["datasets"].get(chart.dataset)
        if ds_id is None:
            raise InvalidInput(f"chart {chart.key!r}: dataset {chart.dataset!r} has not been published")
        aos = {
            "workspace": workspace_slug(ctx.workspace_id),
            "key": chart.key,
            "bundle_hash": ctx.bundle_hash,
            "idempotency_key": ctx.idempotency_key,
        }
        viz, params, qc = build_chart(chart, dataset_id=int(ds_id), dataset=ctx.dataset(chart.dataset),
                                      metrics=ctx.metrics, aos=aos)
        desc = chart.description or chart.rationale
        marker = f"[aos key={chart.key} bundle={ctx.bundle_hash[:12]}]"
        return {
            "slice_name": self.chart_name(ctx.workspace_id, chart),
            "viz_type": viz,
            "datasource_id": int(ds_id),
            "datasource_type": "table",
            "params": json.dumps(params),
            "query_context": json.dumps(qc),
            "query_context_generation": True,
            "description": f"{desc}\n\n{marker}".strip(),
        }

    def create_chart(self, chart: ChartSpec, ctx: PublishContext) -> ExternalId:
        return int(self.client.post("/api/v1/chart/", self._chart_body(chart, ctx))["id"])

    def update_chart(self, external_id: ExternalId, chart: ChartSpec, ctx: PublishContext) -> ExternalId:
        self.client.put(f"/api/v1/chart/{int(external_id)}", self._chart_body(chart, ctx))
        return int(external_id)

    def link_chart(self, chart_id: int, dashboard_ids: list[int]) -> None:
        """Adds dashboards to a chart, preserving links a human made to other dashboards."""
        current = self.client.get(f"/api/v1/chart/{chart_id}")["result"]
        have = {int(d["id"]) for d in current.get("dashboards") or []}
        want = have | set(dashboard_ids)
        if want != have:
            self.client.put(f"/api/v1/chart/{chart_id}", {"dashboards": sorted(want)})

    # -- dashboards -----------------------------------------------------------------------------
    def find_dashboard(self, slug: str) -> int | None:
        body = self.client.get(f"/api/v1/dashboard/{slug}", allow_404=True)
        return int(body["result"]["id"]) if body else None

    def _native_filters(self, dashboard: DashboardSpec, ctx: PublishContext, chart_ids: list[int]) -> list[dict]:
        charts = [ctx.chart(k) for k in dashboard.charts]
        filters = []
        for col in dashboard.native_filters:
            ds_name = next(
                (c.dataset for c in charts if any(x.get("name") == col for x in ctx.dataset(c.dataset).columns)),
                charts[0].dataset,
            )
            ds_id = int(ctx.external_ids["datasets"][ds_name])
            fid = f"NATIVE_FILTER-{re.sub(r'[^A-Za-z0-9_-]+', '-', dashboard.key)}-{re.sub(r'[^A-Za-z0-9_-]+', '-', col)}"
            filters.append(
                {
                    "id": fid,
                    "name": col.replace("_", " ").title(),
                    "filterType": "filter_select",
                    "type": "NATIVE_FILTER",
                    "targets": [{"datasetId": ds_id, "column": {"name": col}}],
                    "controlValues": {
                        "enableEmptyFilter": False,
                        "defaultToFirstItem": False,
                        "multiSelect": True,
                        "searchAllOptions": False,
                        "inverseSelection": False,
                    },
                    "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
                    "cascadeParentIds": [],
                    "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                    "chartsInScope": chart_ids,
                    "tabsInScope": [],
                    "description": "",
                }
            )
        return filters

    def _dashboard_body(self, dashboard: DashboardSpec, ctx: PublishContext) -> dict[str, Any]:
        chart_ids = {k: int(ctx.external_ids["charts"][k]) for k in dashboard.charts}
        charts = {k: ctx.chart(k) for k in dashboard.charts}
        position = build_position_json(
            dashboard,
            chart_ids,
            chart_types={k: c.chart_type for k, c in charts.items()},
            slice_names={k: self.chart_name(ctx.workspace_id, c) for k, c in charts.items()},
            chart_titles={k: c.title for k, c in charts.items()},
        )
        metadata = {
            "native_filter_configuration": self._native_filters(dashboard, ctx, list(chart_ids.values())),
            "refresh_frequency": 0,
            "timed_refresh_immune_slices": [],
            "expanded_slices": {},
            "color_scheme": "",
            "label_colors": {},
            "cross_filters_enabled": True,
            "chart_configuration": {},
        }
        return {
            "dashboard_title": dashboard.title,
            "slug": self.dashboard_slug(ctx.workspace_id, dashboard.key),
            "position_json": json.dumps(position),
            "json_metadata": json.dumps(metadata),
            # css carries the provenance marker: json_metadata is schema-validated and rejects unknown keys.
            "css": f"/* aos workspace={workspace_slug(ctx.workspace_id)} key={dashboard.key} "
            f"audience={dashboard.audience} bundle={ctx.bundle_hash} */",
        }

    def create_dashboard(self, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId:
        body = self._dashboard_body(dashboard, ctx)
        body["published"] = False
        return int(self.client.post("/api/v1/dashboard/", body)["id"])

    def update_dashboard(self, external_id: ExternalId, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId:
        self.client.put(f"/api/v1/dashboard/{int(external_id)}", self._dashboard_body(dashboard, ctx))
        return int(external_id)

    def publish_dashboard(self, external_id: ExternalId) -> str:
        self.client.put(f"/api/v1/dashboard/{int(external_id)}", {"published": True})
        return self.dashboard_url(external_id)

    def rename_dashboard(self, external_id: ExternalId, title: str) -> str:
        """Title only (an approved grouping update, BI-VIEWS-02): layout, charts, filters and the provenance stamp stay."""
        self.client.put(f"/api/v1/dashboard/{int(external_id)}", {"dashboard_title": title})
        return self.dashboard_url(external_id)

    # -- phase 3 --------------------------------------------------------------------------------
    def create_report(self, dashboard_external_id: ExternalId, *, recipients: list[str], name: str = "") -> ExternalId:
        raise NotImplementedError(
            "Superset scheduled reports are not enabled in the MVP (needs ALERT_REPORTS feature flag, a Celery "
            "worker+beat and a headless browser); share the dashboard URL instead"
        )

    def schedule_report(self, report_external_id: ExternalId, *, cron: str, timezone: str = "UTC") -> None:
        raise NotImplementedError("report scheduling is Phase 3")

    def create_alert(self, chart_external_id: ExternalId, *, condition: dict[str, Any], recipients: list[str]) -> ExternalId:
        raise NotImplementedError("Superset alerts are Phase 3 (ALERT_REPORTS + Celery beat not deployed)")

    def export_artifact(self, object_type: str, external_id: ExternalId) -> bytes:
        """Superset export bundle (ZIP of YAML) for a dashboard, chart, dataset or database."""
        resources = {"dashboard": "dashboard", "chart": "chart", "dataset": "dataset", "database": "database"}
        if object_type not in resources:
            raise InvalidInput(f"cannot export {object_type!r}; expected one of {sorted(resources)}")
        resp = self.client.request(
            "GET", f"/api/v1/{resources[object_type]}/export/", params={"q": rison.dumps([int(external_id)])}, raw=True
        )
        return resp.content

    # -- inspect (existing-dashboard mode, §35) -------------------------------------------------
    def list_dashboards(self, workspace_id: str) -> list[dict[str, Any]]:
        """Dashboards this workspace may import: never one AnalystOS published for another workspace."""
        from analystos.knowledge.superset_meta import Scope

        scope = Scope(workspace_id)
        return [{"id": d.get("id"), "title": d.get("dashboard_title"), "slug": d.get("slug"),
                 "published": bool(d.get("published")), "url": self.dashboard_url(d.get("id")),
                 "changed_on": d.get("changed_on_utc") or d.get("changed_on")}
                for d in self.client.list("dashboard", []) if scope.dashboard(d)]

    def chart_data(self, chart_id: ExternalId) -> dict[str, Any]:
        """The chart's numbers as Superset computes them from its saved query context (read-only)."""
        try:
            body = self.client.get(f"/api/v1/chart/{int(chart_id)}/data/", params={"format": "json", "type": "full"})
        except AnalystOSError as exc:
            return {"error": exc.message}
        results = (body or {}).get("result") or []
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return {"error": "Superset returned no result for the chart"}
        first = results[0]
        columns = list(first.get("colnames") or (list(first["data"][0]) if first.get("data") else []))
        rows = [[rec.get(c) for c in columns] for rec in first.get("data") or []]
        return {"columns": columns, "rows": rows}

    def set_dataset_metric(self, dataset_id: ExternalId, name: str, *, expression: str, certify: bool,
                           details: str = "") -> None:
        """Replace one saved metric's expression (and certification) and keep every other metric as is."""
        current = self.client.get(f"/api/v1/dataset/{int(dataset_id)}")["result"]
        payload = []
        found = False
        for m in current.get("metrics") or []:
            item = {k: v for k, v in m.items() if k in _METRIC_PUT_FIELDS and v is not None}
            if m.get("metric_name") == name:
                found = True
                item["expression"] = expression
                if certify:
                    item["extra"] = json.dumps({"certification": {"certified_by": "AnalystOS semantic layer",
                                                                  "details": details or "Approved metric of the workspace semantic model"}})
            payload.append(item)
        if not found:
            raise NotFound(f"Superset dataset {dataset_id} has no metric {name!r}")
        self.client.put(f"/api/v1/dataset/{int(dataset_id)}", {"metrics": payload})

    def apply_changes(self, dashboard: dict[str, Any], changes: list[dict[str, Any]]) -> list[str]:
        known = {str(d.get("id")) for d in dashboard.get("datasets") or []}
        done: list[str] = []
        for change in changes:
            if change.get("type") != "set_dataset_metric":
                raise InvalidInput(f"unsupported dashboard change {change.get('type')!r}")
            if str(change["dataset_id"]) not in known:
                raise InvalidInput(f"dataset {change['dataset_id']} is not part of dashboard {dashboard.get('id')}")
            self.set_dataset_metric(change["dataset_id"], change["metric"], expression=change["expression"],
                                    certify=bool(change.get("certify")), details=change.get("details", ""))
            done.append(f"dataset:{change['dataset_id']}:metric:{change['metric']}")
        return done

    def inspect_dashboard(self, dashboard_id: ExternalId, *, workspace_id: str | None = None) -> dict[str, Any]:
        """With ``workspace_id`` the inspection is tenant-scoped: another workspace's AnalystOS dashboard is
        a 404, and charts or datasets another workspace published are left out."""
        from analystos.knowledge.superset_meta import Scope
        from analystos.publishing.superset_import import chart_query, is_certified

        dash = self.client.get(f"/api/v1/dashboard/{dashboard_id}")["result"]
        scope = Scope(workspace_id) if workspace_id else None
        if scope is not None and not scope.dashboard(dash):
            raise NotFound(f"dashboard {dashboard_id} not found")
        charts = self.client.get(f"/api/v1/dashboard/{dashboard_id}/charts").get("result") or []
        datasets = self.client.get(f"/api/v1/dashboard/{dashboard_id}/datasets").get("result") or []
        if scope is not None:
            charts = [c for c in charts if scope.chart(c)]
            datasets = [d for d in datasets if scope.dataset(
                {**d, "database": {"database_name": (d.get("database") or {}).get("database_name")
                                   or (d.get("database") or {}).get("name")}})]
        try:
            meta = json.loads(dash.get("json_metadata") or "{}")
        except ValueError:
            meta = {}
        try:
            position = json.loads(dash.get("position_json") or "{}")
        except ValueError:
            position = {}
        ds_out = []
        all_metrics = []
        for d in datasets:
            if scope is not None and d.get("id") is not None:
                # The dashboard's dataset listing leaves out a metric's `extra` (its certification): read the dataset.
                try:
                    detail = (self.client.get(f"/api/v1/dataset/{int(d['id'])}", allow_404=True) or {}).get("result") or {}
                except AnalystOSError:
                    detail = {}
                extra = {m.get("metric_name"): m.get("extra") for m in detail.get("metrics") or []}
                d = {**d, "metrics": [{**m, "extra": m.get("extra") or extra.get(m.get("metric_name"))}
                                      for m in d.get("metrics") or []]}
            ms = [
                {"name": m.get("metric_name"), "expression": m.get("expression"), "verbose_name": m.get("verbose_name"),
                 "d3format": m.get("d3format"), "dataset_id": d.get("id"), "certified": is_certified(m)}
                for m in d.get("metrics") or []
            ]
            all_metrics.extend(ms)
            db = d.get("database") or {}
            ds_out.append(
                {
                    "id": d.get("id"),
                    "name": d.get("table_name") or d.get("datasource_name"),
                    "schema": d.get("schema"),
                    "sql": d.get("sql"),
                    "kind": "virtual" if d.get("sql") else "physical",
                    "database": db.get("database_name") or db.get("name"),
                    "main_dttm_col": d.get("main_dttm_col") or d.get("granularity_sqla"),
                    "columns": [
                        {"name": col.get("column_name"), "type": col.get("type"), "is_dttm": col.get("is_dttm")}
                        for col in d.get("columns") or []
                    ],
                    "metrics": ms,
                }
            )
        ds_by_id = {str(d["id"]): d for d in ds_out}
        chart_out = []
        used_metrics: set[str] = set()
        for c in charts:
            fd = c.get("form_data") or {}
            ms = [m for m in (fd.get("metrics") or []) + ([fd["metric"]] if fd.get("metric") else [])]
            names = [m if isinstance(m, str) else (m.get("label") or m.get("sqlExpression")) for m in ms]
            used_metrics.update(n for n in names if n)
            ds_id = str(fd.get("datasource") or "").split("__", 1)[0] or str(c.get("datasource_id") or "")
            chart_out.append(
                {
                    "id": c.get("id"),
                    "name": c.get("slice_name"),
                    "viz_type": fd.get("viz_type") or c.get("viz_type"),
                    "datasource": fd.get("datasource"),
                    "dataset_id": ds_id or None,
                    "metrics": names,
                    "groupby": fd.get("groupby") or [],
                    "x_axis": fd.get("x_axis"),
                    "time_grain": fd.get("time_grain_sqla"),
                    "adhoc_filters": fd.get("adhoc_filters") or [],
                    "aos_key": fd.get("aos_key"),
                    "query": chart_query(fd, ds_by_id.get(ds_id)).model_dump(),
                }
            )
        filters = [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "type": f.get("filterType"),
                "targets": [
                    {"dataset_id": t.get("datasetId"), "column": (t.get("column") or {}).get("name")}
                    for t in f.get("targets") or []
                ],
            }
            for f in meta.get("native_filter_configuration") or []
        ]
        return {
            "id": dash.get("id"),
            "title": dash.get("dashboard_title"),
            "slug": dash.get("slug"),
            "published": dash.get("published"),
            "url": self.dashboard_url(dash.get("id")),
            "charts": chart_out,
            "datasets": ds_out,
            "metrics": [m for m in all_metrics if m["name"] in used_metrics] or all_metrics,
            "filters": filters,
            "layout": parse_position_json(position),
        }

    # -- high level -----------------------------------------------------------------------------
    def _exists(self, resource: str, oid: Any) -> bool:
        if oid is None:
            return False
        try:
            return self.client.get(f"/api/v1/{resource}/{int(oid)}", allow_404=True) is not None
        except (ValueError, TypeError):
            return False

    def publish(self, bundle: PublishBundle, *, idempotency_key: str, previous: dict | None = None) -> PublishResult:
        errors = validate_bundle(bundle)
        if errors:
            return PublishResult(destination=self.destination, status="failed", errors=errors)
        ctx = PublishContext.for_bundle(bundle, idempotency_key=idempotency_key, previous=previous)
        ids = ctx.external_ids
        prev = ctx.previous
        urls: dict[str, str] = {}
        step = "login"
        try:
            self.client.login()
            step = "database"
            self._database_for(ctx)
            db_id = int(ids["database"])

            for ds in bundle.datasets:
                step = f"dataset {ds.name}"
                hint = (prev.get("datasets") or {}).get(ds.name)
                existing = int(hint) if hint is not None and self._exists("dataset", hint) else None
                if existing is None:
                    existing = self.find_dataset(db_id, self.dataset_table_name(bundle.workspace_id, ds.name))
                ids["datasets"][ds.name] = (
                    self.update_dataset(existing, ds, ctx) if existing is not None else self.create_dataset(ds, ctx)
                )
                wanted = [
                    ctx.metrics[mn]
                    for mn in dict.fromkeys(mn for c in bundle.charts if c.dataset == ds.name for mn in chart_metric_names(c))
                ]
                if ds is bundle.datasets[0]:  # metrics no chart uses still get registered somewhere discoverable
                    used = {m.name for m in wanted}
                    used_anywhere = {mn for c in bundle.charts for mn in chart_metric_names(c)}
                    wanted += [m for m in bundle.metrics if m.name not in used and m.name not in used_anywhere]
                if wanted:
                    step = f"metrics on dataset {ds.name}"
                    for name, mid in self.upsert_metrics(ids["datasets"][ds.name], wanted).items():
                        ids["metrics"][f"{ds.name}:{name}"] = mid

            step = "chart reconcile"
            index = self.chart_index(bundle.workspace_id)
            for chart in bundle.charts:
                step = f"chart {chart.key}"
                hint = (prev.get("charts") or {}).get(chart.key)
                existing = int(hint) if hint is not None and self._exists("chart", hint) else index.get(chart.key)
                ids["charts"][chart.key] = (
                    self.update_chart(existing, chart, ctx) if existing is not None else self.create_chart(chart, ctx)
                )

            for dash in bundle.dashboards:
                step = f"dashboard {dash.key}"
                hint = (prev.get("dashboards") or {}).get(dash.key)
                existing = int(hint) if hint is not None and self._exists("dashboard", hint) else None
                if existing is None:
                    existing = self.find_dashboard(self.dashboard_slug(bundle.workspace_id, dash.key))
                ids["dashboards"][dash.key] = (
                    self.update_dashboard(existing, dash, ctx) if existing is not None else self.create_dashboard(dash, ctx)
                )

            step = "link charts"
            for chart in bundle.charts:
                dash_ids = [int(ids["dashboards"][d.key]) for d in bundle.dashboards if chart.key in d.charts]
                self.link_chart(int(ids["charts"][chart.key]), dash_ids)

            for dash in bundle.dashboards:
                step = f"publish dashboard {dash.key}"
                urls[dash.key] = self.publish_dashboard(ids["dashboards"][dash.key])
        except (AnalystOSError, httpx.HTTPError, KeyError, ValueError) as exc:
            msg = exc.message if isinstance(exc, AnalystOSError) else f"{type(exc).__name__}: {exc}"
            log.warning("superset publish failed at %s: %s", step, msg)
            created = bool(ids["datasets"] or ids["charts"] or ids["dashboards"] or ids["database_created"])
            for key, did in ids["dashboards"].items():
                urls.setdefault(key, self.dashboard_url(did))
            return PublishResult(
                destination=self.destination,
                status="partial" if created else "failed",
                external_ids=ids,
                urls=urls,
                errors=[f"{step}: {msg}"],
            )
        return PublishResult(destination=self.destination, status="succeeded", external_ids=ids, urls=urls)

    def rollback(self, external_ids: dict[str, Any]) -> list[str]:
        """Deletes dashboards, charts, then datasets. The database connection is only removed when this
        publish created it and no dataset (any workspace/user) still uses it."""
        deleted: list[str] = []
        failures: list[str] = []
        for kind, resource in (("dashboards", "dashboard"), ("charts", "chart"), ("datasets", "dataset")):
            for _, oid in sorted((external_ids.get(kind) or {}).items()):
                try:
                    if self.client.delete(f"/api/v1/{resource}/{int(oid)}"):
                        deleted.append(f"{resource}:{oid}")
                except AnalystOSError as exc:
                    failures.append(f"{resource}:{oid}: {exc.message}")
        db = external_ids.get("database")
        if db is not None and external_ids.get("database_created") and not failures:
            try:
                remaining = self.client.list("dataset", [{"col": "database", "opr": "rel_o_m", "value": int(db)}])
                if not remaining and self.client.delete(f"/api/v1/database/{int(db)}"):
                    deleted.append(f"database:{db}")
            except AnalystOSError as exc:
                failures.append(f"database:{db}: {exc.message}")
        if failures:
            raise UpstreamUnavailable(
                "Superset rollback incomplete", details={"deleted": deleted, "failed": failures}
            )
        return deleted

    def close(self) -> None:
        self.client.close()

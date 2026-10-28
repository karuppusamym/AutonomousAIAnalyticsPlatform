"""Just-in-time secret resolution for credentials named by reference (S02-T2).

A source, MCP server or delivery row stores only a *reference*, never a value:

    env:NAME                       a process environment variable
    file:/abs/path                 a mounted secret file
    vault:<path>[#field]           HashiCorp Vault (KV v2 path, e.g. secret/data/analystos/acme/pg#password)
    aws-sm:<name>[#json-key]       AWS Secrets Manager (SigV4, no SDK)
    azure-kv:<vault>/<name>[#key]  Azure Key Vault (client credentials or managed identity)
    gcp-sm:<project>/<secret>[@version][#key]   Google Secret Manager (service account or metadata server)

Values are resolved when a connection is opened, never logged or persisted, and secret-manager values are
cached in memory for at most `secret_cache_seconds` (≤ 5 min, bounded LRU).

**Who may name what.** A workspace user registers these references, so a reference is a request to read
a process secret and hand it to a host the user may control. Workspace references therefore pass the
operator's policy (`check_secret_ref`): `env:` only under `secret_env_prefixes` (default
`ANALYSTOS_SOURCE_SECRET_`) or in the operator's `secret_env_names`; `file:` only below `secret_file_dirs`;
manager refs only under the scheme's configured prefix (which may contain `{tenant}`). Platform
configuration the operator wrote (SMTP password, the Vault token) uses `resolve_platform_secret`.
Source credentials additionally resolve only in a process whose role may hold them (`credential_roles`).
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from analystos.core.errors import Forbidden, InvalidInput, UpstreamUnavailable
from analystos.core.logging import get_logger

_log = get_logger(__name__)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCATION = re.compile(r"^[A-Za-z0-9._/@-]{1,400}$")
_KEY = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_AZURE_VAULT = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]$")
_PLATFORM_ENV_NAMESPACE = "ANALYSTOS_"
SCHEMES = ("env", "file", "vault", "aws-sm", "azure-kv", "gcp-sm")
MANAGER_SCHEMES = ("vault", "aws-sm", "azure-kv", "gcp-sm")
Purpose = Literal["source", "mcp", "webhook"]
USAGE = ("secret_ref must look like env:NAME, file:/path, vault:<path>[#field], aws-sm:<name>[#key], "
         "azure-kv:<vault>/<name>[#key] or gcp-sm:<project>/<secret>[@version][#key]")


@dataclass(frozen=True)
class SecretRef:
    scheme: str
    location: str
    key: str | None = None

    def __str__(self) -> str:  # the reference, never a value
        return f"{self.scheme}:{self.location}" + (f"#{self.key}" if self.key else "")


def _settings(settings: Any | None) -> Any:
    if settings is not None:
        return settings
    from analystos.core.config import get_settings

    return get_settings()


def _csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


# ---------------------------------------------------------------------------------------------- parsing
def parse_ref(ref: str) -> SecretRef:
    """Shape only (no policy, no lookup). Raises InvalidInput naming the reference, never a value."""
    text = str(ref).strip()
    scheme, sep, rest = text.partition(":")
    scheme = scheme.lower()
    if not sep or scheme not in SCHEMES:
        if sep and scheme:
            raise InvalidInput(f"secret_ref scheme '{scheme}' is not supported ({USAGE})")
        raise InvalidInput(USAGE)
    if scheme == "env":
        if not _ENV_NAME.match(rest):
            raise InvalidInput("secret_ref env name is not a valid environment variable name")
        return SecretRef("env", rest)
    if scheme == "file":
        if not rest or not Path(rest).expanduser().is_absolute():
            raise InvalidInput("secret_ref file path must be absolute")
        return SecretRef("file", rest)
    location, _, key = rest.partition("#")
    location = location.strip("/")
    if (not location or not _LOCATION.match(location) or ".." in location.split("/")
            or (key and not _KEY.match(key))):
        raise InvalidInput(f"secret_ref {scheme}: location is invalid ({USAGE})")
    parts = location.split("/")
    if scheme == "azure-kv" and (len(parts) != 2 or not _AZURE_VAULT.match(parts[0])):
        raise InvalidInput("secret_ref azure-kv must be azure-kv:<vault>/<secret-name>[#key]")
    if scheme == "gcp-sm" and len(parts) != 2:
        raise InvalidInput("secret_ref gcp-sm must be gcp-sm:<project>/<secret>[@version][#key]")
    return SecretRef(scheme, location, key or None)


# ---------------------------------------------------------------------------------------------- policy
def _current_tenant() -> str | None:
    try:
        from analystos.tenancy.context import current_tenant

        return current_tenant()
    except Exception:  # noqa: BLE001 - tenancy unavailable: a {tenant} prefix then fails closed
        return None


def _expand(prefix: str, tenant: str | None) -> str:
    if "{tenant}" not in prefix:
        return prefix
    if not tenant:
        raise Forbidden("this secret_ref prefix is tenant-scoped and no tenant is bound to the call")
    return prefix.replace("{tenant}", tenant)


def _safe_env_prefix(prefix: str) -> bool:
    """A prefix must name a dedicated namespace: never the platform's own (`ANALYSTOS_`) or a stem of it."""
    return len(prefix) >= 6 and prefix.endswith("_") and not _PLATFORM_ENV_NAMESPACE.startswith(prefix)


def env_prefixes(settings: Any | None = None) -> list[str]:
    configured = _csv(getattr(_settings(settings), "secret_env_prefixes", ""))
    safe = [p for p in configured if _safe_env_prefix(p)]
    for bad in set(configured) - set(safe):
        _log.warning("ignoring unsafe secret_env_prefixes entry %r (must end with '_' and not be ANALYSTOS_)", bad)
    return safe


def check_secret_ref(ref: str | None, *, settings: Any | None = None, tenant: str | None = None,
                     env_prefixes_override: tuple[str, ...] | None = None) -> SecretRef | None:
    """The operator's policy for a reference a workspace user supplied. Raises Forbidden when the reference
    is well formed but names a secret users may not reach. Called at registration and again at resolution."""
    if ref is None or not str(ref).strip():
        return None
    s = _settings(settings)
    parsed = parse_ref(ref)
    if parsed.scheme == "env":
        name = parsed.location
        prefixes = list(env_prefixes_override) if env_prefixes_override is not None else env_prefixes(s)
        names = [n for n in _csv(getattr(s, "secret_env_names", "")) if not n.startswith(_PLATFORM_ENV_NAMESPACE)]
        if not any(name.startswith(p) and len(name) > len(p) for p in prefixes) and name not in names:
            allowed = " or ".join(f"{p}<NAME>" for p in prefixes) or "(no prefix configured)"
            raise Forbidden(f"secret_ref env:{name} is not allowed: environment references must be {allowed}, "
                            "or a name the platform administrator registered (ANALYSTOS_SECRET_ENV_NAMES)")
        return parsed
    if parsed.scheme == "file":
        target = Path(parsed.location).expanduser().resolve(strict=False)
        dirs = [Path(d).expanduser().resolve(strict=False) for d in _csv(getattr(s, "secret_file_dirs", ""))]
        if not any(target != d and target.is_relative_to(d) for d in dirs):
            raise Forbidden(f"secret_ref file:{parsed.location} is not allowed: secret files must sit below one of "
                            "the directories the platform administrator configured (ANALYSTOS_SECRET_FILE_DIRS)")
        return parsed
    tenant = tenant if tenant is not None else _current_tenant()
    location = parsed.location
    if parsed.scheme == "vault":
        prefix = _expand(str(getattr(s, "vault_prefix", "") or ""), tenant).strip("/")
        ok = bool(prefix) and (location + "/").startswith(prefix.rstrip("/") + "/") and location != prefix.rstrip("/")
    elif parsed.scheme == "aws-sm":
        prefix = _expand(str(getattr(s, "aws_sm_prefix", "") or ""), tenant)
        ok = bool(prefix) and location.startswith(prefix) and len(location) > len(prefix)
    elif parsed.scheme == "azure-kv":
        vault, name = location.split("/")
        prefix = _expand(str(getattr(s, "azure_kv_prefix", "") or ""), tenant)
        ok = (vault.lower() in {v.lower() for v in _csv(getattr(s, "azure_kv_vaults", ""))}
              and bool(prefix) and name.startswith(prefix) and len(name) > len(prefix))
    else:  # gcp-sm
        project, secret = location.split("/")
        prefix = _expand(str(getattr(s, "gcp_sm_prefix", "") or ""), tenant)
        name = secret.split("@", 1)[0]
        ok = (project in _csv(getattr(s, "gcp_sm_projects", ""))
              and bool(prefix) and name.startswith(prefix) and len(name) > len(prefix))
    if not ok:
        raise Forbidden(f"secret_ref {parsed.scheme}:{location} is outside the location the platform administrator "
                        f"allows for {parsed.scheme} references")
    return parsed


def credentials_allowed_here(settings: Any | None = None) -> bool:
    s = _settings(settings)
    role = str(getattr(s, "process_role", "all") or "all").strip().lower()
    return role in {r.lower() for r in _csv(getattr(s, "credential_roles", "all,query"))}


# ---------------------------------------------------------------------------------------------- resolution
def resolve_secret(ref: str | None, *, purpose: Purpose = "source", settings: Any | None = None,
                   env_prefixes_override: tuple[str, ...] | None = None) -> str | None:
    """Resolve a reference a workspace user registered. ``None``/empty -> ``None``.

    The policy is re-checked here (a reference stored before the policy tightened is refused), and a
    source credential resolves only in a process role allowed to hold one (worker:query, ingest pools, lite).
    """
    if ref is None or not str(ref).strip():
        return None
    s = _settings(settings)
    if purpose == "source" and not credentials_allowed_here(s):
        raise Forbidden(f"source credentials do not resolve in the '{getattr(s, 'process_role', '?')}' role; "
                        "queries run in worker:query (ANALYSTOS_QUERY_TRANSPORT=temporal)")
    parsed = check_secret_ref(ref, settings=s, env_prefixes_override=env_prefixes_override)
    assert parsed is not None
    return _resolve(parsed, s)


def resolve_platform_secret(ref: str | None, *, settings: Any | None = None) -> str | None:
    """Resolve a reference the operator configured (SMTP password, Vault token): shape-checked, no
    workspace policy. Never call this with a reference a workspace user can set."""
    if ref is None or not str(ref).strip():
        return None
    return _resolve(parse_ref(ref), _settings(settings))


def _resolve(parsed: SecretRef, s: Any) -> str:
    if parsed.scheme == "env":
        value = os.environ.get(parsed.location)
        if value is None:
            raise InvalidInput(f"secret_ref env:{parsed.location} is not set")
        return value
    if parsed.scheme == "file":
        path = Path(parsed.location).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise InvalidInput(f"secret_ref file {path} is not readable: {exc.__class__.__name__}") from None
    return _CACHE.get_or_fetch(parsed, s)


# ---------------------------------------------------------------------------------------------- cache
class _SecretCache:
    """Bounded in-memory cache for secret-manager values (TTL ≤ secret_cache_seconds, ≤ 300 s)."""

    def __init__(self, max_entries: int = 256) -> None:
        self.data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.lock = threading.Lock()
        self.max_entries = max_entries

    def put(self, key: str, value: str, ttl: float) -> None:
        with self.lock:
            self.data[key] = (time.monotonic() + ttl, value)
            self.data.move_to_end(key)
            while len(self.data) > self.max_entries:
                self.data.popitem(last=False)

    def get_or_fetch(self, parsed: SecretRef, s: Any) -> str:
        ttl = min(int(getattr(s, "secret_cache_seconds", 300) or 0), 300)
        key = str(parsed)
        if ttl > 0:
            with self.lock:
                hit = self.data.get(key)
                if hit is not None and hit[0] > time.monotonic():
                    self.data.move_to_end(key)
                    return hit[1]
        value = PROVIDERS[parsed.scheme](parsed, s)
        if not value:
            raise UpstreamUnavailable(f"secret manager returned an empty value for {parsed.scheme}:{parsed.location}")
        if ttl > 0:
            self.put(key, value, ttl)
        return value

    def clear(self) -> None:
        with self.lock:
            self.data.clear()


_CACHE = _SecretCache()
_TOKENS = _SecretCache(max_entries=16)  # provider access tokens (Azure AD, Google OAuth)


def clear_cache() -> None:
    _CACHE.clear()
    _TOKENS.clear()


# ---------------------------------------------------------------------------------------------- providers
_transport: Any | None = None  # tests inject an httpx transport


def set_http_transport(transport: Any | None) -> None:
    """Tests only: route secret-manager HTTP through this httpx transport."""
    global _transport
    _transport = transport
    clear_cache()


def _client(s: Any):  # noqa: ANN202
    import httpx

    kwargs: dict[str, Any] = {"timeout": float(getattr(s, "secret_timeout_seconds", 10.0)), "follow_redirects": False}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.Client(**kwargs)


def _request(s: Any, method: str, url: str, what: str, **kwargs: Any) -> Any:
    import httpx

    try:
        with _client(s) as client:
            resp = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{what}: request failed ({exc.__class__.__name__})") from None
    if resp.status_code in (401, 403):
        raise Forbidden(f"{what}: access denied ({resp.status_code})")
    if resp.status_code == 404:
        raise InvalidInput(f"{what}: not found")
    if resp.status_code >= 400:
        raise UpstreamUnavailable(f"{what}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise UpstreamUnavailable(f"{what}: response is not JSON") from None


def _pick(value: str, key: str | None, what: str) -> str:
    """`#key` selects one field of a JSON secret; without a key the whole value is the secret."""
    if not key:
        return value
    try:
        doc = json.loads(value)
    except ValueError:
        raise InvalidInput(f"{what}: #{key} given but the secret is not a JSON object") from None
    picked = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(picked, (str, int, float)) or picked == "":
        raise InvalidInput(f"{what}: the secret has no usable '{key}' field")
    return str(picked)


def _vault(ref: SecretRef, s: Any) -> str:
    addr = str(getattr(s, "vault_addr", "") or "").rstrip("/")
    token = resolve_platform_secret(getattr(s, "vault_token_ref", "") or None, settings=s)
    if not addr or not token:
        raise InvalidInput("vault: secret references are not configured (ANALYSTOS_VAULT_ADDR, ANALYSTOS_VAULT_TOKEN_REF)")
    headers = {"X-Vault-Token": token}
    if getattr(s, "vault_namespace", ""):
        headers["X-Vault-Namespace"] = str(s.vault_namespace)
    what = f"vault:{ref.location}"
    doc = _request(s, "GET", f"{addr}/v1/{quote(ref.location, safe='/')}", what, headers=headers)
    data = doc.get("data") if isinstance(doc, dict) else None
    fields = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data  # KV v2 | v1
    value = fields.get(ref.key or "value") if isinstance(fields, dict) else None
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{what}: the secret has no usable '{ref.key or 'value'}' field")
    return value


def _aws_sm(ref: SecretRef, s: Any) -> str:
    from analystos.connectors import aws_sigv4

    region = str(getattr(s, "aws_region", "") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "")
    access, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not region or not access or not secret:
        raise InvalidInput("aws-sm: secret references need ANALYSTOS_AWS_REGION and AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")
    host = f"secretsmanager.{region}.amazonaws.com"
    body = json.dumps({"SecretId": ref.location}).encode()
    headers = aws_sigv4.signed_headers_for(
        method="POST", host=host, path="/", payload=body, moment=datetime.now(UTC), region=region,
        service="secretsmanager", access_key=access, secret_key=secret,
        session_token=os.environ.get("AWS_SESSION_TOKEN", ""),
        headers={"content-type": "application/x-amz-json-1.1", "x-amz-target": "secretsmanager.GetSecretValue"})
    what = f"aws-sm:{ref.location}"
    doc = _request(s, "POST", f"https://{host}/", what, headers=headers, content=body)
    value = doc.get("SecretString") if isinstance(doc, dict) else None
    if not isinstance(value, str) and isinstance(doc, dict) and doc.get("SecretBinary"):
        value = base64.b64decode(doc["SecretBinary"]).decode("utf-8")
    if not isinstance(value, str):
        raise InvalidInput(f"{what}: no SecretString in the response")
    return _pick(value, ref.key, what)


def _azure_token(s: Any) -> str:
    def fetch(_: SecretRef, s: Any) -> str:
        tenant, client, secret = (os.environ.get(k) for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"))
        if tenant and client and secret:
            doc = _request(s, "POST", f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token",
                           "azure-kv: token", data={"grant_type": "client_credentials", "client_id": client,
                                                    "client_secret": secret, "scope": "https://vault.azure.net/.default"})
        else:  # managed identity (IMDS)
            doc = _request(s, "GET", "http://169.254.169.254/metadata/identity/oauth2/token", "azure-kv: managed identity",
                           params={"api-version": "2018-02-01", "resource": "https://vault.azure.net"},
                           headers={"Metadata": "true"})
        token = doc.get("access_token") if isinstance(doc, dict) else None
        if not token:
            raise UpstreamUnavailable("azure-kv: no access token")
        return str(token)

    return _cached_token("azure", fetch, s)


def _azure_kv(ref: SecretRef, s: Any) -> str:
    vault, name = ref.location.split("/")
    what = f"azure-kv:{ref.location}"
    doc = _request(s, "GET", f"https://{vault}.vault.azure.net/secrets/{quote(name)}", what,
                   params={"api-version": "7.4"}, headers={"Authorization": f"Bearer {_azure_token(s)}"})
    value = doc.get("value") if isinstance(doc, dict) else None
    if not isinstance(value, str):
        raise InvalidInput(f"{what}: no value in the response")
    return _pick(value, ref.key, what)


def _gcp_token(s: Any) -> str:
    def fetch(_: SecretRef, s: Any) -> str:
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds:
            info = json.loads(Path(creds).read_text(encoding="utf-8"))
            assertion = _google_jwt(info)
            doc = _request(s, "POST", info.get("token_uri") or "https://oauth2.googleapis.com/token", "gcp-sm: token",
                           data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        else:  # GCE / GKE metadata server
            doc = _request(s, "GET", "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                           "gcp-sm: metadata token", headers={"Metadata-Flavor": "Google"})
        token = doc.get("access_token") if isinstance(doc, dict) else None
        if not token:
            raise UpstreamUnavailable("gcp-sm: no access token")
        return str(token)

    return _cached_token("gcp", fetch, s)


def _google_jwt(info: dict[str, Any]) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": info["client_email"], "scope": "https://www.googleapis.com/auth/cloud-platform",
              "aud": info.get("token_uri") or "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}".encode()
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    return f"{signing_input.decode()}.{b64(signature)}"


def _gcp_sm(ref: SecretRef, s: Any) -> str:
    project, secret = ref.location.split("/")
    name, _, version = secret.partition("@")
    what = f"gcp-sm:{ref.location}"
    url = (f"https://secretmanager.googleapis.com/v1/projects/{quote(project)}/secrets/{quote(name)}"
           f"/versions/{quote(version or 'latest')}:access")
    doc = _request(s, "GET", url, what, headers={"Authorization": f"Bearer {_gcp_token(s)}"})
    data = (doc.get("payload") or {}).get("data") if isinstance(doc, dict) else None
    if not isinstance(data, str):
        raise InvalidInput(f"{what}: no payload in the response")
    return _pick(base64.b64decode(data).decode("utf-8"), ref.key, what)


def _cached_token(provider: str, fetch: Callable[[SecretRef, Any], str], s: Any) -> str:
    """Provider access tokens are kept 4 minutes, well inside any provider token lifetime."""
    key = SecretRef(provider, "token")
    with _TOKENS.lock:
        hit = _TOKENS.data.get(str(key))
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
    token = fetch(key, s)
    _TOKENS.put(str(key), token, 240)
    return token


PROVIDERS: dict[str, Callable[[SecretRef, Any], str]] = {
    "vault": _vault, "aws-sm": _aws_sm, "azure-kv": _azure_kv, "gcp-sm": _gcp_sm,
}

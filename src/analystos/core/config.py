"""Runtime configuration. Every value comes from the environment (prefix ANALYSTOS_) or .env.

Secrets are never given defaults and are never logged. OPENROUTER_API_KEY is read unprefixed
because it is shared with the sibling projects and the OpenRouter tooling convention.
"""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_DIR.parents[1]
# A source checkout (editable install, tests, the dev image) reads config/, migrations/, packs/ from the repo; an
# installed wheel reads the copies bundled under analystos/_data (pyproject `sources`) and writes var/ and
# exported contracts below the working directory, never inside site-packages.
SOURCE_CHECKOUT = (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "config" / "models.yaml").is_file()
DATA_ROOT = REPO_ROOT if SOURCE_CHECKOUT else PACKAGE_DIR / "_data"
STATE_ROOT = REPO_ROOT if SOURCE_CHECKOUT else Path.cwd()


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_COMPOSE_SERVICES = {"redis_url": "redis", "superset_url": "superset", "servicenow_mock_url": "servicenow-mock",
                     "temporal_address": "temporal"}


def _to_service(url: str, service: str) -> str:
    """`redis://localhost:6379/0` -> `redis://redis:6379/0` (also `localhost:7233`); any other host is kept."""
    from urllib.parse import urlsplit, urlunsplit

    if "://" not in url:
        host, _, port = url.rpartition(":")
        return f"{service}:{port}" if host in _LOOPBACK else url
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:  # not a URL we can read: leave it for the component that uses it to report
        return url
    if parts.hostname not in _LOOPBACK:
        return url
    netloc = parts.netloc.rsplit("@", 1)
    hostport = service + (f":{port}" if port else "")
    return urlunsplit(parts._replace(netloc=(netloc[0] + "@" + hostport) if len(netloc) == 2 else hostport))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYSTOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    # Deployment profile (ADR-0025): `lite` = Postgres + API + web (local orchestrator with resume, the
    # in-process scheduler, Postgres spend reservations, preview publishing); `standard` = lite + Redis,
    # Temporal, one worker, a scheduler process; `scale` = standard with per-queue pools and HA values.
    # The profile only fills defaults of settings the environment leaves unset (see `_profile_defaults`).
    # The code default stays `standard` so an existing deployment that sets nothing keeps its behaviour;
    # the install defaults (compose, .env.example, values-small) choose `lite`. Without the `temporal` extra
    # (a core install) an unset profile is `lite` (`_profile_defaults`).
    profile: Literal["lite", "standard", "scale"] = "standard"
    # Control plane (application state). Never reachable from user/model SQL.
    database_url: str = "postgresql+psycopg://analystos:analystos@localhost:5432/analystos"
    # Analytics plane: staging database for API/file sources. Two identities:
    # the loader writes staged snapshots, the reader is the only identity the gateway uses.
    analytics_loader_url: str = "postgresql+psycopg://analystos_loader:loader@localhost:5432/analytics"
    analytics_reader_url: str = "postgresql+psycopg://analystos_reader:reader@localhost:5432/analytics"
    # Per-workspace NOLOGIN roles (<prefix><workspace id>) hold SELECT on that workspace's staged
    # schemas; the reader identity only reaches them via SET ROLE, so it cannot read across workspaces.
    analytics_workspace_role_prefix: str = "analystos_r_"
    # BI logins (P4-02): one LOGIN role per workspace (<prefix><workspace id>) that Superset connects as;
    # a member of that workspace's reader role only, so BI SQL cannot SET ROLE into another workspace.
    # Its password is derived from analytics_bi_secret (falls back to jwt_secret), never stored.
    analytics_bi_role_prefix: str = "analystos_bi_"
    analytics_bi_secret: str | None = None
    # Write path (P4-E06, spec v3 §7.4): a third analytics identity used only by the BuildGateway. It
    # reaches per-workspace NOLOGIN build roles (<prefix><workspace id>) by SET ROLE; each holds CREATE
    # on that workspace's designated target schemas only and reads its staged schemas through the
    # workspace reader role. Never used for queries; the query identities never write.
    analytics_builder_url: str = "postgresql+psycopg://analystos_builder:builder@localhost:5432/analytics"
    analytics_build_role_prefix: str = "analystos_b_"
    # Managed output writer (P6-03, ADR-0011): a fourth analytics identity used only to materialize approved
    # pipeline outputs. It reaches per-workspace NOLOGIN writer roles (<prefix><workspace id>) by SET ROLE;
    # each holds CREATE on that workspace's allowlisted destination schemas and reads no source.
    analytics_writer_url: str = "postgresql+psycopg://analystos_writer:writer@localhost:5432/analytics"
    analytics_writer_role_prefix: str = "analystos_w_"
    writer_keep_versions: int = Field(default=3, ge=2, le=50)  # version tables kept per destination (rollback depth)
    # The customer's dbt runner (P4-E04): a dbt Core executable run as a separate process (its own
    # venv or container image), never imported in-process. Projects and job logs live under build_dir.
    dbt_executable: str = "dbt"
    build_dir: Path = STATE_ROOT / "var" / "builds"
    build_timeout_seconds: int = 1800
    # Connection pools per plane (P4-S05; db/pools.py). Per process: control <= size + overflow;
    # analytics and loader the same per URL. `none` = no client-side pool (behind PgBouncer).
    # analytics_/loader_pool_mode inherit db_pool_mode when unset.
    db_pool_mode: Literal["queue", "none"] = "queue"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = -1  # seconds; -1 = never (set below the pooler's/firewall's idle cut-off)
    analytics_pool_mode: Literal["queue", "none"] | None = None
    analytics_pool_size: int = Field(default=5, ge=1)
    analytics_max_overflow: int = Field(default=5, ge=0)
    analytics_pool_timeout: float = Field(default=30.0, gt=0)
    loader_pool_mode: Literal["queue", "none"] | None = None
    loader_pool_size: int = Field(default=2, ge=1)
    loader_max_overflow: int = Field(default=3, ge=0)
    loader_pool_timeout: float = Field(default=30.0, gt=0)
    # The control/analytics/loader URLs go through a transaction-mode pooler (PgBouncer): no
    # server-side prepared statements. The builder URL must stay direct or session-pooled.
    db_transaction_pooler: bool = False
    # Tenant isolation (ADR-009, analystos.tenancy): `strict` (default) restricts every transaction
    # outside system_scope to its bound tenant through Postgres RLS, and an unbound one sees nothing
    # (fail closed); `bound` restricts only transactions with a bound tenant (upgrade aid); `off` skips it.
    tenancy_mode: Literal["off", "bound", "strict"] = "strict"
    tenancy_app_role: str = Field(default="analystos_app", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    # ABAC engine (S01-T6, governance/abac.py): `builtin` (default) decides with the platform and workspace
    # rules; `opa` also asks an OPA sidecar, which may deny what the built-in engine allows, never the reverse.
    # An OPA error, timeout or malformed answer is a deny (fail closed).
    policy_engine: Literal["builtin", "opa"] = "builtin"
    policy_opa_url: str = "http://127.0.0.1:8181"
    policy_opa_decision_path: str = Field(default="analystos/abac/decision", pattern=r"^[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*$")
    policy_opa_timeout_seconds: float = Field(default=1.0, gt=0, le=10)
    redis_url: str = "redis://localhost:6379/0"
    # Neo4j is an optional projection of the Postgres lineage/relationship tables (spec v3 §8), off
    # by default: lineage and table neighbourhood are served from Postgres unless this is on.
    graph_enabled: bool = False
    budget_counter_prefix: str = "aos:budget:"  # run/workspace/purpose budget counters (P4-T07)
    # Query steps of goal runs that may run at once against one source, across runs (S06 §3.6);
    # a source's `config.max_concurrent_steps` overrides it.
    source_max_concurrent_steps: int = 4
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "analystos-neo4j"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Task queues are `<prefix>-<workload>` (analysis, compute, publish, crawl, elt); see
    # config/task_queues.yaml. `analystos worker` serves `worker_queues` unless --queues is given.
    temporal_queue_prefix: str = "analystos"
    worker_queues: str = "all"
    # Isolated compute pools (ADR-0022, P7-06), opt-in: comma-separated pools this installation runs
    # (compute-py, compute-ml). Empty = none: recipe snapshot jobs run on the compute queue / in-process as
    # before, and capabilities that require a pool (`requires: [pool:compute-ml]`) are unavailable with the reason.
    isolated_pools: str = ""
    # How the control plane reaches them: temporal (the `<prefix>-<pool>` queues) | subprocess (local worker
    # processes started by the API, lite) | auto = temporal when the orchestrator is temporal, else subprocess.
    isolated_transport: Literal["auto", "temporal", "subprocess"] = "auto"
    # HMAC key of scoped worker task tokens; None = derived from jwt_secret (domain-separated). Only the
    # control plane and the artifact store (the API) hold it; never give it to a worker.
    worker_token_secret: str | None = None
    # S10-T5: the platform secret tenant manifest-signing keys (ed25519) are derived from; None = derived from
    # jwt_secret (domain-separated). Only public keys are stored; rotating this secret invalidates every signature.
    tool_signing_secret: str | None = None
    # The artifact store as isolated workers reach it (the API's base URL on their network).
    worker_artifact_url: str = "http://localhost:8000"
    # temporal | local. local runs the same engine in-process (lite profile, tests, laptops); lite's default.
    orchestrator: str = "temporal"
    # Local orchestrator (ADR-0025): None = the profile default (lite: 4, otherwise tasks run inline in
    # each run's driver thread, as before). A shared pool of this many threads executes ready tasks.
    local_workers: int | None = Field(default=None, ge=1, le=64)
    # Re-drive every non-terminal run when the API starts (the engine's claims make it safe). Assumes one
    # API process drives local runs: orphaned RUNNING claims are released at startup. None = lite only.
    local_resume: bool | None = None
    # Seconds between sweeps that re-drive parked (WAITING_USER, PAUSED) local runs whose state changed
    # without a signal (an approval decided by another process, an expiry). 0 = no sweep.
    local_sweep_seconds: float = Field(default=60.0, ge=0)
    # Run the schedule/monitor loop inside the API process (lite). Claim-then-execute makes it safe next
    # to a separate `analystos scheduler`. None = lite only.
    inprocess_scheduler: bool | None = None
    # Hard spend cap reservations (P4-06): redis (Lua script) | postgres (row-locked `spend_counter`
    # table) | auto = redis when ANALYSTOS_REDIS_URL is set, else postgres. Both fail closed.
    spend_counter_store: Literal["auto", "redis", "postgres"] = "auto"

    superset_url: str = "http://localhost:8088"
    # Browser-facing base URL. In Docker, workers use ``superset_url`` over the
    # Compose network while people open this URL from their host browser.
    superset_public_url: str | None = None
    superset_username: str = "admin"
    superset_password: str = "admin"
    # How Superset reaches the analytics DB (from inside the compose network).
    superset_analytics_sqlalchemy_uri: str = "postgresql+psycopg2://analystos_reader:reader@postgres:5432/analytics"

    # Knowledge index embeddings (P4-K10): auto | hashing | sentence_transformers. `auto` uses the
    # local sentence-transformer when the `embeddings` extra and the model are installed, hashing
    # otherwise. The model is never downloaded at runtime unless allowed (air-gapped by default).
    # Context2AI is reached through context providers (knowledge/providers.py), not a URL here.
    knowledge_embedding_provider: str = "auto"
    knowledge_embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384-d, ~130 MB; best of four on the K10 benchmark
    knowledge_embedding_dim: int | None = None  # None = the provider's native dimension (hashing: 256)
    knowledge_embedding_allow_download: bool = False
    # Where embeddings live (db/vectors.py): `pgvector` (the `vector` extension, HNSW search in the
    # database) or `array` (plain `real[]` columns, cosine ranked in-process) for a Postgres where
    # extensions cannot be installed. Chosen at install; `analystos knowledge reembed` converts.
    vector_backend: Literal["pgvector", "array"] = "pgvector"
    jwt_secret: str = Field(default="dev-only-change-me-please-32bytes!!")
    jwt_ttl_minutes: int = 12 * 60
    bootstrap_admin_email: str = "admin@analystos.local"
    bootstrap_admin_password: str = "ChangeMe123!"

    models_config: Path = DATA_ROOT / "config" / "models.yaml"
    # Air-gapped install (P4-S04, spec v3 §8): only providers declared `egress: internal` in the models
    # config are routed to, the model transport refuses every other host, and the DecisionService runs
    # on `rules` / `local_classifier` only. Pair with ANALYSTOS_MODELS_CONFIG=config/models.airgapped.yaml.
    air_gapped: bool = False

    # OIDC single sign-on (SEC-001..003), alongside password login. Authorization-code flow with PKCE;
    # the client secret comes from the environment only. Group -> role mapping: `oidc_mapping_file`.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None  # confidential clients; PKCE alone for public clients
    oidc_redirect_uri: str = "http://localhost:8000/api/auth/oidc/callback"
    oidc_scopes: str = "openid email profile groups"
    oidc_groups_claim: str = "groups"
    oidc_mapping_file: Path = DATA_ROOT / "config" / "oidc.yaml"
    oidc_jwks_file: Path | None = None  # static JWKS (air-gapped IdP mirrors, tests); else the discovery jwks_uri
    oidc_discovery_url: str | None = None  # defaults to <issuer>/.well-known/openid-configuration
    oidc_provider_name: str = "Single sign-on"
    password_login: bool = True  # local accounts stay available (break-glass admin) unless turned off
    web_url: str = "http://localhost:5173"  # where the OIDC callback returns the browser
    agents_dir: Path = DATA_ROOT / "config" / "agents"
    artifact_dir: Path = STATE_ROOT / "var" / "artifacts"
    # Nightly audit-chain exports (S01-T5): gzipped JSONL, never overwritten. Point it at WORM storage
    # (an object-lock bucket mount) in production; empty disables the export.
    audit_export_dir: Path | None = STATE_ROOT / "var" / "audit"

    @field_validator("audit_export_dir", mode="before")
    @classmethod
    def _no_export_dir(cls, value: object) -> object:
        return None if isinstance(value, str) and value.strip().lower() in ("", "none", "off") else value
    upload_dir: Path = STATE_ROOT / "var" / "uploads"  # CSV/Parquet sources may only read below this directory

    # Gateway defaults (workspace policy may tighten, never loosen beyond these ceilings).
    query_timeout_seconds: int = 30
    query_max_rows: int = 50_000
    query_cache_ttl_seconds: int = 3600
    # Pre-execution cost guard (S02-T4): refuse a statement whose estimated scan exceeds this many bytes.
    # 0 = no platform ceiling (a request's own budget still applies).
    query_max_scan_bytes: int = Field(default=0, ge=0)
    # Result handles (S02-T3, ADR-010): results above either threshold are written as Parquet to the result
    # store (`tenant/{t}/results/{query_execution_id}.parquet`) and returned as a ResultHandle.
    result_inline_max_rows: int = Field(default=256, ge=1)
    result_inline_max_bytes: int = Field(default=65_536, ge=1024)
    result_store_dir: Path = STATE_ROOT / "var" / "results"
    result_ttl_hours: int = Field(default=24, ge=1, le=24 * 90)
    # Query worker (S02-T1): where gateway execution runs. inprocess = this process (lite, tests);
    # temporal = the `query` task queue served by `analystos worker --queues query` (worker:query role).
    query_transport: Literal["inprocess", "temporal"] = "inprocess"
    query_transport_timeout_seconds: int = Field(default=300, ge=5)
    # This process's role. Source credentials resolve only in roles listed in `credential_roles`
    # (comma-separated): `all` (lite / one process), `query` (worker:query), and the ingest pools that extract
    # staged sources (`crawl`, `elt`). Set ANALYSTOS_PROCESS_ROLE=api on an API that sends queries to worker:query.
    process_role: str = "all"
    credential_roles: str = "all,query,crawl,elt"

    # Secret references (`secret_ref`) that workspace users register on sources and MCP servers. Only the
    # operator decides which process secrets they may name: an `env:` ref must start with one of
    # `secret_env_prefixes` or be listed in `secret_env_names` (both comma-separated); a `file:` ref must sit
    # under one of `secret_file_dirs`. Secret-manager refs (vault:, aws-sm:, azure-kv:, gcp-sm:) must start with
    # the scheme's prefix below; `{tenant}` in a prefix is replaced with the caller's tenant id.
    secret_env_prefixes: str = "ANALYSTOS_SOURCE_SECRET_"
    secret_env_names: str = ""
    secret_file_dirs: str = "/run/secrets"
    secret_cache_seconds: int = Field(default=300, ge=0, le=300)
    secret_timeout_seconds: float = Field(default=10.0, gt=0)
    vault_addr: str = ""
    vault_token_ref: str = ""  # operator-configured (env:NAME / file:/path) Vault token
    vault_namespace: str = ""
    vault_prefix: str = "secret/data/analystos/"
    aws_region: str = ""
    aws_sm_prefix: str = "analystos/"
    azure_kv_vaults: str = ""  # comma-separated vault names users may reference
    azure_kv_prefix: str = "analystos-"
    gcp_sm_projects: str = ""  # comma-separated project ids users may reference
    gcp_sm_prefix: str = "analystos-"
    # Governed REST connector (S02-T8): POST endpoints a source may call, as `host/path` entries
    # (comma-separated). Only an endpoint marked `safe: true` AND listed here is ever POSTed.
    rest_safe_post_allowlist: str = ""

    # Governed ML (P5-01, ADR-0024): hard ceilings every MLSpec search budget is clamped to.
    ml_max_trials: int = Field(default=50, ge=1, le=1000)
    ml_max_seconds: int = Field(default=900, ge=1)
    ml_max_rows: int = Field(default=200_000, ge=100)
    ml_max_features: int = Field(default=200, ge=1)

    sandbox_timeout_seconds: int = 60
    sandbox_memory_mb: int = 1024
    # Isolation of sandbox children (P4-02, sandbox/isolation.py): container = docker run per execution
    # (no network, read-only root, cgroup limits); process = namespaces + read-only root + rlimits in the
    # worker; auto = container when its image is present, else process; none available = refused.
    # off = rlimits only, development only (refused when env is production).
    sandbox_isolation: Literal["auto", "container", "process", "off"] = "auto"
    sandbox_container_image: str = "analystos-sandbox:latest"
    sandbox_container_python: str = "python3"
    sandbox_container_runtime: str | None = None  # e.g. runsc (gVisor) where installed
    sandbox_container_startup_seconds: float = 5.0  # added to the wall clock for `docker run` start-up
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = Field(default=64, ge=4)
    sandbox_scratch_mb: int = Field(default=64, ge=1)
    # Hidden from process-mode children (secrets on the worker's filesystem); /run/secrets and
    # /var/run/secrets (service-account tokens) are always masked.
    sandbox_masked_paths: list[str] = Field(default_factory=lambda: [
        "/root", "/etc/analystos", "/etc/ssl/private", str(STATE_ROOT / ".env"), str(STATE_ROOT / "var")])

    servicenow_mock_url: str = "http://localhost:8090"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # Outbound HTTP (P7-11, tools/http.py). HTTP tool capabilities may call only these hostnames
    # (comma-separated; empty = none). Every outbound call (HTTP tools and MCP servers) is refused when
    # the host resolves to a non-public address, unless the operator lists that hostname or network
    # (CIDR) here -- e.g. "127.0.0.1,mcp.internal,10.20.0.0/16" for internal MCP servers.
    http_tool_allowlist: str = ""
    outbound_private_hosts: str = ""
    # MCP servers only: loopback and RFC 1918 ranges are allowed by default (owner decision 2026-09-26) so
    # in-cluster servers keep working -- for a host the platform MCP allowlist names exactly (P4-X05; a
    # wildcard match reaches public addresses only); link-local/metadata and CGNAT stay refused.
    mcp_private_hosts: str = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # Platform MCP host allowlist (P4-X05), the deployment half; admins extend it at runtime with the
    # platform setting `outbound.mcp_host_allowlist`. A workspace can register and reach only an MCP server
    # whose host matches one of the two (entries: see tools/http.py). Empty in both = no MCP server.
    mcp_host_allowlist: str = ""
    http_tool_timeout_seconds: float = 30.0
    http_tool_max_bytes: int = Field(default=1_000_000, ge=1)

    # External delivery (N-3, services/deliveries.py). SMTP comes from the environment; the password is a
    # secret reference (env:NAME / file:/path), never a value. Webhooks use the outbound guard above (no
    # hostname allowlist: the destination's approval is its allowlist) and sign with the destination's
    # own secret_ref. Empty `smtp_host` = email delivery unavailable (webhooks still work).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password_ref: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 20.0
    delivery_email_allowed_domains: str = ""  # comma-separated; empty = any domain the approver accepts
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    delivery_backoff_seconds: float = Field(default=60.0, gt=0)
    delivery_backoff_max_seconds: float = Field(default=3600.0, gt=0)
    delivery_authorization_days: int = Field(default=90, ge=1)  # an authorized destination is re-approved after this
    delivery_attachment_max_bytes: int = Field(default=10_000_000, ge=0)
    delivery_timeout_seconds: float = 20.0

    # Set by compose.yaml: this process runs in a compose container, where `localhost` is the container itself.
    in_container: bool = False

    @model_validator(mode="after")
    def _container_hosts(self) -> Settings:
        """A `.env` written for running on the host (`redis://localhost:6379`) is also read by compose, and inside a
        container its loopback URLs reach nothing: Redis looked down (billable model calls paused, fail closed) and
        Superset unreachable. In a compose container a loopback host names the compose service that provides it."""
        for field in ("redis_url", "superset_url", "superset_public_url", "servicenow_mock_url", "web_url"):
            value = getattr(self, field, None)
            if isinstance(value, str) and len(value.split()) > 1:  # "http://host:8088   (a note)": a URL has no spaces
                setattr(self, field, value.split()[0])
        if not self.in_container:
            return self
        for field, service in _COMPOSE_SERVICES.items():
            value = getattr(self, field)
            if value:
                setattr(self, field, _to_service(value, service))
        return self

    @model_validator(mode="after")
    def _profile_defaults(self) -> Settings:
        """`lite` needs no Redis, Temporal or Superset: settings the environment did not set take the
        lite values. An explicit variable always wins, so a lite install can still add `bi` (Superset).
        A core install without the `temporal` extra that names no profile or orchestrator is lite: the
        `standard` default could not start a run there (P7-17: `pip install analystos` + 3 variables)."""
        given = self.model_fields_set
        if not {"profile", "orchestrator"} & given and not _installed("temporalio"):
            self.profile = "lite"
        if self.profile == "lite":
            if "orchestrator" not in given:
                self.orchestrator = "local"
            if "redis_url" not in given:
                self.redis_url = ""
            if "superset_url" not in given:
                self.superset_url = ""
        return self

    @property
    def local_worker_count(self) -> int | None:
        """Threads executing local tasks; None = inline in each run's driver (the standard behaviour)."""
        if self.local_workers is not None:
            return self.local_workers
        return 4 if self.profile == "lite" else None

    @property
    def resume_local_runs(self) -> bool:
        return self.local_resume if self.local_resume is not None else self.profile == "lite"

    @property
    def run_inprocess_scheduler(self) -> bool:
        return self.inprocess_scheduler if self.inprocess_scheduler is not None else self.profile == "lite"

    @property
    def isolated_pool_set(self) -> set[str]:
        return {p.strip() for p in self.isolated_pools.split(",") if p.strip()}

    @property
    def spend_store(self) -> str:
        if self.spend_counter_store != "auto":
            return self.spend_counter_store
        return "redis" if self.redis_url else "postgres"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def secret_env(name: str) -> str | None:
    """A provider key named by `api_key_env`: the process environment first, else the same `.env` file the
    settings read (a laptop that keeps its key there, not exported). Never logged; the file is re-read only
    when it changes."""
    import os

    value = (os.getenv(name) or "").strip()
    return value or _env_file_values().get(name)


def _env_file_values() -> dict[str, str]:
    env_file = Settings.model_config.get("env_file")
    if not env_file:  # disabled (the test suite): keys come from the process environment only
        return {}
    path = Path(str(env_file))
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _ENV_FILE_CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if value:
            values[key.strip()] = value
    _ENV_FILE_CACHE[str(path)] = (stamp, values)
    return values


_ENV_FILE_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def openrouter_api_key() -> str | None:
    return secret_env("OPENROUTER_API_KEY")

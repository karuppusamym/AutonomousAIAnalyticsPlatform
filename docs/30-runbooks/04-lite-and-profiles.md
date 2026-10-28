# Runbook 04 — Lite by default, profiles and configuration tiers

Decision: [ADR-0025](../10-architecture/adr/0025-lite-by-default.md) (spec v4 §12a; tracker P7-16, P7-17).
One codebase and one behaviour; the profile only decides which services run and which defaults apply.

## 1. Profiles

| Profile | Containers | How to start |
|---|---|---|
| `lite` (default) | Postgres (pgvector), `api`, `web` | `docker compose up -d --build` |
| `standard` | lite + Redis, Temporal, `worker` (every queue), `scheduler` | `docker compose --env-file .env --env-file deploy/compose/standard.env up -d --build` |
| `scale` | standard with one worker pool per queue | `docker compose --env-file .env --env-file deploy/compose/scale.env up -d --build` |

For a demo, `scripts/demo.ps1` (Windows) or `scripts/demo.sh` starts the full stack (or `-Lite` / `--lite`), waits for
health and seeds a ready workspace (`analystos demo-seed`); see [runbook 05](05-demo-walkthrough.md), which also
covers Windows without Docker.

Optional features, on top of any profile (name every profile on the command line: a `--profile` flag
replaces `COMPOSE_PROFILES` from the env files):

| Feature | Adds | How |
|---|---|---|
| `bi` | Superset; publications may target it (preview stays available) | `--env-file deploy/compose/bi.env --profile bi` |
| `graph` | Neo4j projection (lineage is served from Postgres without it) | `--profile graph`, `ANALYSTOS_GRAPH_ENABLED=true` |
| `demo` | the ServiceNow mock source, in its own small image | `--env-file deploy/compose/demo.env --profile demo` |
| `sandbox` | the container sandbox image (build only) | `docker compose --profile sandbox build sandbox` |
| `pooled` | PgBouncer | runbook 02 "Connection pooling" |
| `isolated` | the credential-free `compute-py` / `compute-ml` worker pools on an internal network (§5) | `--env-file deploy/compose/standard.env --env-file deploy/compose/isolated.env` (standard + isolated) |

The CI stack (everything): `docker compose --env-file .env --env-file deploy/compose/standard.env
--env-file deploy/compose/bi.env --env-file deploy/compose/demo.env --profile standard --profile bi --profile demo up -d`.
Naming services still works without a profile flag (`docker compose up -d postgres redis temporal superset`).

### What `lite` runs inside the API process

* **Local orchestrator** (`workflows/orchestrator.py::LocalRuntime`): the Temporal workflow's control loop.
  * **Resume:** on startup every non-terminal run is re-driven from its Postgres task state. Claims the
    previous process held are released first; a late result of a dead attempt is `superseded`.
  * **No thread while waiting:** a run in `WAITING_USER` (or paused) parks. The approval signal
    (`POST /api/approvals/{id}/approve`), a control or feedback re-drives it, and there is no wall-clock cap.
  * **Sweep:** every `ANALYSTOS_LOCAL_SWEEP_SECONDS` a sweep re-drives parked runs whose state changed
    without a signal, for example an approval decided by another process.
  * **Bounded parallelism:** ready steps of all runs execute on `ANALYSTOS_LOCAL_WORKERS` threads.
* **In-process scheduler:** the schedule and monitor loop runs as a thread of the API. Claim-then-execute
  keeps it safe next to a separate `analystos scheduler`.
* **Spend caps in Postgres:** with no `ANALYSTOS_REDIS_URL`, hard cap reservations lock the
  `spend_counter` rows (migration 0035).
  * They are atomic and still fail closed: a database error refuses the billable call.
  * The store is chosen by configuration (`ANALYSTOS_SPEND_COUNTER_STORE`), never switched at runtime. A
    Redis outage in `standard` refuses calls, as before.
* **Preview publishing:** without a Superset URL, publications target the preview destination with no
  connection attempt, and `tool.superset_publish` is listed as unavailable with the reason.

Durability in `lite` is "resume from Postgres after a restart", not Temporal's timers and history, and
it assumes **one API process** drives local runs (resume releases the claims it finds). Move to
`standard` for more than one API replica or for durable timers.

### Unavailable, with the reason

A capability manifest may require an installation feature: `requires: [profile:bi]`, `requires: [extra:ml]`
or an isolated compute pool, `requires: [pool:compute-ml]` (§5).

* `GET /api/capabilities` returns `available` and `unavailable_reason` for each capability.
* Binding and invocation refuse an unavailable capability, with the reason (`capabilities/enablement.usable`).
* `GET /api/health` shows `installation` (the profile, its features and the installed extras).
* Built-in declarations today:
  * `tool.superset_publish` needs `profile:bi`.
  * `method.driver_model`, `skill.logistic_regression` and `skill.feature_importance` need `extra:ml`.
  * Holt-Winters forecasts fall back to drift, with a stated warning.
  * PDF and XLSX reports need `extra:reports`. Markdown and HTML reports need nothing.

## 2. Images and extras (P7-17)

| Extra | Libraries | Needed by |
|---|---|---|
| core | FastAPI, SQLAlchemy/psycopg, numpy, scipy, duckdb, pandas, polars, pyarrow, redis client | every method of the investigate playbook (grouped logistic, Mantel-Haenszel and Durbin-Watson are numpy) |
| `reports` | matplotlib, fpdf2, openpyxl, python-pptx, python-docx | PDF (bundled DejaVu Unicode font)/XLSX/PPTX/DOCX reports; PPTX/DOCX are checked per library, so an image without them keeps PDF/XLSX |
| `ml` | scikit-learn, statsmodels | driver model; Holt-Winters |
| `temporal` | temporalio | `standard`/`scale` API and `analystos worker` |
| `graph` | neo4j | the Neo4j projection |
| `standard` | all four | worker and scheduler images |

`dev` pulls every extra, so the test suite exercises everything.

Images (`deploy/docker/Dockerfile`, build argument `EXTRAS`):

* the lite API image is `EXTRAS=reports`;
* the standard API image is `EXTRAS=reports,temporal`;
* the worker image is `EXTRAS=standard`;
* the mock has its own image (`deploy/docker/Dockerfile.mock`: FastAPI, numpy, pyarrow and two modules).

Measured 2026-09-26, with `uv` installing into empty Python 3.11 virtualenvs. Docker was not available,
so these are the installed site-packages sizes, not image sizes:

| Dependency set | site-packages |
|---|---|
| before (every library in core) | 900 MB |
| core only | 680 MB |
| core + `reports` (the lite API image) | 765 MB (with pytest) |
| mock image deps | 229 MB (with httpx) |

The API's import cold start (`import analystos.api.app`, median of 3, shared CPU):

* before, with every library installed: ~2.0 s;
* a core-only environment: ~1.3 s.

No ML, Temporal, Neo4j or report library is imported at API start in either case.

The largest remaining core libraries are polars (172 MB, used only by the CSV/Parquet connector and file
ingest, imported on first use) and pyarrow (152 MB, still imported at API start).

Re-measured 2026-09-27 with pip and newer resolved versions ([evidence](../60-delivery/evidence/p7-17-core-install-20260927.md)).
Installed sizes were core 818 MB, core + `reports` 920 MB and core + `standard` 1117 MB. The API import
cost was the same for all three sets. `tests/unit/test_slim_install.py` checks that no extra is imported
at start and that compose, the env files, the Dockerfile and `values-small.yaml` agree on the extras.

## 3. Helm: `values-small.yaml`

`helm install analystos deploy/helm/analystos -f deploy/helm/analystos/values-small.yaml`:

* one API (with the in-process scheduler), one worker on every queue and one web pod;
* no PDBs and no HPAs;
* total requests of 0.8 CPU and 1.8 GiB, so it fits a 2-CPU / 4 GiB node (`tests/unit/test_helm_chart.py`);
* Superset is off, so publishing uses preview;
* Temporal is still needed; for no Temporal use the lite profile with Docker Compose.

The chart runs one image for every component, so build it with `EXTRAS=standard`.

## 4. Environment variables in three tiers

**Tier 1, required (3).** A fresh install needs only these:

* `ANALYSTOS_DATABASE_URL`;
* `ANALYSTOS_JWT_SECRET`;
* `ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD`.

Compose sets its own database URL. On a laptop, development defaults stand in for the other two.
A core install without the `temporal` extra that sets neither `ANALYSTOS_PROFILE` nor
`ANALYSTOS_ORCHESTRATOR` runs as `lite`. Checked 2026-09-27 with only these three variables: migrate,
seed, start and a completed local run
([evidence](../60-delivery/evidence/p7-17-core-install-20260927.md)).

**Tier 2, optional, by feature.**

| Feature | Variables |
|---|---|
| Profile | `ANALYSTOS_PROFILE`, `ANALYSTOS_ENV`, `ANALYSTOS_BOOTSTRAP_ADMIN_EMAIL` |
| Models | `OPENROUTER_API_KEY`, `ANALYSTOS_MODELS_CONFIG`, `ANALYSTOS_AIR_GAPPED` |
| standard / scale | `ANALYSTOS_ORCHESTRATOR`, `ANALYSTOS_REDIS_URL`, `ANALYSTOS_TEMPORAL_ADDRESS`, `ANALYSTOS_TEMPORAL_NAMESPACE`, `ANALYSTOS_TEMPORAL_QUEUE_PREFIX`, `ANALYSTOS_WORKER_QUEUES` |
| bi | `ANALYSTOS_SUPERSET_URL`, `ANALYSTOS_SUPERSET_PUBLIC_URL`, `ANALYSTOS_SUPERSET_USERNAME`, `ANALYSTOS_SUPERSET_PASSWORD`, `ANALYSTOS_SUPERSET_ANALYTICS_SQLALCHEMY_URI`, `ANALYSTOS_ANALYTICS_BI_ROLE_PREFIX`, `ANALYSTOS_ANALYTICS_BI_SECRET` |
| graph | `ANALYSTOS_GRAPH_ENABLED`, `ANALYSTOS_NEO4J_URI`, `ANALYSTOS_NEO4J_USER`, `ANALYSTOS_NEO4J_PASSWORD` |
| demo | `ANALYSTOS_SERVICENOW_MOCK_URL`, `SERVICENOW_PASSWORD` |
| Data plane | `ANALYSTOS_ANALYTICS_READER_URL`, `ANALYSTOS_ANALYTICS_LOADER_URL`, `ANALYSTOS_ANALYTICS_BUILDER_URL`, `ANALYSTOS_ANALYTICS_WRITER_URL` (managed output writer, P6-03), `ANALYSTOS_UPLOAD_DIR`, `ANALYSTOS_ARTIFACT_DIR` |
| ELT builds | `ANALYSTOS_DBT_EXECUTABLE`, `ANALYSTOS_BUILD_DIR`, `ANALYSTOS_BUILD_TIMEOUT_SECONDS` |
| SSO | `ANALYSTOS_OIDC_ISSUER`, `ANALYSTOS_OIDC_CLIENT_ID`, `ANALYSTOS_OIDC_CLIENT_SECRET`, `ANALYSTOS_OIDC_REDIRECT_URI`, `ANALYSTOS_OIDC_SCOPES`, `ANALYSTOS_OIDC_GROUPS_CLAIM`, `ANALYSTOS_OIDC_MAPPING_FILE`, `ANALYSTOS_OIDC_JWKS_FILE`, `ANALYSTOS_OIDC_DISCOVERY_URL`, `ANALYSTOS_OIDC_PROVIDER_NAME`, `ANALYSTOS_PASSWORD_LOGIN` |
| Web | `ANALYSTOS_WEB_URL`, `ANALYSTOS_CORS_ORIGINS` |
| Outbound HTTP and MCP | `ANALYSTOS_HTTP_TOOL_ALLOWLIST`, `ANALYSTOS_OUTBOUND_PRIVATE_HOSTS`, `ANALYSTOS_MCP_PRIVATE_HOSTS` (default: loopback and RFC 1918 allowed for MCP servers only, and only for a host the MCP allowlist names exactly), `ANALYSTOS_MCP_HOST_ALLOWLIST` (platform MCP host allowlist, e.g. `mcp.internal,*.tools.example.com,10.20.0.0/16,bi.example.com:8443`; empty = no MCP server can be registered; admins extend it at runtime with the platform setting `outbound.mcp_host_allowlist`), `ANALYSTOS_HTTP_TOOL_TIMEOUT_SECONDS`, `ANALYSTOS_HTTP_TOOL_MAX_BYTES` |
| External delivery (N-3) | `ANALYSTOS_SMTP_HOST`, `ANALYSTOS_SMTP_PORT`, `ANALYSTOS_SMTP_USERNAME`, `ANALYSTOS_SMTP_PASSWORD_REF` (`env:NAME`, never the value), `ANALYSTOS_SMTP_FROM`, `ANALYSTOS_SMTP_STARTTLS` (credentials are never sent without it), `ANALYSTOS_SMTP_TIMEOUT_SECONDS`, `ANALYSTOS_DELIVERY_EMAIL_ALLOWED_DOMAINS`, `ANALYSTOS_DELIVERY_MAX_ATTEMPTS`, `ANALYSTOS_DELIVERY_BACKOFF_SECONDS`, `ANALYSTOS_DELIVERY_BACKOFF_MAX_SECONDS`, `ANALYSTOS_DELIVERY_AUTHORIZATION_DAYS`, `ANALYSTOS_DELIVERY_ATTACHMENT_MAX_BYTES`, `ANALYSTOS_DELIVERY_TIMEOUT_SECONDS`; webhook signing secrets are `ANALYSTOS_WEBHOOK_<NAME>` (16+ characters), named by a destination's `secret_ref` |
| Channels (S09) | `ANALYSTOS_DELIVERY_EMAIL_TRANSPORT` (`smtp` default, or `graph`), `ANALYSTOS_GRAPH_BASE_URL`, `ANALYSTOS_GRAPH_SENDER`, `ANALYSTOS_GRAPH_TOKEN_REF` (`env:NAME` of an app-only token with Mail.Send), `ANALYSTOS_CHANNEL_TEAMS_HOSTS`, `ANALYSTOS_CHANNEL_SLACK_HOSTS` (hosts a Teams / Slack incoming webhook may point at; a leading dot matches subdomains); a Teams / Slack destination's incoming-webhook URL is `ANALYSTOS_WEBHOOK_<NAME>` (its `url_ref`); inbound channel signing secrets are `ANALYSTOS_CHANNEL_<NAME>` |
| Triggers (S09) | `ANALYSTOS_TRIGGER_SKEW_SECONDS` (HMAC timestamp tolerance, default 300), `ANALYSTOS_TRIGGER_REPLAY_DAYS` (replay cache, default 7), `ANALYSTOS_TRIGGER_MAX_BODY_BYTES`, `ANALYSTOS_KAFKA_BOOTSTRAP_SERVERS` (compose profile `events`), `ANALYSTOS_KAFKA_TOPIC_ACL` (`<tenant id or *>:<topic glob>`, comma-separated; empty = no topic may be consumed); webhook rule secrets are `ANALYSTOS_TRIGGER_<NAME>` (16+ characters), named by a rule's `secret_ref` |
| Isolated compute pools (§5) | `ANALYSTOS_ISOLATED_POOLS`, `ANALYSTOS_ISOLATED_TRANSPORT`, `ANALYSTOS_WORKER_ARTIFACT_URL`, `ANALYSTOS_WORKER_TOKEN_SECRET` (API only; never give it to a worker) |
| Tool registry signing (S10) | `ANALYSTOS_TOOL_SIGNING_SECRET` (platform secret the tenant ed25519 manifest keys are derived from; default: derived from the JWT secret. Rotating it invalidates every signature until `POST /v2/tool-keys:rotate` re-signs) |
| Knowledge embeddings | `ANALYSTOS_KNOWLEDGE_EMBEDDING_PROVIDER`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_MODEL`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_DIM`, `ANALYSTOS_KNOWLEDGE_EMBEDDING_ALLOW_DOWNLOAD` |
| Postgres without pgvector | `ANALYSTOS_VECTOR_BACKEND` (`pgvector` default, or `array`: `real[]` columns, no extension; [self-hosted runbook](03-self-hosted.md#postgres-without-pgvector)) |

**Tier 3, advanced.** Defaults are right for almost every install.

| Area | Variables |
|---|---|
| Local orchestrator (lite) | `ANALYSTOS_LOCAL_WORKERS`, `ANALYSTOS_LOCAL_RESUME`, `ANALYSTOS_LOCAL_SWEEP_SECONDS`, `ANALYSTOS_INPROCESS_SCHEDULER` |
| Compose containers | `ANALYSTOS_IN_CONTAINER` (set by `compose.yaml`: a `localhost` Redis, Superset, Temporal or mock URL from a host `.env` is read as the compose service of that name, and a URL keeps only its first word, so a note after it in `.env` does no harm) |
| Spend caps and counters | `ANALYSTOS_SPEND_COUNTER_STORE`, `ANALYSTOS_BUDGET_COUNTER_PREFIX` |
| Connection pools | `ANALYSTOS_DB_POOL_MODE`, `ANALYSTOS_DB_POOL_SIZE`, `ANALYSTOS_DB_MAX_OVERFLOW`, `ANALYSTOS_DB_POOL_TIMEOUT`, `ANALYSTOS_DB_POOL_RECYCLE`, `ANALYSTOS_ANALYTICS_POOL_MODE`, `ANALYSTOS_ANALYTICS_POOL_SIZE`, `ANALYSTOS_ANALYTICS_MAX_OVERFLOW`, `ANALYSTOS_ANALYTICS_POOL_TIMEOUT`, `ANALYSTOS_LOADER_POOL_MODE`, `ANALYSTOS_LOADER_POOL_SIZE`, `ANALYSTOS_LOADER_MAX_OVERFLOW`, `ANALYSTOS_LOADER_POOL_TIMEOUT`, `ANALYSTOS_DB_TRANSACTION_POOLER` |
| Tenant isolation (ADR-009) | `ANALYSTOS_TENANCY_MODE` (`strict` default: every transaction outside system scope is restricted to its bound tenant by Postgres RLS, unbound sees nothing; `bound`: only transactions with a bound tenant, an upgrade aid; `off`), `ANALYSTOS_TENANCY_APP_ROLE` (restricted role, default `analystos_app`), `ANALYSTOS_AUDIT_EXPORT_DIR` (nightly audit-chain exports, S01-T5; default `var/audit`, empty disables; point at WORM storage in production) |
| ABAC engine (S01-T6) | `ANALYSTOS_POLICY_ENGINE` (`builtin` default; `opa` also asks an OPA sidecar after the built-in rules, which can only add a denial; unreachable or malformed = deny), `ANALYSTOS_POLICY_OPA_URL` (default `http://127.0.0.1:8181`), `ANALYSTOS_POLICY_OPA_DECISION_PATH` (default `analystos/abac/decision`), `ANALYSTOS_POLICY_OPA_TIMEOUT_SECONDS` (default 1) |
| Analytics roles | `ANALYSTOS_ANALYTICS_WORKSPACE_ROLE_PREFIX`, `ANALYSTOS_ANALYTICS_BUILD_ROLE_PREFIX`, `ANALYSTOS_ANALYTICS_WRITER_ROLE_PREFIX` |
| Managed writer | `ANALYSTOS_WRITER_KEEP_VERSIONS` (version tables kept per destination: the rollback depth) |
| Gateway | `ANALYSTOS_QUERY_TIMEOUT_SECONDS`, `ANALYSTOS_QUERY_MAX_ROWS`, `ANALYSTOS_QUERY_CACHE_TTL_SECONDS` |
| Governed ML (hard caps) | `ANALYSTOS_ML_MAX_TRIALS`, `ANALYSTOS_ML_MAX_SECONDS`, `ANALYSTOS_ML_MAX_ROWS`, `ANALYSTOS_ML_MAX_FEATURES` |
| Sandbox | `ANALYSTOS_SANDBOX_ISOLATION`, `ANALYSTOS_SANDBOX_TIMEOUT_SECONDS`, `ANALYSTOS_SANDBOX_MEMORY_MB`, `ANALYSTOS_SANDBOX_CONTAINER_IMAGE`, `ANALYSTOS_SANDBOX_CONTAINER_PYTHON`, `ANALYSTOS_SANDBOX_CONTAINER_RUNTIME`, `ANALYSTOS_SANDBOX_CONTAINER_STARTUP_SECONDS`, `ANALYSTOS_SANDBOX_CPUS`, `ANALYSTOS_SANDBOX_PIDS_LIMIT`, `ANALYSTOS_SANDBOX_SCRATCH_MB`, `ANALYSTOS_SANDBOX_MASKED_PATHS` |
| Auth | `ANALYSTOS_JWT_TTL_MINUTES` |
| Catalogs | `ANALYSTOS_AGENTS_DIR` |

`tests/unit/test_lite_profile.py` checks that every setting in `core/config.py` is listed here.

## 5. Isolated compute pools (P7-06, ADR-0022)

Heavy jobs (recipe snapshot statements today; classical ML in P5-04; notebook Python cells in P7-12) can
run in the credential-free pools `compute-py` and `compute-ml`. They are **opt-in**: with
`ANALYSTOS_ISOLATED_POOLS` empty, recipe snapshot jobs run as before (the `compute` queue or in-process),
and a capability that declares `requires: [pool:compute-ml]` is listed as unavailable with the remedy.

| What | Where |
|---|---|
| Contracts | `contracts/worker.py`: `TaskEnvelope`, `ArtifactRef`, `TaskDispatch`, `TaskResult` (`contracts/task_*.schema.json`) |
| Control plane | `workers/dispatch.py` `dispatch_isolated(pool, envelope)`; tasks and events in `worker_task` / `worker_task_event` (migration 0038) |
| Worker | `analystos worker --queues compute-py` (`workers/main.py`, `runtime.py`, `child.py`) |
| Artifact store | `GET /api/worker/artifacts/{id}`, `PUT /api/worker/tasks/{task}/outputs/{name}`, `POST /api/worker/model`, token-authenticated (`api/routers/worker.py`) |
| Conformance | `tests/conformance/worker/` (any implementation: `ANALYSTOS_CONFORMANCE_IMPL`) |

**Turning them on.**

* *Lite (subprocess transport).* `ANALYSTOS_ISOLATED_POOLS=compute-py` (and/or `compute-ml`). The API starts
  one local worker process per pool with an environment built from scratch (no credentials), which reaches the
  store at `ANALYSTOS_WORKER_ARTIFACT_URL` (default `http://localhost:8000`). Isolation is in-process only
  (the controls below): the worker shares the API container's network. Use it for a laptop or a pilot.
* *Standard / scale (Temporal transport).* Also run the pool as its own deployment: compose
  `--env-file deploy/compose/standard.env --env-file deploy/compose/isolated.env` (standard + isolated) (services `worker-compute-py`,
  `worker-compute-ml` on the internal `isolated` network), or Helm `workers.compute-py.replicas: 1`.
  `ANALYSTOS_ISOLATED_TRANSPORT=auto` picks Temporal when the orchestrator is Temporal.

**What the worker enforces in its own process** (`workers/isolation.py`):

* it refuses to start (exit 78, variable *names* reported) when given `ANALYSTOS_DATABASE_URL`, any analytics,
  Redis, Neo4j or Superset setting, the JWT or token secret, `OPENROUTER_API_KEY`, `PG*`, cloud keys, or any
  `*_PASSWORD`, `*_SECRET`, `*_API_KEY`; then it drops every variable not on a short allowlist;
* it never builds the platform `Settings` (which would read `.env`);
* Python sockets reach only the artifact store (and the Temporal frontend); other connects, datagrams, Unix
  sockets and name lookups are refused;
* each task runs in a child process with rlimits from the envelope budget (CPU, address space, file size,
  open files, no core), a wall-clock kill of its process group, an empty environment and, where the kernel
  allows it, an empty network namespace (`usage.network = namespace`), else the socket guard (`guard`);
* inputs are downloaded and outputs uploaded by the supervisor with the task token; every input is checked
  against its SHA-256; outputs are bound per (workspace, idempotency key, output name), so a retry that
  finishes twice writes one artifact, and a retry with different content is refused.

**Scoped task tokens** (`workers/tokens.py`): HMAC-SHA256 with `ANALYSTOS_WORKER_TOKEN_SECRET` (else a key
derived from the JWT secret), bound to the task, its workspace, the input artifact ids it may read, the output
names it may write, its verbs (`read`, `write`, `model`) and model purposes, expiring after the task's
attempts (wall budget x 3 + 2 min, at most 6 h). A session JWT is not accepted, nor a token of another task.

**Network policy (the outer layer; not verifiable in the test environment, which has no Docker):**

* *compose:* the `isolated` network is `internal: true`; only `api` and `temporal` join it besides the pool
  services, so a pool container has no route to Postgres, Redis, Superset or the internet. The pool services get
  only `ANALYSTOS_WORKER_QUEUES`, `ANALYSTOS_WORKER_ARTIFACT_URL` and the Temporal address, never the
  `x-app-env` block.
* *Kubernetes:* pools with `isolated: true` get no ConfigMap or Secret (`envFrom`), the label
  `analystos.io/isolated: "true"`, and their own NetworkPolicy (`templates/networkpolicy-isolated.yaml`,
  always rendered when such a pool exists): no ingress; egress to cluster DNS, the API pods on port 8000 and
  `isolatedPools.temporalEgress` only. The release-wide egress policy excludes isolated pods so the two
  policies do not add up.
* The shared `artifact_dir` assumption of recipe snapshots still holds for the API and the analysis worker (the
  control plane writes inputs to `<artifact_dir>/worker_artifacts`, which the API serves); isolated pools never
  mount it.

**Plugging a new job in (e.g. P5-04 ML).** Register the handler
(`workers/handlers.py`: `register_handler("ml.job", "analystos.ml.jobs:run_ml_job", pools=("compute-ml",),
adapter="pure")`; `ML_JOB_TARGET` is that default), build a `TaskEnvelope` with `spec=MLJobSpec(job=...)`,
`capability=capability_ref("ml.job")`, `budget=default_budget("compute-ml")`, `required_outputs=["result"]`
and a deterministic `idempotency_key`, then `raise_for_result(dispatch_isolated("compute-ml", envelope))`. A pure
job gets `{**job, "inputs": {artifact_id: path}}` and its returned dict becomes the JSON output `result`;
`ctx.trial()` (context handlers) enforces `max_trials`. When the pool is not configured, `require_pool`
raises `FeatureUnavailable` with the remedy; the manifest should declare `requires: [pool:compute-ml]`.

## Persistent uploads and artifacts

Compose mounts the named `runtime-data` volume at `/app/var` for the API and standard/scale workers and scheduler. This preserves workspace uploads, file-backed sources and artifacts across container replacement. Each file source still uses its workspace folder below `/app/var/uploads`; each staged source has its own analytics schema and workspace reader grants. Isolated compute pools do not receive this volume.

Back up both PostgreSQL databases and this volume. A database backup alone preserves staged tables and source records, but cannot restore missing uploaded files. Ordinary `docker compose up -d` or container recreation keeps the volume; `docker compose down -v` removes named volumes and is destructive. Existing installations must copy their current `/app/var` contents into the volume before replacing their first container with this configuration. Use a reviewed backup/restore process and preserve file ownership for UID 10001.

A recovered staged snapshot can serve as a demo-file recovery input only after authorized reads and type/value checks. It is not proof that the original upstream population was complete. Refresh through Sources after recovery, then recheck dependent findings whose evidence was invalidated by a data-version change.

# Runbook — operations

## Health
`GET /api/health` reports Postgres, Redis, Neo4j, Temporal, Superset reachability and whether chat
models and JEV are routable. It reports observations, not SLOs. With the graph projection off (the
default) the `neo4j` check reads `{"ok": true, "status": "disabled"}` and Neo4j is never contacted.

## Where to look
| Question | Source |
|---|---|
| What did an agent do? | `agent_message`, `tool_execution` (Agent Console) |
| What did it cost? | `model_call` (per call cost/tokens/latency), `/api/admin/usage` |
| Which data did it read? | `query_execution` (SQL, referenced assets, fingerprint, rows, cache hit) |
| Why was something allowed/denied? | `audit_event` (decision + reasons), `/api/workspaces/{id}/audit` |
| What was published, from what? | `publication`, `artifact.external_id`, `lineage_edge` (Neo4j only if enabled) |

## Recovery
* **Worker crash mid-task** — Temporal re-dispatches the activity; the task key is idempotent and
  stale plan versions are discarded.
* **Hung task** — activities heartbeat every third of their queue's `heartbeat_seconds`
  (`config/task_queues.yaml`); an attempt that goes silent (a statistic stuck in native code, a
  frozen pool process) is timed out after that window, and the retry releases the dead attempt's
  task claim and runs the step again. A hung compute-pool process keeps its pool slot until it is
  killed; killing it breaks the pool and the compute worker exits, so run it under a restart policy
  (compose `restart`, a Kubernetes Deployment).
* **Backlog on one workload** — scale that pool only (`--scale worker-compute=N`, or the Helm
  `workers.<pool>.replicas`); Temporal's task-queue backlog per `<prefix>-<workload>` shows which.
* **Partial publication** — `publication.status = partial` with created ids; re-approving/retrying
  the same bundle reconciles by name before creating. To undo: `POST /api/publications/{id}/rollback`.
* **Neo4j lost** — it is an optional projection; the neighbourhood falls back to Postgres at once,
  and the next `finalize` (or `graph.projection.project_workspace`) rebuilds it from Postgres.
* **Rotate OPENROUTER_API_KEY** — update the secret in the environment and restart api/worker; no
  key is stored in the database.

## Connection pooling (P4-S05)
Every API process, worker process and compute-pool child keeps its own SQLAlchemy pools, so the
Postgres connections a deployment can open are roughly
`processes × (control + analytics-per-URL + loader)` maxima. On the 2026-09-25 load run four worker
processes wanted far more than a shared `max_connections = 100` and 0/50 runs completed
(`docs/60-delivery/evidence/load-s05-20260925.md`). Pool sizes are settings (`src/analystos/db/pools.py`):

| Plane | Engine | Settings (defaults) |
|---|---|---|
| control | `db/base.py` | `ANALYSTOS_DB_POOL_SIZE` 10, `ANALYSTOS_DB_MAX_OVERFLOW` 20, `ANALYSTOS_DB_POOL_TIMEOUT` 30 s |
| analytics (reader, pushdown sources; one pool per URL) | `gateway/engines.py` | `ANALYSTOS_ANALYTICS_POOL_SIZE` 5, `…_MAX_OVERFLOW` 5, `…_POOL_TIMEOUT` 30 s |
| loader | `gateway/engines.py` (`plane="loader"`) | `ANALYSTOS_LOADER_POOL_SIZE` 2, `…_MAX_OVERFLOW` 3, `…_POOL_TIMEOUT` 30 s |
| all three | | `ANALYSTOS_DB_POOL_MODE` `queue` (or `none`: no client-side pool), per plane `ANALYSTOS_ANALYTICS_POOL_MODE` / `ANALYSTOS_LOADER_POOL_MODE`; `ANALYSTOS_DB_POOL_RECYCLE` −1; `ANALYSTOS_DB_TRANSACTION_POOLER` false |
| builder | direct psycopg connection per provisioning step, dbt's own connection | not pooled; see below |

Helm: `database.pools.*` and `database.transactionPooler` render these into the ConfigMap.

**PgBouncer.** For more than a handful of processes, put PgBouncer in **transaction** mode in front of
the control database and the analytics loader/reader: client connections are cheap, and Postgres sees
at most `default_pool_size` per (database, user) and `max_db_connections` per database.

* Compose: `ANALYSTOS_PG_POOLER=pgbouncer:5432 ANALYSTOS_DB_TRANSACTION_POOLER=true docker compose --profile pooled up -d`
  (host port 6432; `PGBOUNCER_DEFAULT_POOL_SIZE`, `PGBOUNCER_MAX_DB_CONNECTIONS`,
  `PGBOUNCER_QUERY_WAIT_TIMEOUT` size it). Image `edoburu/pgbouncer:v1.23.1-p3`; logins are checked
  with `auth_query` against `pg_shadow`, so per-plane and per-workspace roles need no userlist.
* Helm: `pgbouncer.enabled=true`, `pgbouncer.postgresHost`, `pgbouncer.existingSecret` (keys `DB_USER`,
  `DB_PASSWORD`: a login allowed to read `pg_shadow`), then point `ANALYSTOS_DATABASE_URL`,
  `ANALYSTOS_ANALYTICS_LOADER_URL` and `ANALYSTOS_ANALYTICS_READER_URL` in the app Secret at
  `<release>-analystos-pgbouncer:5432`. The chart then sets `ANALYSTOS_DB_TRANSACTION_POOLER=true`.
* `ANALYSTOS_DB_TRANSACTION_POOLER=true` turns off psycopg's automatic server-side prepared statements
  (consecutive transactions of one client can land on different server connections); PgBouncer runs
  with `max_prepared_statements = 0` as well.
* Behind PgBouncer keep a small client pool (`queue`, sized to the process's threads) or use `none`;
  the server-side cap is PgBouncer's.

**What is safe in transaction mode.** The platform keeps no session state on the pooled planes: the
gateway's workspace role, `statement_timeout` and `idle_in_transaction_session_timeout` are
`SET LOCAL` inside the query's own read-only transaction (`engines/sql.py`); the knowledge index uses
`set_config(…, true)`; the nightly calibration lock is `pg_try_advisory_xact_lock`; the staging load
(`CREATE`, `COPY`, swap, grants) is one transaction. `tests/unit/test_db_pools.py` fails on any
`execute("SET ROLE …")`: build-target provisioning now uses `SET LOCAL ROLE` too.
**Not safe:** the builder plane. dbt sets `role:` for its whole session, so
`ANALYSTOS_ANALYTICS_BUILDER_URL` must point at Postgres directly or at a **session**-mode pool (compose
keeps it on `postgres:5432`). Pushdown sources (a customer's own Postgres) are outside this: their
connectors set session characteristics on connections they own.

**Size the control database pool above the activity slots.** Some code paths open a second short
transaction while the caller's transaction still holds its connection (e.g. a tool gate inside an
artifact save; the most frequent one, `tools/registry.gate_agent_write`, no longer does). If every
server connection is held by such an outer transaction, all of them wait for a second connection
that never frees: PgBouncer shows `cl_waiting` climbing with `sv_active` at the cap and the server
side `idle in transaction`. The first 2026-09-26 run hit exactly this with 8 control connections for
4 × 16 activity slots. Keep the control database's `default_pool_size` comfortably above the
concurrent analysis slots that touch it (`workers × analysis.max_concurrent` in
`config/task_queues.yaml`, plus API and scheduler), and keep `query_wait_timeout` finite so a stall
becomes an error and a Temporal retry instead of a hang. Measured on 2026-09-26 (4 workers, 50
concurrent runs): a control pool of 8 deadlocked, 20 hit `query_wait_timeout`, 30 completed 50/50
(`docs/60-delivery/evidence/load-s05-runs-20260926.md`); compose defaults to 30 per (database, user).

## Budgets
Per workspace policy: `run_token_budget`, `run_cost_budget_usd`, `workspace_monthly_cost_budget_usd`,
`max_queries_per_run`. When exceeded, model calls fail with `budget_exceeded` and agents continue on
deterministic paths; queries beyond the cap fail the task that issued them.

Hard spend caps: every billable model call first reserves its price-table estimate, atomically in
Redis, against the platform **daily** cap (`llm.daily_spend_cap_usd`, default $2, UTC day) and the
workspace **monthly** cap (`workspace_monthly_cost_budget_usd`), and settles to the actual cost after
the call. Over a cap the call is refused (`spend_cap_reached`, recorded as a `refused` model call)
and the deterministic path runs; at `llm.spend_alert_fraction` (80%) admins get a `budget.warning`
event and notification. Without Redis, reservations fail closed (`spend_counters_unavailable`).

* **"Why are no model calls happening?"** — Admin > Capability registry > Models > Model health
  (`GET /api/admin/models/health`): key present in the api process, last success, credit cooldown
  (HTTP 402), today's spend vs the cap; "Probe credits" (`?probe=1`) sends one tiny billable request.
* **Model routing is cheap first** — small models answer; the large tier (Sonnet) only after a
  deterministic check fails (invalid JSON, schema, SQL still rejected after repairs, no usable
  hypotheses, a narrative the numbers guard rejects). Per purpose: `llm.escalation`
  (`never | on_validation_failure | always_large`); `max_quality` = Sonnet first.
  `python scripts/cost_gate.py usd` prints the expected $ per recorded run for both routings.
* **Demo schedules and monitors** — evidence scripts mark theirs (`[demo] ` name, `config.demo`) and
  disable them on exit. Leftovers: `analystos schedules disable-demo [--dry-run]`; every enabled
  schedule and monitor: `--all` (per workspace: `--workspace ws_...`).

## Tenant isolation (ADR-009, migration 0057)

* `analystos migrate` creates the restricted role `analystos_app` (NOLOGIN, NOBYPASSRLS) and grants it to the
  migrating login. On a managed Postgres where the migrating login has no `CREATEROLE`, a DBA runs once:
  `CREATE ROLE analystos_app NOLOGIN NOBYPASSRLS; GRANT analystos_app TO <app login>;`. Then migrate.
* Row-level security applies after `SET LOCAL ROLE analystos_app` even when the login is a superuser, so dev compose works.
  In production use a non-superuser owner login without `BYPASSRLS` anyway: a superuser session could switch the role back.
* Check isolation on a live database:
  `SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND
  EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name=c.relname AND column_name='tenant_id') AND NOT c.relrowsecurity;`
  It must return no rows.
* New tenant: `POST /api/admin/tenants` (platform admin). Users act for their home tenant, or for another tenant they
  belong to via the `X-Tenant-Id` header.
* `audit_event` is append-only. A retention job sets `app.audit_maintenance = 'on'` in its own transaction, runs as the
  owner identity, and audits itself.
* Mode `ANALYSTOS_TENANCY_MODE=strict` is the default (S01-T4): a transaction with no bound tenant, outside system scope,
  sees no tenant rows. Who binds what:
  * API and MCP requests: the caller's tenant (token, `X-Tenant-Id`). Isolated-worker callbacks: the tenant of the
    task token's workspace.
  * Temporal: the client stamps the `analystos-tenant` header on each workflow it starts; the workflow copies it to its
    activities and children; the worker runs each activity as that tenant. Engine activities also check that the
    tenant owns the run, which covers workflows started before the header existed.
  * Scheduler and sweeps: claims, relays and sweeps are platform loops (system scope); each claimed item (schedule
    run, delivery, dispatch, crawl, run step) runs as its owner.
  * CLI maintenance commands (`migrate`, `seed`, `calibrate`, `knowledge`, `bi-sync`, ...) run in system scope.
* Audit hash chain (S01-T5): the scheduler seals new audit rows into one SHA-256 chain per tenant and runs a nightly
  pass (seal all, verify every chain, export new links to `ANALYSTOS_AUDIT_EXPORT_DIR`). Mount that directory on
  object-lock (WORM) storage in production. Manual: `analystos audit verify [--tenant T]` (exit 4 = broken chain; the
  output names the first bad link), `analystos audit export --out DIR`. A broken chain is audited as
  `audit.chain_broken` and logged at error level: treat it as a security incident (who had owner credentials?).
  Tenants check their own chain with `POST /api/tenants/{t}/audit/verify` (tenant admin or auditor).
* Rolling upgrade from `bound`: deploy the new code everywhere first (workers last is fine: engine activities bind the
  run's tenant without the header), then remove any `ANALYSTOS_TENANCY_MODE=bound` override. A symptom of a missed
  binding is "not found" or an empty list from background work; set `bound` to restore service while you fix it.

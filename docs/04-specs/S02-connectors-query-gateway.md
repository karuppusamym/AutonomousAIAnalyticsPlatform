# S02 — Connectors, query gateway, result handles, multi-source

**Priority:** P0 (gateway, handles, certification kit) / P1 (multi-source, REST) · **Closes:** FR-001, FR-002, FR-006, FR-013, FR-029, FR-034, FR-051 · **ADRs:** 010

## 1. Purpose
One governed path for every statement against customer data, results as handles rather than rows, and a
repeatable way to certify a connector against a live engine.

## 2. Baseline to reuse
| Concern | Code |
|---|---|
| Single gateway | `gateway/` `QueryGateway.execute`, `gateway/validator` (selected tables only; restricted columns denied through `*`, aliases and CTEs; UDFs, hints and unkeyed joins refused) |
| Source kinds | `config/source_kinds.yaml`, `connectors/kinds.py`, `generic_sql.py`, `postgres.py`, `sqlserver.py`, `csv_file.py`, `servicenow.py` |
| Staging | `staging/` snapshot for kinds without a read-only session switch |
| Secrets | `connectors/secrets.py` (`env:`, `file:`) |
| Certification | `connectors/certification.py` |
| Engines | `engines/` (DuckDB) |
| Donor (Atlas) | PostgreSQL/SQL Server pull adapters, SQL Server pooling, secret-manager boundaries (AWS SigV4, Azure, Vault) |

## 3. Build
1. **Worker split.** Move gateway execution into a `worker:query` role, reached by the API and agents through the
   `QueryGateway` protocol (in-process in `lite`, a Temporal activity on the `query` task queue in `standard`).
   Credentials resolve only inside this role.
2. **Secret managers.** Add `vault:`, `aws-sm:`, `azure-kv:` and `gcp-sm:` `secret_ref` schemes, ported from Atlas.
   Cache secrets in memory for ≤ 5 min. Never log or persist them.
3. **Result handles (ADR-010).** If a result exceeds 256 rows or 64 KB, write Parquet to `tenant/{t}/results/{qe_id}.parquet` and
   return `ResultHandle{uri, schema, row_count, bytes, sha256, expires_at, classification_max}`. Inline results keep the current shape.
   Steps, compute and charts consume handles.
4. **ADBC where available.** Snowflake, BigQuery, PostgreSQL and Flight SQL use ADBC drivers, with a fallback to the existing SQLAlchemy path.
   The Arrow stream is written straight to Parquet.
5. **Pre-execution cost guard.** `estimate(sql)` per kind: `EXPLAIN` (PG, MySQL), dry run (BigQuery bytes), `EXPLAIN USING JSON`
   (Snowflake), otherwise row-cap only. Refuse above `budget.max_scan_bytes`. Record the estimate and the actual.
6. **DB-native row security passthrough.** If a source supports session context (PG `SET app.user`, Snowflake
   secondary roles, SQL Server `SESSION_CONTEXT`), the gateway sets the end-user attribute so the source's own RLS applies.
   This is declared per source kind as `capabilities.session_identity`.
7. **Multi-source (FR-051).** A run may declare `sources[]` if the workspace policy has `data_movement: staged_ok`.
   Each source query runs through the gateway independently, producing handles. Joins happen in DuckDB in
   `worker:compute` over the handles. Lineage records every source handle. Optional Trino engine where the customer runs Trino.
8. **Governed REST connector (FR-029).** Declarative YAML (`base_url`, auth `secret_ref`, endpoints with typed
   params, pagination, response → table mapping, rate limit). It is read-only by construction (GET only, or a POST
   allowlist marked `safe: true` by an admin).
9. **Connector certification kit.** `analystos certify-connector --kind snowflake --dsn-ref vault:...` runs the
   validator security suite per dialect, reads read-only enforcement, timeout/cancel, row cap, metadata crawl and type
   mapping, then writes `docs/evidence/connectors/{kind}-{date}.md`. A kind is `certified` only with a dated file
   younger than 180 days.

## 4. Interfaces
```python
class QueryGateway(Protocol):
    def execute(self, req: QueryRequest) -> QueryResult: ...   # QueryResult.inline | .handle
    def estimate(self, req: QueryRequest) -> CostEstimate: ...
    def cancel(self, query_execution_id: str) -> None: ...
```
`QueryRequest{tenant, workspace, principal, source_id, sql | semantic_query, params, purpose, run_id?, request_id?, budget}`.

## 5. Invariants
* There is no source connection outside `worker:query`. An import-linter contract forbids `connectors.*` imports outside `gateway`.
* The query identity cannot reach the control-plane DB (a network policy plus distinct credentials).
* Validator rejections are audited with the reason code. The SQL text is stored hashed plus redacted.

## 6. Acceptance
AT-04 (budget stop), AT-12 (source outage), the validator negative suite per certified dialect, a handle TTL purge
test, and a multi-source join test (PG + CSV) with lineage on both.

## 7. Tasks
| ID | Task | Est |
|---|---|---|
| S02-T1 | `worker:query` role + Temporal activity + protocol shim | 2d |
| S02-T2 | Secret-manager schemes (port from Atlas) | 2d |
| S02-T3 | Result handles + object store + TTL purge | 2d |
| S02-T4 | Cost estimate per kind + budget refusal | 2d |
| S02-T5 | Session-identity passthrough (PG, Snowflake, MSSQL) | 2d |
| S02-T6 | Certification kit + certify PG, MSSQL, Snowflake, BigQuery live | 4d (needs access) |
| S02-T7 | Multi-source via DuckDB on handles | 3d |
| S02-T8 | Declarative REST connector | 3d |

## 8. As built (2026-10-27)

What landed, and where it differs from §3–§7. Evidence is in the tracker rows S02-T1…T8 and S02-SEC.

* **Security fix (S02-SEC).** A workspace editor could register `secret_ref: env:<ANY_VAR>` and receive that process
  variable (for example `OPENROUTER_API_KEY`, `ANALYSTOS_JWT_SECRET`) as the password sent to a host they control.
  Workspace-registered references (sources, MCP servers) now pass an operator policy in `connectors/secrets.py`:
  `env:` only under `ANALYSTOS_SECRET_ENV_PREFIXES` (default `ANALYSTOS_SOURCE_SECRET_`) or operator-listed names
  (never `ANALYSTOS_*`), `file:` only below `ANALYSTOS_SECRET_FILE_DIRS`, manager refs only under their configured
  prefix (optionally `{tenant}`-scoped). Checked at registration (403) and again at resolution. Webhooks keep their
  `ANALYSTOS_WEBHOOK_` namespace; the SMTP password is a platform ref. The demo, compose, packs, runbooks and tests
  use `env:ANALYSTOS_SOURCE_SECRET_SERVICENOW`. The same hole existed for `file:` refs and MCP bearer tokens; both
  are covered.
* **T1.** `gateway/protocol.py` (`QueryRequest`, `QueryService`, `InProcessQueryService`, `TemporalQueryService`,
  `RemoteQueryGateway` for existing `execute(scope, sql)` / `run_sql_for` callers) and `workflows/query_workflow.py`
  (`QueryWorkflow` + activity `query_gateway_op` on the new `query` queue, cancellation through the heartbeat).
  Deviations: the transport defaults to `inprocess` in **every** profile (`standard` opts in with
  `ANALYSTOS_QUERY_TRANSPORT=temporal`); "credentials resolve only in worker:query" is enforced by
  `ANALYSTOS_PROCESS_ROLE` / `ANALYSTOS_CREDENTIAL_ROLES` (default `all,query,crawl,elt`, because staging and crawls
  extract staged sources in their own pools); `explain` (Ask's plan view) is not remoted. The Temporal path is covered
  by unit tests of its pieces only: no live Temporal server was used (the shared one belongs to the lead session).
* **T2.** `vault:` (KV v1/v2, namespace), `aws-sm:` (SigV4 ported from Atlas, pinned to AWS's published vector),
  `azure-kv:` (client credentials or managed identity), `gcp-sm:` (service-account JWT or metadata server), `#key`
  picks a JSON field. Bounded in-memory cache ≤ 5 min. Verified against mocked HTTP transports only: no live
  Vault/AWS/Azure/GCP account was reachable.
* **T3.** `gateway/handles.py`: rows stream through a `ResultSink`; past 256 rows / 64 KB the result is written as
  Parquet (zstd) to `tenant/{t}/results/{qe_id}.parquet` and recorded in `result_handle` (tenant RLS, migration
  `0066_query_gateway_s02`) with schema, row count, bytes, sha256, expiry and the highest column classification.
  `open_dataset` / `read_rows` re-check tenant visibility, expiry and the sha256 on every open; the scheduler's
  housekeeping purges expired files. Deviations: the store is a local directory (`ANALYSTOS_RESULT_STORE_DIR`), not
  an S3-compatible object store, and files are not encrypted by the platform (use an encrypted volume). The legacy
  `QueryGateway.execute(scope, sql)` keeps `result_mode="inline"` by default so existing steps are unchanged;
  `QueryRequest` defaults to `auto`. Consumers on handles today: multi-source joins (T7) and `read_rows` /
  `open_dataset`; charts, compute steps and statistics still take inline rows.
* **T4.** `gateway/cost.py` + `QueryGateway.estimate`. PostgreSQL (analytics DB and pushdown):
  `EXPLAIN (FORMAT JSON, VERBOSE)`; relations read whole (seq / bitmap heap scans) count their `pg_relation_size`,
  because a scan node's rows are counted after its filter. Budget = min(request, workspace policy `max_scan_bytes`,
  `ANALYSTOS_QUERY_MAX_SCAN_BYTES`); above it the statement is refused before it runs and audited with
  `reason_code=budget.max_scan_bytes` and the estimate; an unreadable plan under a budget fails closed. MySQL,
  BigQuery (dry run) and Snowflake parsers exist and are unit-tested on recorded plans, but those kinds are staged
  here, so their queries are estimated by the PostgreSQL path. The estimate runs only when a budget applies (an
  extra round trip otherwise); "actual" is the row count and result bytes, not the bytes the engine scanned.
  In a goal run with `max_scan_bytes` (S06) each admitted statement's estimate is charged to the run
  (`autonomy.budget.record_scan`) and a statement that would pass it is refused first (`check_scan`,
  `reason_code=budget.run_scan_bytes`); the estimate is charged, never the engine's actual scan.
  ADBC (§3.4) is **not done**: no ADBC driver is installed in this environment; the SQLAlchemy path streams instead.
* **T5.** Source-kind catalog `capabilities.session_identity`, driver-bound parameters `%(user)s` (AnalystOS user
  id), `%(user_email)s`, `%(workspace)s`. PostgreSQL sets transaction-local `app.user` / `app.user_email` /
  `app.workspace` (live-tested against a source RLS policy). SQL Server (`sp_set_session_context`, read-only) and
  Snowflake (session variables) are declared but untested (no access; Snowflake is staged-only).
* **T6.** `connectors/certify_kit.py` + `analystos certify-connector --kind K --dsn-ref REF [--schemas S]`, writing
  `docs/evidence/connectors/<kind>-<date>.md`. `connectors/certification.py` reads both evidence layouts and
  certifies only a passing file younger than 180 days. PostgreSQL 16 certified locally on 2026-10-27. Live checks
  exist for PostgreSQL only; **MSSQL, Snowflake and BigQuery stay blocked** (no live access): the kit runs their
  validator suite and reports every live check as failed, so they cannot be certified by it.
* **T7.** A federated statement's legs run through `execute` with `result_mode="handle"` on each source's own scope,
  identity, budget and audit row; DuckDB joins the Parquet datasets lazily; lineage edges record
  `query derived_from result_handle`, `result_handle reads table` and `result_handle from_source source` for every
  leg. Workspace policy `data_movement: none | staged_ok` gates multi-source statements; the default is `staged_ok`
  so existing cross-source runs keep working (set `none` to forbid). The join runs where the gateway runs
  (worker:query under the Temporal transport), not in a separate `worker:compute` activity. No Trino engine.
* **T8.** `connectors/rest.py` (kind `rest`, staged): declarative base URL, typed parameters with fixed values,
  pagination `page | offset | cursor | next_url` (a short page ends paging; a `next_url` must stay on the base URL),
  JSON path response mapping, rate limit, auth `bearer | basic | header` from `secret_ref`. GET only, or a POST marked
  `safe: true` **and** listed in `ANALYSTOS_REST_SAFE_POST_ALLOWLIST`. Every request goes through the outbound SSRF
  guard (pinned address, no redirects, size cap). Link-header pagination is not supported (the guard returns the body
  only).
* **Invariants (§5).** Refusals carry `reason_code` and keep only the hashed + literal-redacted statement. Not done:
  the import-linter contract forbidding `connectors.*` outside the gateway (crawler, staging and the source service
  import connectors today), and the network policy separating the query identity from the control-plane DB.

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

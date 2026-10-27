# S01 — Tenancy, identity, policy, approvals and audit

**Priority:** P0 · **Closes:** FR-012, FR-016, FR-037, FR-044 · **ADRs:** 009, 003 · **Depends on:** none (first in the critical path)

## 1. Purpose
Make isolation fail closed. Add `tenant` above `workspace`, enforce it in Postgres with RLS, prove it in CI,
and make the audit log physically append-only.

## 2. Baseline to reuse
| Concern | Code |
|---|---|
| Users, workspaces, members, policies | `db/models.py` `User`, `UserIdentity`, `Workspace`, `WorkspaceMember`, `WorkspacePolicy` |
| OIDC (PKCE, JWKS, group → role) | `security/`, `config/oidc.yaml`; tested with `scripts/fake_idp.py` |
| Scope resolution | `governance/scope` (server-resolved scope per call) |
| Approvals bound to hashes | `governance/approvals.py` (`verify_for_execution`) |
| Audit | `AuditEvent`, `governance/audit` |
| Atlas donor | Enterprise secret managers, `authorization_gate.py`, access reviews |

## 2a. As built (2026-10-27, tasks T1–T3)
* Ids follow the codebase (`String(40)`, prefix `tnt_`), not UUID. Default tenant `tnt_default`.
* `tenant_id` is filled by a `BEFORE INSERT` trigger from the row's workspace or parent row (then the bound tenant,
  then, for the roots `workspace`/`app_user` only, the default tenant). The ORM classes stay unchanged: the column is
  attached to the metadata by `tenancy/schema.py::attach_tenant_columns`.
* RLS policies apply to the restricted role `analystos_app` (setting `ANALYSTOS_TENANCY_APP_ROLE`). Each transaction
  runs `SET LOCAL ROLE analystos_app` plus `set_config('app.tenant_id', …, true)`, so it is safe under PgBouncer
  transaction pooling. The login (owner) identity is the deliberate **system** identity, used only inside
  `system_scope()`, so tables use `ENABLE` (not `FORCE`) row-level security.
* Modes (`ANALYSTOS_TENANCY_MODE`): `strict` (default since T4) restricts every transaction outside `system_scope`, and an
  unbound one sees nothing (fail closed); `bound` restricts only transactions with a bound tenant (upgrade aid); `off`.
* **T4, background work** (`tenancy/background.py`, `workflows/tenant_propagation.py`):
  * Platform loops (scheduler claims, dispatch relay and reconcile, stale-work sweep, delivery and freshness scans,
    nightly jobs, the local orchestrator's resume/sweep) are marked `@platform_loop` and run in `system_scope`.
  * Tenant work is marked `@owned_by(table, param)`: engine `plan_run`/`get_state`/`execute_task`/`finish_run`,
    `schedules.execute`, `complete_from_run`, `dispatch.dispatch`, `deliveries.attempt`, `crawler.run_crawl`,
    `worker_tasks.record_events`. It runs as the row's tenant; with another tenant bound it raises `TenantMismatch`.
    Row owners are cached (bounded, 50k; a row never changes tenant).
  * Temporal: a client interceptor stamps header `analystos-tenant`; the workflow interceptor copies it to activities
    and child workflows; the activity interceptor wraps the function in a picklable `TenantBound`, so the scope is
    entered in the thread or compute-pool process that runs it. The orchestrator's client loop thread receives the
    caller's tenant through `_submit`.
  * Sign-in (password, OIDC) looks users up in a system session, then binds the session to the user's tenant.
    Isolated-worker callbacks act for the tenant of the token's workspace. CLI maintenance commands run in system scope.
  * The spec's invariant "never inferred from the data" is met in this form: the tenant travels explicitly (header,
    request, token) where the caller has it; where a platform loop picks an item, the item's own row is the authority.
* The request tenant is bound by `TenantMiddleware` (token → home tenant, or `X-Tenant-Id` if the user is a member,
  else 403) before any route code runs, and it reaches every session the request opens.
* Platform rows (NULL tenant) are readable by tenants only for shared catalogs (knowledge packs). They stay private for
  `audit_event`, `model_call`, `decision`, `decision_outcome`.
* API paths follow the codebase: `/api/tenants/me`, `/api/admin/tenants`.

## 3. Build (original plan)
1. **Tenant model.** Add `tenant(id, slug, name, status, data_residency, kms_key_ref, settings)` and
   `tenant_member(tenant_id, user_id, role)` with roles `tenant_admin`, `tenant_auditor`, `member`.
   `workspace.tenant_id NOT NULL`.
2. **`tenant_id` everywhere.** Add it to every table that holds tenant data (all 100+ ORM classes except
   global catalogs such as `PlatformSetting(scope=global)`). Use one Alembic migration per batch of about 15 tables,
   backfilling to a `default` tenant. Use a composite index `(tenant_id, <existing leading key>)`.
3. **RLS.** For each tenant table:
   `ALTER TABLE t ENABLE ROW LEVEL SECURITY; ALTER TABLE t FORCE ROW LEVEL SECURITY;`
   `CREATE POLICY tenant_isolation ON t USING (tenant_id = current_setting('app.tenant_id', true)::uuid) WITH CHECK (same);`
   If `app.tenant_id` is unset, no rows match. That is the fail-closed behavior.
4. **Session binding.** A SQLAlchemy `after_begin` listener runs `SET LOCAL app.tenant_id = :t, app.principal = :p`
   from a contextvar set by the API dependency, by the Temporal activity interceptor (the tenant travels in workflow
   input and headers), or by the scheduler claim. Any statement without a bound tenant raises `TenantUnbound`
   (except migrate/admin roles).
5. **DB roles.** `app_migrator` (owner, DDL), `app_rw` (DML, subject to RLS, `NOBYPASSRLS`),
   `app_audit_writer` (INSERT on `audit_event` only), `app_readonly_ops` (SELECT for support, still RLS-bound),
   and per-source `query_reader_*` (S02). The API and workers connect as `app_rw`.
6. **Audit immutability.** `audit_event` gets `prev_hash` and `hash = sha256(prev_hash || canonical_json(row))` per tenant.
   A trigger raises on UPDATE and DELETE. A nightly job verifies the chain and exports JSONL to object storage (WORM bucket if available).
7. **ABAC.** Extend the policy decision input with `{classification, purpose, region, aggregation_grain, audience}`.
   It is evaluated on every tool call (`ToolExecutor` pre-hook) and every query (`QueryGateway`), not only at login.
   An optional OPA sidecar can be enabled through `policy.engine=opa`. The built-in engine is the default.
8. **Service principals.** OAuth2 client credentials for API and MCP clients (`McpClient` already exists).
   Tokens carry `tenant`, `workspaces[]` and `scopes[]`.
9. **Cache keys.** Every cache (`llm/cache.py`, query result cache, context cache) gains the prefix
   `tenant:{t}:scope:{scope_hash}:policy:{policy_version}`.

## 4. Interfaces
```
GET  /v2/tenants/me
POST /v2/admin/tenants                       (platform_admin)
POST /v2/tenants/{t}/workspaces
GET  /v2/tenants/{t}/audit?cursor=&type=     (tenant_auditor) → JSONL stream
POST /v2/tenants/{t}/audit/verify            → {ok, first_bad_event_id?}
```
Errors: `403 policy_denied {rule, reason, remedy}`, `404` for cross-tenant ids (never 403, to avoid leaking existence).

## 5. Invariants
* No code path may use `app_migrator` at runtime. A startup check fails if the role has `BYPASSRLS`.
* Background work carries its tenant explicitly; it is never inferred from the data.
* A model never sees tenant ids of other tenants, and policy text is never model-editable.

## 6. Acceptance
* **AT-05, AT-13.** `tests/security/test_rls_coverage.py` enumerates `information_schema.tables` and fails
  on any table that has a `tenant_id` column and lacks `relrowsecurity AND relforcerowsecurity`.
* `test_tenant_unbound_raises`: an ORM query with no bound tenant raises.
* `test_audit_tamper`: an UPDATE on `audit_event` raises, and a manual chain break is detected by `/audit/verify`.
* The existing workspace scope and SoD integration tests still pass.

## 7. Tasks
| ID | Task | Est |
|---|---|---|
| S01-T1 | Tenant + member models, API, seed `default` | 1d |
| S01-T2 | Migrations adding `tenant_id` + backfill (batched) | 3d |
| S01-T3 | RLS policies + session binding + role split + startup checks | 3d |
| S01-T4 | Temporal / scheduler tenant propagation interceptors | 1d |
| S01-T5 | Audit hash chain, trigger, verify, export | 1.5d |
| S01-T6 | ABAC attributes in policy decisions + OPA adapter (flagged) | 2d |
| S01-T7 | Cache key prefixing + negative cross-tenant suite | 1.5d |

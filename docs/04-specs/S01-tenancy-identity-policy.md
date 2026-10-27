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

## 2a. As built (2026-10-27, tasks T1–T7)
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
* **T5, audit hash chain** (`governance/audit_chain.py`, migration `0058_audit_chain`):
  * A separate append-only table `audit_chain(event_id, chain, seq, prev_hash, hash)` seals `audit_event` rows,
    one chain per tenant (`platform` for tenant-less rows): `hash = sha256(prev_hash + "
" + canonical_json(event))`,
    genesis 64 zeros. This differs from the plan's `prev_hash`/`hash` columns on `audit_event`: chaining inside every
    audited transaction would serialize a tenant's writes on one lock, and `audit_event` stays strictly immutable.
  * Sealing is asynchronous: every scheduler iteration seals rows created within an hour before each chain's head;
    the nightly pass seals everything (late commits), verifies every chain and exports new links. Verify and
    export seal first. Sealers are serialized per chain by an advisory lock; batches of 2,000 rows.
  * Only the sealer (system scope) writes the chain: the application role has SELECT only, and UPDATE/DELETE are
    refused by the append-only trigger. The chain row's tenant comes from its event, so tenants read only their chain.
  * `verify` streams the chain and names the first bad link: `bad_genesis`, `gap`, `broken_link`, `missing_event`,
    `wrong_chain` or `altered`; it also counts events left unsealed past the window. A chain pruned by retention
    starts at its first remaining link (`anchored_at`).
  * Export: gzipped JSONL per chain under `ANALYSTOS_AUDIT_EXPORT_DIR/<chain>/<chain>-<from>-<to>.jsonl.gz`, never
    overwritten; each export and each verification is itself audited (with the file SHA-256 and head hash), so the
    chain anchors its exports. Each line verifies offline without AnalystOS code.
  * API: `GET /api/tenants/{t}/audit?after_seq=&limit=&action=` (JSON lines, `X-Next-After-Seq`) and
    `POST /api/tenants/{t}/audit/verify`, for the tenant's `tenant_admin`/`tenant_auditor` or a platform admin, for
    the tenant the request acts for (another id is 404). CLI: `analystos audit seal|verify|export [--tenant T | --platform]`
    (verify exits 4 on a broken chain).
* **T6, ABAC on every call** (`governance/abac.py`, `contracts/policy.py` `AbacAttributes`/`AbacRule`/`AbacScope`/
  `PolicyInput`):
  * The decision input is `PolicyInput`: identity facts (tenant, workspace, user, role, agent, tool, source, assets,
    data residency, policy version) plus `{classification, purpose, region, aggregation_grain, audience}`, all derived
    by code. `classification` is the highest class of what the call can read (column tags: `public`; untagged =
    `internal`; `pii`/`confidential`/`sensitive` = `confidential`; `restricted`); denied columns do not count.
    `region` is the user attribute `region` (administrator or IdP mapped), unknown = None. `aggregation_grain` is
    syntactic on the outermost SELECT (`aggregate` when it groups or only aggregates, else `row`; unparseable = `row`).
    `audience` is `external` for publish/export/notify_external and external-write tools, else `workspace`.
    `purpose` is the identity's purpose (tool calls) or the gateway purpose (queries); a rule value also matches the
    purpose class (`monitor` matches `monitor.freshness`).
  * Decision points: `policy.evaluate` (so `ToolRuntime.authorize`, the tool pre-hook the spec calls `ToolExecutor`,
    and run, publish, build, MCP tool and monitor decisions), after the role and autonomy checks; and
    `QueryGateway.execute` for every statement (federated statements once; their legs are not decided again),
    after validation and before the result cache, so a cached result is never a way around a denial.
    `explain` reads no rows and is not ABAC-checked.
  * `resolve_scope` attaches `DataScope.abac` (region, residency = workspace policy else `tenant.data_residency`,
    the class of each readable non-internal column, the workspace rules). It is excluded from the scope hash, so plan
    hashes and approvals do not change.
  * Rules only deny. Platform rules (always on): `platform.data_residency` (known region outside the residency;
    `eu` covers `eu-west-1`) and `platform.restricted_external` (restricted data to an external audience).
    Workspace rules: `WorkspacePolicyDoc.abac_rules`, each `{id, when: [{attribute, op in|not_in|at_least|at_most,
    values}], applies_to: [tool, query, action], reason, remedy}`, versioned with the policy. An unknown region is
    never `in` a list and always `not_in` it, so "EU only" rules fail closed.
  * `ANALYSTOS_POLICY_ENGINE=opa` (default `builtin`): after the built-in engine allows, the adapter posts
    `{"input": PolicyInput + workspace_rules}` to `ANALYSTOS_POLICY_OPA_URL/v1/data/<ANALYSTOS_POLICY_OPA_DECISION_PATH>`
    (default `analystos/abac/decision`, timeout `ANALYSTOS_POLICY_OPA_TIMEOUT_SECONDS`, 1 s) and expects
    `{allow: bool, rule?, reason?, remedy?}` (a bare boolean is accepted). OPA can only add a denial. Unreachable,
    HTTP error, timeout or a non-boolean `allow` is a deny (`opa.unavailable`, `opa.invalid_decision`). No decision
    cache. The setting is an environment setting, not the `policy.engine` runtime setting the plan named.
  * Errors: `403 policy_denied` with `details {rule, reason, remedy, engine}` from the tool gate, the gateway and the
    run, publish and build paths (`policy.denied`); the denial is recorded on `tool_execution.decision.abac`, the
    `policy.denied` event (`rule`) and, for queries, a `rejected` `query_execution` row.
* **T7, tenant-scoped cache keys** (`tenancy/cache_keys.py`): every tenant-data cache key carries
  `tenant:{t}:scope:{scope_hash}:policy:{policy_version}`:
  * query result cache `analystos:qcache:<prefix>:<sha>` (tenant = the source's workspace tenant, scope hash and policy
    version of the DataScope); model response cache L0 `aos:llm:<prefix>:<workspace>:<purpose>:<hash>`
    (`CallContext.tenant_id/scope_hash/policy_version`, filled by run steps, Ask, the Request Desk and
    `workspace_call_ctx`); context cache `aos:ctx:<prefix>:<workspace>:<kind>:<digest>` for knowledge retrieval and
    compiled contexts (`context.cache.scope_of(ctx)`). The retrieval key had no scope before; `load_knowledge` filters
    by the scope, so a narrower scope could reuse a wider one's retrieval. It now cannot.
  * The tenant segment is the bound tenant; unbound (system scope, unit tests, `bound`/`off` modes) it is the owner
    tenant passed in (the workspace's), else `-`. A bound tenant that differs from the owner gets key `None`: no read,
    no write. Unbound entries are never served to a bound tenant.
  * Not prefixed, by design: pure functions of their input with no authorization in them (SQL semantics of verified
    queries, now bounded at 4,096; catalog text screening; lexicon), platform catalogues (playbooks, capability
    manifests, platform settings, decision calibration), and the system lookups keyed by globally unique ids
    (`tenancy/resolve.py` user tenants, `tenancy/background.py` row owners, `services/model_config.py` workspace
    layers). Counter hashes (`aos:ctx:stats`, budget counters) hold counts, not data.
  * Entries written before this change are unreachable (old key shapes) and expire with their TTLs.
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

# Internal security self-check, 2026-10-30 (prep for S12-T8)

**This is not a pen test.** It was run by the platform team on a local install, with an automated scanner and a code
review against the project's own rules. S12-T8 (customer IdP + independent pen test and threat-model review) stays
**External**; see [`../s12-t8-idp-pentest-handoff.md`](../s12-t8-idp-pentest-handoff.md) §4.

Base: `main` = `develop` = `6e7eff9`. Branch: `claude/security-self-check-deffb7`.

## 1. Scope

| Part | What |
|---|---|
| Code review | Every commit merged since 2026-10-28 (`git log --since=2026-10-28`; `cd173d6..6e7eff9`, 38 non-merge commits, ~25k lines in `src/`, `web/src`, `.github`, `config`) |
| Areas | conversational dashboards and reports (`dashboards/`, `reports/`, `services/report_docs.py`, `services/stories.py`); drill and cross-filter (`dashboards/interact.py`, `api/routers/dashboards_v2.py`); dashboard collections and grouping (`dashboards/collections.py`); Ask follow-ups, metrics and periods (`agents/ask_followup.py`, `ask_metrics.py`, `ask_period.py`, `services/ask.py`, `services/periods.py`); context federation (`context/federation.py`, `api/routers/context_v2.py`); Trino and SQL Server connectors (`engines/trino.py`, `connectors/generic_sql.py`, `connectors/kinds.py`, gateway federation); playbook editor and signing (`autonomy/playbook_editor.py`, `playbook_signing.py`, `toolreg/signing.py`); LangGraph backend and run leases (`workflows/langgraph_backend.py`, `driver.py`, `compute_pool.py`); release/deploy workflows; pilot reviews and meaning review (`services/investigation_value.py`, `services/meaning_review.py`, `api/routers/context_grounding.py`) |
| Dynamic | OWASP ZAP `ghcr.io/zaproxy/zaproxy:stable` (throwaway containers, removed after): API scan from `/openapi.json` (459 paths, 523 operations, 628 imported URLs) authenticated as the seeded analyst; web baseline (spider + passive) against the built SPA |
| Dependencies | `pip-audit` over the resolved `.[standard,otel]` install (what CI audits) and over the dev venv (adds dev, trino, sqlserver, langgraph, mcp extras); `npm audit` prod and dev |

Out of scope (the external test covers them): a real IdP, a cluster (ingress TLS/HSTS, NetworkPolicies, gVisor
runtime class, egress allowlist), sandbox escape, the adversarial SQL corpus beyond what CI already runs, other tenants'
data on a staging install, denial of service.

## 2. Method

* **Review checklist:** CLAUDE.md rules 3–7 and `.claude/agents/governance-reviewer.md` (SQL outside
  `QueryGateway.execute`; model output reaching SQL, approvals, publishing or a published number without the
  deterministic path; side effects without `request_approval` + `verify_for_execution`; scope widening; secrets in
  code, logs or prompts; model calls outside `llm/router.py`), plus injection, authorization and tenant isolation,
  approval bypass/replay, SSRF in connectors, path traversal in exports/files, XSS in rendered reports, CI script
  injection.
* **Local stack for the scan:** API from this worktree (`uvicorn analystos.api.app:app`, port 18731) on a fresh control
  database `analystos_seccheck` on the compose Postgres (`analystos migrate` + `analystos seed` from this worktree);
  analytics URLs pointed at a database that does not exist, Superset and the mock at a closed port, no model keys
  (no model calls), lite profile, local orchestrator. Web: `npm run build`, served by `vite preview` (port 18732), then
  with the exact header set of `web/nginx.conf` (port 18733) for the re-test. Nothing in the compose stack was scanned,
  stopped or changed.
* **ZAP API scan:** `zap-api-scan.py -f openapi`, `ZAP_AUTH_HEADER=Authorization` with the analyst's bearer token (all
  requests authenticated), active scan capped at 40 minutes (`scanner.maxScanDurationInMins=40`), 4 threads.
* **ZAP web baseline:** `zap-baseline.py -m 3`, Host header rewritten to `127.0.0.1` (Vite refuses other hosts).
  The SPA keeps its token in the browser, so the baseline is unauthenticated by nature; the authenticated surface is the
  API scan.

## 3. Findings

Severity: High = exploitable by a signed-in user against another user or tenant, or a governance bypass; Medium =
needs another flaw or an insider with repository write; Low = hardening; Info = noted only.

### 3.1 Code review

No High finding. No path where model output reaches SQL, approvals, publishing or a published number without the
deterministic checks, no SQL outside the gateway, no approval executed without `verify_for_execution` immediately
before the side effect, no cross-tenant read, no SSRF or path traversal in the changed code.

| # | Severity | Area | Finding | Outcome |
|---|---|---|---|---|
| CR-1 | Medium | `deploy.yml` | The cosign signer identity accepted `release.yml` at **any** ref. `release.yml` can be dispatched from a branch and signs with the same identity, so a version released by hand from an unreviewed branch passed signature verification for a manual `production` deploy (the Environment's reviewers were the only gate). | **Fixed:** for `production` the identity must be `release.yml@refs/tags/v…`; staging unchanged. Test `test_production_deploys_only_images_signed_by_a_tagged_release`; runbook 14 §3 |
| CR-2 | Low | `deploy.yml` | `${{ inputs.environment }}` was expanded inside two `run:` scripts. Values are a choice list or a constant today, so not exploitable, but it is the script-injection pattern. | **Fixed:** passed through `env: DEPLOY_ENV`. Test `test_release_and_deploy_scripts_read_inputs_from_the_environment_never_inline` (release.yml and deploy.yml) |
| CR-3 | Low | all workflows | Third-party actions are pinned to tags (`@v4`, `@v0`), not commit SHAs, in jobs holding `packages: write` and `id-token: write`. Already a known gap of S12-T2. | **Accepted** (open): pin by SHA when CI can run again (GitHub Actions blocked by billing, so a re-pin cannot be proven green now) |
| CR-4 | Info | reports | A report built from a dashboard chart copies the chart's stored preview rows, which every workspace viewer of that dashboard already sees (pre-existing S08 behaviour: a dashboard preview is published to the workspace). Reports add no wider audience: content is resolved with the reader's rights (`report_docs.resolver`), exports need an approval bound to the requester and the snapshot hash. | Noted for the tester (column-restricted viewer + dashboard preview) |
| CR-5 | Info | `context/feed` | The REST federation feed is open to any workspace viewer (not only owners). It returns only shareable entries (trust, `internal` classification, the caller's scope and denied columns), i.e. what the caller can already read in the ledger. | Accepted by design |

Checked and clean (with where): drill/cross-filter predicates are sqlglot nodes with `sqlbuild` literals, fields must be
categorical fields bound by a chart and values known to the vocabulary, columns are checked against the viewer's scope
and denied columns, and every statement runs through `QueryGateway.execute` with the viewer's scope (`interact.py`
`validate`, `pushdown`, `detail_sql`, `run`). Collections and grouping re-derive the payload from stored state and call
`verify_for_execution` right before the BI call, single use (`collections.py`). Report exports: approval must be an
export approval for this artifact, in this workspace, requested by the caller, bound to the snapshot hash; file names are
`Identifier`-constrained; rendered HTML escapes every value and previews render in `sandbox=""` iframes. Ask follow-ups
narrow with rules only (sqlglot + literals) or recompile the governed `SemanticQuery`; Ask threads stay private
(`_thread_for`, pilot `ask` subjects). Trino: every connection is a READ ONLY transaction (fails closed), three-part
names are refused by the validator and again by `qualify_catalogs`, cross-cluster or cross-identity federation is
refused. Playbook signing: `playbook.sign` is in `ALWAYS_SEPARATE_DUTIES`, the approval binds the content hash and
simulation digests, `verify_definition` checks purpose/tenant/key/content hash and the ed25519 signature; YAML is
`safe_load` with a 256 KiB cap; tool manifests forbid extra fields, so a signature cannot cross kinds. Run driver and
LangGraph: lease bookkeeping is `@platform_loop`, each drive is `@owned_by("analysis_run")`, every node binds the run's
tenant (`as_owner_of`), pool threads get `carry_context`. Context federation re-binds to the source tenant inside
`system_scope()` only to set it, requires owner on both sides, one hop, copies are data. Model uses added in this
window only rank known candidates or word sentences that must pass `numbers_guard`. New log lines carry class names,
ids or counts, not connection strings or secrets.

### 3.2 ZAP web baseline (SPA)

First run (built SPA under `vite preview`; production nginx then sent only `nosniff` and `Referrer-Policy`, and only
on `/`): 0 failures, 8 warnings.

| Alert | Severity (ZAP) | Outcome |
|---|---|---|
| CSP header not set [10038] | Medium | **Fixed**: CSP on every web location (`web/nginx.conf`, Helm web config): scripts only from the origin (no inline, no eval), `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`, `frame-ancestors 'none'` |
| Missing anti-clickjacking header [10020] | Medium | **Fixed**: `X-Frame-Options: DENY` + `frame-ancestors 'none'` |
| X-Content-Type-Options missing [10021] | Low | **Fixed**: `nosniff` on `/assets/` too (nginx dropped the server-level headers in locations with their own `add_header`) |
| Permissions-Policy not set [10063] | Low | **Fixed** |
| Cross-Origin-Embedder-Policy missing [90004] | Low | **Accepted**: `require-corp` would block the embedded Superset dashboards unless Superset sends CORP; `Cross-Origin-Opener-Policy: same-origin` added |
| Storable but non-cacheable [10049], Modern web application [10109], Suspicious comments [10027] | Info | **Accepted**: `index.html` is `no-cache` on purpose; the comment is in the minified React bundle |

After (re-test against the nginx header set, port 18733): the four fixed alerts are gone; remaining are 90004, 10049,
10109, 10027 (above) and a new **CSP: wildcard directive [10055]** for `frame-src 'self' https: http:`. **Accepted**:
the Superset origin is per deployment and the SPA embeds it; a deployment that wants it narrower edits the Helm web
config. Inline styles stay allowed because the report preview (a sandboxed `srcdoc` iframe) inherits the page's CSP and
carries its own stricter one.

The SPA was loaded under the new CSP in a browser: sign-in page rendered, no CSP violations in the console.

### 3.3 ZAP API scan (authenticated as the seeded analyst)

First run (code at `6e7eff9`): 0 failures, 3 warnings, 118 rules passed. The API answered 112,814 requests: 126 × 200,
945 × 401, 4,373 × 403, 42,313 × 404, 62,671 × 422, 1,460 × 428 (If-Match required), 3 × 500 (the two below). Most requests answered 404/422 because ZAP fills path ids with placeholders, so the active rules mostly
exercised validation, authentication and routing; object-level authorization needs real ids and two users (the
external test's two-tenant accounts).

| Alert | Severity (ZAP) | Outcome |
|---|---|---|
| Server error [100000]: `POST /api/workspaces` with an over-long `name` reached the database (`StringDataRightTruncation`, 500) | Low | **Fixed**: `WorkspaceIn.name` is 1–200 characters (the column), 422 at the boundary. Test `tests/unit/test_workspace_input_bounds.py` |
| Server error [100000]: `POST /api/workspaces/{ws}/playbook-editor/drafts` with YAML whose `id` is missing or not a key raised an uncaught `ValidationError` (500) | Low | **Fixed**: 422 `invalid_input` listing the problem. Test `test_a_draft_whose_manifest_id_is_no_key_is_refused_not_a_server_error` |
| X-Content-Type-Options missing [10021] | Low | **Fixed** with the API header middleware (section 4) |
| Cross-Origin-Resource-Policy missing [90004] | Low | **Fixed**: `Cross-Origin-Resource-Policy: same-origin` |
| Non-storable content [10049], authentication request identified [10111], client error responses [100000] | Info | Expected (login, OIDC callback, placeholder ids) |

No 500 response carried a stack trace or SQL to the client (Starlette's plain `Internal Server Error`); the traceback
stayed in the server log.

Triaged while reading the scan's successful responses (pre-existing, not changed; for the tester):

* `GET /api/users` lists every user of the caller's tenant (id, email, name, admin flag) to any signed-in user. Row-level
  security on `app_user` limits it to the current tenant and its members, so it is an in-tenant directory, not a
  cross-tenant read. **Info**.
* `GET /api/admin/models` (routing view) answers any signed-in user: model names, provider base URLs, modes, reason
  codes; no keys or key names. **Info** (the Ask and model-status screens read it).
* Any signed-in user may create a workspace and becomes its owner (pre-existing design: no data until an admin-
  registered source is attached). **Info**.

Re-test (fixed code, API restarted, same authenticated scan, active scan capped at 20 minutes): **0 failures, 0
warnings, 120 rules passed**; 115,219 requests, no 5xx and no traceback in the server log. Remaining alerts are
informational only (client errors for placeholder ids, the login request, non-storable auth responses).

### 3.4 Dependencies

| Audit | Before | After |
|---|---|---|
| `pip-audit` (resolved `.[standard,otel]`, 96 packages) | 0 known vulnerabilities | 0 |
| `pip-audit` (dev venv, 134 packages incl. trino, pymssql, langgraph, mcp) | 0 | 0 |
| `npm audit --omit=dev` | 0 | 0 |
| `npm audit` (dev tooling) | 2 high (`vite` 8.3.1: launch-editor command injection on Windows, cross-origin script execution through dev middleware, `server.fs` bypasses; `source-map-js` 1.2.1 DoS), 2 moderate (`vitest` / `@vitest/mocker` 5.0.1) | **0**: `npm audit fix` (lockfile only, semver-compatible): vite 8.3.5, vitest 5.0.5, rolldown 1.2.16, postcss 8.5.32; no `package.json` change |

The dev findings affect developer machines running `npm run dev`, not the shipped image (nginx serves the static build).

## 4. Fixes and regression tests

| Fix | Files | Test |
|---|---|---|
| API hardening headers on every response (errors and streams included; a route's own header wins): `nosniff`, `X-Frame-Options: DENY`, CSP `default-src 'none'; frame-ancestors 'none'` (docs: anti-framing only), `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy`, `Cache-Control: no-store` unless set | `src/analystos/api/security_headers.py`, `api/app.py` | `tests/unit/test_security_headers.py` (3 API tests) |
| Web hardening headers on every location that sets headers, `always` | `web/nginx.conf`, `deploy/helm/analystos/templates/scheduler-web.yaml` | `tests/unit/test_security_headers.py` (both configs) |
| Bad input answered 422 instead of 500: workspace name 1–200 characters; playbook draft with an invalid manifest id | `src/analystos/api/routers/workspaces.py`, `api/routers/playbook_editor.py` | `tests/unit/test_workspace_input_bounds.py`, `test_playbook_authoring.py` (1 new) |
| Production deploys only tag-signed images; no `${{ inputs }}` in scripts | `.github/workflows/deploy.yml`, runbook 14 | `tests/unit/test_supply_chain.py` (2 new) |
| Dev tooling CVEs | `web/package-lock.json` | `npm audit` 0; web typecheck, build, 516 vitest tests |

## 5. Accepted risks (hand these to the tester)

1. Actions pinned to tags, not SHAs (CR-3).
2. No COEP on the web (Superset embedding); CSP `frame-src` allows any http(s) origin (Superset per deployment).
3. Inline styles allowed in the web CSP (report preview inherits it; scripts stay strict).
4. HSTS is not sent by the app; it belongs to the TLS-terminating ingress (not exercised here).
5. Stored dashboard previews are visible to every workspace viewer (CR-4).
6. In-tenant user directory (`GET /api/users`), the model routing view to every signed-in user, and workspace creation
   by any signed-in user (section 3.3).
7. Object-level authorization was not exercised dynamically (placeholder ids); the code review covered the new routes'
   `load_in_workspace` / `require_role` checks only.

## 6. Results

| Check | Result |
|---|---|
| Python fast suite (`pytest -m "not integration"`, worktree code via `PYTHONPATH`, Windows) | 5261 passed, 32 skipped, 1 failed: `test_compute_pool.py::test_warm_up_starts_children_before_the_first_task` (`WinError 6` in Windows multiprocessing while the ZAP scan loaded the host); it passes 3/3 on its own and the change does not touch it |
| New and touched tests | `test_security_headers.py`, `test_workspace_input_bounds.py`, `test_playbook_authoring.py`, `test_supply_chain.py`, `test_request_id.py`: all pass |
| `ruff check src tests` | clean |
| Web | `tsc --noEmit` clean, `vite build` ok on vite 8.3.5, `vitest run` 516 passed, 1 skipped |
| ZAP | API: 3 warnings → 0; web: CSP, anti-clickjacking, nosniff, Permissions-Policy fixed; rest accepted above |
| Audits | pip-audit 0 / 0; npm audit prod 0, dev 4 → 0 |

ZAP reports (HTML/JSON/Markdown) were kept outside the repository with the session's scratch files; the throwaway
containers were removed and the `analystos_seccheck` database dropped after the run. Not run: `lint-imports` (not
installed in the shared venv; the new module imports only `typing`), integration tests, GitHub Actions (blocked by
billing).

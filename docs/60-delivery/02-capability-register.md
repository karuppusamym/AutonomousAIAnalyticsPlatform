# Capability register (evidence)

## 2026-11-05 — `context.lookup` and usage as a ranking signal (GKC-P0-2, GKC-P1-4)

From the Google Knowledge Catalog review (`docs/70-reviews/2026-11-05-google-knowledge-catalog-review.md`).

| Capability | Code | Evidence |
|---|---|---|
| `POST /api/v2/context/lookup {workspace, assets[≤10], format: yaml\|json, budget_chars}`: one deterministic block per call. Fill order identity+keys → curated description + guidelines → validated joins between the requested assets (with join SQL) → glossary/metrics/verified queries → quality → profile → usage. Identities over budget are refused (422), later sections left out with omissions | `context/lookup.py`, `api/routers/context_v2.py` (audited `context.looked_up`) | `tests/unit/test_context_lookup.py::test_yaml_contract_is_sorted_deterministic_and_in_fill_order`, `test_a_small_budget_keeps_keys_and_joins_and_records_omissions`, `test_identities_over_budget_are_refused_not_trimmed` |
| Scope: same `access_for` as search/compile; an out-of-scope or unknown asset is absent and not counted; denied columns never named; profile values only for untagged columns from stored vocabulary; quality value-free (no message text) | `context/lookup.py` | `test_an_out_of_scope_asset_is_absent_and_not_counted`, `test_denied_columns_and_values_never_appear`, `test_profile_values_are_withheld_for_a_sensitive_column_the_caller_may_see` |
| MCP tool `context.lookup` (read-only; granted by `context.lookup` or `context.read`) | `mcp/server.py`, `mcp/grants.py`, `web/src/lib/toolRegistry.ts` | `test_mcp_tool_is_a_read_only_context_grant` (handler called directly; the `mcp` extra is not installed on this dev host, so the HTTP transport was not run) |
| `guidelines` on an asset: written by a person only (a service account is refused), kept across crawls, in the table's OKF document and the Catalog editor | `SourceAsset.semantics["guidelines"]` (no migration), `api/routers/catalog.py`, `services/crawler.KEPT_SEMANTICS`, `knowledge/crawl_docs.py`, `web/src/pages/Catalog.tsx` | `test_guidelines_are_written_by_people_only_and_survive_a_crawl`, `test_table_document_carries_the_guidelines`, `increment3.test.tsx` "shows each join's origin…" |
| Usage factor in the ledger rerank (trust × freshness × in-scope × usage): distinct users and runs over 30 days from audited `query_execution`, platform statements excluded, bounded to [1.0, 1.10]; common filter columns by operator class (no literals) in the lookup block | `context/usage.py`, `context/retrieval.rank/search` | `test_usage_factor_is_bounded_and_neutral_without_usage`, `test_asset_usage_counts_distinct_users_and_runs_in_the_window_only`, `test_rerank_lifts_the_used_table_but_never_beats_a_clearly_better_match` |
| Usage benchmark configuration `ledger_rank` vs `ledger_rank_usage` | `context/benchmark.usage_run`, `tests/fixtures/context/usage-benchmark.json` | `test_usage_benchmark_configuration_measures_the_gain`: on the fixture, tie questions recall@1 0.0 → 1.0, control questions 1.0 → 1.0. The fixture is synthetic: it shows the factor breaks ties without overriding relevance, not a gain on a customer corpus |
| Catalog shows each join's origin, confidence, validated state and the validated join SQL | `GET /api/workspaces/{ws}/relationships` (`join_sql`), `skills/relationships.join_sql`, `Catalog.tsx` `AssetRelationships` | `increment3.test.tsx`, `test_join_sql_is_value_free_and_dialect_aware` |

Not run: a live call on the compose stack. OpenAPI regenerated (`web/openapi.json`, `web/src/generated/openapi.ts`).
## 2026-11-05 — Declared quality rules and data-product contracts (GKC-P1-7, GKC-P2-9)

From the Google Knowledge Catalog review (`docs/70-reviews/2026-11-05-google-knowledge-catalog-review.md`, P1-7 and
P2-9). Migration `0080_quality_rules` (renumbered at the integration merge; follows `0079_mcp_grant_slice`).

| Capability | Code path | Evidence (2026-11-05) |
|---|---|---|
| An owner declares a quality rule on a catalog asset or column: not_null, unique, accepted_values, range (the recipe gate types), regex, statistic range, owner SQL row condition or aggregate; each with a pass threshold, dimension and severity | `contracts/quality.py`, `quality/service.py::declare`, `POST /api/workspaces/{ws}/quality-rules` | `tests/unit/test_quality_rules.py::test_rule_contract_shapes`, `test_declare_validates_against_the_catalog_and_is_idempotent`, `test_named_source_owners_decide_their_assets_rules` |
| Rules render to one read-only aggregate statement through `skills/sqlbuild`, run through `QueryGateway.execute` in the runner's scope; owner SQL is refused unless it is an expression over the asset's own columns; a refused statement is an error, never a pass | `skills/quality_rules.py`, `quality/service.py::run_rules` | `test_rules_compile_run_and_judge_on_duckdb` (13 rule shapes on DuckDB), `test_rule_sql_is_one_aggregate_statement_in_every_dialect`, `test_owner_sql_is_an_expression_over_the_asset_only`, `test_a_refused_statement_is_an_error_never_a_pass`, `test_rule_on_an_asset_outside_the_runner_scope_is_an_error` |
| Persisted with tenant isolation | `quality/models.py`, `migrations/versions/0080_quality_rules.py` | `tests/unit/test_tenancy_rls_coverage.py::test_quality_rule_tables_are_owned_by_their_workspace`; on a throwaway Postgres 16 (pgvector/pg16): `alembic upgrade head` from empty to 0077, both tables with RLS on, `tenant_isolation` policy, `tenancy_fill` trigger, `tenant_id NOT NULL`; downgrade to 0076 drops them, re-upgrade restores |
| Recommendations from the stored profile land as review drafts; an owner approves (or edits) them; never a literal on a PII column | `quality/recommend.py`, `knowledge/suggestions.py` kind `quality_rule` | `test_profile_recommendations_land_as_drafts_and_an_owner_approves` |
| Monitors run approved rules on schedule and alert on failing warning/critical rules | `quality/monitoring.py`, `services/monitors.py` kind `quality_rules` | `test_quality_rules_monitor_alerts_on_failing_rules` |
| Pass rate as a trust signal on the asset (API + Catalog) | `quality/service.py::asset_trust`, `api/routers/catalog.py` (`quality`), `web/src/components/QualityRules.tsx` | `test_run_rules_through_the_gateway_records_counts_and_trust`, `web/src/test/qualityRules.test.tsx` |
| Rules as code: OKF v0.2 export, import as drafts, identical bytes after a round trip | `quality/okf_rules.py`, `GET/POST /api/workspaces/{ws}/quality-rules/{export,import}` | `test_rules_round_trip_through_okf` |
| A data product's signed `contract` block (freshness SLA, rule refs), validated at publish; old manifests still verify | `contracts/tool_manifest.py`, `toolreg/publish.py`, `quality/contracts.py` | `tests/unit/test_data_product_contracts.py::test_contract_block_is_signed_and_absent_blocks_hash_as_before`, `test_publish_validates_and_signs_the_contract`; `tests/unit/test_tool_registry.py` unchanged and passing |
| Contract breaches judged without a query, shown on the product, alerted by a `data_contract` monitor | `quality/contracts.py::contract_status`, `GET /v2/tools/{id}/contract`, `web/src/components/ProductContract.tsx` | `test_contract_status_and_monitor_report_breaches`, `web/src/test/qualityRules.test.tsx` |
| Consumer access request = approval bound to (product, version, requester, role); a person decides, code applies the role or MCP grant after `verify_for_execution`, once | `toolreg/access.py`, `governance/approvals.py` (`data_product.access` always separate duties; `requester_min_role`) | `test_access_request_is_decided_by_a_person_and_applied_by_code`, `test_separation_of_duties_and_binding`, `test_a_new_product_version_invalidates_an_open_request`, `test_mcp_grant_request_adds_the_product_to_the_clients_grant` |

Live: `tests/integration/test_quality_rules_live.py` 2 passed (2026-11-05) on a throwaway Postgres 16 with strict
tenancy: rules declared over HTTP on a staged parquet table ran through the real `QueryGateway` (query ids recorded;
accepted_values 199/200), the catalog showed `quality.level = at_risk`, the OKF export zip held 4 rule documents; a
verified query published with a contract showed `ok`, a viewer's access request was refused to the requester and
applied by the approver (viewer → analyst). Without Postgres the file skips cleanly. Not verified: Redis, Temporal and
the scheduler were not running (rules ran on request, not on a schedule tick); no live browser check of the two panels. The context lookup
(`context/lookup.py`, another branch) does not yet read rule pass rates; `quality.service.asset_trust` is the call.

## 2026-10-30 — Registry screens fixed; MCP endpoint shipped; prompts editable (REG-FIX-01/02, PROMPT-01)

Found in the browser on the redeployed stack (Settings → Capability registry):

| Screen | What was wrong | Fix | Test |
|---|---|---|---|
| Data tools, MCP servers | "Error: Not Found" on tools, signing keys and the review queue: the browser calls `/api/v2/tools…`, the router answered only `/v2/tools…` | `api/app.py`: `tools_v2` also mounted under `/api` (`include_in_schema=False`) | `test_web_reaches_v2_routes.py` (fails without the fix) |
| MCP clients | Tokens offered for `/mcp`, but the images had no `mcp` extra, so the endpoint did not exist | `mcp` in `standard` and the compose API extras; `checks.mcp` in `/api/health`; a warning on the screen when it is off | `test_slim_install.py` |
| Prompts | Read-only | Editable by administrators through versioned platform settings, validated against the built-in template | `test_prompt_overrides.py`, `increment3.test.tsx` |

Whether these features are needed: data tools publish certified metrics and verified queries as signed, typed tools
that agents and outside callers use without writing SQL (S10); MCP clients let outside AI assistants (for example a
desktop assistant) use those tools as a governed service identity; MCP servers admit outside tools under quarantine.
All three stay administrator screens. Local results: Python fast suite, ruff, web typecheck, 517 vitest, build and
110 Playwright tests pass.

## 2026-10-30 — Redeploy on Docker compose and live e2e re-run (V2-E2E-1)

The full compose stack (profiles standard, bi, demo: API, worker, scheduler, Temporal, Redis, Superset 6.1.0, ServiceNow
mock, web) was rebuilt from `main` and redeployed on the Windows dev host. Host ports 5432 and 8090 belong to other
software here, so a local, uncommitted override published Postgres on 127.0.0.1:15432 and the mock on 127.0.0.1:8091.

| Finding | Fix | Evidence |
|---|---|---|
| Temporal listened on the `isolated` network address only; the API and worker on `default` were refused, the worker restart-looped | `compose.yaml`: `BIND_ON_IP=0.0.0.0`, `TEMPORAL_BROADCAST_ADDRESS=127.0.0.1` (commit 22e8a7c) | `/api/health` temporal, worker ok |
| Superset could not decrypt stored connection passwords: its generated secret key was new, the metadata DB was older | metadata DB backed up (`pg_dump`), the 10 undecryptable `dbs.password` / `encrypted_extra` values cleared; the publisher re-sets a workspace's connection on its next publish | Superset healthy; 37 dashboards kept |
| Every new workspace's first publish failed: Superset's database create took 38 s (per-schema permission sync over ~530 analytics schemas), past the 30 s client timeout | `publishing/superset.py`: the create gets 180 s; a create that still times out adopts the connection Superset made (commit 7d4d871) | `tests/unit/test_publishing_superset.py` (2 new) |
| The e2e hung when the model proposed metrics: the run waits for the owner's meaning review, and the script never answered | `scripts/e2e_demo.py`: the owner defines each proposed metric from its named columns (flag rate, mean, coded priority, recurring share) and asks for a recheck; any other question stops the scenario | applied to the stalled run (5 metrics accepted, run resumed) |
| The script crashed printing its summary on a Windows console | UTF-8 stdout (commit 1caeac3) | |

Result: [`evidence/e2e-20261030-203355.md`](evidence/e2e-20261030-203355.md) (+ `.json`), **26/26**, 19 model calls,
USD 0.009, no Claude or Gemini. In this run the model proposed no metric needing review, so the owner step was not
exercised end to end; it was exercised on the stalled run above.

## 2026-10-30 — Owner decisions taken under delegation (ADR-016): model policy, JEV price, demo metrics, SLOs

The owner delegated these on 2026-10-30. They are recorded as the agent's decisions, to revisit:
[ADR-016](../03-decisions/adr-log.md).

| Capability | Code path | Evidence |
|---|---|---|
| Dev and pilot model policy (N-14) | Preset `pilot_low_cost` (`contracts/platform.py`: modes `auto`, `PILOT_LOW_COST_DISABLED`, $2/day, JEV on); `platform_settings.seed_dev_default` called by `analystos seed` when `env=dev` and nothing is stored | `tests/unit/test_pilot_low_cost_preset.py` (every `models.yaml` model decided, only low-cost open models and JEV enabled, no Claude/Gemini); `tests/integration/test_platform_settings.py::test_seed_stores_the_dev_default_once_and_never_overwrites_a_saved_choice`; fresh-database `analystos seed` (v1 stored, a second seed writes nothing) |
| JEV price (N-13), provisional | `config/models.yaml` `typesafe/jev-1.13` $0.15 / $0.60 per Mtok, `price_provisional: true` (`llm/config.ModelMeta`), `prices_version` 2026-10-30 | OpenRouter list read 2026-10-30 (jev-1.13 not listed); `test_a_priced_jev_passes_the_approval_threshold_instead_of_falling_back` |
| Approved metrics in the demo workspaces | `semantic` sections in `packs/itsm/demo.yaml`, `packs/itsm/process_mining_demo.yaml`, `packs/sales/retail_demo.yaml`; `demo/seed.ensure_semantic_model` / `--only metrics` (admin@ proposes, approver@ approves, through `/api/workspaces/{id}/semantic`) | `tests/unit/test_demo_semantic_seed.py` (specs pass `definition_problems`; C06/C04 questions match without a model); live on a clone of the rehearsal database: three models and 14 metrics approved, idempotent; readiness definitions pass |
| C06 / C04 re-walk | Ask governed rung (`agents/ask_metrics.py` → `semantic/compiler.py`); dashboard draft (`dashboards/drafts.py`) | [evidence/adr016-owner-decisions-20261030.md](evidence/adr016-owner-decisions-20261030.md): C06 `answered_by=semantic` from Late Delivery Rate v1, 0 model calls, numbers match; C04 KPIs and charts from the approved incident metrics, by-priority resolution hours match the answer key |
| SLO targets | `config/slo.yaml` header: provisionally accepted 2026-10-30, numbers unchanged, S12-T6 capacity finding recorded | — |

Not shown:
* a live model run on the new policy (the re-walk had no provider key, so it was rules only);
* a live JEV call at the provisional price;
* the shared `analystos` database seeded with the metrics or switched to the preset.

## 2026-10-30 — Internal security self-check before the external pen test (prep for S12-T8; not a pen test)

Evidence: [evidence/security-self-check-20261030.md](evidence/security-self-check-20261030.md). S12-T8 stays External.

| Capability | Code path | Evidence |
|---|---|---|
| API hardening headers | `api/security_headers.py` (pure ASGI, outermost but request id): `nosniff`, `X-Frame-Options: DENY`, CSP `default-src 'none'; frame-ancestors 'none'` (docs: anti-framing only), `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy`, `Cache-Control: no-store` unless the route set one | `tests/unit/test_security_headers.py`; ZAP API scan re-test 2026-10-30 |
| Web hardening headers | `web/nginx.conf`, Helm `templates/scheduler-web.yaml`: CSP (scripts from the origin only, no eval, `object-src 'none'`, `frame-ancestors 'none'`), `X-Frame-Options`, `nosniff` (now on `/assets/` too), `Permissions-Policy`, COOP, all `always` | `tests/unit/test_security_headers.py` (both configs); ZAP web baseline re-test; SPA loaded under the CSP with no violations |
| Input bounds that were 500s | `api/routers/workspaces.WorkspaceIn.name` 1–200; `api/routers/playbook_editor.create_draft` turns an invalid manifest id into 422 | `tests/unit/test_workspace_input_bounds.py`, `test_playbook_authoring.py::test_a_draft_whose_manifest_id_is_no_key_is_refused_not_a_server_error` |
| Production deploys only tag-signed images | `.github/workflows/deploy.yml` `SIGNER` (`release.yml@refs/tags/v` for production); inputs reach scripts through `env:` | `tests/unit/test_supply_chain.py` (2 new); runbook 14 §3 |
| Dependency audit | `pip-audit` (resolved `standard,otel`; dev venv), `npm audit` prod + dev | 0 known vulnerabilities after `web/package-lock.json` dev bumps (vite 8.3.5, vitest 5.0.5) |

Not shown: object-level authorization with real ids across two tenants (ZAP used placeholder ids), a cluster (TLS,
HSTS, NetworkPolicies, gVisor), a real IdP. Accepted risks are listed in the evidence file for the tester.

## 2026-10-30 — Meaning review that converges; readiness in plain words (PILOT-FIX-04)

From the [VALUE-05 rehearsal](evidence/value-05-pilot-rehearsal-20261030.md): card C11 took 3 meaning rounds and about 11
owner minutes, and missing return reasons blocked the run.

| Capability | Code path | Evidence |
|---|---|---|
| Ask once | `services/meaning_review.build`, called by `context_grounding.resolve`: review, rule, metric and model questions in one list, one per term (`key_of` merges plurals); the model's new `open_terms` (prompt `context_resolution.v1`) are checked against the catalog and authorised refs | `test_round_one_asks_every_open_term_once_and_turns_absent_data_into_a_limitation` |
| Resolve without asking | `catalog_fields`: a term naming one selected, non-identifier field resolves `by: catalog` (grouping only, never a metric or a rule, never a term a rule says needs a definition); `map_glossary_to_catalog`: a glossary term with no mapping takes the one field of its name (metrics and rules keep their review); `resolve_known` matches singular owner terms to plural questions | `test_plain_fields_resolve_from_the_catalog…`, `test_a_glossary_term_without_a_mapping…`, `test_a_field_never_stands_in…`, `test_an_owner_definition_answers_the_plural…` |
| Absent data is a limitation | `absent_limitation`: a "why" noun (reason, cause, satisfaction …) or a model's `absent` claim, accepted only when no selected field holds the head noun; proposed metrics with no fields for such a noun too. Carried across rounds; `finding_caveats` adds "Data limitation: …" to each finding (`agents/insight.py`), and so to reports | `test_a_model_cannot_turn_a_field_that_exists_into_a_limitation`, `test_a_proposed_metric_for_a_field_nobody_records…`, `test_finding_caveats_carry_only_absent_data` |
| Recheck asks only what changed | `record_round` (on `POST /grounding/runs/{id}/recheck`): artifact `Meaning review round N` with asked keys, binding revisions, owner minutes; the next round asks what is still open, every code-raised gate question (reviewed-definition state, rules such as the reporting period), and model terms an answer caused (changed binding, its wording); other new model questions are recorded as not reviewed | Unit `test_recheck_asks_only_what_an_answer_changed`, `test_a_conflict_an_answer_creates…`; Postgres `tests/integration/test_meaning_review_rounds.py` |
| Pinned to the run | `meaning_review.pinned` (binding id + revision) in the run's grounding; hypothesis mapping receipts unchanged; `GET /grounding/runs/{id}` returns `rounds` | Integration test above |
| UI | `ContextGrounding.tsx::MeaningReviewPanel`: "N meanings to confirm, all at once", "What the data cannot show", "Understood without asking", round and owner time; columns, ids and revisions behind Show expert tools | `web/src/test/meaningReview.test.tsx`; Playwright `web/e2e/meaning-readiness.spec.ts` (axe WCAG 2.1 AA, 390 px dark) |
| Readiness in plain words | `contracts/pilot.PilotCheck`: `action`, `who`, `screen`; `services/pilot.py` fills them; the API path or script stays in `remediation` (expert view only). `PilotReadiness.tsx::GapAction` links to Members & policy, Sources, Metrics or the scorecard | `test_every_gap_says_in_plain_words…`; web readiness tests |
| Readiness for analysts | `WorkspaceReadiness` on the workspace home (any role, read-only, collapsed): verdict and each gap with who does it; owner-only screens not linked for others | `web/src/test/meaningReview.test.tsx`; Playwright `web/e2e/meaning-readiness.spec.ts` (axe WCAG 2.1 AA, 390 px dark) |
| C11 re-walk | Rehearsal data copy, recorded model answers replayed, no model call | [evidence/pilot-fix-04-meaning-rewalk-20261030.md](evidence/pilot-fix-04-meaning-rewalk-20261030.md): 1 round, 1 owner definition (was 3 and 5), reasons recorded as a limitation |

Not shown: real owner time, a model call under the new `open_terms` prompt, and the re-walked run's findings.
`web/openapi.json` and the generated web types were regenerated for the new `PilotCheck` fields.

## 2026-10-30 — Orchestrator backends: seam, LangGraph driver, run-lease drivers (ORC-T1..T7, ADR-015)

| Capability | Code path | Evidence |
|---|---|---|
| Backend seam | `workflows/backends.py` (protocol, `Capabilities`, `validate`, `get_backend`), `orchestrator.py` `TemporalBackend` / `InProcessBackend`; capability reads in `api/app.py`, `core/profiles.py`, `workers/dispatch.py`; `ANALYSTOS_ORCHESTRATOR` a `Literal` | `tests/unit/test_orchestrator_backends.py` (12, incl. no name comparison left); `tests/unit/test_local_orchestrator.py` unchanged and green; `tests/integration/test_temporal_queues.py` 5/5 (2026-10-30) |
| One decision function | `runtime/loop.py::next_action` (local and langgraph); Temporal keeps its inline copy | `tests/conformance/orchestrator/test_next_action_parity.py`: 20 shapes x 3 drivers, AST coverage of `engine._decide` keys; a deliberate drift fails 4 cases |
| Conformance suite | `tests/conformance/orchestrator/` (`ConformanceEngine`), Temporal leg `tests/integration/test_orchestrator_conformance_temporal.py` | 106 passed (local + langgraph), Temporal 6/6 on a dev server (2026-10-30) |
| LangGraph backend | `workflows/langgraph_backend.py` (`LangGraphRuntime`: no checkpointer, fixed graph, `Send` on the shared pool, tenant per node, recursion bound + re-invoke, parks) | Conformance suite; live e2e 26/26 and restart drill 7/7 ([`evidence/orc-t6-t7-live-20261030.md`](evidence/orc-t6-t7-live-20261030.md)) |
| Packaging and guards | `langgraph` extra (1.2.x), `[tool.importlinter]` contract, `workflows/langgraph_guard.py` (tracing refused without `ANALYSTOS_LANGGRAPH_ALLOW_TRACING`; `tracing_context(enabled=False)`), `/api/health` `checks.orchestrator`, `profiles.summary()["orchestrator"]` | `lint-imports` 1 kept / 0 broken; `test_orchestrator_backends.py`; `test_slim_install.py` (core install has no langgraph; no image carries it) |
| Run-lease drivers | `workflows/driver.py`, `analystos driver`, `ANALYSTOS_RUN_DRIVER=process`, migration `0076_run_leases` | `tests/integration/test_run_driver_leases.py` 4/4 (two driver processes, kill + takeover, no overlap, fencing, migration); live takeover under the e2e (11 s, epoch 1 -> 2, run completed; e2e 25/26, publication timeout) |
| Operator docs | [runbook 04 §6](../30-runbooks/04-lite-and-profiles.md), README, CLAUDE.md, ADR-015 implementation note | `.env.example` not edited (unreadable from the building session); variables listed in runbook 04 §6 |

Checks on the final code (merged with develop 809a53e, Windows 11 dev host, 2026-10-30): fast suite 5111 passed /
106 skipped / 0 failed; ruff clean; `lint-imports` clean. Not run here: the worker conformance suite (POSIX-only),
CI (GitHub Actions blocked by billing).

Not done / follow-ups: the Superset publisher's fixed 30 s timeout on `POST /api/v1/database/` is too short when the
analytics database holds hundreds of schemas (Superset syncs a permission per schema); `.env.example` entries.

## 2026-10-29 — Release pipeline, kind proof install, gVisor compute pods (FND-004, S07-T1)

| Capability | Code path | Evidence |
|---|---|---|
| Release | `.github/workflows/release.yml`: tag `vX.Y.Z` or dispatch → app, web, mock, sandbox, superset built, SBOM + Grype gate (fixable high/critical stop the release before any push), pushed as `:<version>` and `:sha-<commit>`, cosign keyless sign + SPDX attestation by digest; chart packaged at the release version, pushed to `oci://ghcr.io/<owner>/charts`, signed; `release-manifest.yaml` with every digest | `tests/unit/test_supply_chain.py::test_release_publishes_signed_images_and_chart_then_deploys_through_a_gated_environment`; actionlint 1.7.12 + shellcheck 0.11.0 clean |
| Deploy | `.github/workflows/deploy.yml` (reusable + dispatch for redeploy/rollback): Environment `staging` (required reviewers, `KUBECONFIG` secret, `HELM_VALUES_FILES`), cosign verify against the release workflow identity, pods pinned to the verified digests, `helm upgrade --install --atomic --wait`, `/api/health` + web smoke test; `production` only with its own Environment and `PRODUCTION_DEPLOY=true` | `test_deploy_verifies_signatures_and_pins_digests_with_cluster_credentials_from_the_environment`; owner setup [14-release-and-deploy.md](../30-runbooks/14-release-and-deploy.md) |
| First install | `templates/migrate-job.yaml`: the pre-install migration Job brings its own hook ServiceAccount and ConfigMap (the release's do not exist yet), same `analystos.configData` | `test_the_migration_hook_brings_its_own_service_account_and_config`; the kind install that found the hang |
| Runtime requirement | `analystos.poolRequiredRuntime`: isolated pools under the gVisor class get `ANALYSTOS_WORKER_REQUIRE_RUNTIME=gvisor` (`workers.<pool>.requireRuntime` overrides) | `test_isolated_gvisor_pools_require_the_runtime_they_are_given` |
| Proof install | `deploy/kind/` (`proof.sh up/check/down`, `install-gvisor.sh`, `infra.yaml`), `values-kind.yaml` | Live run on kind 2026-10-29: packaged chart deployed, API health `ok`, web page and proxy answer; gVisor pods report the gVisor kernel, `check_runtime` accepts; forced onto runc the isolated worker exits 78. [evidence/fnd-004-s07-t1-kind-gvisor-20261029.md](evidence/fnd-004-s07-t1-kind-gvisor-20261029.md) |

Not verified: a run of either workflow on GitHub (Actions is blocked by account billing), a push to GHCR, and a deploy to
a real staging cluster; the production target is not configured (owner's choice).

## 2026-10-29 — Drill-down and cross-filtering in the dashboard preview, every re-query governed (CONV-06)

From the [conversational analyst gap review](../70-reviews/2026-10-29-conversational-analyst-gap-review.md), wave 2.
Spec: [S08 §10](../04-specs/S08-dashboards-publishing.md).

| Capability | Code path | Evidence |
|---|---|---|
| Cross-filter as a session view | `dashboards/interact.py::Explorer.view`, `POST /api/v2/dashboards/{id}/view`: field bound by a chart and categorical, value through `dashboards/env.py`; affected tiles re-queried as the viewer; unbound tiles `not_filtered` with the reason; the clicked chart `source`; no version | `tests/unit/test_dashboard_interact.py`; integration below |
| Governed re-query | Metric charts and KPIs: `SemanticQuery` + `SemanticFilter` → `semantic/compiler.compile_query` under the viewer's scope; other charts: `bundle.Resolver.sql` (verified query / gateway execution) wrapped with sqlglot nodes and `skills/sqlbuild` literals; every statement through `QueryGateway.execute` (purposes `dashboard.cross_filter`, `dashboard.drill`, `dashboard.detail`) | Unit tests run every statement through `gateway/validator.validate_sql` (denied columns, row filters) and then DuckDB; the integration test checks every `QueryExecution` row (actor, purpose, status) |
| Drill-down | A time axis grouped by `DATE_TRUNC`: `regrain` year → quarter → month → day within the clicked member; metric charts: the metric's approved dimensions in order; breadcrumbs back | Unit (month → day, year → quarter → month, metric dimension path); integration (a month's days sum to the month; priority → category equals pandas) |
| Rows behind a selection | `detail_sql`: the dataset's FROM and WHERE plus the member predicates, only readable columns (denied columns counted, never named), ≤100 rows; joins and set operations refused | Unit + integration (a viewer with row filter `region IN {{user.regions}}` sees only EMEA rows and no PII column) |
| Bounds (ADR-014) | ≤24 statements per request, identical statements once, ≤4 concurrent per dashboard (`gate_for`, a bounded map), `busy` after 15 s; previews ≤200 inline rows; gateway result cache; client debounce 250 ms, aborts the older request, LRU of 20 views | `test_concurrent_tile_queries_are_capped_and_identical_statements_run_once`, `test_a_dashboard_at_its_concurrency_limit_reports_busy`, `test_gates_are_per_dashboard_and_bounded`, `dashboardExplore.test.tsx` |
| Apply as dashboard filter | The server returns the typed `dashboard.add_filter` op (scope = charts with the exact field); the client posts it to `/ops` (editor, new version); `refresh_saved` re-queries tiles a saved value filter restricts | Unit (the op applies through the ops engine), integration (viewer 403, editor → version + 1), Playwright |
| UI | `components/DashboardExplore.tsx`: "Filtered by Priority = 1 - Critical ×" chips, per tile Filtered / Not filtered / Your selection / Dashboard filter, keyboard lists for filter and drill, breadcrumbs, rows panel that takes focus; query ids only in expert view. Builder cells are labelled groups with a Select toggle (a chart with controls cannot sit inside a button) | `web/src/test/dashboardExplore.test.tsx`, `web/e2e/dashboard-explore.spec.ts` (axe clean with a filter, a drill and the rows panel open) |
| Live run | `tests/integration/test_dashboard_interact.py` on compose Postgres 16, strict tenancy | [evidence/conv-06-drill-crossfilter-20261029.md](evidence/conv-06-drill-crossfilter-20261029.md) |

Not built: embedded Superset dashboards keep Superset's native filters (unchanged); time members are drilled, not
cross-filtered; a SQL Server time axis (DATEADD/DATEDIFF) is not re-grained (its rows can still be listed); rows behind a
chart whose dataset joins tables are refused.

## 2026-10-29 — Conversational dashboards: a chart from a question, a draft from a prompt, an Ask answer into a draft (CONV-01…03)

From the [conversational analyst gap review](../70-reviews/2026-10-29-conversational-analyst-gap-review.md), wave 1a.
Spec: [S08 §9](../04-specs/S08-dashboards-publishing.md).

* **CONV-01.** Builder chat "add resolution time by priority" → op `chart.from_question` (rules rung, no model) →
  `dashboards/questions.py` runs the question through `agents/sql_agent.ask` unchanged (semantic compiler, verified
  query, Ask rules, else model SQL; gateway validator + `QueryGateway.execute`) → a `ChartDoc` with the chart type
  from `skills/viz.recommend_chart`, `dataset_ref` `metric@v` + dimensions (else the gateway execution), a bounded
  preview and provenance. Each applied batch is a new `ArtifactVersion`; the diff carries how the chart was answered.
  An ungrounded question is a refused op with a clarifying reply and answerable examples; nothing changes.
* **CONV-02.** `POST /api/v2/workspaces/{ws}/dashboards/draft {prompt}` → `dashboards/drafts.py`: rules plan from the
  caller-visible approved metrics, trusted glossary synonyms and the effective brief (catalog Ask-rules questions
  without a metric layer): 2–4 KPIs, 3–6 charts, 1–2 filter controls; purpose `dashboard_plan` ([cache, rules,
  llm_small]) may only reorder known tile ids. Every tile goes through CONV-01 on its own; failures are listed. No
  match → clarifying reply, no dashboard.
* **CONV-03.** Ask promote target `draft` → `dashboards/service.add_answer`: the stored answer (no re-query) as the
  recommended chart in a new draft or an existing builder dashboard (a published one forks). The approval-bound
  direct publish is unchanged and offered beside it ("Add to dashboard" asks where).
* **UI.** Outputs › Dashboards › Build a dashboard ("What should the dashboard show?"), builder chat answers ("Added
  a bar … from an approved metric", clarifying reply with questions to try), Ask "Add to dashboard" chooser. Query
  ids, metric versions and planning detail only with expert tools on.

Tests: `tests/unit/test_dashboard_conversation.py` (rules phrasings, answer → chart for semantic / rules / model
answers, KPI, time grain, stacked and top-N, named type only when it fits, bounded preview, refusals, the op, id
clash, filter controls in ops and bundle, plan / glossary / catalog fallback / clarify, ranking rung off-auto-always
with unknown ids dropped, build with partial failures); `web/src/test/dashboardDraft.test.tsx` (7);
`web/e2e/dashboard-draft.spec.ts` (prompt → draft → clarifying reply → refine → request publication, axe clean);
`tests/integration/test_dashboard_conversation.py` (compose Postgres, seeded users, approved semantic model; the
numbers equal pandas over the same file; publish request is a hash-bound approval).
Live 2026-10-29 against the compose Postgres, no model called: a 2-KPI, 6-chart, 2-filter draft from "Build an incident operations dashboard" (numbers equal pandas over the same file), a metric-bound chart from "add resolution time by priority", a clarifying reply for "add weather by city", an Ask answer into a new and an existing draft, and a hash-bound publish request — [evidence](evidence/conv-01-03-conversational-dashboards-20261029.md).

## 2026-10-29 — SQL Server live: connector certification (S02-T6), session identity (S02-T5), META-002

SQL Server 2022 Developer runs locally as compose profile `mssql` (container `analystos-mssql`, host port 14330; SA
password from the environment, never the repository). `scripts/mssql_cert_seed.py` creates `analystos_cert`
(`cert.items`, `cert.other`) and the least-privilege login `aos_cert_ro` (db_datareader). The certification kit gained
SQL Server checks (`connectors/certify_kit.py::_mssql_checks`) that run through the gateway's tsql path
(`run_session_sql` with the connector's query URL and session statements): `analystos certify-connector --kind
sqlserver --dsn-ref env:ANALYSTOS_CERT_MSSQL_DSN --schemas cert` passed 9/9 —
[evidence](../evidence/connectors/sqlserver-2026-10-29.md). The tsql validator suite now also holds identity spoofing
(`EXEC sp_set_session_context`), dynamic SQL, `SET CONTEXT_INFO`, `OPENQUERY` / `OPENDATASOURCE`, four-part and
cross-database names, `sys.sql_logins` and `SELECT INTO` (30 statements).

The first live run failed five checks; four were defects in `engines/sql.py::run_session_sql`, fixed here: (1) pymssql
raises OperationalError for server errors, so a permission refusal (262) was reported as the source being unavailable
— errors are now classified by SQL Server / DB-Library number (`MSSQL_DENIED`, `MSSQL_TIMEOUT`, `MSSQL_UNAVAILABLE`);
(2) a driver timeout left a dead connection whose rollback raised and masked the timeout — failed or cancelled
connections are now closed, not pooled; (3) `cancel_running` found no cancel on a pymssql connection — it now uses the
inner `_mssql` cancel (interrupts `WAITFOR` in 0.3 s); (4) **the read-only `SESSION_CONTEXT` stayed on the pooled
connection, so the next user's query on it failed** (and nothing else stopped a reused session from carrying the
previous user) — a connection that carried session identity is now always closed. The fifth was the kit's own
comparison. Unit tests: `tests/unit/test_engine_session_sql.py` (classification, discard, cancel). Live:
`tests/integration/test_gateway_mssql.py` (needs compose Postgres + `ANALYSTOS_MSSQL_SA_PASSWORD`; skips otherwise) —
a SQL Server security policy on `SESSION_CONTEXT(N'app.user')` filters `s02shop.events` per end user through
`QueryGateway.execute` with users alternating, a denied column is refused, a statement setting its own identity is
refused; with the identity fix reverted the alternating-user test fails.

Not done: no SQL Server plan estimate (tsql has no explainer, so `max_scan_bytes` cannot stop a statement before it
runs; row cap and driver timeout bound it); a connection per identity-bearing query (no pooling for those);
Snowflake and BigQuery remain blocked on live access; the kit ran against a local container, not a customer server.

## 2026-10-29 — S06-T6 Playbook editor, simulator and signing (FR-058, P2)

Editor (`autonomy/playbook_editor.py`, `api/routers/playbook_editor.py`, prefix `/api/workspaces/{id}/playbook-editor`):
workspace playbooks are written as YAML and stored as the existing versioned `playbook` definitions (ADR-0021: one draft
per key, If-Match revisions, immutable published versions). YAML is read with `yaml.safe_load` only (one mapping,
256 KiB); `validate` runs the same contract and registry checks a pack load runs and lists every problem; drafts show
the diff against the newest published version (or the installed manifest of that id). Web: `web/src/components/
PlaybookEditor.tsx` in Work → Workflows, shown only with Show expert tools on; the form-based workflow builder now
publishes through the same signing steps.

Simulator (`autonomy/simulator.py`, fixtures `autonomy/simulation_fixtures.yaml`: supervised, autonomous, scheduled,
rejected, tight_budget, redirect, or a custom fixture): builds the engine's plan, applies `when`/`skip_when`, asks the
workspace's enablement and certification the way `bind_run` does, walks the DAG in dependency waves with deterministic
mock agents, counts steps/retries/replans/cost as `autonomy/budget.py` does, stops at each approval gate with the
fixture's decision, and applies replans with the engine's reset and approval-invalidation rules. It runs no SQL, builds
no model router, dispatches nothing and writes nothing (`test_simulation_never_executes`). It is the playbook tester:
`POST .../drafts/{id}/simulate` makes a draft `tested` with the simulation digests bound to its content hash.

Signing (`autonomy/playbook_signing.py`): `request-signature` asks for a `playbook.sign` approval whose payload is the
definition, content hash and simulation digests (separation of duties always); publish runs `verify_for_execution`
immediately before signing `(purpose, tenant, key, version, content hash, approval)` with the S10 tenant ed25519 key
(`toolreg/signing.sign_payload`, domain-separated by `purpose`) and consumes the approval. An edit invalidates the
sign-off. `definitions.require_runnable` (every run start), schedule pin checks and promotion refuse an unsigned,
changed, forged, other-tenant or revoked-key playbook. Migration `0073_playbook_signature` adds `definition.signature`.

Tests: `tests/unit/test_playbook_authoring.py` (14: YAML safety and problem lists, versions and diff, options and
template, golden simulation of `investigate.v1` against every fixture (`tests/fixtures/playbooks/simulation_golden.yaml`),
never-executes, enablement/certification, budget/gates/replans, ungated side effect, the full sign-off, re-approval
after a change, runtime refusals, failing simulation, promotion, API round trip); existing playbook-publishing tests
go through `tests/unit/playbook_signoff.py`; `web/src/test/playbookEditor.test.tsx`. Integration tests
(`test_agent_forms_api.py`, `test_workbench_contracts_api.py`) were updated for the sign-off and not run.
Runs (2026-10-29, Windows host): fast suite 4765 passed, 117 skipped, 1 failed (`test_health_states.py` deadline under
load, passes on rerun); `ruff check src tests` clean; web typecheck, `npm run build` and vitest (469 tests; the workflow
journey in `lightIa.test.tsx` now walks simulate → sign-off → publish) pass.

Not built or not evidenced: no live-stack run or browser walkthrough; migration not run against Postgres; the editor is a
text area (no schema-aware completion); simulation cost is the agents' declared budget, not a model price; workspace
playbooks published before this change are unsigned and refused until a new version is signed; drafts still run unsigned
in an `environment: dev` workspace (ADR-0021).

## 2026-10-27 — S05 Model gateway: providers, layered configuration, switch gate, air-gap, Admin › Models

Providers (`llm/providers.py`, `llm/config.py`): `openai_compatible` with declared capabilities (json_mode, tools,
context_window; requests are fitted to them), and the Atlas ports `openai` (Responses API), `azure_openai`
(quoted deployment and api-version) and `gemini` (generateContent). Claude and Gemini stay blocked
(`blocked_families`); the effective view and the UI show them as blocked. Layered configuration (`llm/layers.py`,
`services/model_config.py`): file ⊕ platform ⊕ tenant ⊕ workspace, each lower layer only narrowing (allowlist ∩,
residency subset with unknown regions excluded, budgets min, no added or earlier model rung, profile kind fixed so JEV
is never wired in or out); versions in `model_config_override` with tenant RLS, If-Match, re-checked rollback, audit;
the router applies a workspace's layers on every call and fails closed when they cannot be read. Model-switch gate
(`services/model_switch.py`, `config/model_switch_gate.yaml`): a changed primary model is scored on the purpose's
golden suite (deterministic grading) against the current model; a regression beyond 0.10, a score below 0.5 or an
unevaluable model answers 409 with both scores unless a platform admin gives a reason (audited). Air-gap posture
(`services/airgap.py`, `GET /api/admin/models/airgap`) and runbook `docs/30-runbooks/08-local-models.md`. Admin ›
Models (`web/src/pages/AdminModels.tsx`) inside Platform settings: purposes, providers (health, test), profiles,
budgets and overrides with source layers, presets, spend, evaluations, air-gap.

Tests: `tests/unit/test_openai_compatible.py` (round trip over a local HTTP stub), `test_model_adapters.py`,
`test_model_layers.py` (`test_layer_can_only_narrow`, AT-15 residency, `test_jev_never_grants`, versions, API),
`test_model_switch_gate.py`, `web/src/test/adminModels.test.tsx`; integration `tests/integration/test_model_gateway_db.py`
on Postgres 16 under strict tenancy (cross-tenant 404, 409 keeps the scores, override, worker lookup without a bound
tenant). Migration `0059_model_config_layers` checked upgrade/downgrade/re-upgrade and RLS on Postgres 16.

Not built or not evidenced: per-run/day/user budgets are resolved and shown but are not hard caps yet; decision
purposes have no typed golden suite (the gate needs an override); no live provider test, no live suite run, no
browser walkthrough (no billable calls; live stack untouched); the §62 scenario on an air-gapped install (S05-T8
fine-tuning not started).
## 2026-10-27 — S13 remainder and S11-T1/T7: Desk UI, value dashboard, nightly value rollup

The Request Desk now has its screens and its nightly job. Work → Request desk (`web/src/pages/Desk.tsx`) gives
requesters their requests and answers (accept or reopen) and analysts the queue by SLA with assignment, answer or
approve (minutes required), escalate, decline, duplicate, certify and the prepared data pack. Overview → Value
(`web/src/pages/Value.tsx`) shows the FR-053 numbers, the daily trend, per-class results, baselines and a CSV export.
Backend: `desk_daily` (migration `0068_desk_daily`, RLS) filled by `desk/rollup.py` — a `@platform_loop` that claims
the night and picks workspaces, then `@owned_by("workspace")` work per workspace — hooked into the scheduler with an
SLA sweep built the same way; R4/R5 requests escalate with a draft schedule or monitor the desk never creates.
Tests (2026-10-27, Windows dev host): `tests/unit/test_request_desk.py`; `tests/integration/test_request_desk.py`
on Postgres 16 under strict tenancy (5 passed: AT-19, dedupe, escalation flow, cross-tenant 404, two-tenant nightly
rollup and export); `web/src/test/desk.test.tsx`; `npm --prefix web run build`. Not done: workspace-home Desk
widgets, axe/browser pass on the new screens, business-hours SLA, skills-based assignment.

## 2026-10-27 — S07 Analysis, verification and evidence (T1, T3–T7)

Compute isolation (ADR-012): the isolated worker refuses to start on a kernel other than the sandboxed runtime it
was given (the Helm runtimeClass is S12's), the compose pools run non-root, read-only, capability-free with limits,
and the in-process sandbox runs only for lite on synthetic data. Every finding carries a claim label decided by code from REV's verdict, and the numbers guard
refuses causal wording unless the label is a supported association from a causal design (none registered). A finding
is served as one Evidence v2 object (`GET /v2/evidence/{id}`). `investigate.v1` 1.1.0 judges the v1 §7.2
investigation checks after their steps (statistics and conclusions block). The methods registry gains a
time-series pack: seasonality (STL), level shift (CUSUM), a forecast judged by a rolling-origin backtest, and
distribution shift (KS + PSI). A void (any fingerprint component, event or sweep) puts a banner on the published
charts and dashboards presenting the finding and escalates one Request Desk task per artifact. Evidence: unit tests
`test_compute_isolation.py`, `test_claim_labels.py`, `test_evidence_v2.py`, `test_investigation_gates.py`,
`test_void_banner.py`, `tests/methods/test_timeseries_pack.py`; `tests/integration/test_s07_evidence.py` on Postgres 16
with strict tenancy (2026-10-27, Windows dev host): a full deterministic investigation passes its six gates, labels
its findings, serves Evidence v2 objects, publishes to the preview destination; a policy change voids them, the
dashboards show the banner, an escalated Desk task exists, and another tenant sees none of it. Not verified: a gVisor
node, the web banner in a browser, the banner inside Superset.
## 2026-10-27 — S08 Dashboards, conversational authoring and publishing (T1–T6)

Builder dashboards are typed documents (`contracts/dashboard_dsl.py`, JSON Schemas in `contracts/`) changed only by
validated ops: a person (or the `dashboard_design` ladder: rules first, a small model only when no rule matches, every
proposed op parsed and re-validated) proposes, `dashboards/ops.py` decides, and each applied batch is a new artifact
version with the ops as its diff (migration `0070_dashboards_publishing`). Editing a published dashboard forks a draft.
Publishing is a hash-bound, single-use approval of {artifact, version, content hash, destination, audience, bundle},
re-derived from the stored artifact and verified immediately before the call; dataset SQL comes only from verified
queries, recorded gateway executions or approved metrics (compiled), with filters and period comparisons as
sqlglot-built wrappers. Superset: tenant/workspace-stamped connections (another tenant's refused), `ws_{slug}_viewer|editor`
roles, dashboard RBAC from the audience, policy row filters as RLS rules (fail closed when they need the viewer), guest
tokens minted after a request-time recheck with the viewer's own RLS, and a reconcile job that flags drift and repairs
only under `verify_for_reapply`. Exports are watermarked and audited; at or above the policy's sensitivity they are
publish-class approvals. PDFs print Tamil, CJK and Arabic from bundled OFL fonts. Evidence (2026-10-27, Windows dev
host, Postgres 16 with strict tenancy and RLS): `tests/unit/test_dashboard_{dsl,ops,bundle,exports}.py` and
`test_superset_access.py` (56), `tests/integration/test_dashboards_publishing.py` (9: migration up/down/up,
`test_patch_ops_versioning`, `test_edit_published_forks_draft`, `test_guest_token_rechecks_auth`,
`test_cross_tenant_superset_denied`, publish, failed-publish retry, reconcile, exports), `web/src/test/dashboardBuilder.test.tsx`
(5), and a browser check of the builder against a local API. Superset is an in-memory fake in those tests.
Live 2026-10-28 (AT-07/AT-08, [evidence](evidence/superset-embedding-at07-at08-20261028.md)):
`tests/integration/test_superset_embedding_live.py` against Superset 4.1.1 with embedding on. Static RLS rules on the
workspace viewer role AND together and fail closed; the guest token's per-viewer clause gives the exact analytics-DB
count; a token reads no other dashboard; the embedded page loads only from the allowed origin. The UI component
`EmbeddedDashboard.tsx` rendered the filtered dashboard in the built-in browser. Open: the platform endpoint was not in
that browser journey (stand-in endpoint; its checks are proven by `test_guest_token_rechecks_auth`).

## 2026-10-27 — S01-T6 ABAC on every call + OPA adapter, S01-T7 tenant-scoped cache keys

ABAC (`governance/abac.py`): the policy decision input carries `{classification, purpose, region,
aggregation_grain, audience}`, derived by code from the DataScope (column tags, the caller's `region` attribute,
the workspace or tenant data residency), the validated SQL (syntactic grain) and the tool contract. It is decided in
`policy.evaluate` (so the `ToolRuntime` pre-hook and run, publish, build, MCP-tool and monitor decisions) and in
`QueryGateway.execute` for every statement, before the result cache. Rules only deny: platform rules
(`platform.data_residency`, `platform.restricted_external`) plus versioned workspace `abac_rules`. Denials are
`403 policy_denied {rule, reason, remedy}`, recorded on the tool execution, the `policy.denied` event and a rejected
query audit row. `ANALYSTOS_POLICY_ENGINE=opa` adds an OPA sidecar that can only add a denial; unreachable or
malformed answers deny. Cache keys (`tenancy/cache_keys.py`): the query result cache, the model response cache and
the context caches (retrieval, compiled contexts) key every entry `tenant:{t}:scope:{scope_hash}:policy:{v}`; a
caller bound to another tenant than the workspace's gets no cache. Tests: `tests/unit/test_abac.py` (attributes,
grain, rules, OPA allow/deny/malformed/unreachable over a local HTTP server, gateway denial before the cache, tool
gate denial and record, restricted data to an external audience), `tests/unit/test_cache_tenant_isolation.py`
(negative cross-tenant suite over all four caches, forged owner tenant, unbound entries, scope and policy segments),
`tests/integration/test_cache_tenant_isolation.py` on Postgres 16 with RLS (strict) and Redis db 3 (2026-10-27,
Windows dev host): tenant B never reads tenant A's query results, model answers or context entries with the same
workspace layout, SQL and prompt; B presenting A's scope gets 404 and runs nothing; tenant residency read under RLS
denies a query from outside it, audited only in B. Not built: a real OPA sidecar in compose (adapter tested against
an OPA-shaped HTTP server), an OPA decision cache, ABAC on `explain` (reads no rows).
## 2026-10-27 — S09 Proactive monitoring, triggers and recurring work (T1–T5)

New package `src/analystos/triggers/` and API `/v2/triggers`, `/v2/channels`: signed webhook and Kafka events are
durably queued with a 7-day replay cache and processed as the owning tenant; a monitor's anti-noise gate
(hysteresis, debounce, cooldown, quiet hours with an explicit zone, daily caps, correlation into alert groups, the spec's incidents) decides
what fires; a rule monitor (`notify` / `on_trigger`) is approved once, bound to its scope, recipients, pinned
playbook and limits, and every outbound message and goal run verifies that approval first; recurring packs
auto-deliver after N clean runs and route to a hash-bound review otherwise; Teams / Slack cards and Graph email
go out, signed Slack / Teams / email messages come in as Request Desk requests. Evidence (Windows dev host,
Postgres 16 on :55432 with strict tenancy and RLS, 2026-10-27): `tests/unit/test_s09_noise.py` (18),
`tests/unit/test_s09_triggers.py` (16), `tests/unit/test_s09_packs_channels.py` (11),
`tests/integration/test_s09_triggers_pg.py` (3: a webhook posted with no tenant bound -> processed by the platform
loop -> one alert in one alert group -> Teams send refused until the rule is approved, then delivered; Kafka enqueue
under RLS; a signed Slack message -> a real Desk request; another tenant sees none of the rows). Migration
`0071_triggers` upgrade, downgrade -1 and re-upgrade checked. Fakes only for brokers and channels (Kafka consumer,
Teams/Slack/Graph HTTP, Desk submit in unit tests): not certified against a real broker or workspace. Not built:
the freshness poll trigger (`get_freshness()` vs a contract), a UI for rules, alert groups and bindings, replies in
the originating Slack/Teams thread, Bot Framework (JWT) Teams bots.

## 2026-10-27 — S03 Context Service + Context Ledger (T1–T6) and S13-T5 certify-on-close

The Context Ledger (`context_ledger_entry`, `context_ledger_change`; migration `0061_context_ledger`, RLS) is a
versioned projection of nine owner tables, written in the owner's own transaction by a session hook, with a
consistency check and `analystos context reconcile`. Reads (`context/retrieval.py`) filter by the caller's access
before ranking (workspace membership, trust, data scope from `resolve_scope`), rank hybrid (exact/alias, mention,
synonyms, lineage proximity, cosine × trust × freshness × in-scope) and compile a bundle with receipts, omissions and
an identity hash; suggestions never enter it and conflicting certified definitions block it. The run's reviewed brief
is a first-class compiler section (receipted, in the cache key and identity hash, inside the untrusted envelope,
refused rather than trimmed). Interfaces: REST `/api/v2/context/*`, MCP `context.search|get|compile|changes` (grant
`context.read`) and ledger resources; OKF export (deterministic, access-filtered, withheld entries counted only) and
import as suggestions. The desk `certify` action turns an analyst's answer into a verified query, glossary entry,
metric suggestion or playbook in the ledger.
Tests (Windows dev host, Python 3.12, Postgres 16 with RLS, strict tenancy, fake model transport, 2026-10-27):
`tests/unit/test_context_ledger.py` (15) and `tests/integration/test_context_service.py` (6): ledger isolated per
tenant; REST access filter (an analyst without PII clearance never sees the PII-mapped entry; W2 404); 409 on
conflicting certified metrics; AT-17 (MCP client with `context.read` on W gets receipts of W's approved entries only,
W2 refused 403); AT-16 (export → import into a clean tenant → export byte-identical; restricted export omits the PII
entry and counts it in `log.md`); AT-20 (escalated R2 answered and certified as a verified query; the paraphrased repeat
triages R1 and is auto-answered by the registry rung with no model call). Migration upgrade from empty, downgrade -1
and re-upgrade checked. Regression: full unit suite shows only the 5 failures that also fail on `main`; 18 integration
files (tenancy, desk, MCP, registries, knowledge, context, semantic, crawler, API) show 3 failures unrelated to this
change (Claude models blocked on `main`, a Windows path as push remote, migration-0017 column check vs `tenant_id`).
Not built: federation (T7), OpenLineage/ODCS export, run plan-hash wiring of the identity hash (S07), MCP HTTP
transport run (SDK extra absent on this host; tool handlers tested directly).
## 2026-10-27 — S10 Tool, agent and data-product registry; MCP server and client (T1–T5 backend)

New package `src/analystos/toolreg/` and API `/v2/tools`: certified metrics, verified queries, published datasets and
playbooks are published as typed, signed tools (`ToolManifestV2`, migration `0065_tool_registry`) and invoked through
one path for REST, MCP and agents: grant or role, version pin (`@4`), ed25519 signature check, JSON-Schema arguments,
hash-bound approval for side effects, then exactly one deterministic execution path (semantic compiler, verified-query
template with typed literals, a SELECT tree over the dataset, `create_run`, or the MCP client), all SQL through
`QueryGateway`. MCP clients see and call registry tools within their grants (exact ids, families, `tool_patterns`,
per-minute rate, daily quota, expiry); every call is a `tool_invocation` row. External MCP tools are admitted
quarantined (`write_external`), refused when screening flags them, re-screened into a new quarantined version when the
server changes a definition, and certified only by an owner's classification. Evidence:
`tests/unit/test_tool_registry.py` (17) and `tests/integration/test_tool_registry.py` (8) on Postgres 16 with strict
tenancy and RLS, 2026-10-27 (Windows dev host; the MCP protocol and admission tests ran with the `mcp` 2.3 extra):
migration up/down; REST publish and invoke through the real gateway over staged ServiceNow data equal to reference
queries; AT-22 over the official MCP SDK client; grants, rate and usage; single-use hash-bound approval; tenant
isolation of rows and keys; quarantine, classification and re-screen against the SDK test double (a double, not a
certification). Not built: registry UI, built-in tools/agents as registry rows, a real third-party MCP server.

## 2026-10-27 — S02 Connectors, query gateway, result handles, multi-source

Security first: a workspace editor could register a source with `secret_ref: env:<ANY_VAR>` and receive that process
variable as the connection password on a host they control. References a workspace user registers now name only what
the operator allows (`ANALYSTOS_SOURCE_SECRET_*` for `env:`, configured directories for `file:`, configured prefixes for
`vault:`, `aws-sm:`, `azure-kv:`, `gcp-sm:`), checked at registration and at resolution. The gateway gained a
`QueryRequest` protocol with an in-process and a Temporal (`query` queue) service, Parquet result handles above 256 rows
/ 64 KB (tenant RLS, sha256, TTL purge), a pre-execution scan budget (PostgreSQL EXPLAIN plus relation sizes), session
identity for the source's own row security, cancel, refusals with reason codes and redacted SQL, multi-source joins in
DuckDB over leg handles with lineage, a declarative read-only REST connector, and a certification kit
(`analystos certify-connector`) that certified PostgreSQL 16 on 2026-10-27. Evidence: tracker rows S02-SEC, S02-T1…T8,
S02-REG; spec As built in `docs/04-specs/S02-connectors-query-gateway.md` §8. Not done: ADBC, live Temporal,
MSSQL/Snowflake/BigQuery certification (no access), object-store backend and encryption for handles, import-linter
contract.

## 2026-10-27 — S13 Analyst Request Desk (T1–T4, T6 backend)

New package `src/analystos/desk/`: requests from the UI/API (`POST /api/requests`) and MCP (`request.create`) are
deduplicated, triaged R1–R9 by the `request_triage` ladder (rules first; a typed decision and a small model only when
no rule is confident, limited to R1–R9 and unable to lower an export or a confident escalation), then answered by the
existing governed paths (Ask for R1/R2, an investigation run for R3/R6, a reviewed definition for R7) or escalated
with an SLA, an assignee and a data pack. Every state change is a `request_event` and a `request.*` UI event; the
tables are tenant-owned with RLS (migration `0060_request_desk`). Value metrics on `GET /api/value`.
Tests: `tests/unit/test_request_desk.py` (state machine, triage rules and ladder, floors, dedupe, value math with
fixture baselines, review/SLA/answer/accept flows, R3/R7 routes, MCP tool) and
`tests/integration/test_request_desk.py` on Postgres 16 with RLS (2026-10-27, Windows dev host): AT-19 — 20 seeded
R1/R2 requests over staged ServiceNow data with a fake model transport, 18 auto-answered with numbers equal to an
independent reference query on the analytics plane, 2 escalated with a refusal, assignee, SLA and inspector link,
none answered wrongly; dedupe through the API; escalated request answered, accepted and measured; another tenant gets
404 on read, actions, value and intake. Not built: certify-on-close (T5), nightly rollup, Teams/Slack/email intake,
R4/R5 schedule/monitor proposals, UI.

## 2026-10-26 — ASK-BRIEF-01 Ask-to-investigation handoff

Ask's Investigate why action opens an inline editable objective, initially the original question.
It explains broader investigation scope, non-inheritance of SQL filters and sample limits, hypothesis
outcomes and publication review. Only Start investigation calls the existing promotion API, passing
the edited objective and retaining the Ask lineage path. Cancel makes no request. Tests: 33 Ask and
downstream UI cases, production build, one browser journey and a live Retail sample-thread editor check.
See [evidence](evidence/ask-investigation-brief-20261026.md).

## 2026-10-26 — BI-LIBRARY-01 published workspace collection

Outputs now offers a searchable published dashboard/report collection across the current workspace's
investigations. Repeated records for the same platform/type/external ID show the newest record;
drafts and other workspace records are excluded. Direct BI links retain separate BI authentication,
while Details & evidence selects the originating artifact and investigation. Missing or unsafe URLs
keep the evidence action available. Nine UI checks, production build and a live Retail browser check
passed. See [evidence](evidence/published-library-20261026.md).

## 2026-10-25 — DEMO-03 cleanup and OpenRouter readiness

Pinned SQLGlot to 30.20.0 after a rebuilt image installed 30.24.0 and broke the gateway's
`infer_schema` qualification argument. Existing gateway validation, dialect, corpus, lineage,
service and skill suites pass. A real OpenRouter analytical question subsequently ran through
the rebuilt API and gateway, returning 12 rows; the Ask browser UI rendered that answer without
errors. All four retained workspaces answered a scoped analytical question. Eight primary demo
screens loaded without errors. Provider health probe passed; it covers chat, not the separate
decision-provider endpoint.

Notification visibility now excludes archived/disabled workspaces before limiting results;
mark-as-read uses the same access filter. Tests cover admin/member visibility, private messages,
archived clutter and inaccessible notification IDs. Workspace deletion regression tests pass.

A fresh investigation created its execution plan and stopped at the required plan-approval gate;
no approval was made on behalf of the user. Optional isolated Python execution remains unavailable
and correctly refused. See [readiness evidence](evidence/demo-readiness-20261025.md).

## 2026-10-25 — DEMO-02 investigation conversation

`services/run_questions.py` answers explicit question feedback from persisted findings, verification
states, checks and caveats; no source query, model call, plan change or external action is needed.
Question/answer history is saved in feedback data and run instructions. Automatic classification
that selects question mode keeps its classification identity and decision references. The UI offers
editable suggested prompts, shows timestamped saved-evidence answers with finding links, and keeps
redirect/deeper-analysis options available separately. The conversation is now below the brief.

Coverage: 13 pure backend cases, a Postgres API persistence/access-boundary test, 38 UI cases across
conversation/overview/increment-4 suites, a final 34-case conversation/increment-4/analyst-answer
regression pass, and two desktop/mobile browser journeys including refresh, links and accessibility.
Production build and targeted Python lint pass. A local live browser rehearsal used the rebuilt API
to summarize a completed synthetic ServiceNow run, reload saved history, ask for limitations and
follow a finding to verification checks without page errors. See
[dated evidence](evidence/demo-conversation-20261025.md).

Limits: bounded saved-evidence explanation, not general LLM conversation or fresh statistical analysis.

## 2026-10-25 — DEMO-01 investigation brief

`web/src/components/InvestigationOverview.tsx` presents current verification counts, a bounded
finding shortlist, direct evidence links, execution progress, limitations and a state-aware next
action. `RunView.tsx` provides focus handoffs to the existing hypothesis/task board and approvals.
Void/legacy/pending/failed verdicts are not counted as currently verified; rejected and superseded
findings are excluded. Historical narrative is expandable and labelled as recorded prose.

Coverage: seven new component tests, 73 existing UI regression tests, two Playwright journeys
including desktop and phone dark-theme accessibility scans. Production build passes; the
existing large-chunk advisory remains. A real local API/browser rehearsal opened a completed
synthetic ServiceNow investigation and followed a verified finding to its checks and evidence;
no browser errors were recorded. See [dated evidence](evidence/demo-brief-20261025.md).
This does not certify a new model run, production deployment, or third-party framework integration.

Companion to the [tracker](01-tracker.md): the tracker holds status, this file holds dated
evidence. Each row gives the code path, the automated coverage, the live evidence and the
remaining limitation (spec v1 §70). Entries are appended, not rewritten; a later entry supersedes
an earlier one for the same capability.

## 2026-09-27 — P7-21 analysis context and output ownership

`analysis_context` is a versioned definition of business purpose, starting question, source IDs and
approved metric names. The same source may appear in several contexts. Start work selects a
published version, allows the question to change per investigation and pins the exact context
specification to the run. The pinned business context enters model prompts and the context artifact;
it never widens source scope. Outputs groups findings and artifacts by run question, with a
three-panel desktop layout and responsive two/one-panel fallbacks. Data provides a two-panel
context editor, while its definitions panel explains semantic-model validation. Run bindings and
metric approval read approved semantic structure when it exists. Before the first structure approval,
metric approval can validate against the run's proposed structure; run bindings still use approved
structure only. Context text is
screened for prompt injection before save or publication.

Evidence: `tests/unit/test_analysis_context.py`, `tests/unit/test_definitions_pins.py`,
the context compiler and semantic compiler suites (48 focused Python tests),
`web/src/test/analysis-context.test.tsx` (289 web tests passing), production build, and 12
mock-browser journeys including context draft → publish and Outputs accessibility in both themes.
No AgentSwarms source or
assets were copied. Remaining: real API browser journey across multiple source engines and a
large-history visual pass; the workspace semantic graph is shared, so separate context-specific
metric expressions require separately named and approved metrics.

Follow-up evidence: the backend CI integration suite exposed the first-run metric approval case.
`tests/unit/test_check_semantics.py` now covers that fallback, while existing approval and run
binding behavior remains governed by the approved structure when one exists.

## 2026-09-26 — P7-19 workflow step list, third slice

Work has a workflow builder for registered, enabled agents with a default entry and no external
write side effect. It saves a workspace `playbook` definition through server validation, edits
drafts with revision checks, publishes an immutable version and starts a run bound to its exact
definition id. New workflows carry draft certification, so schedules and autonomous runs remain
blocked; the UI starts a manual run at autonomy level 2. Earlier-step dependencies avoid cycles,
and the backend still validates every agent reference and dependency before save or publish.

Evidence: `test_definitions_pins.py` (11 passed) validates the builder's manifest shape and
publication; the web suite (243 passed / 1 skipped) includes create, publish and exact-version
run from Work. TypeScript, production build, Ruff and diff checks pass. Remaining: agent
manifest authoring, optional canvas, real API browser journeys and live category validation.

## 2026-09-26 — P7-19 guided ML start, second slice

Start work routes Forecast and Predict to a published `ml_spec` selection with the plan's task,
dataset, target, budget and exact version visible before training. The request calls the governed
experiment API with the selected definition id. Work has an ML experiments tab with status,
evaluation, readiness, verification and refusal reasons. Server availability requires a published
definition matching the chosen job, alongside existing role, source, capability and mode checks.

Evidence: focused `test_ml_work_orders.py` and `test_workspace_modes.py` pass; web suite
242 passed / 1 skipped, including an API journey asserting the selected id is sent to
`/ml/experiments` and the result opens in Work. TypeScript, production build and Ruff pass. Remaining: agent editor,
workflow authoring, real API browser journeys and live category validation.

## 2026-09-26 — P7-19 configurable work modes, first slice

`services/workspace_modes.py` validates a nonempty combination of Analysis, Engineering and ML,
previews changes to existing playbook enablement, and applies them with owner checks and audit.
`api/routers/workspaces.py` exposes creation and later editing; generic workspace settings updates
cannot bypass the dedicated route. `capabilities/job_kinds.py` reports the selected mode and any
blockers. The web workspace form and owner settings card show the selection and preview; Start work
uses the server's availability and does not send ML jobs to the analysis route. The source, role,
certification and approval checks remain separately enforced.

Evidence: `tests/unit/test_workspace_modes.py`, `test_workspace_lifecycle_map.py`,
`test_brief_readiness.py` and `test_capability_platform.py` (33 passed together); web suite
241 passed / 1 skipped; TypeScript and production build passed. The route binding audit test
could not run on Windows because `workers/isolation.py` imports Unix-only `resource`; it was
not counted as passed. No AgentSwarms source was copied. Remaining: typed guided
ML start, agent editor, workflow authoring, real API browser journeys and live category validation.

## 2026-09-26 — P7-15 gateway hardening, partial

`gateway/dialects.py` and `gateway/validator.py` enable strict PostgreSQL and T-SQL function
checks and reject table hints and unconditioned joins. The focused validator suites in
`tests/unit/test_gateway_validator.py` and `tests/unit/test_gateway_dialects.py` pass. The
integration timeout fixture now uses a keyed join. Remaining: the 106-case corpus and expectations
file in CI, plus live source tests; this is why P7-15 remains Partial. See the
[AgentSwarms implementation comparison](../70-reviews/2026-09-26-agentswarms-implementation.md).

## 2026-09-25 — Phase 0 + Phase 1 MVP

Environment: compose stack on one host (Postgres 16 + pgvector, Redis 7, Neo4j 5, Temporal 1.25,
Superset 4.1.1 + psycopg2), API + Temporal worker as processes, OpenRouter (chat + Decisions API).
Data: synthetic ServiceNow-shaped dataset (20,000 incidents, 3,000 changes, planted effects and
data-quality defects) served by the bundled Table-API mock.

### Live end-to-end (spec v1 §62 definition of done)

[`evidence/e2e-20260925-054942.md`](evidence/e2e-20260925-054942.md) (+ `.json`): **26/26
checks passed** through the HTTP API, with the Temporal orchestrator, real model calls and a real
Superset publication. Highlights:

* 5 verified findings, all reproducible (re-run result hash identical) and confirmed by an
  independent second method; narratives written by a low-cost model and accepted by the numbers
  guard. They match the planted ground truth: 3+ reassignments → 28.8% vs 9.7% missed SLA
  (3.0x); Network Operations 22.1% vs 10.2%; priority-1 concentration by CI (top 3 = 46.7%,
  Gini 0.57); Payments Gateway P1 concentration.
* Redirect before publication: JEV classified the instruction as `redirect`
  (P(consequential) = 0.09); the LLM interpreted it into the validated filter
  `category != 'inquiry'`; plan v2 reset 9 tasks and removed 9 dynamic tasks; findings were
  recomputed on 16,995 rows.
* Pause while waiting for approval → `PAUSED`; the analyst (editor) was refused approval (403);
  the approver approved; publication to Superset (dashboards 21, 22) completed in 9 s.
* 41 model calls, 19 of them JEV decisions across all six decision purposes; 1 failed call
  (malformed JSON, retried); total model cost **$0.32** for the run.
* Safety probes via the SQL console: restricted/PII column → `sql_rejected`; `DELETE` →
  rejected; `analystos.public.app_user` (control plane) → rejected; a workspace the analyst is not
  a member of → 404.

### Capabilities

| Capability | Code | Automated coverage | Live evidence | Limitation |
|---|---|---|---|---|
| Governed SQL gateway | `gateway/validator.py`, `gateway/service.py` | 109-case validator security suite; service unit tests; live gateway tests (reader cannot write, cannot reach control plane, timeout, audit, cache) | e2e safety probes | SQL Server read-only relies on the login |
| Scope + policy + approvals | `governance/*` | 13 integration tests (PII/restricted denial, clearance, roles, publish gating, payload/plan binding, SoD, policy-version change, revoked approver, expiry, pause persistence) | e2e approval + 403 | Local JWT identity |
| ServiceNow connector | `connectors/servicenow.py` | unit tests vs mock (types, keys, references, display values, pagination, 401) + staging integration | e2e (mock) | **Not certified**: no real instance |
| PostgreSQL connector | `connectors/postgres.py` | integration against compose Postgres | — | — |
| SQL Server connector | `connectors/sqlserver.py` | metadata mapping + T-SQL compile/transpile tests | — | Never run against SQL Server |
| Analytical skills | `skills/*` | 462 tests incl. benchmarks: planted effects found, 6 null controls not supported, every method×derivation×dialect compiled and gateway-validated | e2e findings | Sampled methods cap at 50k rows |
| Model router | `llm/router.py` | fake-transport tests (fallback, fail-closed allowlist, family exclusion, redaction, JSON parsing) | e2e 41 calls | Provider region metadata not available |
| JEV decisioning | `llm/jev.py` | fake-transport tests (score/choice/noul parsing, invalid choice ignored, unavailable → None) | e2e 19 calls, 6 purposes | Escalate-only by design |
| Agent runtime + Temporal | `runtime/*`, `workflows/*` | plan/dependency unit tests; deterministic full-flow integration test (no models, preview destination, 20 s) | e2e on Temporal | — |
| Replanning | `runtime/engine.apply_replan`, `services/runs.submit_feedback` | cycle/stale-plan unit tests | e2e redirect | — |
| Superset publishing | `publishing/superset.py` | 64 unit tests; live integration (15 charts render data, idempotent republish, rollback) | e2e dashboards 21/22 | Report scheduling is Phase 3 |
| Web UI | `web/` | 27 vitest tests, typecheck, build; live smoke across all screens | manual review against running API | Docker image build not verified in this sandbox |
| CI | `.github/workflows/ci.yml` | lint, unit, integration with Postgres/Redis services, web build | green on `51c4e03` | Superset/Neo4j/Temporal tests skip in CI |

## 2026-09-25 — Increment 2: Phase 3 (scheduled & continuous analytics)

Same environment as above plus the `analystos scheduler` process.

### Live evidence

[`evidence/e2e-phase3-20260925-064436.md`](evidence/e2e-phase3-20260925-064436.md): **13/13**.
Earlier pass [`e2e-phase3-20260925-061710.md`](evidence/e2e-phase3-20260925-061710.md) (12/12)
is kept for history: its diff was noisy, which led to carrying claims and KPI definitions forward.

* Scheduled re-analysis (Europe/London cron) re-tested all 4 previously verified claims with
  identical specs: 4 persisting, 0 resolved, 0 not re-tested; 10 KPIs with 0.0 change on unchanged
  data; publication tasks skipped; weekly report generated (PDF 212 KB, XLSX 36 KB, HTML 17 KB)
  and downloaded through the audited, hash-checked endpoint.
* Monitor schedule evaluated 4 monitors (drift, change point, threshold, data quality) through
  the gateway; the threshold alert was triaged by JEV (`typesafe/jev-1.13`) and escalated; the
  monitor's automatic investigation run completed with `origin: alert` and no publication.
* Notifications for the report, the alert and the investigation.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Scheduler | `services/schedules.py` | validation, timezone, idempotent claim, run-now path | ✅ | no back-fill of missed slots |
| Re-analysis diff | `services/changes.py`, investigator carry-forward, semantic carry-forward | claim identity unit tests; same-data integration assertion (0 resolved / 0 not re-tested / equal KPIs) | ✅ | diff granularity is per claim, not per group value |
| Reports | `reports/*`, `services/reports.py` | renderer tests (escaping, formula injection, determinism, CJK/Unicode PDF extraction) + integration download | ✅ | bundled Noto Sans CJK SC under SIL OFL 1.1; unsupported glyphs shown as `?` |
| Monitors + alerts | `services/monitors.py` | drift/threshold/change-point unit tests; integration (alert, de-dup, DQ baseline, auto-investigation) | ✅ | DQ monitor re-profiles each run (cost grows with columns) |
| Notifications | `services/notifications.py` | integration | ✅ | in-app only |
| Phase-3 UI | `web/src/pages/{Schedules,Monitoring,Reports}.tsx` | 23 new vitest tests | smoke | monitor config not editable after creation |

## 2026-09-25 — Increment 3: universal sources, metadata crawler, token economy, admin control plane

Same environment as above. The source for the live run is a seeded **SQLite** retail database, not
ServiceNow: 24,000 orders, 2,500 customers, 60 products, 4 regions. It contains three planted
effects and null controls:
- marketplace orders are returned about 3× as often;
- Enterprise order value is about 2.1× higher;
- APAC shipping takes about 3 days longer;
- payment method, discount and coupon have no effect.

### Live evidence

[`evidence/e2e-increment3-20260925-150550.md`](evidence/e2e-increment3-20260925-150550.md): **13/13**.

The run went through the HTTP API, with the Temporal orchestrator, real model calls under the
`token_saver` preset, and a real Superset publication. Earlier passes are kept for history:
- [`…145158`](evidence/e2e-increment3-20260925-145158.md) (12/13): three duplicate driver-model
  findings, and only the boolean outcome was tested.
- [`…145827`](evidence/e2e-increment3-20260925-145827.md) (12/13): measures were tested against one
  dimension only.
- [`…150310`](evidence/e2e-increment3-20260925-150310.md) (12/13): KPI × dimension follow-ups ran,
  but were ordered naively.

Each of those passes changed the investigator (see tracker P3 findings).

**Catalog and crawl**
- **Any database.** `/api/source-kinds` lists 15 kinds. The SQLite file was uploaded and registered
  as a `sqlite` source, which is staged.
- **Discovery.** Discovery is a full crawl with **0 model calls**. It derived role/domain/grain for all
  4 tables (orders = fact, sales, "one row per order", confidence 0.82). It tagged `customer.email`
  and `customer.customer_name` as PII by name rules, recorded 3 declared relationships and wrote 4
  context entries plus the graph.
- **Incremental crawl** after staging: 4 unchanged, 0 touched, 4 profiled and value-sampled through
  the gateway.
- **Drift.** A column `coupon_code` was added and a full crawl run. It reported exactly one changed
  table with `added: [coupon_code]` and nothing deprecated. It **re-staged** the changed selected
  table and profiled with 0 errors. The owner's reviewed description was kept, and a
  `schema_change` notification was raised.

**SQL and admin**
- **Explain.** Deterministic, with no execution. The PII column query was rejected by the gateway
  with its reason, and the rejection was audited.
- **Admin.** The `token_saver` preset was applied at runtime (`/api/admin/models` shows
  `hypothesis_generation: auto`, `verification: always`). The previous settings version was
  restored at the end.

**Analysis**
- **Run:** 14 hypotheses over 3 rounds, all deterministic: rule playbook and KPI × dimension
  follow-ups.
- **Found:** 4 verified findings, all 3 planted effects:
  - returns driven mainly by channel (marketplace odds ratio ≈ 3.25);
  - returns concentrate in channel = marketplace;
  - APAC has the longest shipping;
  - Enterprise has the highest order value.
- **Rejected:** all 10 non-planted combinations.
- **Model use:** 10 model calls (verification and JEV where required), **15 deterministic skips**.
  Cost **$0.023**, 8,048 tokens. This is not directly comparable with the MVP run's $0.32, which
  used another dataset and the default `always` modes.
- **Publication:** after approver approval, to Superset (dashboards 39/40, 7 KPIs, 9 charts).

**Monitoring and scheduling**
- A forecast-deviation monitor was evaluated on weekly orders.
- A `crawl` schedule run-now succeeded.

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Source-kind catalog + generic SQL connector | `config/source_kinds.yaml`, `connectors/{kinds,generic_sql}.py` | 120 unit tests; integration against MySQL 8.4 (docker), SQLite, DuckDB and Postgres: discovery, read-only session, staging, gateway query with a denied column | SQLite (this run) | Not certified: SQL Server, Oracle, Snowflake, BigQuery, Databricks, Redshift, ClickHouse, MariaDB (Trino certified 2026-10-29, TRN-004 row) |
| Metadata crawler | `services/crawler.py`, `skills/catalog.py` | 72 catalog-skill tests; crawler rule tests; integration lifecycle (drift, curation kept, deprecation only on full uncapped crawls, re-staging) | ✅ | English keyword heuristics; renames are candidates only |
| Token economy | `llm/router.py`, `llm/cache.py`, `agents/common.model_gate` | router tests: off/auto, cache, oversize refusal, overrides, downgrade within allowlists | ✅ 15 skips in one run | `tokens_saved` for skips is an estimate (prompt-size heuristic) |
| Admin control plane | `contracts/platform.py`, `services/platform_settings.py`, `api/routers/admin.py` | integration: admin-only, reference validation, preset → router, rollback, stale-merge guard | ✅ preset + rollback | 5 s propagation between processes |
| SQL explain | `skills/sqlexplain.py` | unit tests | ✅ | Explains structure, not intent |
| Forecasting + forecast-deviation monitor | `skills/forecast.py`, `services/monitors.py` | 18 forecast tests (coverage over 30 seeds) + monitor test | ✅ | Holt-Winters ~0.1–0.3 s per series |
| Investigator breadth | `agents/investigator.py` | role playbook, outcome-diverse selection, driver-model deduplication, KPI × dimension continuation tests | ✅ 3/3 planted, 0 false | Continuation is one dimension per outcome per round |
| Increment-3 UI | `web/src/pages/{Catalog,AdminSettings}.tsx`, `components/CrawlPanel.tsx` | 26 new vitest tests (76 total) | smoke incl. Catalog | Column-level curation not in the UI |

## 2026-09-25 — Design review and next delivery scope

The [design review](04-design-review.md) inspected spec v2, architecture/ADRs, the existing
tracker/readiness/evidence, the UI screen map and relevant API, service, contract and verification
code. The resulting target design covers workspace semantics, UI/API evolution, ML, engineering,
evidence and evaluation. P4–P6 in the tracker contain the implementation acceptance criteria.

This is documentary evidence only. No runtime capability, test count, connector certification
or release-readiness promotion is added. Existing live runs were not repeated. Validation for
this change checks changed Markdown links, code fences, tracker IDs/status consistency and diff
whitespace; application tests and browser execution are not part of this documentation change.
## 2026-09-25 — Increment 4, wave 0 (plus X05–X08 and U01)

Same environment. Built in parallel streams, merged with re-chained migrations
(0005 → 0006 → 0009 → 0007 → 0008 → 0010, verified up, down to 0005 and up again).
Suites at merge: 1,197 unit, 102 integration, 114 vitest, 21 Playwright.

### Live evidence (P4-C08)

| Evidence | Result | Notes |
|---|---|---|
| [`e2e-20260925-171115.md`](evidence/e2e-20260925-171115.md) | 26/26 | Full model use; 382 s; $0.29; 7 distinct KPIs, all percent KPIs fractions; new cross-workspace isolation probe passes |
| [`e2e-phase3-20260925-171755.md`](evidence/e2e-phase3-20260925-171755.md) | 13/13 | One alert (de-duplicated), but it read 0.3827 — see findings |
| [`e2e-20260925-173738.md`](evidence/e2e-20260925-173738.md) | 26/26 | After the fixes; OpenRouter returned HTTP 402 (credits) for large `max_tokens` requests, so planning, hypotheses, follow-ups and semantic modelling ran deterministically; 81 smaller calls succeeded |
| [`e2e-phase3-20260925-173848.md`](evidence/e2e-phase3-20260925-173848.md) | 13/13 | One alert at 0.04192 (matches the data); KPI definition stable across the scheduled run |
| [`sse-load-20260925-165049.md`](evidence/sse-load-20260925-165049.md) | pass | 200 streams, p95 38.9 ms, 0 slow-callback warnings |

**Defects found by the live re-runs, fixed and tested:**
- A low-cost model (`deepseek/deepseek-v4-flash`) wrote a finding in Chinese that passed the numbers
  guard. Model narratives must now be English; words quoted from the evidence may be in any script.
- A scheduled run's model re-proposed `critical_incident_rate` as `priority = 1 OR impact = 1 OR
  urgency = 1`, and the artifact upsert silently replaced the carried-forward definition. A KPI name
  now keeps its first definition; runs without a scheduled predecessor inherit the workspace's latest
  completed run's definitions.
- The threshold monitor resolved its KPI by name and followed the redefinition, alerting on 0.38
  instead of 0.04. Monitors now pin the definition they measure (`config.pinned`, ignored by the
  condition key).
- On HTTP 402 the router tried every fallback model per call (32 failed calls). A credit refusal now
  cools the provider down for 60 s, and callers take their deterministic path without a request.

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Workspace model policy (samples, providers, cost approval, residency) | `llm/router.py` `CallContext.for_policy` | `test_model_policy.py` | — | No provider regions configured yet: residency blocks all models until set; prices are list-price estimates |
| Structured prompt trimming, prompt version hashes | `agents/common.fit_payload`, `agents/prompts.py` | `test_prompt_payload.py` | ✅ | — |
| Replayable model calls | `llm/replay.py`, `model_payload` | `test_model_replay*.py` | — | Replays calls, not a whole run end to end |
| Query budgets (critic re-runs, Ask) | `governance/budgets.py`, `agents/critic.py` | `test_tool_gate_and_budgets.py` | ✅ | — |
| Workspace isolation in depth | `staging/roles.py`, gateway `SET LOCAL ROLE`, lineage key, Neo4j keys | `test_workspace_isolation.py` (validator bypass) | ✅ probe | The reader login can `SET ROLE` to any workspace role; isolation relies on the gateway choosing the role |
| Tool-gate coverage | `ToolRuntime.authorize`, `gate_agent_write` | `test_tool_gate_and_budgets.py` | ✅ | Exempt paths named in spec v2 §6 |
| Non-blocking SSE | `events/stream.py` | `test_sse_stream.py`, load script | ✅ 200 streams | Measured on the standard asyncio loop |
| Replan supersedes artifacts | `artifact.plan_version` | `test_e2e_local_run.py` redirect test | ✅ | — |
| Population of staged snapshots | `connectors/sampling.py`, `staging/snapshots.py`, critic `representative_population` | `test_snapshot_sampling.py`, `test_snapshot_population.py` | — | ServiceNow/files: `full` and `first_n` only |
| Capability manifests and registry | `contracts/capability.py`, `capabilities/registry.py` | `test_capability_registry.py` | — | Enablement, certification gating and plan-hash binding pending (X01) |
| MCP client | `mcp/client.py`, `core/config.py` | `test_mcp_logic.py`, `test_outbound_http.py`; `test_mcp_client.py` (SDK-built test double, optional MCP extra) | — | Exact platform host allowlist at registration, workspace allow, and call time; DNS/IP pinning. Not registered against a real Superset/dbt MCP server |
| MCP server | `mcp/server.py`, `mcp/grants.py` | `test_mcp_server.py` | — | No per-client OpenAPI, no MCP prompts, no OAuth yet |
| Domain packs | `packs/{itsm,sales}`, `skills/hypothesis_templates.py` | grep test, pack benchmarks (3/3 planted each) | ✅ via demo | Pack KPIs not yet read by the semantic agent |
| Connector certification from evidence | `connectors/certification.py`, `scripts/certify_connectors.py` | `test_connector_certification.py` | postgres, mysql, sqlite, duckdb | Warehouses remain `tested` |
| Five-journey UI shell | `web/src/routes.ts`, `components/CommandPalette.tsx` | 114 vitest, 21 Playwright (axe AA) | — | Journey content (U02–U07) not built |


## 2026-09-25 — Increment 4, wave 2 (token and decision economy) plus S02, U03, U06, U07

Merged from parallel streams onto `claude/gracious-knuth-ievauj`. Migrations chain
0011 → 0015 (registries) → 0016 (task claims) → 0012 (ladder) → 0013 (decisions) → 0014 (prompt cache).
Up/down/up was verified on a scratch database after each re-chain.

### Measured (fake transport; live model runs are blocked by exhausted OpenRouter credits)

| Measure | Before | After | Evidence |
|---|---:|---:|---|
| Chat calls per standard run | 20 | 0 | `evidence/token-default-20260925-183030.md` |
| Decision calls per standard run | 15 | 7 (`rev_second_opinion`) | same |
| Tokens per standard run | 25,075 | 1,435 | same; guarded by `scripts/cost_gate.py` in CI |
| Prompt tokens, compiled purposes | 18,942 | 15,342 (−19 %) | `evidence/context-compiler-20260925-182819.md` |
| Ask registry hit latency | — | p50 < 1 s asserted (no model call) | `test_registries.py` |
| Queries per engine task | measured | — | `evidence/engine-queries-20260925-182754.md` |

### Capabilities

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Execution ladder, `answered_by` per call | `llm/router.py` (`ladder`, `mode_of`), `config/models.yaml` `ladders:` | `test_ladder_and_budgets.py` | — | The JEV rung counts as a model rung, so `off` removes it |
| Deterministic-first default | `contracts/platform.py` presets, `agents/{publisher,visualization,critic}.py` | `test_token_default.py`, `test_cost_gate.py` | — | Planning is not yet merged into hypothesis generation |
| Context compiler with receipts | `context/compiler.py`, `agents/common.compile_for` | `test_context_compiler*.py`, `test_context_tokens.py` | — | Lexical relevance only (hybrid ranking is P4-K05) |
| Prompt-cache layout and accounting | `llm/router.py` `wire_messages`, `cached_prompt_tokens` | `test_prompt_cache.py` | ❌ not measured | Stable prefix ≈ 18 % of sent text; the ≥ 60 % target is unlikely without a larger header |
| Verified-query and hypothesis registries | `registries/`, `agents/sql_agent.py`, `agents/investigator.py` | `test_registries.py`, `test_registry_logic.py` | — | — |
| Knowledge version in cache keys | `context/version.py` | `test_context_compiler_db.py` | — | — |
| Redis budget counters, price table | `runtime/budget_counters.py`, `runtime/usage.py` | `test_budget_counters.py` | — | Ask's rolling-hour budget still counts in the database; JEV has no list price (`missing_price`) |
| DecisionService, authority classes | `decisions/` | `test_decision_service.py`, `test_decisions_db.py` | — | Local-classifier weights are hand-set (v1) |
| Decision calibration and downgrade | `decisions/calibration.py`, `analystos calibrate` | `test_decision_calibration.py`, `test_decisions_db.py` | — | No Operate screen for the report yet |
| CI cost gate | `scripts/cost_gate.py`, `tests/fixtures/cost_baseline/` | `test_cost_gate.py` | — | New agent call sites are only caught once the fixture is re-recorded, or by `test_token_default.py` |
| Engine loop without locks or N+1 | `runtime/engine.py`, `artifacts/registry.py`, `governance/policy.py` | `test_engine_claims.py`, `test_engine_queries.py` | ✅ | In-flight Temporal workflows must be drained before deploying |
| Investigation board, Operate, capability-driven UI | `web/src/` | vitest, Playwright journeys | — | Capability invoke endpoint pending (P4-U02 stream) |

### Merge decision recorded

When the ladder and the DecisionService were merged, their meanings of `auto` differed. The
ladder used `auto` to mean "rules first, then the model". The DecisionService used it to mean
"the rule decides".

The resolution: under `auto`, the service moves `rules` to the front of the purpose's backend
chain, so JEV answers only what the rule leaves open (a tie, an abstention, an escalation). `off`
removes the model backends. Direct `JevDecisions` callers keep "auto = the rule decides". The
cost gate is unchanged (7 and 3 calls).

## 2026-09-25 — Increment 4, wave 3: semantic layer (P4-K03)

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| Ossie 0.1.1 semantic model, pinned schema | `semantic/ossie.py`, `semantic/schema/` (+ `PROVENANCE.yaml`) | `test_semantic_ossie.py` (upstream examples) | — | Upstream's Salesforce fixture fails its own SQL rule; recorded, not hidden |
| Metric approval workflow | `semantic/service.py`, `governance/approvals.py` (`ALWAYS_SEPARATE_DUTIES`) | `test_semantic_layer.py` | ✅ deterministic run: run 1 refused at publish, approved, run 2 publishes (37 s) | Semantic-model structure versions have no approve endpoint |
| dbt 1.12 import/export | `semantic/dbt.py` | `test_semantic_ossie.py` fixtures from real `dbt parse` | ✅ by hand (dbt-core 1.12.0 + metricflow 0.213.0) | dbt is not in the project environment; metricflow mangles percent KPIs (export warns) |
| Publish gate on approved metrics | `semantic/service.gate_bundle`, `build_bundle` | `test_semantic_ossie.py`, `test_semantic_layer.py` | ✅ | Existing workspaces default to off; new ones on |

## 2026-09-25 — Increment 4, waves 4–6: build, Ask, self-hosting, evaluation (E04–E06, U02, S04, V01)

Migrations 0017 → 0020 (build gateway) → 0021 (Ask threads) → 0023 (user identity); up/down/up verified.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| BuildGateway write identity | `build/gateway.py`, `build/targets.py`, `deploy/postgres/01-init.sql` | `test_elt_build.py` (Postgres refuses non-target, source and `public` writes; approval tamper/expiry) | ✅ | Postgres analytics engine only |
| dbt builder playbook | `build/project.py`, `build/runner.py`, `build/lineage.py`, `playbooks/elt_build.v1.yaml` | `test_build_project.py`, `test_elt_build.py` | ✅ dbt Core 1.12.5, 20,000 rows, 3/3 tests (`evidence/elt-build-dbt-20260925.md`) | dbt lives in a separate venv (`ANALYSTOS_DBT_EXECUTABLE`), not in the worker image; tables only (no incremental) |
| dlt vs staged loader | `scripts/spike_dlt_vs_staged.py`, ADR-0016 | — | ✅ measured | Static mock data |
| Ask threads, stages, decisions, promote | `services/ask.py`, `api/routers/ask.py`, `agents/sql_agent.py` | `test_ask_threads.py`, `test_ask_threads_api.py`, vitest, Playwright | — | Tool rung unused; dashboard promote creates an approved chart, not a Superset publish |
| Capability invoke | `capabilities/invoke.py` | `test_ask_threads*.py` | — | Methods run only inside investigations |
| Paste-SQL explain | `QueryGateway.explain` | `test_ask_threads_api.py` | — | Postgres plans only |
| Model providers + air-gapped egress guard | `llm/providers.py`, `config/models.airgapped.yaml` | `test_model_providers.py` | ❌ no local model run | Bedrock against a double |
| OIDC SSO + ABAC | `security/oidc.py`, `governance/policy.py` | `test_oidc_abac.py`, `test_oidc_sso.py` (fake IdP) | ❌ | No real IdP login yet |
| Sandbox network isolation | `sandbox/` | sandbox tests | ✅ | Falls back to not isolated under the chart's default seccomp; NetworkPolicy is the boundary |
| Helm chart, offline bundle | `deploy/helm/analystos`, `scripts/bundle_images.sh` | `test_helm_chart.py` (skips without helm) | — | helm not in CI |
| Analytical benchmark | `evaluation/`, `scripts/benchmark_analytical.py` | CI `--check` (precision/recall ≥ 0.90, FDR ≤ α) | ✅ deterministic + platform tiers (`evidence/2026-09-25-analytical-benchmark-*.md`) | No live-model report; effects well above materiality |

## 2026-09-25 — Increment 4, wave 3 knowledge and wave 4 engines (K01, K02, K09, K10, E01, E03)

Migration 0018 (knowledge packs) now follows 0023. The chain is 0017 → 0020 → 0021 → 0023 → 0018, and it has been checked up, down and up.

| Capability | Code | Automated coverage | Live | Limitation |
|---|---|---|---|---|
| OKF knowledge packs, revisions, index | `knowledge/{okf,store,index,platform}.py` | `test_knowledge_okf.py`, `test_knowledge_pack.py` (reindex identity, HNSW, isolation) | ✅ seeded platform pack (27 documents) | `ts_rank_cd`, not BM25 |
| OKF / Atlas bundle import and export | `knowledge/bundle.py` | real Atlas-exported sample, byte-identical round trip | — | No export API route |
| Context providers | `knowledge/providers.py`, `mcp/client.py` | MCP SDK server + MOCK fixture | ❌ no live Atlas | External results not in prompts yet |
| Embedding providers, re-embed | `knowledge/embeddings.py`, `scripts/benchmark_embeddings.py` | benchmark evidence | — | Default model chosen on the benchmark set |
| Engine protocol, multi-dialect validator | `engines/`, `gateway/dialects.py` | `test_gateway_dialects.py` (286 cases) | ❌ no warehouse instance (Trino: live, TRN-004 row) | Snowflake/BigQuery/Databricks stay staged; Trino pushes down on request |
| Federated cross-source runs | `QueryGateway._execute_federated`, `skills/federation.py` | `test_federation.py`, `test_cross_source_runs.py` | ✅ Postgres + DuckDB file | Not yet a playbook step; no filter pushdown per leg |

Evidence: `evidence/2026-09-25-knowledge-k01-k10.md`, `evidence/engines-federation-20260925.md`.

### 2026-09-25 — governance review of the wave 4–6 merges, and fixes

A read-only governance review of the build, Ask and self-hosting merges found no blockers, 5 major findings and 11 minor ones. All are fixed, each with a test.

| Finding | Fix | Test |
|---|---|---|
| M1: an IdP claim could grant PII clearance (widening scope; any string was truthy) | Platform-controlled attributes (`*clearance*`, `is_admin`, `sso_managed`) are never mapped or synced from the IdP. Clearance counts only when it is the boolean `True`. | `test_idp_claims_never_grant_pii_clearance`, `test_idp_claims_never_grant_or_revoke_pii_clearance` |
| M2: a capability invoke approval was usable by any workspace member | `requested_by` is in the hashed payload, so only the requester can execute it. | `test_an_approval_runs_only_for_the_user_who_requested_it` |
| M3: a build did not re-check scope at execution | `execution_scope_check` re-validates every rendered model and the dataset SQL against the requester's current scope, right after `verify_for_execution`. | `test_gateway_rechecks_the_data_scope_at_execution` |
| M4: the default builder password could be provisioned in production | `check_builder_credentials` refuses a missing or default password outside dev. The builder URL is added to the chart's required secrets. | `test_build_targets_guard.py` |
| M5: sandbox children had network access in a default Helm install | Superseded by P4-02 (2026-09-26): `ANALYSTOS_SANDBOX_ISOLATION=auto|container|process|off`, fail closed when no backend isolates; Helm default `process` (`sandbox/isolation.py`) | `test_sandbox_isolation.py` (hostile probes on process and container backends) |
| m1–m11 | Invoke gate runs before approval use; editor role for side effects; Azure internal only with `private_link`; Bedrock endpoint egress check; OIDC byte compare, exact issuer and admin only for SSO-created accounts; `AttributeRule` needs a condition; explain hides out-of-scope relations; runbook notes | per-finding unit and integration tests |

Compatibility: a stored workspace policy with an attribute rule whose `require` is empty now fails validation on load (m9).

## 2026-09-26 — cost controls, Ask reliability, roadmap P0 rows

The owner reported roughly $10 of OpenRouter credit spent in a day, mostly on Sonnet. This container's ledger recorded $3.31 in total ($2.98 of it Sonnet), all during the live evidence runs on 2026-09-25. Two causes were found:

- `scripts/e2e_increment3.py` restored the `max_quality` preset (Sonnet first).
- Dev schedules and monitors (16 schedules, 28 monitors) were left enabled, and the scheduler was running with the key in its environment.

| Capability | Code | Coverage | Measured |
|---|---|---|---|
| Cheap-first routing with escalation only on a failed check | `config/models.yaml` (`escalation_models`), `llm/router.py` (`validate`, `_run_tier`), migration 0027 (`escalated_from`) | `test_escalation_and_caps.py`, `scripts/cost_gate.py` (usd metric, `escalation.json`) | Model-heavy standard run $0.0853 → $0.0090 (9.4× cheaper); a novel question $0.0249 → $0.0050 (fake transport, price table) |
| Hard spend caps | `runtime/budget_counters.py` (atomic Lua reservation), `llm.daily_spend_cap_usd` (default $2/day), monthly workspace cap, 80 % alerts | `test_escalation_and_caps.py`, `test_spend_caps.py` (competing threads never overshoot) | — |
| Model health | `GET /api/admin/models/health` (`?probe=1`), Admin models panel | `test_escalation_and_caps.py`, `modelHealth.test.tsx` | — |
| Demo cleanup | `[demo]` naming and end-of-script disabling in evidence scripts; `analystos schedules disable-demo` | script tests | This container's 44 leftover demo items disabled |
| Precise Ask refusals | `agents/common.ModelOutcome`, `services/ask.MODEL_REFUSALS`, web refusal card | `test_ask_model_refusals.py` (one per reason) | "no model route" is now `no_api_key`, `provider_cooldown` (with seconds left), `cap_reached`, and so on |
| Deterministic Ask rules | `agents/ask_rules.py`, `skills/sqlbuild.aggregate_query`, the pack-declared `ask_distributions` | `test_ask_rules.py`, Ask benchmark | Off-tier accuracy 0.495 → 0.619 with no model; 0 confident wrong, 0 leaks (`evidence/2026-09-26-ask-benchmark-off-rules.md`) |
| Connection pooling | `db/pools.py`, PgBouncer (compose `pooled`, Helm) | `test_db_pools.py`, `test_semantic_concurrent_saves.py` | 50/50 concurrent runs (`evidence/load-s05-runs-20260926.md`) |

## 2026-09-26 — FND-006 agent contract: manifest is the only source; knowledge and output contract enforced

| Capability | Code | Coverage | Measured |
|---|---|---|---|
| `AgentSpec` reconciled with the manifest | `capabilities/agents.to_agent_spec`/`contract_for`, `tools/registry.sync_agent_definitions`/`get_agent_spec`, `api/routers/admin.py` (`GET /api/agents` from manifests; `POST`/`PATCH` with `spec` refused `manifest_only`), `capabilities` reload refreshes the row cache | `test_agent_contract.py::test_every_agent_view_field_equals_its_manifest`, `::test_admin_api_and_rows_derive_from_the_manifest_never_the_row` (a hand-edited row never leaks) | All 18 built-in agents: every view field equals its manifest |
| Knowledge contract | `AgentKnowledge` (`sections`/`purposes`, `budget_chars`), `agents/common.knowledge_scope`, `context/compiler.compile_context(knowledge_chars=)` | `::test_an_agent_only_receives_its_declared_knowledge_sections` (undeclared sections neither loaded nor compiled; no block = no knowledge), `::test_the_agent_knowledge_budget_caps_the_knowledge_sections` | — |
| Output contract | `AgentOutputDecl` (`contract:<module>.<Model>` or JSON Schema), `enforce_output` in `artifacts/registry.save_artifact` and `RunContext.check_output` (hypothesis, experiment, insight, publication); load-time checks in `capabilities/validation.py` | `::test_enforce_output_refuses_undeclared_and_off_schema_outputs`, `::test_agent_artifacts_are_checked_against_the_contract_before_they_are_written` (nothing refused is written), `::test_bound_contract_governs_the_run`, `::test_hypotheses_off_the_contract_are_dropped_with_their_reason`, `::test_bad_contract_declarations_fail_the_load` | — |

Verification on 2026-09-26, merged with `origin/claude/gracious-knuth-ievauj` 35781ac:
- Unit suite: 2127 passed, 7 skipped. `ruff` clean.
- Web: build passes, vitest 188 passed.
- Integration: 228 passed, 12 skipped, 2 failed. Neither failure is caused by FND-006:
  - `test_duckdb_engine_run` (result-hash determinism) fails identically on base 35781ac.
  - `test_workspace_list_counts` depends on test order and passes when run alone; its writes use `run_id=None`, which the output contract does not check.
- No migration: `agent_definition.spec` is JSON.

## 2026-09-26 — Combined head: run determinism, re-embed pool, final test pass, $1-capped live run

| Capability | Code | Coverage | Measured |
|---|---|---|---|
| Identical runs build identical datasets, KPIs and chart previews | `db/models.by_code` on run-scoped Hypothesis/Insight reads (critic, insight, investigator, sql_agent, supervisor, visualization); `artifacts/registry.save_artifact` gives each artifact its own `created_at`; explicit orders in `governance/policy.resolve_scope`, registry replay, profiling histograms | `tests/unit/test_run_ordering.py`, `test_skills_profiling_quality.py`; `tests/integration/test_duckdb_engine_run.py` | Passed 4 runs in a row (previously failed on shared `created_at` ties) |
| Re-embed does not break other pooled connections | `knowledge/index._use_provider` disposes the engine pool once the retype commits | `test_knowledge_pack.py::test_reembed_changes_dimension_and_rebuilds_the_hnsw_index` | 3/3 green (previously flaky in the full suite) |
| Deterministic rungs first; the model is used only when rules cannot answer | `config/models.yaml` ladders; `scripts/e2e_demo.py` check `decisions_recorded_with_rung` (replaces `jev_decisioning_used`, which wrongly required a billed decision call) | [`evidence/e2e-20260926-060649.md`](evidence/e2e-20260926-060649.md) 26/26; [`evidence/ask-live-20260926.md`](evidence/ask-live-20260926.md) | Full scenario: 0 billed calls, 35,825 tokens saved. Ask SQL generation: `gpt-5.4-mini`, $0.00265, no escalation |

Final pass on the combined head (determinism fix, FND-006 and the re-embed fix):
- `ruff`: clean.
- Unit suite: all passed.
- Integration, first run: one failure, `test_workspace_list_counts`. It depends on test order: it passed when run alone and in the second full run.
- Integration, second run: one failure, the re-embed test. It is fixed above.
- Web: build passes, vitest 188 passed, Playwright 59 passed.

## 2026-09-26 — Increment 7 wave 1 (spec v4): trust core, security ports, definitions, semantics, lite profile, engineering

Integrated head `b82a69c` on `claude/cool-heisenberg-isaz0q` (six parallel branches merged, migrations
re-chained 0030 → 0032 → 0033 → 0036 → 0034 → 0035). Local stack: Postgres 16 + pgvector 0.8.1, Redis 7,
no Temporal, Superset or model key. Combined results on that head: `ruff` clean; unit 2918 passed, 9 skipped;
integration 247 passed, 31 skipped (skips are services not running); deterministic evaluation gates v1 PASS
(grounding, analytical component precision/recall 1.000, FDR 0, cost rise 0); the lite smoke (core + reports
extras only, no Redis, no Temporal) 11 passed; web vitest 191 passed, typecheck and build clean.

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P7-01 verdicts that void themselves | `evidence/verification.py`, migration 0032, voiding in `evidence/manifest.py`, `semantic/service.py`, `governance/policy.py`, `knowledge/index.py`, capabilities reload; consumers in `agents/publisher.py`, `services/reports.py`, `reports/*`, `knowledge/attested.py` | `tests/unit/test_verification_p701.py` (25); `tests/integration/test_verification_records_live.py` | A governed run: step edit, cited glossary edit, metric v2 approval and an unannounced method bump each void exactly their dependents; the nightly sweep reports the method bump as a late void; the publish gate and reports refuse VOID; migration 0032 up/down/up. Limits: baseline monitors don't read the state yet; no explicit re-verify action (a new run creates new records) |
| P7-08 "why this number?" (API) | `evidence/why.py`; `GET /api/insights/{id}/why`, `GET /api/workspaces/{ws}/analysis/{run}/why` | same files | Six links per number (fact, step, query receipt, data version, semantic version, verdict), each with a state; a tampered result hash shows as broken. Limits: UI drawer and Ask-turn numbers pending |
| P7-15 gateway gaps | `gateway/dialects.py`, `gateway/validator.py` (`_join_keyed`, `_single_row_cross_join`) | `tests/unit/test_gateway_corpus.py` + `tests/fixtures/adversarial_sql_corpus/*.json` | Atlas corpus 106/106 match expectations; `fn_send_mail(...)` refused on postgres and tsql; NOLOCK/UPDLOCK/HOLDLOCK/TABLOCKX/XLOCK/READPAST refused; `ON TRUE OR a.k=b.k`, `ON b.k=b.k`, comma and CROSS joins refused; owner decision: CROSS JOIN only to a provably single-row derived table (ungrouped aggregate or LIMIT 1) |
| P7-10 injection, redaction, approval claim | `security/injection.py` (port), `llm/redaction.py`, `governance/approvals.claim` | `tests/unit/test_security_injection.py`, `tests/unit/test_llm_redaction.py`, `tests/integration/test_approval_claim.py` | Unseen attacks caught 38/40 (was 8/40), benign flagged 0/46 (was 2/46), donor corpus 37/37 (was 10/37); SSN/IBAN (mod-97) redacted; restorable question tokens in Ask; two threads race a Postgres claim, exactly one wins |
| P7-11 outbound HTTP | `tools/http.py` (port), `mcp/client.py`, `capabilities/invoke.py` | `tests/unit/test_outbound_http.py` (55) | CGNAT, loopback, metadata/link-local, DNS rebinding, redirects and the byte cap refused; real-socket pinning with Host/SNI kept. Owner decision: MCP servers on loopback and RFC 1918 allowed by default (`ANALYSTOS_MCP_PRIVATE_HOSTS`), metadata and CGNAT never. Limit: draft-tool-over-MCP and query-tool lifecycle |
| P7-03 definitions and pinned schedules | `services/definitions.py`, `services/pins.py`, migration 0033, `api/routers/definitions.py` | `tests/unit/test_definitions_pins.py`, `tests/integration/test_pinned_schedules.py`, `test_workbench_contracts_api.py` | A real pack upgrade through the admin reload leaves the next fire on pinned versions; a newer approved metric shows "upgrade available" with a diff and doesn't change the next fire; a retired pin blocks and notifies; fires state `nothing_changed`. Limits: dev → test → prod promotion; method code can't be frozen (a version bump shows as an upgrade) |
| P4-06 work orders, idempotency, outbox | `contracts/work.py`, `services/{idempotency,dispatch,work_orders}.py`, `api/http.py` | `tests/unit/test_idempotency_outbox.py`, `tests/integration/test_workbench_contracts_api.py` | Duplicate keys replay; a different body is 409 `idempotency_conflict`; If-Match/ETag 412/428; cursor pages with old-client arrays; run + outbox in one transaction; relay and orphan reconciliation in the scheduler loop; cancel before dispatch never starts |
| P7-02 semantic compiler | `semantic/compiler.py`, `governance/row_filters.py`, `semantic/diff.py` (port), `semantic/check.py`, `evaluation/ask_governed.py`, `config/models.yaml` `semantic_query` | `test_semantic_compiler_joins`, `test_row_filters`, `test_semantic_query_purpose`, `test_semantic_diff`, `test_check_semantics`, `test_ask_benchmark::test_governed_slice…` | Governed slice: 16/16 compile to identical SQL, rows match gold, repeat gives the same SQL, 0/6 negatives labelled governed, 0 model calls; fan-out refused with the edge named; `{{user.attr}}` fails closed; `analystos check-semantics` in CI. Limit: multi-fact plans (refused), relative time windows |
| P7-09 relationship cardinality | `skills/relationships.py`, `semantic/review.py`, migration 0034 | `test_composite_keys`, `test_relationship_assessment`, `integration/test_semantic_review` | Composite keys and FK candidates measured through the gateway; Atlas `assess_relationship` rules on measured facts; acceptance is hash-bound, another person, fresh measurement; a body naming `cardinality` is 422 |
| P4-05 reviewed definitions | `semantic/service.py`, `semantic/review.py` | as above + `test_semantic_review_rules` | Structure approval with separation of duties; stale approved metrics deprecated with the reason; denominator-mismatch conflicts; `GET /semantic/reconciliation`; lineage `metric → table`, `table joins_to table`. Limit: a live analyst workflow run, ownership transfer |
| P7-16 lite by default | `workflows/orchestrator.LocalRuntime`, `runtime/budget_counters.py` + migration 0035, `core/profiles.py`, `compose.yaml`, `capabilities/enablement.py` | `tests/unit/test_local_orchestrator.py`, `tests/integration/test_lite_profile_db.py`, `test_e2e_local_run.py`, CI job `lite` | Restart mid-run resumes and completes; a two-hour approval wait completes; spend caps hold with Redis absent (row-locked, fail closed); unavailable capabilities report the reason. The code default profile stays `standard` so existing deployments keep Temporal; compose and `.env.example` default to lite |
| P7-17 slim images | `pyproject.toml` extras `ml/reports/temporal/graph/standard`, guarded imports, `deploy/docker/Dockerfile`, `Dockerfile.mock`, `values-small.yaml`, runbook tiers | `test_lite_profile.py` (every setting documented in a tier) | Installed packages 900 MB → 680 MB core (765 MB with reports; mock 229 MB); API import ~2.0 s → ~1.3 s. Not measured: Docker image sizes, Helm install on a 2-CPU node |
| P7-07 evaluation gates | `config/eval_gates.yaml`, `evaluation/{gates,grounding}.py`, `scripts/eval_gates.py`, CI | `tests/unit/test_eval_gates.py` | Seeded regressions (lenient binder, a checker that never voids, cost rise, precision drop, CLI exit) each fail the gate; v1 values precision 1.0, recall 1.0, FDR 0, cost rise 0, Ask fake 0.99, Ask off 0.62. Live tiers report skipped without a key; `--require-live` fails a release on skip |
| P6-04 recipe IR and compilers | `contracts/recipe.py`, `recipes/{compiler,execute,gates,lineage,dbt}.py`, migration 0036 | `tests/unit/test_recipe_ir.py`, `tests/integration/test_recipes.py` | The same recipe gives equal results through the gateway and DuckDB; undeclared columns and implicit casts refused; compiled SQL passes the gateway in postgres, tsql and duckdb; join pre-flight refuses violated cardinality; the dbt project passes `build/project.check_files`. Limit: `dbt parse` skips without dbt; pivot and incremental nodes; isolated pool (P7-06) |
| P6-05 DQ gates and baselines | `recipes/gates.py`, `services/monitors.py` (port of Atlas baselines) | `test_gates_keep_last_good_quarantine_and_drop`, `tests/unit/test_monitor_seasonal_baselines.py` (17) | A failing `fail` gate keeps the previous output byte-identical and quarantines; `drop` rows counted and read back through the gateway; schema policy evolve/warn/strict |
| P6-06 file ingestion | `services/file_ingest.py`, `staging/loader.py` | `tests/unit/test_file_ingest.py`, `tests/integration/test_file_ingest_loads.py` | Merge idempotent on retry; null/repeated keys and mismatches refused with the column named; CSV, JSON array, NDJSON, Excel, Parquet |
| P6-07 column lineage | `evidence/lineage/{sql,dbt}.py`, recipe IR lineage | `tests/unit/test_sql_lineage.py` (55 Atlas cases + new), `tests/unit/test_dbt_column_lineage.py` (13) | Traces through CTEs to base columns; OpenLineage events validate against the pinned facet; SQL literals redacted before storage |

### Donor ports (P7-13) and licence decision

All three repositories belong to the same owner, who authorised on 2026-09-26 reusing Atlas and
DataPilot code inside AnalystOS ("proceed and copy the required code"). Neither donor has a licence
file; this authorisation is the licence basis for the ported code. Ported modules record their origin in
their docstrings:

| Donor | Commit | Ported into AnalystOS |
|---|---|---|
| `AIDataAnalyst` (Atlas) | `8b48fd9` | `injection_defense.py`, `prompt_risk.py` (generic signals), `ingest_screening.py`, `question_redaction.py`, `sql_redaction.py` (rewritten) → `security/injection.py`, `llm/redaction.py`; adversarial SQL corpus → `tests/fixtures/adversarial_sql_corpus/`; `semantic_diff.py` → `semantic/diff.py`; `composite_key_inference.py` bounds and `relationship_validation.assess_relationship` rules → `skills/relationships.py`; `data_quality.py` weekday/month-end baselines → `services/monitors.py`; lineage tests → `tests/unit/test_sql_lineage.py`, `test_dbt_column_lineage.py` |
| `AienginnerAgentOs` (DataPilot) | `15235dc` | `tool_runtime.py` SSRF-safe call → `tools/http.py` (with `is_global` fix); quality rule types → `recipes/gates.py` (rewritten through the gateway) |

No non-permissive dependency was added (new: `jsonschema`, MIT).

## 2026-09-26 — Integrated product and tracker audit at e9b0f05

Independent rerun: 257 focused backend cases passed (verification, semantic compilation/joins,
definition pins, evaluation gates, gateway corpus, governed benchmark); frontend 191 passed / 1
skipped; TypeScript and production build passed after correcting local Playwright dependency drift.
Deterministic grounding, analytical-component, and cost gates passed. These are fixture results,
not live-model/warehouse certification. Restored N-12 from `63a3d37` to the tracker as Not started;
it had disappeared from the integrated tracker. No capability was promoted to Done by this audit.
Commands, scope, limitations, product assessment and follow-ups are recorded in
[the integrated validation review](../70-reviews/2026-09-26-integrated-product-validation.md).

## 2026-09-27 — Increment 7 wave 2: governed ML, isolated compute pools, pipelines, brief/steps/notebooks, light UI

Integrated on `claude/cool-heisenberg-isaz0q` from `develop@ed38523` (PR #9 merged): five branches merged,
migrations chained 0035 → 0040 → 0039 → 0038 → 0037. Two API schema-name collisions found in the merge
(`RunIn`, `Decision`) were renamed, and `tests/unit/test_openapi_schema_names.py` now fails on any colliding
model name. Local stack: Postgres 16 + pgvector 0.8.1, Redis 7, no Temporal service (a Temporal dev server
ran the worker conformance suite), no Superset, no model key. Combined results on `2cb8380`: `ruff` clean;
unit 3098 passed, 11 skipped; integration 270 passed, 31 skipped; web vitest 240 passed, typecheck and build
clean, Playwright 73 passed.

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P5-01 MLSpec, leakage, splits, bounded search | `contracts/work.py` (`MLSpec`, `ExperimentSpec`), `ml/readiness.py`, `ml/splits.py`, `ml/jobs.py` (`run_ml_job`), migration 0037 | `tests/unit/test_ml_core.py` (24), eval gate `ml` | Leakage fixtures refused (7/7); chronological with embargo, group and group+chronological splits; hard trial/time/row/feature caps; deterministic re-run. Limit: jobs run on the `compute` queue until wired to `compute-ml` (wave 3) |
| P5-02 holdout, backtests, model card | `ml/` evaluation modules, P7-01 dependency kinds `ml_dataset`/`ml_split`/`ml_package` | `test_ml_core.py`, `tests/integration/test_ml_governed.py` | Holdout claimed once; a different spec on a consumed holdout is refused; paired-bootstrap uncertainty, slices, guardrails; no-improvement abstains; evidence dimension `predictive_evaluated` |
| P5-03 promotion, scoring, monitoring (backend) | `services/ml.py`, `api/routers/ml.py` | `test_ml_governed.py` | Train → evaluate → approve → score → monitor end to end; duplicate scoring detected; rollback; schema-drift refusal; a tampered package voids the verdict and refuses promotion; retraining makes a challenger, never a silent promotion. UI pending (wave 3) |
| P5-04 ML method pack | `capabilities/builtin/ml/methods.yaml` (`requires: [extra:ml]`), `ml/` | `test_ml_core.py`, `evaluation/ml.py` | forecast (ETS/ARIMA vs seasonal naive), classify, regress, cluster (stability), anomaly (history-calibrated); all numeric decisions in code; one native thread per job |
| P5-05 experiment records, MLflow export | `ml/mlflow_export.py`, artifact records | `test_ml_core.py` | Six record artifacts per experiment; MLflow file-store zip with `MLmodel`; packages load only when a platform record names the hash and bytes match. Not checked with a real MLflow client |
| P5-06 ML agents and playbooks | `config/agents/ml_practitioner.yaml`, `ml_engineer.yaml`, `playbook.train`, `playbook.score`, purpose `ml_spec_proposal` | `tests/unit/test_ml_propose.py` (10), `test_ml_governed.py` | Proposals validated before use; `off` mode takes the rules path; both playbooks ran end to end |
| P7-06 isolated compute pools | `contracts/worker.py`, `workers/*`, `api/routers/worker.py`, migration 0038, Helm NetworkPolicy | `tests/conformance/worker/` (59), `tests/unit/test_worker_protocol.py`, `tests/integration/test_compute_pools_db.py`, `test_worker_conformance_temporal.py` | Envelope round-trip, token-scope refusal, egress refusal, budget kill (cpu, memory, wall, bytes, trials), idempotent retry writes one artifact, error mapping — on subprocess pools and on a Temporal dev server; workers refuse to start with DB/Redis/provider credentials. Not verified: compose internal network and NetworkPolicy under a real CNI |
| P6-01 PipelineSpec and cross-source dry run | `pipelines/{spec,dryrun}.py`, `services/pipelines.py` | `tests/integration/test_pipelines.py::test_cross_source_dry_run_manifest_checks_and_reconciliation` | Per-source scope and snapshot manifest, dry-run SQL, key/fan-out/unmatched checks, reconciled virtual output; a reconciliation that loses a measure blocks |
| P6-02 incremental, deletes, replay, backfill | `staging/incremental.py`, recipe `incremental` block | `tests/unit/test_incremental_staging.py`, `test_pipelines.py` | Incremental equals a reference full load through updates, inserts, reconcile, a crash before COMMIT, overlapping refreshes and rows changing mid-pagination; offset paging on a moving filter shown to skip rows (keyset used instead). Limit: only ServiceNow implements watermark reads |
| P6-03 managed writer (backend) | `pipelines/writer.py`, writer identity in `deploy/postgres/01-init.sql`, migration 0039 | `test_pipelines.py::test_build_test_approve_materialize_fail_and_recover` | No/pending approval refused; a fail gate never replaces v1; crash before promotion resumes without re-staging; rollback; a moved rollback pointer invalidates the approval; freshness alert; the writer is denied INSERT/CREATE/SELECT on sources. Workbench UI pending (wave 3) |
| P6-08 engineer agent | `config/agents/engineer.yaml`, `skills/pipeline_propose.py`, `prepare.v1` | `tests/unit/test_engineer_agent.py` | Only IR-, scope- and PipelineSpec-validated proposals; never compiles or saves; `off` mode rules path |
| P4-04 brief, readiness, job kinds | `contracts/brief.py`, `services/{brief,readiness}.py`, `capabilities/job_kinds.py`, migration 0040 | `tests/unit/test_brief_readiness.py` (11), `tests/integration/test_brief_steps_api.py` | Inferred grain/keys/terms stay suggested until reviewed; readiness ready/needs_input/blocked/unsupported with per-check reasons, no score; missing label blocks prediction; work orders refuse blocked work (`readiness_blocked`) |
| P7-04 step model and self-check | `contracts/step.py`, `services/{steps,step_pins}.py`, `skills/selfcheck.py` | `tests/unit/test_steps.py` (11), `test_selfcheck.py` (11) | Edit → version+1, dependents re-run, verdicts voided, old versions readable; self-checks (empty with safe case correction, magnitude, truncation, grouping, fan-out with safe COUNT DISTINCT, DQ); sweep finds 0 late voids; pins need a hash-bound approval and replay the frozen query |
| P7-05 branching (backend) | `services/branches.py` | `test_steps.py` | Fork with parent pointer, compare by lineage root, merge into a report keeping both lineages; a VOID verdict refuses the merge. UI pending (wave 3) |
| P7-12 notebooks (backend) | `services/notebooks.py` | `tests/unit/test_notebooks.py` (4) | Cells are steps; SQL through the gateway; Python only on upstream results in the sandbox (connection libraries refused, spin loop killed); edits void dependents. Limit: `compute-py` pool wiring (wave 3) |
| P7-18 light IA | `web/src/routes.ts`, `Layout.tsx`, `StartWork.tsx`, pages | `web/src/test/lightIa.test.tsx`, `ia.test.tsx`, `conceptCount.test.tsx`, `e2e/journeys.spec.ts` | Five areas + admin-only ⚙ Settings, 18 screens (budget 20); one home per concept (tested); investigation/finding vocabulary; technical terms under Technical details on 7 screens (tested); first-login concepts 44 → 20 (analyst), 24 (admin) |
| P4-07 onboarding and workbench (part) | `web/src/pages/WorkspaceHome.tsx`, onboarding, `WhyNumber.tsx` | same | Resumable onboarding (connect → select → brief → goal → start), evidence drawer, keyboard and narrow layouts. Limits: real-API journeys, reconnect/stale edits, preview filters, glossary/column curation (wave 3) |

## 2026-09-27 — Increment 7 wave 3: pools wired, follow-ups, UI for wave 2, held-out corpus, parity checklists

Three branches merged onto the wave-2 head (`1530491`), migration 0041 after 0037 (one head). Local stack as
in wave 2. Combined results on `ac7622e` plus the regenerated client: `ruff` clean; unit 3177 passed,
11 skipped; integration 277 passed, 31 skipped (on `12e121a`, before the UI merge, which changes no backend code); web vitest 284 passed (1 skipped), typecheck and build clean,
Playwright 95 passed with no retries; OpenAPI and the generated client current.

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P5-01 / P7-06 ML on `compute-ml` | `workers/ml.py` (`run_ml_isolated`), `services/ml.py` (`run_ml_compute`, `load_package` refuses in pool mode) | `tests/conformance/worker/test_ml_on_isolated_pool.py` (6) | Pool result equals the in-process `run_ml_job`; every returned file checked against its content hash; the control plane never unpickles a package in pool mode. Limits: subprocess pool only here; MLflow export omits the separate sklearn model in pool mode |
| P7-12 Python cells on `compute-py` | `workers/python.py`, `PythonCellSpec`, `services/steps.py::execute_python` | `tests/conformance/worker/test_python_on_isolated_pool.py` (9) | Policy refusals and a resource bomb killed; sandbox path kept when no pool is configured. UI: Work → Notebooks (`web/src/test/notebooks.test.tsx`) |
| ArtifactRef unified | `contracts/worker.py` (re-exported by `contracts/step.py`) | conformance and step tests | One definition; `id` still accepted and returned beside `artifact_id` so stored snapshots and old clients keep their shape |
| P7-01 follow-ups | `services/monitors.py` (baseline state), `evidence/reverify.py`, `POST /api/verification/{id}/reverify` | `tests/unit/test_verification_followups.py` (8) | A VOID baseline is refused by default (no query, no alert) or relabelled with `on_void_baseline: relabel`; re-verify writes a new record through the subject's own path and never edits the old one. Re-verifying a finding returns the replay run id, not the record |
| P7-08 Ask turns | `GET /api/ask/turns/{id}/why` | `tests/unit/test_why_ask_turn.py` (3) | Every number in an Ask answer resolves through the six links; missing or void links shown |
| P7-11 query tools | definition status `tested`, `POST .../definitions/{id}/test`, MCP `query.<key>` / `http.<id>` | `tests/unit/test_query_tools_mcp.py` (16) | Publish needs a passing test bound to the content hash; an edit returns a tested draft to draft; JSON Schema parameters bound as typed literals; drafts neither listed nor callable over MCP |
| P7-03 promotion | `POST .../definitions/{id}/promote`, migration 0041 (`bindings`, `promoted_from`) | `tests/unit/test_definition_promotion.py` (5) | dev → test → prod copies the published version with the identical spec and hash; connections re-bound per environment |
| P4-05 ownership transfer | `POST .../semantic/ownership`, `/{id}/accept`, `/{id}/decline` | `tests/unit/test_semantic_ownership.py` (6) | Hash-bound offer; only the named editor accepts; a definition changed since the offer is refused; the approvals inbox cannot approve an offer. A refused accept is not recorded as invalidated (the request rolls back) |
| P4-06 request ids and If-Match | request-id middleware, monitor and Ask-thread `revision` (0041) | `tests/unit/test_request_id.py` (4), `tests/integration/test_wave3_backend_api.py` (6, migration up/down/up) | `X-Request-ID` on every response and `error.request_id` in every error body; 412 on a stale If-Match |
| ML work orders | `services/work_orders.py`, `capabilities/job_kinds.py` | `tests/unit/test_ml_work_orders.py` (4) | A complete MLSpec starts `playbook.train` on the published `ml_spec` whose hash matches; drafts and incomplete specs refused; Predict/Forecast offered once an `ml_spec` is published |
| P4-04 UI | `web/src` Start work, Data → Brief & readiness | `briefReadiness.test.tsx`, `lightIa.test.tsx` | Disabled kinds list each reason and fix and cannot start; per-check readiness, no score; facts vs open questions with provenance and versions |
| P7-04 / P7-05 UI | Work → Data Thread | `dataThread.test.tsx`, analyst Playwright journey (keyboard) | Step versions, checks, verdicts with void cause; edit shows re-run and voided steps; fork, compare side by side, merge into a report; void merge refusal shows the server's reason. Mocked API |
| P5-03 UI | ML spec form, experiment view, model card, Outputs → Models & scoring, Operate monitors | `ml.test.tsx`, ML Playwright journey | Split, data version and metric direction shown together; consumed holdouts marked; promote/score/rollback through approvals. Mocked API |
| P6-03 UI | Prepare data → pipelines, Outputs → Managed tables | `pipelines.test.tsx`, engineer Playwright journey | Diagram and version diff; rows ledger with quarantined and late rows apart (late rows "not reported", never 0); approve → materialize → rollback. Mocked API |
| P4-07 remainder | step-result filters, curation, stale-edit and reconnect UI | `workbench.test.tsx` | Filters re-query through the gateway; preview filters visibly static; 412/428 → reload and compare; reconnect shows attempt and retry time and re-reads the run. Real-API browser journeys still pending |
| P4-08 held-out corpus | `evaluation/heldout/`, `scripts/benchmark_heldout.py`, `config/eval_gates.yaml` v3 (`heldout`, non-blocking) | `tests/unit/test_heldout_corpus.py`, `tests/integration/test_heldout_platform.py` | 34 frozen tasks disjoint from the dev benchmarks. Component: accepted 17/20, confident-wrong 0, abstention 14/14. Platform (models off): 18/20, confident-wrong 1 (HO-ML-04), abstention 13/14, p50/p95 2.8/5.0 s (`evidence/2026-09-27-heldout-component-platform-off.md`). Not run: human baseline, live model, 60+ tasks |
| P5-07 null false-positive rate (measured) | `ml/evaluation.py::decide` | 200 coin-flip targets (`ml_pumps` `null_target`, seeds 8000–8199, component path) | 3/200 ended `improved` (1.5%; the 95% gain interval allows up to 2.5%): HO-ML-04 is a chance event, not a defect in the rule. Tightening is an owner decision (tracker P5-07) |
| P7-14 parity checklists | `docs/60-delivery/donor-parity-{atlas,datapilot}.md` | each capability names an existing code path and test, or a disposition | Atlas 67 (31 / 21 / 15), DataPilot 58 (38 / 15 / 5); unsigned; freeze procedure written; donors untouched |
| Merge with PR #12 (P7-19) | `web/src/components/StartWork.tsx`, `lib/jobKinds.ts`, `pages/Runs.tsx`, `api.ts` | `lightIa.test.tsx`, `ml.test.tsx`, Playwright ML journey | Job kinds from the server's P4-04 list, with its `work_mode` reason for an unselected mode; Forecast/Predict open PR #12's published-plan picker, with "Write a new spec" leading to the wave-3 form; one home for experiments (Work → Experiments; `?tab=ml` links open it); one ML client and one job-kind type. vitest 286 passed, Playwright 95 passed |
| Windows imports | `workers/isolation.py`, `sandbox/runner.py` | `tests/unit/test_no_posix_resource.py` (3) | Both modules import without POSIX `resource` (the P7-19 note that a test could not run on Windows); an isolated job or the `none` sandbox refuses to start there instead of running without limits |
| Backend asks from the wave-3 UI | `services/notebooks.py::cell_view`, `GET /api/approvals/{id}` (`ApprovalView`), `PATCH /api/assets/{id}/columns/{column}/metadata` (migration 0042), `evidence/why.py::explain_step`, `pipelines/late.py` | unit: `test_notebooks.py`, `test_approval_view.py` (3), `test_column_curation.py` (2), `test_step_why.py` (3), `test_pipeline_late_rows.py` (8); integration cases added to `test_api_routers.py`, `test_brief_steps_api.py`, `test_pipelines.py` (run by CI, not locally: the local Postgres had stopped); `pipelines.test.tsx` | Cell POST returns the cell as the notebook GET shows it; one approval readable by its requester or inbox members, 404 otherwise, no decision route; a user business name/description is never overwritten by a crawl or knowledge ingest; step "Why this number?" with the six links, a voided version shown as void; `late_rows` measured on a dry run through the gateway, `null` with a reason when there is no watermark or nothing committed (never 0 unmeasured). A pipeline run reports `late_rows: null` (a windowed run cannot see behind its window). Unit 3199 passed |
| Governance review of the follow-ups (2026-09-27) | `api/routers/artifacts.py`, `services/pipelines.py`, `pipelines/late.py`, `evidence/why.py`, `services/{file_ingest,crawler}.py`, `knowledge/remote.py` | `test_approval_view.py` (+2), `test_pipeline_late_rows.py` (+3), `test_step_why.py` (+2), `test_remote_url.py` (7) | Fixed: an approval is readable only by current inbox members (no requester bypass after leaving) and reads `expired` once lapsed; the scope check precedes the stored-watermark read; a truncated candidate, an unreadable committed output or any error is "not measured" and never fails the dry run; the bound carries a UTC offset, identifiers are quoted, rows without a watermark are counted apart; a downstream step shows an upstream void; a pushdown table beside a staged one is listed unknown; a computation receipt without network isolation is not a clean link; file ingest and the crawler respect and record business-name origins; a bare token as the https user is refused. Deferred to P7-20: curated columns deleted on a crawl gap; owner text screened before prompts. Unit 3214 passed |

## 2026-09-27 — demo readiness: remaining rows, live journeys, UI design pass, end-to-end scripts

Six parallel branches merged on `claude/cool-heisenberg-isaz0q` (with develop through PR #23); migrations 0043
(`column_absent_since`) and 0044 (`named_owners`, renumbered at merge), one head, up/down/up on a fresh database.
Local stack: Postgres 16 + pgvector 0.8.1, Redis 7, a Temporal dev server, a real Superset 4.1.1 in a scratch venv
(demo branch only), no model key. Combined results: unit 3261 passed; integration 288 passed, 27 skipped; eval gates
v5 PASS (ml null_rate 0 over 60 seeds; heldout a non-blocking 0.884); web typecheck and build clean, vitest 308
passed, mocked Playwright 97 passed with no retries; **real-API browser journeys 13/13 on two consecutive runs**
against a live API, worker and database built from the merged tree.

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P5-07 null control | `ml/evaluation.py::decide`, `MLSpec.confirmation` | `test_ml_null_control.py` (6), `evaluation/ml_null.py`, eval gate `ml.null_rate` | 3/200 → 1/200, 9/400 → 1/400 coin-flip targets `improved`; 6/6 planted signals still detected; `{cross_validation: false, holdout_level: 0.95}` restores the old rule |
| P7-20 curation durability, screening | `db/column_presence.py`, migration 0043, `skills/catalog.screen_for_prompt` | `test_curation_durability.py` (8) | Curated columns survive a crawl gap and return with their curation; every origin's text screened at prompt build, labelled |
| P5-05 real MLflow | `ml/mlflow_export.py` | `test_mlflow_client.py` (skips without `ANALYSTOS_MLFLOW_PYTHON`) | MLflow 3.16.1 reads run, params, metrics, tags; sklearn and pyfunc load and predict identically; file store needs `MLFLOW_ALLOW_FILE_STORE=true` on 3.x |
| P4-05 live analyst workflow | `POST /semantic/model/proposals` (`review.propose_structure`) | `tests/integration/test_semantic_analyst_workflow.py` | Separation of duties at every approval over HTTP; metric versions void dependents; ownership offer hash-bound |
| P4-08 corpus v2 | `evaluation/heldout/` (75 tasks), `scenarios.py` | `test_heldout_corpus.py`, platform smoke | Component 38/43, platform 39/43 (before P5-07); transfer tasks expose column-name sensitivity (not tuned) |
| P4-09 pilot | `security/oidc.py`, `scripts/fake_idp.py`, owners (0044), `/pilot-readiness`, `scripts/recovery_drill.py` | `test_oidc_local_idp.py`, `test_observable_failures.py`, `test_oidc_provision.py`, `pilotFailures.test.tsx` | Highest role wins across mappings (fixed); drill restores and resolves everything; certified: postgres, sqlite, duckdb, file |
| Pinned snapshot fix | `recipes/execute.py` | unit test | A recipe pinned to a snapshot is never pushed down onto changed live data |
| P7-19 agent forms | `capabilities/agent_forms.py`, `contracts/agent_form.py`, `AgentForms.tsx` | `test_agent_forms.py` (12), `test_agent_forms_api.py`, `agentForms.test.tsx`, `e2e/agent-forms.spec.ts` | Grants never widen (form or raw API); runs bind the exact published version; schedules pin it; a revoked grant fails the step |
| P7-17 core install | `core/config.py` (lite when Temporal absent), lazy polars | `test_slim_install.py` (7) | Core 818 MB, +reports 920 MB, +standard 1117 MB; core install completes a run as lite |
| Real-API journeys (P4-07, P7-05, P7-19) | `web/e2e-live/`, `playwright.live.config.ts` | 13 journeys | 9 bugs fixed on the way (step binding of highlights, merge notice, pin on findings, Predict before a spec, promote button, scoring default table, pipeline creation in the UI, integer→double append, markdown underscores) |
| UI design pass | `StatusBadge`, `EmptyState`, layout and wording | `components.test.tsx`, axe in both themes | 7 P1 + 14 P2 fixed across 44 views (`docs/70-reviews/2026-09-27-ui-design-review.md`) |
| Demo | `analystos demo-seed`, `scripts/demo.ps1`, `scripts/demo.sh`, `StatusBanner.tsx`, health states | `test_health_states.py`, `statusBanner.test.tsx`, `test_compose_static.py`, `test_worker_sigterm.py`, `test_workflow_nudge.py` | `e2e_demo.py` 26/26 (published to real Superset 4.1.1), `e2e_phase3.py` 13/13, `e2e_increment3.py` 13/13 from a fresh database; lite path 25/26 (Superset absent by design) (`evidence/2026-09-27-demo-readiness.md`, `docs/30-runbooks/05-demo-walkthrough.md`) |
| Run failure cause | `runtime/engine.py` | `test_engine_unit.py` | A failed run shows each failed task's cause and remedy (e.g. an unapproved KPI under `require_approved_metrics`) |
| N-12 reviewed domain rule (2026-09-27) | `knowledge/suggestions.py`, `skills/catalog.py`, `services/crawler.py`, `ReviewQueue.tsx` | `test_crawler_domain_assist.py`, `test_skills_catalog.py`, `test_knowledge_k05_k08.py::test_reviewed_domain_keyword_becomes_workspace_rule_and_can_be_revoked` (live Postgres), `knowledge.test.tsx` | Model candidate alone is inert; reviewer-supplied safe metadata keyword enters the current workspace pack, drives deterministic reclassification on a full crawl, and is revoked when that document is removed. Live source/model classification accuracy is not yet measured. |
| P8-01 data context (2026-09-27) | `services/crawler.py`, `skills/{profiling,catalog,relationships}.py`, `services/{brief,sources}.py`, `semantic/review.py`, `agents/{metadata,profiler}.py`, `connectors/servicenow.py`, `api/routers/catalog.py` | `test_brief_key_contract.py`, `test_relationship_evidence.py`, `test_catalog_descriptions.py`; integration `test_crawler_context.py`, `test_column_drafts.py` | Every item of the 2026-09-27 crawler audit fixed or explained; review decisions reach the store readiness and SQL read; profiles exist right after selection |
| P8-02 suggested data model (2026-09-27) | `semantic/suggest.py`, `api/routers/semantic.py`, `web/src/components/ModelSuggestion.tsx` | `test_semantic_suggest.py`, `test_model_suggestion.py`, `modelSuggestion.test.tsx` | Rules only; measured keys and fan-out through the gateway; propose keeps separation of duties |
| P8-03 token economy (2026-09-27) | `agents/common.py`, `context/{compiler,cache,service}.py`, `llm/{router,cache,config}.py`, `agents/{investigator,critic,insight,generic,sql_agent}.py`, `api/routers/admin.py` | `test_token_economy.py`, `test_context_economy.py` | `evidence/2026-09-27-token-economy.md`: −31% sent tokens, stable prefix 15% → 66% (fake transport) |
| P8-04 Ask analyst mode (2026-09-27) | `agents/analyst.py`, `skills/result_facts.py`, `services/ask.py`, `api/routers/ask.py`, migration 0045, `web/src/components/AnalystAnswer.tsx` | `test_analyst_mode.py`, `test_ask_analyst_mode.py`, `analystAnswer.test.tsx` | Behaviour re-implemented from the AgentSwarms AI Analyst (no code copied); no model output reaches a number without binding |
| P8-05 guidance (2026-09-27) | `web/src/components/{Guide,Tour}.tsx`, `web/src/lib/{guide,starters,methods}.ts`, `agents/ask_rules.py` | `guide.test.tsx`, `starters.test.ts`, `test_ask_rules.py` | Tours point only at anchors that exist (tested); starters answered by rules |

## 2026-09-28 — P8-14 Postgres without pgvector

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P8-14 vector backend | `db/vectors.py`, `db/models.py`, migrations 0001/0018, `knowledge/{index,cli}.py`, `context/service.py`, `core/config.py` (`vector_backend`), Helm `config.vectorBackend` | `test_vector_backends.py` (8); knowledge/context integration on both backends (32 passed, 1 skipped each) | `evidence/2026-09-28-vector-backend-array.md`: plain Postgres 16 migrates and seeds; rankings identical to pgvector on 10/10 questions; conversion both ways. Exact in-process scan: fine to thousands of sections |

## 2026-09-28 — P8-15 Weak findings and held-out confirmation

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P8-15 holdout + strength | `skills/sqlbuild.py` (`Partition` predicate), `contracts/analysis.py` (`Partition`), `contracts/evidence.py` (`Strength`, `HoldoutCheck`), `evidence/{holdout,strength,confirmation,bundle}.py`, `agents/{data_scientist,insight,critic,supervisor}.py`, `api/serialize.py`, `contracts/platform.py` (`analysis.holdout_fraction`), `web/src/lib/standing.ts`, `InvestigationBoard.tsx`, ADR-0008 amendment | `test_holdout_confirmation.py` (187), `standing.test.tsx` (2); integration `test_holdout_p815.py`, `test_typed_evidence_p403.py` (4 passed on local Postgres, own test databases) | Retail generator in process: random-status price difference not confirmed (rejected on discovery rows by Order ID; weak + not confirmed by whole row); planted Marketplace returns confirmed on held-out rows (n 900, one-sided p 0.019). Governed Postgres run: one held-out test per verified claim, lock before access, no other query reads held-out rows. Numbers describe discovery rows; a whole-table fluke can still pass the holdout (strength flags it); live journey re-run not done |

## 2026-09-28 — P8-16 Causes one join away

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P8-16 joined segments and `later_than` | `contracts/analysis.py` (`Join`, `via`, `later_than`, `tolerance_hours`; compat serializer keeps old spec hashes), `skills/sqlbuild.py` (`base_select` joins for every method, `later_than` per dialect), `skills/lookups.py` (lookups, `complete_joins`, joined dimensions, planned/actual pairs), `agents/investigator.py` (`validated_lookups`, `validate_spec` joins, playbook, drill-downs, matrix continuations, `related_tables` in the prompt), `agents/prompts.py`, `agents/sql_agent.py` (dataset joins, aliases), `methods/{base,rate_by_segment,driver_model,contribution_decomposition}.py` | `test_joined_segments.py` (49), integration `test_joined_segments_pg.py` (2: Postgres engine in its own database; catalog-backed lookups on the test control database) | Retail journey data in process (same generator and seed): round 1 (8 hypotheses) finds late delivery by customer region = West (14.8% vs 7.6%) and returns by product category = Electronics (10.1% vs 6.3%), both verified and BH-significant; no-effect joined segments rejected; the drill-down into Electronics finds Marketplace. Joins keep row counts (6000 of 6000 on duckdb, T-SQL and Postgres). Limits: data-version check on the base table only; notebook steps are not join-validated (gateway still checks scope and columns); live journey re-run pending |
| P8-16 joined evidence follow-up (2026-09-28) | `agents/{insight,critic,investigator,sql_agent,visualization}.py`, `evidence/{manifest,bundle,verification,why}.py`, `services/{steps,relationship_safety}.py` | `test_evidence_p403.py` (joined-table version change and bundle staleness), `test_verification_p701.py`, `test_joined_segments.py` (joined chart filter, retail West monthly follow-up, sampled/stale user-key refusal), `test_joined_segments_pg.py`, `test_steps.py` | Every joined table is in the run manifest, REV stability check, finding bundle and version dependencies; a changed lookup table voids or marks the finding stale and appears in "Why this number?". Notebook method steps require a reviewed many-to-one relationship and depend on all tables read. A joined drill-down projects and filters the related attribute in the dataset; a planned-versus-actual finding gets a within-segment monthly test. An unvalidated user-declared join requires a current, complete profile proving target-key uniqueness. Remaining: full-journey live re-run. |

2026-09-28 live addendum: the rebuilt Compose stack completed `scripts/e2e_full_journey.py`
with 69/69 checks in `ws_272b2ca5401b`, including joined West/Electronics findings and a PDF
download. This closes P8-16's live journey gap.

The 2026-09-28 N-2 (`services/existing_dashboard.py`) and N-3 (`services/external_delivery.py`) implementations from a parallel session were superseded on 2026-09-29 by the close-out versions (`services/existing_dashboards.py`, `services/deliveries.py`), recorded below; their modules and tests were removed.

## 2026-09-29 — Close-out of the buildable open rows (branch `closeout-2026-09-28`)

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P4-G01 contract alignment | ADR-0017 (Accepted, owner sign-off 2026-09-28), `contracts/{decision_purpose.schema.json,approval_hash.md,okf_profile_pin.json}` | `test_cross_repo_contracts.py` (reads UTF-8 explicitly; passes on Windows) | Donor-side adoption is each donor repository's change |
| P4-X05 MCP host allowlist | `tools/http.py` (`parse_url`, `canonical_host`, `match_host`, `pin(internal_only=)`), `mcp/client.py` (`_pin`), `contracts/platform.py` (`OutboundSettings`) | `test_mcp_host_allowlist.py`, `test_outbound_http.py`, integration `test_platform_settings.py`, `test_mcp_client.py::test_platform_host_allowlist_gates_registration_and_connection` (needs the `mcp` extra) | Empty allowlist = no MCP server; existing servers need their host listed. Not yet run against a real Superset/dbt MCP server |
| N-2 existing dashboards | `services/existing_dashboards.py`, `publishing/{base,superset,superset_import}.py`, `api/routers/bi_dashboards.py`, `ExistingDashboards.tsx`, migration 0048_n2 | `test_existing_dashboards.py` (13), `test_existing_dashboards_superset.py` (13), live `test_existing_dashboards_live.py` (compose Superset, 2026-09-28) | KPI, bar and weekly series verified through the gateway against Superset's chart data; histogram not reproducible; certify write round-trips. No BI-012 copy mode |
| N-3 external delivery | `services/deliveries.py`, `api/routers/deliveries.py`, `DeliveryDestinations.tsx`, migration 0047_n3 | `test_deliveries_n3.py` (44, fake SMTP / httpx MockTransport), `deliveries.test.tsx` (3) | No live SMTP relay or webhook receiver run; standing destination authorization (90 days) needs an owner decision for restricted-scope reports |
| N-6-font Unicode PDF | `reports/pdf.py`, `reports/fonts/` (DejaVu, licence) | `test_reports_pdf.py` (10) | Latin-ext/Greek/Cyrillic as written; CJK/Indic fall back |
| N-7 / INT-004 entity matching | `skills/entity_matching.py`, `services/entity_matching.py`, `api/routers/entity_matching.py`, `evaluation/entity_match_datasets.py`, migration 0054_n7 | `test_entity_matching.py` (19), live `test_entity_matching_live.py` (3, compose Postgres) | n=1000: match band P 1.0 / R 0.870; match+review P 0.994 / R 0.997 (synthetic truth). TRN-004 done (next row) |
| N-7 / TRN-004 Trino federation | `engines/trino.py` (READ ONLY connection, run, catalog qualification), `gateway/service.py` (`_run` trino, `_federate_on_trino`, trino estimate), `gateway/validator.py` (`federation_dialect`), `gateway/cost.py` (`trino_estimate`), `skills/sqlbuild.py` (trino), `connectors/certify_kit.py` (trino checks), `config/source_kinds.yaml`, compose profile `federation` + `deploy/trino/` | `test_trino_engine.py` (17), live `test_trino_federation.py` (6, Trino 476 + demo Postgres, 2026-10-29); kit 9/9 (`evidence/connectors/trino-2026-10-29.md`); `evidence/trn004-trino-federation-20261029.md` | Federated revenue by region (`pg.sales` ⋈ `tpch.tiny`) through the gateway; denied column refused inside the join; timeout 1 s → stopped < 2 s; cancel < 3 s. Limits: one cluster and identity per federated statement; no PK/FK/comments from the Trino PostgreSQL connector; JSON as text; no grouped median (no exact percentile); no session identity; `explain()` summaries postgres-only |
| N-8 evidence fusion | `contracts/citations.py`, `evidence/fusion.py`, `services/citations.py`, `agents/critic.py`, `services/ask.py`, `Citations.tsx`, migration 0050_n8 | `test_evidence_fusion_n8.py` (22), `citations.test.tsx` (5) | Conflict detection lexical; no live run |
| N-9 what-if scenarios | `contracts/scenario.py`, `skills/scenario.py`, `services/scenarios.py`, `api/routers/scenarios.py`, `WhatIf.tsx`, migration 0051_n9 | `test_scenarios.py` (18), `whatIf.test.tsx` (3) | Simulated numbers never publishable; no knock-on effects; no live run |
| N-10 PPTX/DOCX | `reports/{word,slides,_ooxml}.py`, `services/reports.py` | `test_reports_office.py` (19) | Same `report_data_hash` as the other formats; deterministic bytes |
| N-11 index advice | `skills/index_advice.py`, `services/index_advice.py`, `api/routers/index_advice.py`, `gateway/service.py` (`scans`), migration 0052_n11 | `test_index_advice.py` (29), live `test_index_advice_live.py` (compose Postgres) | Advisory only; plan evidence Postgres only; no UI |
| N-12 + column language | `skills/lexicon.py`, `skills/{catalog,profiling}.py`, `agents/investigator.py`, `evaluation/classification.py` | `test_n12_classification_confidence.py`, `test_column_language_invariance.py` (42 total) | Held-out v2 component (models off) 37/43 → 39/43, transfer 1/3 → 3/3; confident-classification error 2.3% unseen (`evidence/2026-09-29-heldout-v2-column-language.md`) |
| P7-17 wheel + images | `hatch_build.py`, `core/config.py` (`DATA_ROOT`/`STATE_ROOT`), `deploy/docker/Dockerfile` (wheel stage) | `test_wheel_contents.py` | API 1.44 GB / full 1.70 GB; cold start 4.2 s / 4.1 s (`evidence/p7-17-images-2026-09-29.md`). Helm 2-CPU install not run |
| Migrations | 0047_n3 → 0048_n2 → 0050_n8 → 0051_n9 → 0052_n11 → 0054_n7 | upgrade → downgrade 0045 → upgrade on a scratch Postgres (2026-09-29) | One head |

## 2026-09-29 — Live-model benchmarks (P4-V01, P4-V02, P4-08)

| Row | Code | Coverage | Measured / limits |
|---|---|---|---|
| P4-V01 live tier | `evaluation/live.py` (`--preset`, `--enable-models` overlay for the benchmark process only), `scripts/benchmark_analytical.py` | `evidence/2026-09-29-analytical-benchmark-live.md` (+ same-seed models-off pair) | Precision 0.964, recall 20/20, FDR 0.033, null FDR 0; $0.20. Small/mid-tier models only (stored dev settings disable the strong tier, N-14) |
| P4-V02 live | `services/ask.py` (placeholder/write refusal, `cannot_answer`), `agents/{prompts,sql_agent}.py`, `llm/{config,router}.py` (`reasoning_effort`) | `test_ask_placeholder_answers.py`, `test_ask_rules.py`, `test_model_router.py::test_json_profiles_ask_openrouter_for_minimal_reasoning`, `evidence/2026-09-29-ask-benchmark-live.md` | Execution 0.962, confident-wrong 2/162, leaks 0; residual misses sales-a31 (fraction vs percent), sales-a29 (scoring artefact), sales-a06 (malformed JSON, refused), finance-a24 (rule path) |
| P4-08 live | `agents/investigator.py` (`_round1_selection`: model proposals take a quarter of round 1) | `test_hypotheses_model_adds.py`, `evidence/2026-09-29-heldout-v2-live.md` | 39/43, confident-wrong 0, abstention 1.000, identical statuses to models off; $0.24. Practitioner baseline outstanding |

Total OpenRouter spend for the three live benchmarks: about $3.7 (key usage $10.34 → $14.07). JEV never ran (N-13).

## 2026-10-25 — Investigation value (VALUE-01 through VALUE-04)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Guided investigations | `services/investigation_value.py`, `StartWork.tsx`; four versioned templates | Live SLA run pins template and reviewed context; execution still requires plan approval |
| Reusable reviewed context | Existing versioned brief, `ReviewedContext.tsx`, context agent snapshot consumption | Scope filtering, immutable snapshot and API redaction tested; ten facts pinned live |
| Owned follow-up actions | Migration 0055, scoped API, `InvestigationActions.tsx`, lineage and events | Revision/ownership/current-verification checks; one proposed synthetic-demo action persisted live; no operational execution |
| Pilot evaluation tooling | `PilotScorecard.tsx`, scoped assessments and `analyst-pilot-protocol.md` | Rehearsals separated, failed outcomes included, metrics self-reported; real analysts and results outstanding (VALUE-05) |

Validation: 17 backend/API tests, 54 relevant UI tests and four browser journeys passed; production build and contract checks passed. See [dated evidence](evidence/investigation-value-20261025.md).

## 2026-10-25 — Reviewed context and hypothesis grounding (GROUND-01 through GROUND-07)

Reviewed business facts and mappings now reach relevant prompts with version receipts. Question resolution uses applicable knowledge before bounded model assistance; remaining ambiguity pauses execution for review. Hypothesis selection checks requested coverage and requires reviewed contracts for new measures. The Catalog provides guided mappings, lifecycle/conflict visibility, structural sample checks and stored golden grounding evaluations. Typed population/quality contracts remain separate from prose guidance.

Validation: 197 unit tests, six API/end-to-end integration tests, 50 frontend tests and five browser journeys passed. Live OpenRouter resolved the existing SLA measure without clarification; the gateway checked 1,000 mapped rows. The live mapping remains a draft. Golden grounding checks do not establish numerical-answer accuracy; actual analyst pilot results remain outstanding (VALUE-05). See [release evidence](evidence/context-grounding-20261025.md) and [owner runbook](../30-runbooks/context-mapping-and-domain-onboarding.md).

## 2026-10-25 — Simple everyday usage (USAGE-01)

`UsageGuide.tsx` puts a five-step journey and result terminology beside the work. A successfully resolved question opens the existing Start work preflight with the question preserved. Old previews are discarded after question/model edits or mapping saves; typed rule inputs preserve numbers and booleans. Role-aware actions explain read-only access. Catalog sections are selectable on phones, owner evaluation controls are expandable, and header hover contrast is corrected.

Evidence: 53 UI tests, five mock browser journeys, one real-API existing-workspace journey and production build passed. The real journey signs in, resolves SLA context, opens/closes the preflight and switches mobile sections without starting work or granting approvals. See [dated evidence](evidence/simplified-usage-20261025.md) and [everyday usage](../30-runbooks/everyday-usage.md).

## 2026-10-25 — Workspace source repair (SOURCE-REPAIR-01)

Compose now shares persistent `runtime-data:/app/var` across the API and trusted workers/scheduler, excluding isolated pools. Recovered missing file inputs without deleting workspace definitions. Live checks cover all four active workspaces, six connections, 16 selected table refreshes with retained counts, six separate source schemas and four rejected cross-workspace read probes. Connections and counts survive two container recreations. Eleven static configuration checks pass; bundled ServiceNow remains a mock. See [repair evidence](evidence/workspace-source-repair-20261025.md).

## 2026-10-25 — Sample-record follow-ups (ASK-SAMPLE-01)

`agents/ask_samples.py` and the Ask thread service resolve small example requests using current catalog scope and a prior successful table/filter. Related details follow validated relationships; SQL still runs through QueryGateway after authorization and budget checks. Exact retail wording and a related-details request both return the same five order IDs across orders/customers/products. 126 unit tests, one Postgres API integration case and one real browser journey pass. See [dated evidence](evidence/ask-samples-20261025.md).

## 2026-10-25 — Workspace report collections (BI-VIEWS-01/02)

Workspace display names now prefix generated dashboards; topic collections reuse available descriptive measures and validated chart specs. Eleven local dashboard drafts and eight historical-run HTML/Markdown reports were prepared with snapshot caveats. 114 unit tests, three local full-flow integrations and a real-API browser preview pass. Existing eight Superset dashboards remain unchanged pending refreshed bundles and approval. Independent per-collection publishing remains open. See [evidence](evidence/workspace-bi-20261025.md) and [usage guide](../30-runbooks/workspace-reports-and-superset.md).

## 2026-10-29 — Collections published on their own; grouping of existing Superset dashboards (BI-VIEWS-02)

`dashboards/collections.py`. A person chooses some of an investigation's dashboards (by default the one open) and asks
for approval `dashboard.collection_publish`; the payload binds the run, its plan hash, the sorted dashboard keys and a
bundle holding only those dashboards, their charts (previews stripped) and the KPIs those charts use, so another
selection, an edited chart or a replan is a different hash. The request runs the run publication's governance review
(bundle structure, dataset SQL against the requester's current scope, destination policy, VOID-finding gate, KPI
stamping; unapproved KPIs are approved with the publication by its approver). `POST /api/v2/collections/publish`
re-derives the payload from the stored artifacts, calls `verify_for_execution` and the Superset switch check, consumes
the approval, then publishes; a failed BI call restores it for a retry. Idempotency key `collection:<run>:<selection>:<hash>`
keeps reconcile's "newest per run" apart per selection; a collection rollback removes its dashboards and only the
charts/datasets no other live publication uses. Grouping: `GET/POST /api/v2/workspaces/{ws}/dashboard-grouping[/request|/apply]`
lists dashboards this workspace published (slug prefix and provenance stamp both checked) with `Workspace / title`;
`dashboard.grouping_update` binds the exact {id, slug, from, to} list, and apply re-reads the titles from Superset so a
title changed there since invalidates the approval instead of being overwritten. `SupersetPublisher.rename_dashboard` PUTs
the title only. Web: Outputs › Dashboards › Publish chooser and a "Group published dashboards" panel, business wording;
approval inbox labels. 20 unit tests (`tests/unit/test_dashboard_collection_publish.py`) and 4 UI tests
(`web/src/test/collections.test.tsx`). Limits: not exercised against live Superset (compose stack down on 2026-10-29),
so the existing dashboards are unchanged until an editor requests and an approver approves the rename; a later
republish of an older approved bundle carries that bundle's own titles.

## 2026-10-25 — Single-reviewer publishing (PUBLISH-SELF-01)

Per the user's explicit request, KPI review now respects workspace separation-of-duties configuration through both API routes. Existing role/hash/policy/expiry/definition checks remain; structural model, relationship and destination reviews retain independent-review rules. The user's already-approved, unchanged retail bundle was reverified and retried successfully: four published Superset dashboards and 12 working chart data requests. Twenty integration, 44 unit and 36 UI tests pass. See [evidence](evidence/single-reviewer-publishing-20261025.md).

## 2026-10-28 — S12 Platform operations: deployment, observability, evaluation gates, cost ledger, load/SLO, DR (T1–T7)

Helm (`deploy/helm/analystos`): every pod has a role (`api`, `query` = the analysis pool, `compute` incl. the isolated
pools, `publish`, `crawl`, `elt`, `scheduler`, `web`, `migrate`, `ops`); opt-in per-role NetworkPolicies with an
egress allowlist per peer group (model hosts only for roles that call models; compute reaches neither models nor
sources), KEDA ScaledObjects on Temporal task queues (replacing the pool HPA), gVisor `runtimeClassName` for compute
pools, External Secrets as a pre-install hook, an OpenShift (restricted-v2) mode, the migration Job as `app_migrator`,
PodMonitors, a PrometheusRule and three Grafana dashboards, nightly CronJobs for `analystos audit verify` (+ WORM export)
and the new `analystos ops rls-check`. Rendered and linted with helm 3.16.4 for default/ha/airgapped/small and every
S12 switch on (`tests/unit/test_helm_chart.py`, 28 passed); not installed on a cluster.

Observability (`src/analystos/observability/`): Prometheus metrics for request latency per route class, runs
(outcome, duration, steps), gateway statements and rejection reasons, model calls/tokens/cost/fallbacks per
provider and model, cache hit rates, approval latency, VOID sweeps, Desk resolutions, RLS denials and budget use;
`/metrics` on the API and on workers/scheduler; OTel spans (request, model call, gateway statement) whose attributes
pass an allowlist and hash any text, so no prompt, SQL or row reaches a trace. Alerts: SLO burn rates (Ask latency,
run success, availability), queue backlog > 100 for 10 min, provider errors > 5%, spend > 80% of a budget, audit chain
verify failed (page), RLS coverage failed (page). `tests/unit/test_observability.py` (13).

Evaluation gates (`config/eval_gates.yaml` v6): `release_safety` (pytest negative suites: SQL safety corpus 100%,
cross-tenant 0 leaks, publish integrity 100%; skipped = fail) and `model_matrix_fake` block CI after migrate; the live
per-model matrix runs nightly and is served at `GET /v2/admin/models/evaluations`. Candidates are allowlisted models
outside `blocked_families` (Claude and Gemini never evaluated nor called; `blocked_family_calls` gated at 0).
Run locally on a migrated plane (`evidence/2026-10-28-s12-release-gates.json`): release_safety passed (SQL safety 350/350, cross-tenant 51 / 0 leaks, publish integrity 54/54, nothing skipped), model_matrix_fake passed (14 rows, pinning proven 5/5 per coding candidate, 0 blocked-family calls); only the `coding` profile is exercised by the Ask golden suite. `tests/unit/test_model_matrix.py` (9).

Cost ledger: `GET /v2/admin/spend?group_by=` over model calls, query scan bytes/credits, compute CPU-seconds and
artifact storage (rates `config/cost_rates.yaml`), by request, run, conversation, user, workspace, tenant, day,
category and item; monthly CSV/Parquet export (API and `analystos ops spend-export`); the `cost_ledger` view
(security_invoker) for SQL readers. Blocked-family spend stays in totals, flagged. `tests/unit/test_cost_ledger.py` (8),
`tests/integration/test_platform_ops.py` (3, Postgres 16: migration 0066 up/down/up, view and service bound to the
tenant by RLS, rls-check, matrix grants).

DR drill (`scripts/dr_drill.py`, evidence `evidence/2026-10-28-s12-dr-drill.md`): PITR on Postgres 16 with WAL
archiving — base backup 5.5 MB in 5.8 s, an operator deleting every workspace, recovery to the target time in 16.9 s;
phase-A data present, deleted rows back, the incident's row absent, Alembic head unchanged, `ops rls-check` and
`audit verify` pass; RPO bound 60 s (archive_timeout; spec ≤ 15 min). Temporal 1.25: 5 workflows mid-flight across a
`pg_dump` of both Temporal databases, server and database destroyed, restored in 62 s; all still RUNNING with
unchanged histories, all completed exactly once on a new worker (AT-09 at the Temporal layer).

Load and SLOs (`scripts/load/slo_report.py`, `config/slo.yaml`, `tests/unit/test_slo_report.py`; evidence `evidence/2026-10-29-s12-slo-{temporal,lite,ask-only}.md`, superseding 2026-10-28): at 2x pilot (20 people asking for 120 s, each with their own analyst login, + 20 concurrent investigations) on a Windows 8-core dev host without a model: availability 100% in every run; runs 20/20 to the approval gate on API + 2 Temporal workers (p95 245 s) and in lite (p95 305 s). Ask latency missed under the full load: 74% within 3 s, p95 9.0 s (Temporal, host 100% busy) and 44%, p95 14.2 s (lite); met with the host to itself (Ask alone: 95.5%, p95 0.94 s), so the gap is shared capacity, not the Ask path. Compute pool on Windows fixed (`workflows/compute_pool.py`, `evidence/2026-10-29-s12-compute-pool-windows.md`: a dead child fails only its attempts with `ComputeChildDied` instead of stopping the temporalio worker; generation recycling; warm-up), tested by `tests/unit/test_compute_pool.py` (8) and `tests/integration/test_temporal_queues.py` (now on Windows, 3/3). Ask: one-statement tenant binding and no unused Data Thread view, 144 -> 118 statements per Ask. Not a sign-off: rerun on Linux with the API on dedicated cores.

Supply chain: `.github/workflows/supply-chain.yml` (gitleaks, Syft SBOM, Grype, cosign keyless signing + SBOM
attestation on main, npm audit, pip-audit, weekly rescan) — checked by `tests/unit/test_supply_chain.py` (5); not yet
run on GitHub. Not done: actions pinned by SHA, `uv.lock`, engines filling `scan_bytes`/`credits`, desk/context/compute
spans, a cluster install, the customer IdP and pen test (T8, hand-off `s12-t8-idp-pentest-handoff.md`).

## 2026-10-29 — S03-T7 Context federation subscription

A workspace follows another workspace's Context Ledger through its change feed (`context/federation.py`, migration
`0074_context_federation`: `context_subscription` and `context_federated_entry`, both owned by the subscriber workspace
with RLS, policy and fill trigger). The source may be in another tenant the subscribing owner belongs to. Each source
read re-binds from `system_scope()` to the source workspace's tenant, so it runs under that tenant's RLS. What is
shared:
* certified, approved and workspace entries, classified `internal`;
* allowed by the subject's access in the source (scope, denied columns);
* never a federated copy, so federation is one hop.

Copies are a ledger owner table and land in the subscriber's ledger as `approved`, `source=federation` and read-only.
A new source version opens a new ledger version.

Delivery and safety:
* The cursor is the source `seq`. A batch is read as the source tenant and applied as the subscriber tenant in a
  separate transaction, so delivery is at least once.
* The apply is idempotent: version compare plus tombstones.
* Every sync re-checks the live copies, which catches access lost without a ledger change and erased entries.
* Losing source access suspends the subscription and retires its copies (audited); `resync` replays from 0.
* `sync_subscription` is `@owned_by`; `sync_due` is a `@platform_loop` in scheduler housekeeping.
* REST: `/api/v2/context/subscriptions` (create with a first sync, list, sync, resync, revoke) and `/feed` for an
  external subscriber, with the caller's access and tenant applied.

Tests (Windows dev host, Python 3.12, Postgres 16 with pgvector image and array vectors, strict tenancy, 2026-10-29):
* `tests/unit/test_context_federation.py` (13): approved read-only copies; refresh and retire on source change;
  redelivery and replay change nothing; out-of-order versions and tombstones; feed withholding (scope, denied column,
  personal, suggested, confidential, federated copies); a version that loses access sent as a remove; suspend and
  resync; re-check after narrowed access; revoke; subscribe rules; kinds; the copy cap; injection text stays data and
  is screened from a compile; malformed items skipped; `@owned_by` and `@platform_loop` markers.
* `tests/unit/test_tenancy_rls_coverage.py`: both tables are tenant-owned through the subscriber workspace.
* `tests/integration/test_context_federation.py` (2): tenant A follows tenant B over REST under RLS. The trigger fills
  tenant A, tenant B sees no subscription rows, and a user outside tenant B gets 404. Also covered: refresh on version
  change, idempotent resync, the external feed only in tenant B's context, the scheduler pass across tenants, and
  suspension when the owner leaves tenant B.
* Migration upgrade from empty, downgrade -1 and re-upgrade checked.

Not built: subscriptions to a published pack URL, an MCP tool for the feed, a UI, and lineage edges from a copy to its
source entry (named in the ledger provenance).


## 2026-10-29 — N-14 visible rules fallback (dev model policy still open)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Structured fallback reason | `llm/fallback.py` (`ModelFallback`, `fallback_for`, `summarize`); `llm/router.py` `_no_route` names `models_disabled` + the disabled models, `_within_cost` flags `missing_price`; `agents/common.py` `note_fallback` in `llm_json` (run message, `model.fallback` event once per purpose and cause per step, avoided call recorded as a rules skip); `contracts/events.py` | `tests/unit/test_model_fallback_n14.py` (22, no services): disabled models, allowlist, missing price, cap reached, mode off not a fallback, one report per step, classification table |
| Run detail and run view | `api/routers/analysis.py` `model_fallbacks` (folded `model.fallback` events); `web/src/components/ModelFallbacks.tsx` on `RunView.tsx` — one plain sentence in business view, cause/steps/router detail with expert tools on | `web/src/test/modelFallbacks.test.tsx` (4): business vs expert view, run view from the run detail |
| Live evaluation labels | `evaluation/live.py` (`fallbacks`, `fallback_label`, `fallback_lines`; `model_usage` returns `fallbacks`), `scripts/benchmark_analytical.py`, `scripts/benchmark_heldout.py` + `evaluation/heldout/report.py`: a tier heading carries "fell back to rules (n purposes)" and a per-purpose cause table | Unit-tested on SQLite events; no live benchmark re-run yet. The dev-stack model policy (which models are enabled, the $2/day cap) is the owner's decision and was not changed |


## 2026-10-29 — CONV-04 conversational report authoring, CONV-05 data stories and digests

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Report document shaped by conversation | `contracts/report_doc.py` (`ReportDoc`: title, sections, text/chart/KPI/finding blocks, a derived sources section; artifact type `report_document`, so no migration); `reports/document.py` typed ops (title; section add/remove/rename/move; text set/shorten; block add/remove/move; schedule pin; deferred export request), atomic batches; `reports/design.py` `report_design` ladder [cache, rules, llm_small] (`config/models.yaml`) | `tests/unit/test_report_documents.py`: 14 phrasings ("add a section on EMEA backlog", "include the resolution trend chart", "make the summary shorter", "move the risks section up", …); ambiguity asks instead of guessing; model ops parsed one by one and re-validated |
| Only verified content | `services/report_docs.py` `catalog` / `resolver`: builder-dashboard chart previews and KPI values, verified (not void) findings, run charts and validated metrics, the user's own answered Ask questions, each read with the actor's rights. A block keeps a bounded copy plus its ref and source hash | Unit (SQLite) and integration tests; another workspace's member gets 404 |
| Numbers guard | `dashboards/ops.numbers_guard` over every text, caption, section title and the report title, against the blocks' values and the code-derived story values; a number it cannot back is refused, not written | `test_numbers_guard_refuses_unbacked_numbers_and_accepts_backed_ones`; the Playwright journey shows the refusal |
| Versions, revert, fork-on-edit | ArtifactVersion+1 with the op diff, If-Match, revert as a new version. Editing an exported (`final`) report forks a draft; editing one with a pending export invalidates that approval | `test_report_docs_service.py`, `tests/integration/test_report_documents.py` |
| Rendering from the verified snapshot | `to_report_data` → `ReportData` kind `document` (`sections`, `origin`). MD/HTML/PDF/DOCX/PPTX render its sections in order, with chart images. The snapshot hash is in the HTML head, the PDF keywords, the Office core properties and the provenance block. Run-report hashes are unchanged (`ReportData.snapshot()` omits the new fields when unset) | `test_every_format_renders_deterministically_from_the_snapshot`, `test_snapshot_hash_is_in_each_formats_metadata`, `test_run_report_hash_is_unchanged_by_the_document_fields` |
| Approval-bound export | `request_export` → approval `report_document.export` bound to {artifact, version, content hash, snapshot hash, format, requester}. `export` re-derives the payload, calls `verify_for_execution`, then `consume` (single use); requester only; audited with the file SHA-256 | Unit and integration tests |
| Scheduled refresh and delivery | `report.pin_schedule` op. `schedules._finish` → `after_schedule_fire`: refresh with the owner's current rights (text whose numbers went away is held), a final `report` snapshot, `deliver` to the schedule's approved destinations | `test_story_becomes_a_report_and_a_pinned_report_refreshes_on_a_schedule_fire` |
| Data stories | `reports/story.py`: deterministic facts from previews and the version diff, plus threshold monitors on the same metric. `data_story` purpose: the template sentences are the rules rung; model wording only in `always` mode, every sentence guarded against its fact's numbers | `tests/unit/test_data_story.py` (5) |
| Story → report, digests | `services/stories.py`: `to_report` (a CONV-04 document with story text and derived values, rewritten on refresh); `digest_report` / `attach_digests` for schedules (`config.digest_dashboards`) and monitors (`config.attach_digest`, a how-key) through the existing delivery path (destinations must accept reports) | Unit (SQLite) and integration tests |
| UI | Reports page: "Write a report by asking" → `components/ReportBuilder.tsx` (outline, chat panel, sandboxed preview, export request and download, versions). Engineering detail (ops, ids, snapshot hash) is behind Show expert tools. Dashboard Summary panel `components/DashboardSummary.tsx` with "Turn this into a report" | `web/src/test/reportBuilder.test.tsx` (8), `web/e2e/report-builder.spec.ts` (build by chat → refusal → preview → export request, axe clean) |

Postgres evidence (2026-10-29, Windows dev host, Postgres 16, strict tenancy): `tests/integration/test_report_documents.py`
passed all 3 tests over REST:
* a report built by conversation from a builder dashboard, with the numbers guard, If-Match, versions and the preview;
* the export approval bound to version and snapshot hash (requester only, single use, audited), then fork-on-edit;
  another workspace's member gets 404;
* an edit after the request invalidates the export; revert is a new version;
* the dashboard story → a report document; a pinned report refreshed on a schedule fire into a final `report` snapshot.

Not verified: a live delivery to email or a webhook, and model wording (`always` mode) against a real model.

## 2026-10-30 — VALUE-05 analyst pilot kit and rehearsal (real pilot pending)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Pilot kit | [`pilot/`](pilot/README.md): one-page owner plan with thresholds from the evaluation plan, 12 task cards (Ask, investigation, dashboard and report by conversation, scheduled digest), timing sheet and observation form mapped one-to-one onto the scorecard, analyst invite and briefing, check-in and review agendas, data and privacy note, coordinator answer key | Walked once by an agent; not used by analysts yet |
| Scorecard for conversational work | Migration `0075_pilot_review_subjects`: `pilot_review.subject_type` / `subject_id` (investigation run, own Ask conversation, builder dashboard, report document, scheduled digest that has run), `review_minutes`, `interventions`; `run_id` optional. `services/investigation_value.py` `save_review` / `pilot_subjects`, `GET …/pilot-reviews/subjects`; summary split `by_feature`. `PilotScorecard.tsx` "What was assessed" | `tests/unit/test_investigation_value.py` (scoping, privacy of Ask conversations, lineage per subject, per-feature split, contract); `web/src/test/investigationValue.test.tsx`; recorded 15 rehearsal assessments live |
| Readiness for the cards | `services/pilot.py`: `conversation.definitions` (approved metrics / verified queries) and `pilot.rehearsal` (every task-card feature rehearsed in the workspace) | `tests/unit/test_pilot_readiness.py`; live: all three demo workspaces not ready (owners, SSO, live connector certification; definitions missing in the ServiceNow and process-mining workspaces) |
| Fixes from the rehearsal | `dashboards/drafts.py` (typed words outweigh glossary synonyms), `dashboards/design.py` (filter-control phrasings), `reports/design.py` (identical names use the newest; plain note for invalid model ops), `reports/story.py` (no share of a total for rates and averages) | Regression tests in `test_dashboard_conversation.py`, `test_report_documents.py`, `test_data_story.py`; re-run live after each fix |

Rehearsal evidence: [`evidence/value-05-pilot-rehearsal-20261030.md`](evidence/value-05-pilot-rehearsal-20261030.md). Every
record is `evidence_kind = rehearsal` with machine timings; no analyst time, accuracy or usefulness is claimed. Open
problems are tracker rows PILOT-FIX-01…08.

## 2026-10-30 — PILOT-FIX-02 and PILOT-FIX-05: Ask follow-ups, approved metrics, business refusals

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Narrowing follow-ups (PILOT-FIX-02) | `agents/ask_followup.py`: a rules rung after the sample rung. It handles "only X", "just for Q3", "exclude X", "without X", "what about X" and "same for Q2". The restriction is resolved on the previous answer's table: column words and glossary synonyms (`ask_rules._match`), values against the complete profiled vocabulary or the numeric profile range ("P1" = priority 1), and periods by `ask_period`. It is added to the previous SQL in the SELECT that reads the table; the same column's earlier filter, or the same time column's window, is replaced. A governed previous answer gets a `SemanticFilter` or time window and is compiled again. Two columns holding a value → clarify. Anything unresolved → generation, with `refines` = previous question and SQL as data. `conversations/state.py` shows the filters and period (`origin` = followup or answer; a filter a person set stays) | `tests/unit/test_ask_followup.py` (24). Live C02: follow-ups answered by rules with 0 model calls; numbers match the answer key |
| Period coverage and left-out records | `agents/ask_period.py`: the period a question names (quarter, half, month, year, this/last) and the date window its SQL applied. One MIN/MAX/COUNT aggregate through the gateway; the note names the missing months or says the period is still in progress. A GROUP BY column required `IS NOT NULL` is said to be left out. Notes are in `provenance.notes` and shown under the answer (`AnswerNotes`) | `tests/unit/test_ask_period.py` (21), `web/src/test/ask.test.tsx`. Live: Q3 2026 note on the C02 answers |
| Approved metric routing (PILOT-FIX-05) | `agents/ask_metrics.py`: an approved metric named inside a longer question (name, display name, `ai_context` and glossary synonyms; longest phrase, else every word of a multi-word name). The rest of the question must be its dimensions, a grain or a period; output-only sentences are set aside. Tied metrics are ranked by `metric_match` (authority rank, rules backend on names), else a clarify. `sql_agent.compile_governed` labels the answer "Approved metric definition <name> (v<n>)". Notes say when an approved metric matched but did not cover the question, and when no semantic model is approved yet | `tests/unit/test_ask_metric_match.py` (13). Live C06: governed, 0 model calls, numbers match |
| Compiler: lookup joins and grain | `semantic/compiler.py` `_base_only`: a dataset whose only joins are LEFT JOINs from a base table is read from the base table alone. A LEFT JOIN never removes a base row and no uncertified join reaches the query (ADR-0019); fields over the joined tables are unavailable with reason `uncertified_join`. Inner, right, nested, filtered-by-join and unqualified-column datasets are still refused. `_grain_holds`: `record`, or the base table's own entity (`order` on `orders`) with no join | `tests/unit/test_semantic_base_only.py` (12); semantic and dashboard suites pass unchanged |
| Business wording for model-setup refusals | `services/ask.refusal` adds `business` (title, message, remedy) for `no_api_key`, `mode_off`, `no_model`, `policy_blocked`, `residency_blocked` and `provider_cooldown`. `lib/ask.refusalText`: the business view shows it to people who are not platform admins; admins and the expert view keep the technical remedy | `test_ask_metric_match.py::test_model_setup_refusals…`, `web/src/test/ask.test.tsx` (business vs expert). Live rules-only pass |

Re-walk: [`evidence/pilot-fix-ask-20261030.md`](evidence/pilot-fix-ask-20261030.md), on a clone of the rehearsal database.
Limits: no demo workspace has an approved semantic model (owner action), so C06 stays ad hoc on the shared databases,
with a note saying why. Governed metrics cannot use lookup fields (product category, customer region) until that join
is certified.

## 2026-10-30 — Pilot fixes: cross-filter scope, digests on schedules, report summary, coded numbers (PILOT-FIX-03, -07, -08)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Cross-filter reaches every tile its source can take (PILOT-FIX-03) | `dashboards/interact.py`: `origins` (tables a chart showing the field reads), `reach`, `pushdown` (predicate in the tile query's own WHERE on that table's column, sqlglot nodes and `sqlbuild` literals; denied columns, set operations and other tables refused), `why_not` in plain words; Show rows carries the pushed filter. Every statement through `QueryGateway.execute` as the viewer | `tests/unit/test_dashboard_interact.py` (real validator + DuckDB); web and Playwright explore journeys; re-walk C05 matches the answer key. "Apply as dashboard filter" still scopes the saved filter to charts that show the field |
| Dashboard summary on a schedule (PILOT-FIX-07) | `web/src/components/DashboardDigest.tsx` on the Summary panel; schedule form checkbox list; `lib/schedules.withDigest` keeps every other config key | `test_report_docs_service.py` (editor only, builder dashboards only, next fire saves the digest, nothing sent without a destination); `dashboardDigest.test.tsx`; `e2e/dashboard-digest.spec.ts` (axe-clean); re-walk C09 |
| Report summary drafted from verified blocks (PILOT-FIX-08) | `reports/summary.py` + `reports/design.draft_summary`: template sentences (rules rung), optional `data_story` rewording in `always` mode with a per-sentence numbers guard, plain fallback note; a `text.set` the engine guards again | `tests/unit/test_report_summary.py`; re-walk C07 (rules only). The summary repeats a finding's discovery-row figure (PILOT-FIX-06) |
| Coded numbers are groupings | `skills/viz.is_coded_number`; `dashboards/drafts.classify` (prompt-named groupings first, text categories before codes); `dashboards/questions.infer_fields` (ordinal axis); a second identical filter control is refused (`dashboards/ops.py`) | `test_dashboard_conversation.py`, `test_skills_viz_dashboards.py`, `test_dashboard_ops.py`; re-walk C04: no "Average priority" KPI |

Re-walk evidence: [`evidence/pilot-fix-dash-reports-20261030.md`](evidence/pilot-fix-dash-reports-20261030.md) (agent,
rules only, on a copy of the rehearsal database; no analyst results).

## 2026-10-30 — PILOT-FIX-01 and PILOT-FIX-06: typed periods, findings scoped to the rows they describe

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Typed period and baseline (PILOT-FIX-01) | `GuidedInvestigationIn.period` / `comparison_mode` (previous period, same period last year, custom, none) / `baseline` are date ranges. `services/periods.py`: baseline from the mode (whole months shift by months), one sortable label style ("2026 Q2" / "2026 Q3"), each table's start-of-record timestamp (pack `event_start` hints, then crawler roles; end-of-life timestamps never), `run.constraints.period` (plan hash). `agents/investigator.py` applies it to every accepted spec and refuses specs on a table without a period column; `with_constraints` re-applies it at execution and in REV | `tests/unit/test_pilot_fix_periods.py` (baselines, labels, column choice, contract, filters, plan hash, gateway on four dialects, planted Q3 rise on retail data, data facts, source choice); live C03 re-walk |
| Period comparison in the closed vocabulary | New `Derivation.type = "period"` (2–4 labelled, end-exclusive, non-overlapping windows; `contracts/analysis.py`), compiled by `skills/sqlbuild.period_exprs` (label + order, NULL outside), ordered like a bucket; `rate_by_segment` quotes the baseline as reference; `contribution_decomposition` takes a two-window period as `time`. Guided runs always test baseline vs period for the playbook's outcomes and split the change within groups vs mix, outside the round-1 cap | Same tests; `contracts/analysis_spec.schema.json` regenerated |
| Period data facts | `investigator.period_data_facts` (agent.investigator 1.2.0 adds `sql.execute`): two aggregates per table state an incomplete period (data ends before the period does), months without rows, and rows dated after today. They become caveats of the table's findings, `period:` limitations on every Evidence object, the run summary's "Requested period" block and the Run overview's "Period analysed" panel | `test_an_incomplete_period_and_future_rows_are_stated_as_data_facts`, `test_c03_shape_…`; live C03: no September rows, 5 future-dated incidents |
| Requested comparison on every row | `critic.period_comparisons`: after every claim was locked and tested, each baseline-vs-period comparison runs once on every row (`full_data` experiment) and is stated with its direction and the run's own significance verdict, significant or not | `test_the_requested_comparison_reads_with_its_direction_even_when_not_significant`; live C03: missed SLA 14.2% (5,029) → 13.5% (3,421), equal to direct SQL |
| Guided source choice (friction) | `investigation_value.guided_sources`: the caller's choice, else the only source, else the one whose tables the template's pack fits (with a date column); asks only when still ambiguous. The web form no longer demands a source for a template | `test_a_guided_run_reads_the_source_its_template_fits` |
| Finding scope (PILOT-FIX-06) | `agents/critic.py`: once the held-out rows confirm a finding, its test runs once on every row (`full_data` experiment, agent.critic 1.2.0), the sentence is re-rendered from those figures and must bind to them (numbers guard), and the discovery title, sentence, n and facts stay in `evidence_bundle.claim.scope.discovery`; a different top group or direction keeps the discovery sentence. Every other partitioned finding ends with `strength.DISCOVERY_NOTE`; confirmed ones with `ALL_ROWS_NOTE`. Reports, dashboards and the run summary quote `Insight.finding` and inherit it | `tests/unit/test_pilot_fix_finding_scope.py`; live C03/C11: sentences equal direct SQL |
| Web | `StartWork.tsx` date inputs + "Compare with"; `RequestedPeriod.tsx` on the Run overview; `web/openapi.json` and generated types regenerated | `web/src/test/investigationValue.test.tsx` |

Live evidence: [`evidence/pilot-fix-investigate-20261030.md`](evidence/pilot-fix-investigate-20261030.md) (rules only, no model
call). Not verified: a live run with models; the guided source choice on a live two-source workspace (unit tested only).

## 2026-11-05 — S03-T8: a knowledge review batch is tested against golden questions before approval

From the Google Knowledge Catalog review ([`70-reviews/2026-11-05-google-knowledge-catalog-review.md`](../70-reviews/2026-11-05-google-knowledge-catalog-review.md), P0-1).

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Effect preview | `knowledge/effect.preview`: the selected decisions run through `suggestions.review` inside a savepoint, `services/context_grounding.run_golden` scores the golden questions before and after (no model call), the savepoint rolls back; per case `fixed / newly_failing / still_failing / still_passing`, baseline, projected and delta in points. `POST /api/workspaces/{ws}/knowledge/suggestions/preview` (editor) | `tests/unit/test_knowledge_effect.py`; `tests/integration/test_knowledge_effect_api.py::test_preview_scores_the_batch_without_applying_it_then_review_reports_the_effect` (draft still pending, no glossary entry, no cached synonym after the preview) |
| Regression gate | `knowledge/effect.review_checked` behind `POST .../suggestions/review`: a batch that makes a passing golden case fail answers 409 `details.code = golden_regression` with the effect; with `override_reason` it applies and audits `knowledge.review.golden_effect` (decision `override`, reason, case ids, both scores, revision). An improving or neutral batch applies and returns `effect` | unit gate tests; the existing knowledge/glossary/studio/grounding integration tests pass through the gate (21 passed, Postgres 16, 2026-11-05) |
| Golden question names a verified query | `GoldenIn.expected_query` (must be a verified query of the workspace, 422 otherwise); the case passes only when `verified_queries.match` serves it, so a synonym change that breaks or fixes Ask's tool-first answer shows | `test_golden_question_must_name_a_verified_query_of_its_own_workspace`; served query before/after in the preview |
| Lexicon cache safety | `verified_queries.forget_lexicon` before the projected run and after the rollback: the 60 s workspace lexicon cache never keeps an unapproved batch's synonyms for Ask | asserted in the integration test |
| Web | `ReviewQueue.tsx`: "Check effect on golden questions", blocked-approval panel with a required reason, effect line in the result; `web/openapi.json` and generated types regenerated | `web/src/test/knowledge.test.tsx` (2 new: preview, 409, approve with reason; improving batch) |

Limits: golden cases are capped at 30 per workspace (as before), and an approved glossary term is stored with origin `agent`,
which grounding does not use, so glossary drafts change only the verified-query part of a case. Optional auto-approve for
low-risk kinds is not built. Not run against a live model or the compose stack.
## 2026-11-05 — GKC-P1-5: PII along column lineage, impact report on a schema change

From the Google Knowledge Catalog review ([P1-5](../70-reviews/2026-11-05-google-knowledge-catalog-review.md)). Rules
decide; nothing is applied without a person. No migration.

| Capability | Code | Evidence |
|---|---|---|
| Column edges from every recorded lineage | `evidence/lineage/propagation.py`: `edges_from_sql` (CTEs, subqueries; catalog resolves bare columns), `edges_from_dbt_manifest`, `edges_from_recipe` (`recipe_run.lineage`), `edges_from_openlineage` (`build_job.openlineage` column facets). FILTERED / `*` / INDIRECT edges move no values and are not followed | `tests/unit/test_pii_lineage_impact.py` (CTE chain over two statements, dbt model chain, OpenLineage + recipe adapters, filters raise nothing) |
| Tag gaps, add-only | `propagate`: multi-source BFS per tag (pii, restricted, sensitive), shortest witness path, confidence from the weakest hop (copy 0.95, derivation 0.8, aggregate 0.6); cycles terminate | same file (missing tags listed, cycles) |
| Review drafts that add the tag | `services/pii_lineage.scan` (crawl facet `pii_lineage`, `POST /api/workspaces/{ws}/lineage/pii-scan`): one `column_tag` draft per catalogued column via `knowledge/suggestions.propose`; uncatalogued targets reported only. Approval (`_apply_column_tag`) unions tags in the TAGGABLE set, sets `tags_origin=user`, audits `column.tags_added`; an edit to a non-sensitive tag or an empty list is refused; a rejected tag for a column is never proposed again, whatever path the lineage takes. Event `lineage.pii_gap_detected` | `test_scan_proposes_add_only_drafts_through_a_recipe_chain`, `test_approval_adds_tags_and_never_removes_one`, `test_uncatalogued_targets_are_reported_not_drafted` |
| Impact report | `services/change_impact.py`, `GET /api/crawls/{id}/impact`: removed/retyped columns and deprecated tables, downstream columns along recorded lineage, then metrics, verified queries (parsed SQL, placeholders bound), dashboards, monitors, schedules (config + pins), with `via` links (a schedule of an impacted monitor). Counts on event `schema.impact_reported` and `crawl.stats.impact` | `test_impact_lists_what_reads_a_removed_column`, `test_a_deprecated_table_impacts_everything_reading_it` |
| Pause impacted schedules (rule 6) | `POST /api/crawls/{id}/impact/pause` requests approval `schedule.pause_for_change` bound to schedule ids + revisions; `POST /api/approvals/{id}/execute-schedule-pause` re-derives the impact and payload, `verify_for_execution`, `consume` (single use), then disables. An edited schedule changes the hash | `test_pausing_impacted_schedules_needs_a_bound_approval`, `test_a_schedule_edited_after_approval_needs_a_new_one` |
| UI | `CrawlPanel.tsx` `ImpactPanel` on a succeeded crawl with drift (counts, downstream columns, dependents, pause request → execute); ReviewQueue label `column_tag` | `web/src/test/crawlImpact.test.tsx` (2), `increment3.test.tsx` CrawlPanel still green |

Not verified: a live crawl on the compose stack. Matching for dashboards, monitors and schedules is on identifiers
(asset then column), so it over-reports rather than misses. Published Superset datasets are not a lineage source yet
(they carry no column lineage). Found in passing: `evidence/lineage/dbt.py` loses a model's column lineage when its
compiled SQL has a numeric literal (redaction writes `<NUM>`, which does not parse).
## 2026-11-05 — Review P1-6 Context change push subscriptions (S03 §5)

Filtered push of the ledger change stream to a webhook (`context/webhooks.py`, migration `0077_context_webhooks`, based
on head `0076_run_leases`: `context_webhook` and `context_webhook_delivery`, both owned by the workspace with RLS, policy
and fill trigger; `tests/unit/test_tenancy_rls_coverage.py::test_context_webhook_tables_are_owned_by_their_workspace`).
Not the federation `context_subscription` (that pulls another workspace's ledger).
* Filters: `kinds`, `changes` (`create|update|deprecate`), `tag_added` (`pii|sensitive|restricted`) and `schema_changed`.
  `kinds` and `changes` narrow; when a signal filter is set the change must raise one of them. The signals are diffs of
  consecutive dataset versions: the dataset projection (`context/ledger._datasets`) now records the asset's structural
  fingerprint and sensitive column tags in provenance (one new dataset version per asset at rollout; a version without a
  fingerprint is never a schema change). Column tags change the dataset only once the asset row is flushed or `analystos
  context reconcile` runs.
* Approval (rule 6): create/update asks `context_webhook.authorize` bound to the hash of filters + URL + secret reference
  (`ALWAYS_SEPARATE_DUTIES`; decided in the approvals inbox, `artifacts._decide`). A rename needs nothing. Every send
  calls `verify_for_execution` right before the POST and re-checks the outbound guard; a row edited behind the API is
  refused and the subscription shows `lapsed`. A new subscription starts at the ledger head (no replay).
* Delivery: `services/deliveries` URL guard, HMAC signature and pinned outbound call are reused. The body is ledger
  metadata only (ids, kind, change, version, signals), never a title or body; personal and suggested entries are not
  announced; scanning is by workspace and bounded by the creator's current access (`retrieval.access_for`, resolved each
  scan): an entry the creator cannot see is withheld (the cursor still moves), and a creator who is gone or no longer a
  member lapses the subscription. At least once with an idempotency key, one delivery per `(subscription, seq)`;
  bounded exponential backoff, `dead_letter` after `delivery_max_attempts` or a permanent 4xx, editor redrive; the
  `context_webhook_delivery` table is the log.
* Internal consumer: each workspace with dataset context gets a platform `internal` subscription (`schema_changed`
  only); on a delivery it re-evaluates the enabled `data_quality` monitors that name the asset (S09 `evaluate_monitor`).
* Tenancy: `scan` is `@owned_by("context_webhook", ...)`, `attempt` is `@owned_by("context_webhook_delivery", ...)`,
  `dispatch_due` is a `@platform_loop` in scheduler housekeeping.
* API `/api/v2/context/webhooks` (list, create, patch, revoke, reauthorize) and `/webhook-deliveries` (log, redrive);
  UI `ContextWebhooks.tsx` on the Schedules page.

Tests: `tests/unit/test_context_webhooks.py` (filter validation and matching, signals, approval bound to the hash and
separation of duties, nothing sent before approval, changed filters re-approve and refuse queued sends, tamper refused,
revoke, signed metadata-only payload, workspace scope and hidden trusts, retry/backoff/dead-letter/redrive, internal
re-check); `web/src/test/context_webhooks.test.tsx`. Not verified: a live run against Postgres RLS and a real receiver;
the dispatch pass is unit tested with a fake HTTP transport.
## 2026-11-05 — P2-8 context index and MCP grant slice (Google Knowledge Catalog review)

| Capability | Where | Evidence |
|---|---|---|
| Generated context index (machine draft) | `context/index.py`: deterministic `index.md` per workspace and `domains/<d>/index.md` per catalog domain (`semantics.domain`, reviewed or rule-derived) listing verified queries, tables, metrics, glossary terms and documents, one line and a path each. Written by `knowledge/drafts.write_drafts` (curated documents kept, unchanged index writes no revision), kind `index` / `trusted: false` so retrieval and the ledger never treat it as knowledge. Refreshed after a crawl (`services/crawler.py::_knowledge`) and after a knowledge review (`knowledge/suggestions.review`) | `tests/unit/test_context_index.py` (determinism, curated kept, idempotent, not projected into the ledger) |
| Index read first | `retrieval.compile_bundle(index_first=True, domain=...)` opens with the caller's index digest and carries items as short pointers; MCP `context.compile` args `index_first`, `domain`; MCP resources `analystos://<ws>/index/<workspace\|domain>` built per caller over their `Access`. Savings measured by `context/benchmark.index_first_savings` | `test_index_first_compile_reads_the_index_and_spends_fewer_tokens`; tokens saved asserted > 20% on a 16-entry fixture, live figure not measured |
| Grant slice (`domains`, `assets`) | `mcp_grant.domains/assets` (migration `0079_mcp_grant_slice`, after 0078 when merged), `mcp/grants.set_grant`/`check_grant` (a sliced grant holds only context tools; the gate refuses any other), `retrieval.Access.slice_assets`: an entry outside the slice is withheld in `candidates` before ranking, so it is never scored, counted, omitted or put in a receipt; same for `get`, `changes`, resources and the index. Entries with no table reference are in a domain only through their `domain_pack` provenance. Admin UI: Domains/Assets fields in the grant editor (`ToolRegistry.tsx`) | `test_slice_hides_out_of_domain_entries_before_ranking_and_counts`, `test_grant_slice_is_context_only_and_enforced_at_the_gate`, `test_mcp_tools_and_resources_follow_the_slice` |

Not done: a domain hierarchy (deferred per the review); removing the index document of a domain that has disappeared;
no UI test for the new fields; live MCP client run against a sliced grant.
## 2026-11-05 — P2-10 Read-only knowledge sources: GitHub and Confluence

Review item P2-10 of [`2026-11-05-google-knowledge-catalog-review.md`](../70-reviews/2026-11-05-google-knowledge-catalog-review.md).
A workspace registers a GitHub path (README, docs Markdown, dbt `schema.yml`/`sources.yml`, `.sql`) or one Confluence space; a sync turns
every file into an untrusted draft in the review queue, never knowledge directly.

| Capability | Where | Evidence |
|---|---|---|
| Source registry | `knowledge_source` table (migration `0078_knowledge_sources`, after 0077 when merged, RLS installed), `services/knowledge_sources.py`, API `GET/POST /api/workspaces/{id}/knowledge/sources`, `PATCH/DELETE .../{source_id}`, `POST .../{source_id}/sync` | `tests/unit/test_knowledge_sources.py` (service tests on SQLite; RLS coverage `test_tenancy_rls_coverage.py`) |
| Credentials | `secret_ref` only (`connectors/secrets.py` policy at registration and at sync); a config key such as `token` or `password` is refused; nothing resolved is stored, logged or returned | `test_a_credential_in_the_config_is_refused`, `test_a_secret_ref_outside_the_operator_policy_is_refused`, `test_the_credential_goes_in_a_header_only` |
| SSRF-safe fetch | `knowledge/sources.py` over `tools/http.call`: https only, host allowlist = `api.github.com`, `*.atlassian.net` + the operator's `http_tool_allowlist`, public addresses only, pinned connection, no redirects, response byte cap, 20 s timeout | `test_only_known_hosts_are_reachable_unless_the_operator_lists_more`, `test_non_public_resolution_is_refused`, `test_redirects_are_not_followed`, `test_response_byte_cap_is_enforced_by_the_http_layer` |
| Screening | every file through `knowledge/documents.screen` (redaction, instruction-line markers, control characters, link neutralising), their frontmatter dropped, SQL/YAML inside a fence that cannot be closed from inside; drafts carry `trusted: false`, `status: draft` | `test_files_are_screened_like_uploads_and_arrive_as_untrusted_drafts`, `test_sql_and_yaml_are_data_inside_a_fence_and_cannot_close_it` |
| Caps | 200 files/pages, 256 KiB per file (declared and downloaded size), 8 MiB per sync, 600 requests, 50 pages per Confluence request | `test_caps_on_file_size_count_and_total`, `test_confluence_incremental_and_page_cap` |
| Incremental | per path: remote revision (git blob sha, Confluence version) skips the download, sha256 of the bytes skips the rewrite; removed remote files are reported, never deleted | `test_second_sync_skips_unchanged_files_without_fetching_them`, `test_changed_file_is_fetched_and_same_bytes_under_a_new_revision_are_not_rewritten`, `test_removed_remote_files_are_reported_not_deleted` |
| Schedule | schedule kind `knowledge_sync` (existing scheduler claim-then-execute, editor role, 15-minute minimum interval) | `test_schedule_kind_is_registered` |
| UI | Data dictionary → Import & export → "Remote sources" (`web/src/components/KnowledgeSources.tsx`): add, sync now, pause, remove, daily schedule | Not exercised in a browser or type-checked in the building session (no `node_modules` in the worktree) |

Not verified: a live call to GitHub or Confluence (unit tests use a mock transport only); a Postgres run of migration 0077 (the SQLite unit
fixtures and the RLS coverage test pass; the integration migration test was not run). dbt `schema.yml` is shown as screened text, not parsed
into catalog descriptions (the manifest path in `knowledge/dbt_manifest.py` stays the structured route). Jira and other kinds are not built.
## 2026-11-05 — P2-11 Glossary spreadsheet round trip (Google Knowledge Catalog review)

| Capability | Where | Evidence |
|---|---|---|
| Export the workspace glossary (term, definition, synonyms, linked columns, owner, trust, status) as CSV or XLSX | `knowledge/glossary_sheet.py`, `GET /api/workspaces/{id}/knowledge/glossary/export` (viewer; audited `knowledge.glossary_exported`). Cells are neutralised against formula injection: CSV prefixes an apostrophe (exactly reversible on import), XLSX writes every cell as text through `reports/excel.put` | `tests/unit/test_glossary_sheet.py` (injection, plain text, XLSX cell types) |
| Access: a term linked only to columns the caller's data scope hides is omitted, a hidden column in a visible term's links is dropped; both counted in `X-Omitted-Terms` / `X-Omitted-Links`, never named | same, `resolve_scope` (restricted tag, PII, ABAC) | `test_restricted_links_and_terms_are_omitted_and_counted` (owner and viewer) |
| Import the same format as review drafts: `POST …/knowledge/glossary/import` (editor; `dry_run`; audited). Each row is a `glossary_term` suggestion via `suggestions.propose`, provenance `import:csv`, field confidence 0.5; `suggestions.review` raises imported fields to 1.0 and records `confirmed_by` only on approval. Nothing is certified by an import; `owner`/`trust`/`status` in a file are ignored and reported | `knowledge/suggestions.py::confirm_imported` | `test_round_trip_is_identical_and_certifies_nothing_until_approved` (csv and xlsx), `test_importing_needs_no_existing_confidence_claim` |
| Per-row report: unknown columns (a hidden column reads the same as a missing one) are dropped and named, term/definition errors, duplicates in the file, existing term flagged, instruction-like definition text screened (`knowledge/documents.screen`) | same | `test_row_errors_…`, `test_unknown_headers_…`, `test_import_cannot_link_a_hidden_column`, `test_instruction_like_definitions_are_screened`, `test_existing_term_is_flagged…` |
| Idempotent re-import (draft content hash); unchanged rows skipped; caps 2 MB, 2000 rows, term 300, definition 4000, 10 synonyms, 50 links, XLSX unpacked size | same | `test_reimport_is_idempotent`, `test_size_and_row_caps`, `test_synonym_and_link_caps` |
| UI: Data → Catalog & definitions → Import & export → Glossary spreadsheet (download with omitted counts, check-only and queue, per-row problems) | `web/src/components/ContextExport.tsx`, `web/src/api.ts`, OpenAPI schema and types regenerated | not run in a browser; `tsc`/vitest not run (no `node_modules` in this worktree) |

Limits: links exported are the term's own `mapped_columns` (crawler auto-links are heuristics, not steward data); the owner column is the entry origin (the glossary has no steward field); pack vocabulary is not exported; a file-level approval screen groups by batch in the existing review queue, nothing new there.
## 2026-11-05 — Review P1-3: knowledge-gap mining from failed Ask questions

Source: [`70-reviews/2026-11-05-google-knowledge-catalog-review.md`](../70-reviews/2026-11-05-google-knowledge-catalog-review.md) item P1-3 (S03 / S04).

| Capability | Where | Evidence |
|---|---|---|
| Mining of Ask outcomes (`clarify`, `not_answerable` decline, ad-hoc model answer with >= 2 SQL repairs) plus a repeated rephrase in the same conversation, grouped by the normalised unresolved term (`glossary_scan.ask_terms` against catalog names, code values and glossary) | `knowledge/gaps.py` `collect`, `outcome_of` | `tests/unit/test_knowledge_gaps.py` (grouping, outcomes, rephrase, known vocabulary) |
| Threshold and window: a term in >= k distinct conversations over the last N days; both and a per-pass draft cap are platform settings `knowledge_gaps.{min_conversations (3), window_days (14), max_drafts_per_run (10)}` | `contracts/platform.py` `KnowledgeGapSettings` | `test_knowledge_gaps.py` (threshold, window, settings) |
| Redaction: only the question text is read, passed through `redact_question` first; placeholders are never terms; `result` and `sql` are not selected, so no result value is read or stored | `gaps.redacted_plain`, `gaps._turns` | `test_questions_are_redacted_before_terms_and_examples_are_read` |
| A `knowledge_gap` Note draft per term through `knowledge/suggestions.propose`: counts, outcome mix, example turn ids and redacted example questions; routed to the named owner of the source the conversations touched, else the workspace owner (business first) | `gaps.mine_workspace`, `gaps.route`; `KIND_SPEC["knowledge_gap"]` | `test_models_off_pass_proposes_one_note_...`, `test_routes_to_the_owner_of_the_source_...` |
| De-duplication against pending, rejected (and approved) drafts of the same subject, and against rejected wording (`rejected_values`) | `gaps.decided`, `mine_workspace` | `test_pending_and_rejected_drafts_are_not_proposed_again` |
| Models `off`: the rules rung writes the whole note. Optional wording purpose `knowledge_gap_note` (`config/models.yaml`: cache, rules, llm_small; low_cost; never escalates; cacheable). Called only in mode `always`; otherwise `record_skip` shows the saved call. Output is screened (injection, placeholder, 400 chars) and falls back to the rules text; the counts are never the model's | `gaps._wording`, prompt `knowledge_gap_note.v1` | `test_model_not_called_unless_always_...`, `test_always_mode_words_the_note_...`, `test_bad_model_wording_falls_back_...` |
| Background rule: `mine_all` is a `@platform_loop` that only lists active workspaces; each is mined inside `as_owner_of("workspace", id)`. Wired into scheduler `housekeeping()` at most hourly per process (`mine_quietly`) | `gaps.mine_all`, `services/schedules.py` | code path; the live scheduler pass is not run |
| Review queue shows the draft distinctly: warning tag "knowledge gap", a notice with conversations, outcome mix, rephrases and the routed owner; only the note text is editable | `web/src/components/ReviewQueue.tsx`, `web/src/lib/knowledge.ts` `gapSummary` | `web/src/test/knowledge.test.tsx` |

Not verified: a live scheduler pass against real Ask history with a model (unit tested only; no migration, no new table).
Cross-reference: [`01-tracker.md`](01-tracker.md) is untouched; this item is a review follow-up, not a v2 task ID.

## 2026-11-06 — Local mode with nothing installed; MCP for any application (S12-LOCAL, S10-T6)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| One-command local run | `analystos local [--demo]` (`local.py`): Postgres 16.2 + pgvector 0.6 from the `pgserver` wheel in a data folder, 127.0.0.1 only; roles and databases of `deploy/postgres/01-init.sql`; `migrate` + `seed`; API, MCP and the built web app on one port; a per-install JWT secret; waits out crash recovery after a hard kill | Live, Windows 11, 2026-11-06: fresh folder → health 200 in 90 s; `--demo` loaded 4 ServiceNow tables (118k rows) and the process-mining workspace; restart after a kill recovered (WAL redo) and answered |
| Local database hardened | every role, the superuser included, gets a generated password kept owner-only in the data folder; pg_hba trust becomes scram-sha-256; the API binds loopback only unless --allow-remote with no dev defaults (governance review 2026-11-06) | live: after upgrading an existing folder, a passwordless superuser login is refused; unit test_trust_rules_become_scram_and_comments_stay, test_a_local_run_listens_on_loopback_unless_shared_without_dev_defaults |
| No vector store to run | embeddings stay a pgvector column of the bundled database; `ANALYSTOS_VECTOR_BACKEND=array` still works | SQLite was rejected for the control plane: RLS tenancy, per-workspace analytics roles, run leases and spend locks are Postgres features (runbook 15 §1) |
| Web on the API port | `api/web_static.py`: registered after every route; API-shaped paths keep their JSON 404; files only from inside `web/dist`; the web CSP of `web/nginx.conf` (the API default `default-src 'none'` blanked the page) | `test_web_static_serves_the_app_but_never_shadows_api_paths`; checked in the browser: sign-in and the workspace home render |
| Metadata for an application's own SQL | MCP `catalog.tables` (sources, dialect, tables, columns minus denied ones; scope and grant slice) and the existing `context.lookup` | live: 6 tables in 2 sources |
| Stored queries for any application | MCP `queries.list` (visible = every table in scope and slice, no denied column; enum values and defaults only with `context.lookup.values`; `question` returns the best match with extracted values) and `queries.run` (one-pass typed binding: a value cannot capture another placeholder; `needs_input`, hit counted) | live: "priority 3" extracted from a new question; no value → `needs_input`; `"2; DROP TABLE x"` refused as not a number; run returned 5 rows |
| The application's own SQL | MCP `sql.run` through `_governed_run`: on a slice the parsed table list is checked first (one generic refusal, before the validator's hints), then the validator under the client's scope, `QueryGateway.execute` (masking, caps, audit), ≤ 200 rows back | unit: row cap, refusal audited and never executed, outside-slice refused before running; live: COUNT(*) = 3000, DELETE refused (`sql_rejected`) |
| Connect a client | `analystos mcp-client create` (client + grant as the admin screens, audited; VS Code `mcp.json` with a password prompt instead of the token; generic `mcpServers` JSON) and `analystos mcp-client check` | live: client created for the demo workspace, `check` all ok |
| Knowledge source approval in the UI | `KnowledgeSources.tsx`: binding state tag, note with the approvals inbox link, Sync now enabled only when approved | `web/src/test/knowledgeSources.test.tsx` |

Not verified: GitHub Copilot in VS Code itself (the check uses the same MCP SDK transport Copilot speaks; VS Code was
not driven here); macOS/Linux local runs; the `ask` tool without any model configured answers only from stored queries.

## 2026-10-30 — S07-T8 Number check (from the blue ocean discovery)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| Check a quoted number | `evidence/number_check.py`: the question runs through Ask under the caller's scope (new thread "Number check: …"); `parse_claim` keeps the written precision, `pick_value` singles out one number (scalar, `label` row, `column`), `compare` applies the numbers guard's rounding rule (percent ↔ fraction, optional relative `tolerance`). Verdict confirmed / contradicted with authority certified (verified query, certified metric) or ad_hoc, or unverifiable with the reason and a remedy. The claimed value is never in a model prompt. Audit `number_check.completed` | `tests/unit/test_number_check.py` (parsing, rounding, picking, verdicts); `tests/integration/test_number_check.py` (Postgres RLS + strict tenancy: confirmed/contradicted equal a reference query, labelled breakdown row, unverifiable cases, 422 on a non-number, claim absent from every model call, audit rows, non-member refused) |
| Surfaces | `POST /v2/number-checks` and `/api/v2/number-checks`; MCP tool `number.check` (grant role analyst); "Check a number" card on the Ask screen with verdict, difference, authority, tables, freshness, SQL receipt and "Open the answer"; `web/openapi.json` + generated types regenerated; `number.check` in the web's built-in MCP tool list | `web/src/test/numberCheck.test.tsx`; live walkthrough below. MCP over HTTP not run (the `mcp` extra is not installed on this host) |

Live evidence: [`evidence/number-check-20261030.md`](evidence/number-check-20261030.md) (5/5, rules rung, $0.000012 total).
Not verified: a certified verdict live (no certified incident metric in the demo workspace); a model-written answer live.

## 2026-10-31 — S07-T9 Trust loop (label, certification coverage, check → Desk)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| One trust label | `web/src/lib/trust.ts` (`trustOf`, `trustOfAuthority`), `components/TrustBadge.tsx`: Certified / Checked / Unchecked with the same words and hint everywhere; Ask "Answered by" line, number-check result | `web/src/test/numberCheck.test.tsx` ("trust label") |
| Check → Desk | `evidence/number_check.handoff`, `POST /v2/number-checks/handoff`: escalated R7 request (assignee, SLA, data pack with the check and the Ask turn as evidence), verdict recomputed from the stored turn; refused for a confirmed certified number; "Send to an analyst" button | `tests/integration/test_number_check.py::test_handoff_to_the_desk_and_trust_coverage`; live `rq_ede96ebb4daa` (2026-10-31) |
| Certification coverage | `evidence/number_check.coverage` → `trust` in `GET /api/value` (answered, certified, certified share, check outcomes); Trust card on Value with the way to raise it | Same integration test; live: 0 of 12 certified, 10 checks |

Not done: plain names for the remaining internal terms outside expert view.

## 2026-10-30 — S10-MCP-LIVE: MCP over streamable HTTP, live

The official MCP Python SDK client (`mcp` 2.3.0, protocol `2026-07-28`) called this worktree's API at `/mcp` over
streamable HTTP, with the API running against the compose stack. Two admin-created clients had analyst grants on
`ws_c0f6d56a3ce0`. `tools/list` listed `number.check` with its schema. `ask` returned 20000. `number.check`: `20,000`
confirmed, `20037` contradicted, `775` with label `1` confirmed (all equal to psql, rules rung, $0 model cost). A client
without the grant did not see the tool and was refused (SDK `MCPError`; raw HTTP 403 at the gate). With no token the
endpoint returned 401. The trail reads as follows: `mcp.request` allow/deny audit rows; `number_check.completed` with
channel `mcp`; every query receipt's actor is the client's service user (one gateway); denied calls are not counted in
`mcp_usage`. This closes the "MCP over HTTP not run" gap in the S07-T8 entry above.

Live evidence: [`evidence/mcp-live-20261030.md`](evidence/mcp-live-20261030.md) (+ `.json`, 9/9). Not verified: a
certified verdict over MCP, MCP resources, a third-party MCP client application as the caller.

## 2026-10-30 — S05-T7 air-gapped §62 run: blocked (no local model server)

| Capability | Implementation | Evidence / limits |
|---|---|---|
| §62 scenario on an air-gapped install with a real local model | unchanged: `services/airgap.py`, `config/models.airgapped.yaml`, runbook `docs/30-runbooks/08-local-models.md` | ❌ not run: the dev host has no local model server (no Ollama, LM Studio or vLLM; no GPU; CPU-only, 32 GB RAM). Nothing installed, compose stack untouched, no model call |

Blocked report: [`evidence/airgap-e2e-20261030.md`](evidence/airgap-e2e-20261030.md) — what was checked, what is
missing, and the exact commands (Ollama native or in Docker, `qwen3:8b`, a local models file, the API env, the
posture check, `scripts/e2e_demo.py`). Still evidenced by unit tests only (`test_model_switch_gate.py::test_airgap_posture_*`).

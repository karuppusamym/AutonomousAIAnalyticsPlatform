# Capability register (evidence)

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
| Source-kind catalog + generic SQL connector | `config/source_kinds.yaml`, `connectors/{kinds,generic_sql}.py` | 120 unit tests; integration against MySQL 8.4 (docker), SQLite, DuckDB and Postgres: discovery, read-only session, staging, gateway query with a denied column | SQLite (this run) | Not certified: SQL Server, Oracle, Snowflake, BigQuery, Databricks, Trino, Redshift, ClickHouse, MariaDB |
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
| Engine protocol, multi-dialect validator | `engines/`, `gateway/dialects.py` | `test_gateway_dialects.py` (286 cases) | ❌ no warehouse instance | Snowflake/BigQuery/Databricks/Trino stay staged |
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
| N-7 / INT-004 entity matching | `skills/entity_matching.py`, `services/entity_matching.py`, `api/routers/entity_matching.py`, `evaluation/entity_match_datasets.py`, migration 0054_n7 | `test_entity_matching.py` (19), live `test_entity_matching_live.py` (3, compose Postgres) | n=1000: match band P 1.0 / R 0.870; match+review P 0.994 / R 0.997 (synthetic truth). TRN-004 open |
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

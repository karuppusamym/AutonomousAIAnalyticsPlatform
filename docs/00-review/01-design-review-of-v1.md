# Design review of "Autonomous AI Analytics Platform — Full Design Spec v1"

Reviewed 2026-10-27 against the v1 `.docx` (26 Oct 2026) and the two existing codebases:

| Repo | What it is | Size / maturity (from its own docs) |
|---|---|---|
| `AIAnalystAgentOS` (**AnalystOS**) | Governed autonomous analytics agent OS. FastAPI, Postgres+pgvector, Temporal, Superset, React/Vite. Designated "the single platform" by its ADR-0018. | ~115k lines Python, ~59k lines TS, 309 test files, 561 commits, 25 ADRs. Readiness: **prototype** (synthetic/file data, local identity). |
| `AIDataAnalyst` (**Atlas**) | Bank-grade, value-free metadata intelligence: catalog, OIDC/JWKS, secret managers, MCP endpoint for external agents, OpenAI/Gemini/Azure adapters. | 1,045 commits. Donor to AnalystOS (31 of 67 capabilities covered, 21 partial per the parity checklist). |

The coverage assessment below is based on repository docs, the tracker and the code layout. It is not
a certification. Re-verify each "Built" row with its test before claiming it in a release.

## 1. Verdict

The v1 document is a sound *target description* of a governed analytics agent. As a *build plan* it has three problems:

1. **It is greenfield, but the product already exists.** About 80% of the v1 functional catalogue is
   already built in AnalystOS, often more rigorously (for example verification fingerprints that void
   stale results, a deterministic-first model ladder, a closed analysis vocabulary, and approvals bound
   to content hashes). Starting a fourth codebase would cost months and throw away tested governance.
2. **Several of its technology choices conflict with decisions AnalystOS has already proven**:
   LangGraph instead of Temporal, ten microservices instead of a modular monolith, Next.js instead of the
   existing React app, Kafka as a core dependency, and Cube/Wren as the semantic layer. Each would add
   operational surface without adding capability. See §3.
3. **It underspecifies the parts that make this product reduce analyst workload**: request intake
   and triage, recurring work automation, a reusable context and memory ledger for other apps and agents,
   data tools exposed as products, configurable models (OpenRouter, JEV, direct, local), and a cost model.
   See §4.

**Recommendation:** make this repository the product home for the next release. Seed it from
AnalystOS (keep its history), adopt the v1 doc's good additions (multi-tenancy, DB-native row-level security,
proactive triggers, conversational dashboard edits, production deployment), and add the missing
analyst-replacement layer. The specs in `docs/04-specs/` are written against that baseline.
The decision is ADR-001 in [`docs/03-decisions/adr-log.md`](../03-decisions/adr-log.md).

## 2. What v1 gets right (keep)

* The invariant: **models propose, deterministic services authorize, execute, validate, version and publish.**
* Separating execution modes (conversational, goal, proactive, assisted authoring) from authorization modes.
* Budgets as first-class goal constraints (steps, wall clock, scan bytes, cost) that stop with partial findings.
* The evidence object and provenance chain (goal → plan → step → query → snapshot → evidence → chart → approval → publication).
* Claim labels (observed, supported association, possible contributor, rejected, unknown) with no unwarranted causality.
* Wrapping Superset through an adapter instead of forking it.
* SQLGlot as a parser, **not** a security boundary. Database permissions remain the real control.
* The threat table and the acceptance scenarios AT-01…AT-12. These are carried forward unchanged into `docs/04-specs/`.

## 3. Decisions in v1 that should change

| # | v1 says | Problem | Change to |
|---|---|---|---|
| R1 | LangGraph is the primary orchestrator; Temporal "only if needed". | AnalystOS already runs durable runs on Temporal with a typed playbook engine (pause, resume, redirect, replan, approvals). Adding LangGraph gives two sources of truth for step state, which v1 itself warns against (§15.3). LangGraph checkpoints are not a substitute for durable timers, signals and schedules. | **Temporal + the existing playbook/plan engine** for every run (`profile=standard`), with an in-process local runner for `lite`. Agents are typed steps inside a playbook. No LangGraph. (ADR-002) |
| R2 | Ten services (api-gateway, workspace, context, autonomy, query, …). | One team, one database, one release cadence. Ten services means ten deploys, network failure modes and distributed transactions, and slower delivery. | **Modular monolith, one image, separately scaled process roles**: `api`, `worker:analysis`, `worker:query`, `worker:compute` (sandbox), `scheduler`, `web`. Module boundaries are enforced by import contracts so a module can be split out later. (ADR-003) |
| R3 | Kafka topics are core (§13.4). | Most deployments do not need Kafka. It adds an operations burden for something the transactional outbox already solves. | **Postgres outbox is the source of truth.** Kafka/Redpanda is an *optional* trigger source and event sink (profile `events`). |
| R4 | Evaluate Wren AI or Cube as the semantic layer. | AnalystOS has a deterministic semantic compiler (ADR-0019) with row filters and masks inside it, fan-out refusal, and `governed`/`ad_hoc` labels. An external semantic server becomes a second policy point. | **Keep the in-house compiler as the authority.** Import and export dbt MetricFlow / Cube YAML (and track the Open Semantic Interchange effort) for interop only. |
| R5 | Model gateway: "LiteLLM is one candidate". | AnalystOS already has a purpose → profile → model router with ladders, budgets, fallback, redaction, residency, prompt caching and JEV decisions. LiteLLM as a library would duplicate this. | **Keep the in-house router.** Add a generic `openai_compatible` provider type, which covers vLLM, Ollama, LM Studio, Groq, Together and a LiteLLM *proxy* if an enterprise already runs one. (ADR-005, spec S05) |
| R6 | Next.js web. | The existing React 18 + Vite + ECharts UI has 18 pages and tests. SSR adds nothing to an authenticated analytics workspace. | **Keep React + Vite.** Add the missing screens (Request Desk, Dashboard Builder chat, Autonomy Center budgets, Model admin). |
| R7 | `tenant_id` on tables "or equivalent RLS" is described, but isolation is via application filters. | Cross-tenant leakage is the most expensive failure. Application-only filters fail open on one missed `WHERE`. | **Tenant → workspace hierarchy, `tenant_id` on every row, Postgres RLS enforced with `SET LOCAL app.tenant_id`, and a separate DB role per process role.** AnalystOS has *no tenant concept today*, so this is the biggest structural gap. (ADR-009, spec S01) |
| R8 | Neo4j/DataHub for graph and lineage. | Another stateful service to back up and secure. | **Lineage graph in Postgres** (edges plus recursive CTEs, as Atlas already certified). Optional Apache AGE extension for Cypher. DataHub and OpenLineage are export targets, not dependencies. |
| R9 | Python analysis "sandboxed". | AnalystOS's own readiness doc says its rlimit/import sandbox *is not a security boundary*. | Compute workers run in **gVisor or Kata, or at minimum a `--network none --read-only --cap-drop ALL` non-root container** with a per-task ephemeral volume. This is a release gate, not a P2 item. |

## 4. What v1 is missing (add)

| # | Gap | Why it matters | Where specified |
|---|---|---|---|
| G1 | **Analyst Request Desk**: intake, triage, auto-answer, escalate to a human analyst with an SLA, and close the loop by turning the human answer into a certified example. | Analyst workload is a queue of requests. Without intake and triage you cannot measure or reduce it. | Product spec §4, S13 |
| G2 | **Recurring-work automation catalogue**: weekly KPI packs, month-end variance, reconciliations, report refresh, data-quality reviews, run as pinned playbooks. | Recurring work is the largest, most automatable share of analyst time. | Product spec §4, S09 |
| G3 | **Context Ledger**: a durable, versioned, access-controlled store of glossary, metrics, verified Q→SQL, findings, decisions and conversation summaries, readable by other apps and agents. | Context should outlive the conversation and the product. | S03, S04 |
| G4 | **OKF for export/import, MCP and REST for live use.** v1 does not say how other systems consume context. | Answers "OKF for export, or use by the agent?": both, for different jobs. | ADR-004, S03 |
| G5 | **Data tools as products**: register a certified metric, a saved query or a dataset as a typed callable tool, exposed through MCP to external agents. | This is how the platform becomes infrastructure for other agents rather than another chat UI. | S10 |
| G6 | **Model configuration as an admin feature**: providers (OpenRouter, TypeSafe JEV, Anthropic, OpenAI, Azure, Gemini/Vertex, Bedrock, OpenAI-compatible local), per-purpose routing, per-workspace allowlists, residency, budgets, presets and evaluation per model. | You asked for it. v1 has one sentence on it. | ADR-005, S05 |
| G7 | **Deterministic-first execution ladder** (cache → registry → rules → decision model → small LLM → large LLM) and token economy. | Cost and latency at scale; reproducibility. | S05 |
| G8 | **Verification that voids itself**: a fingerprint over query, data, semantics, method, model and policy. Any change marks dependent answers `VOID`. | Prevents stale published numbers, which is the trust killer in automated analytics. | S07 |
| G9 | **Feedback → learning loop**: corrections become certified examples, glossary updates and golden questions, all reviewed and versioned. | The only way accuracy improves without retraining models. | S04, S12 |
| G10 | **Cost attribution and chargeback** per tenant, workspace, user and run (LLM, warehouse, compute). | Needed to run this as a production service. | S12 |
| G11 | **Value metrics**: analyst hours saved, auto-resolution rate, time to answer, accepted-answer rate, confident-wrong rate. | "Replace or reduce analysts" must be measured, not asserted. | Product spec §6 |
| G12 | **Air-gapped / data-residency mode** (local models, decisions on rules, no external egress). | Enterprise and banking deployments. | S05, S12 |
| G13 | **Multi-source analysis**: staging snapshots to DuckDB, or Trino, with declared data-movement policy. | v1 defers it to P2, but real questions cross systems. AnalystOS is one source per run today. | S02 |

## 5. Functional requirement coverage (v1 FR → existing code)

Legend: **Built**: exists in AnalystOS with tests per its tracker. **Partial**: exists with a named gap. **Gap**: not present.

| FR | Requirement (short) | Status | Evidence / gap |
|---|---|---|---|
| FR-001 | Register source, secret ref, read-only role | Built | `connectors/`, `secret_ref: env:NAME`, reader role with `default_transaction_read_only` |
| FR-002 | Profile allowed columns only | Built | `services/crawler.py`, restricted columns denied |
| FR-003 | Select tenant/workspace/source conversationally | Partial | Workspace yes, **tenant no** |
| FR-004 | Versioned glossary, joins, metrics | Built | `semantic/`, `knowledge/` revisions, suggestions with review |
| FR-005 | Parameterized SQL from semantic model | Built | Semantic compiler (ADR-0019) |
| FR-006 | AST, budget and scope validation | Built | `gateway/validator`, `QueryGateway.execute` |
| FR-007 | Answer with refs, freshness and assumptions | Partial | `governed`/`ad_hoc` labels; freshness contract per source to complete |
| FR-008 | Typed chart specs | Built | ECharts specs, `chart_selection` purpose |
| FR-009 | Conversational chart/filter/dashboard edits | Partial | Run redirects and Ask follow-ups exist; **typed dashboard patch ops** missing |
| FR-010 | Persist goal/plan/step/evidence | Built | `AnalysisRun`, `RunTask`, `Hypothesis`, `Insight`, `VerificationRecord` |
| FR-011 | Safe retry/cancel/resume | Built | Temporal workflows, `IdempotencyRecord`, `DispatchOutbox` |
| FR-012 | Tenant isolation end to end | **Gap** | Workspace isolation only; no tenant, no DB RLS |
| FR-013 | Redaction, row/column security, output caps | Partial | Compiler masks and row caps; DB-native RLS passthrough per source to add |
| FR-014 | Approval before sharing | Built | `governance/approvals.py`, hash-bound |
| FR-015 | Superset publish adapter | Built | `publishing/` |
| FR-016 | Immutable audit | Partial | `AuditEvent`; append-only enforcement (trigger and role) to verify and add |
| FR-017 | Hypothesis trees | Built | `investigate.v1` playbook |
| FR-018 | Significance and quality checks | Built | SciPy/statsmodels, BH correction, numbers guard |
| FR-019 | Templates and workflow definitions | Built | Published/pinned playbooks (ADR-0021) |
| FR-020 | Schedule, Kafka, webhook and freshness triggers | Partial | Schedules and monitors built; **signed webhook and Kafka ingress** to add |
| FR-021 | Rate-limit, dedupe, debounce | Partial | Claim-then-execute; debounce, quiet hours and hysteresis to add |
| FR-022 | Email/Teams/webhook notifications | Partial | External delivery service exists; approval-gated channel adapters to finish |
| FR-023 | Typed tool contracts | Built | `ToolDefinition`, capability registry |
| FR-024 | Catalog lineage (DataHub) | Partial | OpenLineage export; DataHub emitter to add (optional) |
| FR-025 | Dashboard approvals, rollback, preview | Built | Preview and rollback endpoints |
| FR-026 | Corrections and certified SQL | Built | `VerifiedQuery`, `GoldenQuestion`, `Feedback` |
| FR-027 | LLM budget, timeout, fallback | Built | `llm/router.py`, `SpendCounter` |
| FR-028 | Run timeline, SQL trace, provenance | Built | Run view, `LineageEdge`, "Why this number?" |
| FR-029 | API-backed datasets | Partial | ServiceNow connector, HTTP tools; generic governed REST connector to add |
| FR-030 | Configurable PII policy | Partial | Owner tags authoritative; policy-as-config per asset class to add |
| FR-031 | Forecasting with backtesting | Partial | Governed ML module; forecast method pack to add |
| FR-032 | Suggested adjacent metrics | Partial | Semantic suggestions |
| FR-033 | DQ drift detection | Partial | Crawler drift, DQ gates |
| FR-034 | Federated joins | **Gap** | One source per run; Trino federation partial |
| FR-035 | User-defined skills with manifests | Partial | YAML agents and capabilities; review/signing flow to add |
| FR-036 | Story/report builder | Partial | Reports (PDF has a non-Latin font gap) |

**Totals:** 17 Built, 17 Partial, 2 Gap. The two gaps (tenancy, federation) plus production hardening are the critical path.

## 6. Production-readiness gaps that no amount of feature work closes

From AnalystOS `docs/60-delivery/03-release-readiness.md`. These are the real "full production" blockers:

1. SSO against the customer IdP (OIDC is proven only against a local fake IdP).
2. Independent security review and pen test.
3. Live certification for each pilot connector (SQL Server, Snowflake, BigQuery, Databricks and others are catalog-tested only).
4. Measured SLOs and load tests.
5. Alerting and dashboards for the platform itself.
6. Hard sandbox isolation for Python compute.
7. Superset per-workspace isolation (today it uses one shared reader identity).
8. PITR, failover and automated secret rotation.
9. JEV has never run live because it has no price entry (tracker N-13). Every JEV purpose falls back to rules.
10. A real analyst pilot with measured outcomes (tracker VALUE-05). No workload reduction is claimed until this exists.

## 7. Answers to the questions in the request

| Question | Answer |
|---|---|
| Use OKF to export context, or let the agent use it? | **Both, for different jobs.** OKF (pinned v0.2 profile, already implemented) is the *interchange and review* format: Git-diffable knowledge packs, export to other apps, import from Atlas or partners. At runtime agents never read raw OKF files. They read through the **Context Service** (scoped, access-checked, budgeted, injection-screened, receipted), exposed in-process, over REST and over **MCP** to external agents. OKF bundles are snapshots and never an authorization boundary. See ADR-004 and S03. |
| OpenRouter, JEV and other models, configurable? | Yes. OpenRouter is the default gateway. JEV (TypeSafe decision model through the OpenRouter Decisions API) handles typed choices that may escalate risk but never grant it. Direct Anthropic, OpenAI, Azure OpenAI, Gemini/Vertex and Bedrock, plus any `openai_compatible` endpoint (vLLM, Ollama, LiteLLM proxy) for local or air-gapped use. Admins configure purpose → profile → models per workspace in the UI, versioned. See S05. |
| Reuse existing code or develop new? | Reuse AnalystOS as the base; port the remaining Atlas capabilities (catalog depth, enterprise secret managers, MCP gateway screen, provider adapters) under the base's contracts. Build new: tenancy and RLS, Request Desk, Context Ledger APIs, data-tool products, conversational dashboard patches, webhook/Kafka triggers, production operations. |
| Use Apache open-source projects? | Adopt Apache **Superset** (BI), Apache **Arrow** (result handles as Arrow IPC/Parquet, ADBC drivers where available), optional Apache **AGE** (graph queries in Postgres), optional Apache **Kafka** (triggers and events). Integrate with, but do not embed, Apache **Ranger** where an enterprise already uses it for policy. Do **not** adopt Airflow (Temporal covers it) or Calcite (SQLGlot covers it). |
| Can it replace or reduce analysts? | It can absorb the *repeatable* share: certified-metric questions, recurring packs, first-pass root cause, monitoring and dashboard drafting. Humans keep metric ownership, approvals, novel questions and decisions. The product measures this through the Request Desk (G1) and value metrics (G11). It is not claimed until the pilot measures it. |

## 8. Open-source reference assessment (updates v1 §21, adds DB-GPT)

Rule: a reference repo may contribute **patterns, datasets, prompts and isolated pure functions**
(license permitting, with attribution in `THIRD_PARTY.md`). It never contributes a second runtime path to data,
models or publishing. Everything ported runs behind our gateway, router, approvals and audit.

| Project | License | What to take | What not to take | Decision |
|---|---|---|---|---|
| **DB-GPT** (`eosphoros-ai/DB-GPT`) | MIT | (1) **DB-GPT-Hub Text2SQL** fine-tuning recipes and Spider/BIRD-style evaluation harness, used to fine-tune and benchmark a *local* SQL model for the air-gapped profile and to extend our golden-question suite. (2) **AWEL**-style DAG operators as a reference for the playbook step library (we already have typed playbooks). (3) **SMMF** multi-model deployment notes for vLLM and llama.cpp, used for the `openai_compatible` local provider runbook. (4) Database profiling report and financial-report templates as report-pack ideas. (5) Document parsing pipeline ideas for knowledge ingestion. | Its runtime, agents, web app or model manager. Its public page states no RLS, hash-bound approvals, audit or tenant isolation, so running it beside us would create an ungoverned path to data. | **Reference / selective reuse** |
| Apache Superset | Apache-2.0 | BI publishing, embedding, guest tokens | Forking | **Adopt** (adapter) |
| Apache Arrow / ADBC | Apache-2.0 | Result handles, columnar transfer, ADBC drivers (Snowflake, Postgres, BigQuery…) | — | **Adopt** |
| Apache AGE | Apache-2.0 | Optional Cypher over lineage and knowledge graph inside Postgres | Making it required | **Optional** |
| Apache Kafka / Redpanda | Apache-2.0 / BSL | Optional trigger ingress and event sink | Core dependency | **Optional** |
| Apache Ranger | Apache-2.0 | Policy *import* where the enterprise already uses it | Second policy engine | **Integrate if present** |
| Temporal | MIT | Durable runs, schedules, signals | — | **Adopt** (already in use) |
| SQLGlot | MIT | Parse, normalize, transpile | Security boundary | **Adopt** (in use) |
| DuckDB | MIT | In-process analysis over result handles; multi-source joins over staged snapshots | Using it to bypass the gateway | **Adopt** (in use) |
| pgvector | PostgreSQL | Embeddings in the system of record | — | **Adopt** (in use) |
| Microsoft Data Formulator | MIT | Exploration-thread UX: branching, "derive from this chart" | Code | **UX reference** |
| Wren AI / Cube | AGPL-3.0 (Wren engine, verify) / Apache-2.0 (Cube core) | Semantic YAML import/export shapes | Runtime semantic authority (see R4) | **Interop only** |
| DataHub / OpenLineage | Apache-2.0 | Lineage export targets | Dependency | **Optional export** |
| LangGraph | MIT | Nothing at runtime (see R1). Prompt and agent patterns only. | Orchestrator | **Do not adopt** |

Re-check every license and version at adoption time. DB-GPT does not state a release version on its
repository page, so pin a commit SHA for anything ported.

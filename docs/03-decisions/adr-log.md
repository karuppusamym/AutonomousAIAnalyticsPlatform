# Architecture decision log

Status values: **Proposed**, **Accepted**, **Superseded**. The owner delegated these decisions on 2026-10-27 ("you make decisions, best options"); all entries below are accepted on that basis.
ADRs inherited from AnalystOS (`AIAnalystAgentOS/docs/10-architecture/adr/0001…0025`) remain in force
unless an entry below supersedes them. Each entry gives the decision, why, and what it costs.

---

## ADR-001 Build on the AnalystOS codebase, not greenfield — Accepted (2026-10-27)

**Context.** The v1 design describes a system that AnalystOS already implements at ~80% (see the
[design review](../00-review/01-design-review-of-v1.md) §5). AnalystOS has 309 test files, 25 ADRs,
hash-bound approvals, a governed SQL gateway and Temporal runs. Atlas is already designated a donor.

**Decision.** Seed this repository from AnalystOS with its git history (`git subtree` or a fresh
clone with a remote rename), rename the product surface, and continue development here. AnalystOS and
Atlas become read-only donors once their parity checklists are signed.

**Why.** It is the only option that meets "fast and production-capable". Rebuilding the gateway,
verification and approval logic alone would take longer than closing every production gap in §6 of the review.

**Costs.** It inherits AnalystOS conventions (Python package `analystos`, Vite UI, a large module
count) and its prototype-level readiness. The Python package can be renamed later behind a
compatibility shim. Do not rename during the hardening phase.

**Alternatives rejected.** (a) Greenfield per v1: 4–6 months to parity with no new value. (b) Keep
developing in AnalystOS and leave this folder for docs only: workable, but splits specs from code.
This is acceptable if the owner prefers it; the specs apply unchanged.

---

## ADR-002 Temporal + typed playbooks; no LangGraph — Accepted (2026-10-27) (supersedes v1 ADR-003)

**Decision.** Every run (conversational escalation, goal, proactive) is a Temporal workflow that
interprets a versioned, published playbook. Agents are typed steps. The `lite` profile uses the
in-process local runner with the same step semantics.

**Why.** One source of truth for step state. Durable timers, signals (pause, resume, redirect, approve)
and schedules come built in. It is already implemented and tested (AnalystOS ADR-0005, ADR-0011).
LangGraph would add a second checkpoint store and does not supply durable scheduling.

**Costs.** Temporal is an operational dependency in `standard` and above. Mitigation: the lite profile
runs without it, and Temporal Cloud is an option.

---

## ADR-003 Modular monolith with process roles — Accepted (inherits AnalystOS ADR-0001)

**Decision.** One image, roles `api`, `worker:analysis`, `worker:query`, `worker:compute`,
`scheduler`, `web`. Package boundaries are enforced with `import-linter` contracts. `QueryGateway`,
`BIPublisher`, `ContextService`, `ModelRouter` and `ToolExecutor` are protocol-shaped so any of them can
become a service without callers changing.

**Why.** Delivery speed, one migration stream and one policy model. Each role scales independently on Kubernetes.

---

## ADR-004 OKF is the interchange format; the Context Service is the runtime interface — Accepted (2026-10-27)

**Context.** The question was whether to use OKF to export context, or let agents use it directly.

**Decision.**
* **Runtime (agents in this platform and external agents):** read context only through the
  **Context Service**: `compile(purpose, scope, budget) → ContextBundle + receipt`. This is exposed in-process,
  over REST (`/v2/context/*`) and over MCP (`context.search`, `context.get`, `context.compile`). Every read
  is tenant- and workspace-scoped, access-checked at read time, injection-screened and receipted.
* **Interchange and review:** export and import **OKF v0.2 knowledge packs** (pinned profile, already
  implemented in `knowledge/okf.py`) for Git review, migration between environments, sharing
  with partner systems (Atlas and others), and offline archives. Lineage exports as **OpenLineage**,
  data contracts as **ODCS**, and verified computations as attested-computation records.
* **System of record:** Postgres (`knowledge_*`, `semantic_*`, `context_ledger_*` tables). OKF files are projections.

**Why.** Files cannot enforce per-reader access, freshness or budgets. A live service cannot be
reviewed in a pull request or carried across air gaps. Each job gets the right tool.

**Costs.** Two representations must stay in sync. Mitigation: OKF export is deterministic and
round-trip tested (`export → import → export` gives a byte-identical pack).

---

## ADR-005 In-house model router; providers are configuration — Accepted (2026-10-27) (refines AnalystOS ADR-0006/0012/0015)

**Decision.** Keep `llm/router.py` (purpose → profile → models, ladders, budgets, fallback,
residency, redaction, prompt caching). Supported provider types: `openrouter` (default), `decision`
(TypeSafe JEV through the OpenRouter Decisions API), `anthropic`, `openai`, `azure_openai`, `gemini`/`vertex`,
`bedrock`, and `openai_compatible` (vLLM, Ollama, LM Studio, Groq, Together, LiteLLM proxy). Model
configuration is a versioned platform setting edited in the Admin UI, with per-tenant and per-workspace narrowing.

**Why.** Routing is coupled to governance (allowlists, residency, budgets, the escalation ladder). A
generic proxy cannot enforce workspace policy. `openai_compatible` gives the breadth of LiteLLM without the dependency.

**Invariant.** No model output grants permission, authorizes a query or publishes. JEV may rank,
choose among valid options, or escalate risk. It never grants.

---

## ADR-006 Superset through an adapter, per-tenant isolation — Accepted (v1 ADR-001)

Adds: one Superset database connection per tenant (a separate reader identity), datasets created per
workspace, and Superset roles mapped from platform roles through the publisher. Embedded dashboards
use guest tokens minted by the platform after it rechecks authorization.

---

## ADR-007 MCP is the agent interoperability protocol — Accepted (2026-10-27)

**Decision.** The platform is an **MCP server** (tools: `ask`, `investigate`, `get_finding_evidence`,
`validate_sql`, `query.<certified_query>`, `metric.<name>`, `context.*`, `conversation.*`), authenticated
per client with scoped grants. It is also an **MCP client** for admitted external servers. Imported tools
default to `side_effect: write_external` until an admin classifies them. REST stays the primary API for
first-party UI and apps.

**Why.** MCP is the de facto protocol other agents use. Exposing certified data tools through it turns the
platform into shared analytics infrastructure.

---

## ADR-008 Postgres is the system of record; no separate vector or graph database by default — Accepted

Postgres 16 with pgvector (embeddings, HNSW), RLS, the outbox and lineage edges. Optional extensions: Apache AGE
(Cypher over lineage). Neo4j, OpenSearch and Kafka are optional projections, never authoritative.

---

## ADR-009 Tenant → Workspace hierarchy with Postgres RLS — Accepted (2026-10-27) (new; closes FR-012)

**Decision.**
* Add `tenant` above `workspace`. Every tenant-owned table gets `tenant_id NOT NULL`. Migrations backfill
  existing rows to a `default` tenant.
* Enable `ROW LEVEL SECURITY` with `FORCE` on every tenant-owned table. Policy:
  `tenant_id = current_setting('app.tenant_id')::uuid`.
* The application sets `SET LOCAL app.tenant_id` (and `app.workspace_ids`) per transaction from the
  server-resolved principal. Connection pool resets happen on checkout.
* DB roles: `app_rw` (subject to RLS), `app_migrator` (owner, used only by the migrate job), `app_audit_writer`
  (INSERT-only on `audit_event`), `query_reader` per source (cannot reach the control plane).
* Object storage paths are prefixed `tenant/{tenant_id}/…` with per-tenant KMS keys when enabled.
* Caches are keyed by tenant, principal scope hash, policy version and data snapshot.
* High-sensitivity tenants can be placed in a dedicated database (same schema) by routing configuration.

**Why.** Application filters fail open with one missed predicate. RLS fails closed.

**Costs.** Every query path needs the session variable. Background jobs must carry the tenant explicitly.
A CI test enumerates tables and fails if any tenant-owned table lacks RLS.

---

## ADR-010 Results and evidence as Arrow/Parquet handles in object storage — Accepted (2026-10-27)

Query results above a small inline threshold are written as Parquet (Arrow IPC for streaming) under
`tenant/{t}/results/{query_execution_id}`, encrypted and TTL-governed. Steps pass handles, not rows.
DuckDB reads handles for in-process analysis and multi-source joins (S02 §5).

---

## ADR-011 Analyst Request Desk as the unit of value — Accepted (2026-10-27) (new)

Every business question, whether from chat, API, MCP, email or a ticket, becomes a `request` with a lifecycle
(`new → triaged → auto_answered | in_investigation | escalated → answered → accepted | reopened`).
Value metrics (auto-resolution rate, time to answer, analyst minutes, accepted rate, confident-wrong rate)
are computed from requests. A human answer to an escalated request is offered for certification as a
verified example, which closes the learning loop. See S13.

---

## ADR-012 Compute isolation is a release gate — Accepted (2026-10-27) (new)

Python and statistics execution runs only in `worker:compute` pods with gVisor (runsc) or Kata, a
read-only root, no network, non-root, seccomp, CPU/memory/time limits and an ephemeral volume. Dev
fallback: Docker `--network none --read-only --cap-drop ALL`. The in-process rlimit sandbox is
allowed only in `lite` with synthetic data.

---

## ADR-013 Language and technology stack: Python + TypeScript, not Java — Accepted (2026-10-27)

**Question.** Should the backend be Python or Java?

**Decision.** **Python** for every backend role (api, workers, scheduler, compute) and **TypeScript** for the web.
No Java. A role may be rewritten in Go only if measurement shows Python is the bottleneck. No such role exists today.

| Criterion | Python | Java | Why it decides |
|---|---|---|---|
| Analytics and ML libraries | polars, duckdb, pyarrow, scipy, statsmodels, scikit-learn: native, first class | thin or JNI wrappers; DuckDB/Arrow are usable but stats/ML is weak | Most of the product's logic is statistics, SQL analysis and ML |
| SQL tooling | sqlglot (parse, transpile, 25+ dialects) | Calcite (powerful, heavier, fewer dialects maintained for analytics) | The gateway validator is built on sqlglot |
| LLM, MCP, Temporal SDKs | first-party for every provider, the MCP SDK and the Temporal Python SDK | available but secondary for most LLM providers | Model routing is core |
| Apache Superset | Python | — | Same language as the BI layer |
| Existing asset | ~115k lines, 309 test files, 25 ADRs | 0 | Speed to production |
| Raw throughput / GC | slower interpreter, GIL | faster JIT, mature GC | **Not decisive.** Hot paths run inside databases, DuckDB and Arrow (C++), and model calls dominate latency. Python orchestrates and never loops over rows (ADR-014). |

**Pinned stack.**

| Layer | Choice |
|---|---|
| Runtime | Python 3.11 now → **3.12** after Wave 1 (CI tests both during the move); `uv` for envs and locks |
| API | FastAPI + Pydantic v2 (contracts), SSE for streams, OpenAPI 3.1 → generated TS client |
| Persistence | PostgreSQL 16 (RLS, pgvector, outbox), SQLAlchemy 2 + Alembic, PgBouncer (transaction pooling) |
| Workflow | Temporal (standard and above); in-process runner (lite) |
| Data plane | sqlglot, ADBC/SQLAlchemy drivers, Apache Arrow, Parquet, DuckDB, Polars |
| Analytics | SciPy, statsmodels, scikit-learn |
| Models | in-house router: OpenRouter, TypeSafe JEV, Anthropic, OpenAI, Azure, Gemini/Vertex, Bedrock, OpenAI-compatible |
| BI | Apache Superset (adapter), ECharts in-app |
| Web | React 18 + Vite + TypeScript + ECharts, generated OpenAPI client, Vitest |
| Ops | Docker, Helm (Kubernetes/OpenShift), KEDA, gVisor, External Secrets, OpenTelemetry, Prometheus/Grafana |
| Quality | ruff, mypy (strict on `contracts`, `gateway`, `governance`, `security`, `llm`), import-linter, pytest, gitleaks |

---

## ADR-014 Memory management, scalability and maintainability rules — Accepted (2026-10-27)

**Memory (process).**
1. Python never holds result rows beyond the inline threshold (256 rows / 64 KB). Larger results stream as Arrow
   record batches to Parquet handles (ADR-010) and are processed with DuckDB or Polars lazily, out of core.
2. DuckDB per task: `SET memory_limit` to 60% of the pod limit and `temp_directory` on the ephemeral volume, so it spills instead of OOM-killing.
3. Workers recycle: Temporal `max_concurrent_activities` per role, and a restart after N tasks or when RSS exceeds 80% of the limit
   (liveness probe). Kubernetes requests and limits are set per role in Helm values.
4. SQLAlchemy sessions are per unit of work, never long-lived. Large reads use `yield_per`. Pool sizes are set per role behind
   PgBouncer. Tenant binding uses `SET LOCAL` so it is safe under transaction pooling.
5. Every cache is bounded (size-capped LRU in process; Redis `maxmemory` with `allkeys-lru`) and keyed by tenant, scope and policy version.

**Memory (agent context).** Token budgets per purpose. The context compiler refuses rather than truncating required
definitions. Conversation memory tiers T0–T3 (S04). Raw rows are never written to long-term memory or embeddings.

**Scalability.** Stateless `api`/`web` (HPA). Workers scale on queue depth (KEDA). The query pool scales per source with semaphores.
Postgres reads that do not need fresh state go to a replica. Large payloads live in the object store, and the DB holds only references.

**Maintainability.** One package with enforced module boundaries (import-linter), contracts first (Pydantic → JSON Schema/OpenAPI),
one migration stream, features as capability packs and profiles (lite → standard → ha) so a deployment runs only what it needs,
ADR for every cross-cutting change, and the tracker plus evidence as the single status authority.

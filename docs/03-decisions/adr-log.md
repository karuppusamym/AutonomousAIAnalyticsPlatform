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

## ADR-002 Temporal + typed playbooks; no LangGraph — Accepted (2026-10-27), superseded in part by ADR-015 (2026-10-29) (supersedes v1 ADR-003)

**Decision.** Every run (conversational escalation, goal, proactive) is a Temporal workflow that
interprets a versioned, published playbook. Agents are typed steps. The `lite` profile uses the
in-process local runner with the same step semantics.

**Why.** One source of truth for step state. Durable timers, signals (pause, resume, redirect, approve)
and schedules come built in. It is already implemented and tested (AnalystOS ADR-0005, ADR-0011).
LangGraph would add a second checkpoint store and does not supply durable scheduling.

**Costs.** Temporal is an operational dependency in `standard` and above. Mitigation: the lite profile
runs without it, and Temporal Cloud is an option.

**Superseded in part.** [ADR-015](#adr-015-langgraph-as-an-optional-third-orchestrator-backend--accepted-2026-10-29-supersedes-adr-002-in-part)
(Accepted 2026-10-29) supersedes this entry's "no LangGraph" clause. Temporal stays the default for `standard` and above.

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

---

## ADR-015 LangGraph as an optional third orchestrator backend — Accepted (2026-10-29) (supersedes ADR-002 in part)

**Status.** Proposed 2026-10-27; accepted by the owner 2026-10-29. Build tracked as ORC-T1..T7. ADR-002 is
*superseded in part*: its decision stands except the clause "no LangGraph".

**Context.** The owner asked for the run orchestrator to be a deployment choice that includes LangGraph,
next to Temporal and the local runner. ADR-002 and spec v4 §17 (open question 5) declined LangGraph for two
reasons: a second checkpoint store, and no durable scheduling. Spec v4 said to revisit only if a flow
could not be written as a playbook. This request is different. Nobody has found a flow that playbooks
cannot express. It asks for an orchestrator choice: for teams that know LangGraph, for customers that
standardise on it, and as a place for LangGraph-style graphs inside agent steps later.

The code already has the needed seam. `runtime/engine.py` is orchestrator-agnostic: `plan_run`,
`get_state`, `execute_task`, `finish_run`, `apply_replan`, plus claims and orphan release. All state
(tasks, claims, budgets, approvals) lives in Postgres. Two drivers run the same decision loop over it:

| Backend | Where the loop lives | Waits | Durability |
|---|---|---|---|
| `temporal` | `workflows/analysis_workflow.py::AnalysisWorkflow` | `nudge` signal + timer (budget-capped) | Temporal history; `continue_as_new` |
| `local` | `workflows/orchestrator.py::LocalRuntime.drive` | park (no thread held), re-driven by `signal_run` / 60 s sweep | Postgres task state; `resume()` at API start |

Schedules and monitors do not depend on the orchestrator. They run in `analystos scheduler` (or in
process under lite) with claim-then-execute. So ADR-002's "no durable scheduling" objection does not
apply to an orchestrator *backend*. It applies only if LangGraph were to replace Temporal.

**Decision.**

1. `ANALYSTOS_ORCHESTRATOR` becomes `Literal["temporal", "local", "langgraph"]`. The profile defaults
   don't change: lite → `local`, standard/scale → `temporal`. `langgraph` is never a default. Selecting
   it requires the new `langgraph` extra, and startup fails with a clear error if the extra is missing,
   the same way the `temporal` extra works today.
2. **Postgres stays the only source of run state.** The LangGraph graph is a *driver*, like
   `LocalRuntime.drive`. It holds no state the engine does not hold. It is compiled **without a
   persistent checkpointer**. A wait for a person or a pause ends the invocation (it *parks*, as in
   ADR-0025), and `signal_run`, the sweep or a restart starts a fresh invocation from `get_state`. This
   removes ADR-002's second-checkpoint-store objection.
3. **Playbooks remain the only flow definition.** The graph is fixed in code and mirrors the engine
   loop. It is not generated from playbooks and models cannot edit it. Its edges are chosen by
   deterministic routing on `get_state`, never by a model (CLAUDE.md rule 3). No LangChain chat
   model, tool, agent or retriever is used. Agents keep running through `agents/dispatch.py`, and SQL
   keeps going through `QueryGateway.execute`.
4. **Scope:** analysis runs (goals, conversational escalations, proactive runs). Crawls, recipe and ML
   compute, and query transport behave as under `local`: in process or via the subprocess pool
   transport (`isolated_transport=auto` resolves to `subprocess`). Schedules and monitors are
   unchanged.
5. **Hosting (phase 1):** the API process, reusing `LocalRuntime`'s machinery (driver registry,
   nudge-while-parking guard, bounded task pool `ANALYSTOS_LOCAL_WORKERS`, resume, sweeper). It has the
   same limit as `local`: one API process drives runs. A dedicated driver process with Postgres run
   leases, safe for several API replicas, is phase 2 and deferred until a deployment needs it.
   standard/scale deployments that need HA keep `temporal`.

**Design.**

```
            ┌──────────┐
 START ───▶ │  plan    │  engine.plan_run (idempotent)
            └────┬─────┘
                 ▼
            ┌──────────┐   next_action(state)  (shared pure function)
      ┌───▶ │  state   │──────────────┬───────────────┬──────────────┬─────────────┐
      │     └──────────┘              ▼               ▼              ▼             ▼
      │                          ┌─────────┐    ┌──────────┐    ┌─────────┐   ┌────────┐
      │                          │ finish  │    │  replan  │    │ execute │   │  park  │
      │                          │ outcome │    │ plan_run │    │ (Send   │   │ return │
      │                          └────┬────┘    └────┬─────┘    │ per key)│   │ WAIT/  │
      │                               ▼              │          └────┬────┘   │ PAUSED │
      │                              END             │               │        └───┬────┘
      └──────────────────────────────────────────────┴───────────────┘           END
         (+ `idle` node: sleep ≤ poll, capped by remaining wall-clock budget, then back to `state`)
```

* **One decision function.** `runtime/loop.py::next_action(state) -> Action` (finish with outcome,
  replan, execute keys, park, idle) is extracted from the two existing loops. `local` and `langgraph` call
  it. The Temporal workflow keeps its inline copy until a `workflow.patched` migration is worth doing,
  because changing workflow code risks replay. A table test asserts the three agree on every
  `get_state` shape, so a new state field cannot be handled in one driver and missed in another.
* **Nodes** are thin wrappers over engine calls. Each runs as the run's tenant (`as_owner_of
  ("analysis_run", run_id)`) and does not rely on LangGraph's executor carrying context variables
  (ADR-009, rule 5).
* **Parallel tasks.** `Send` fans out one `execute` per ready key. `execute` submits to the shared
  bounded pool and waits, so the global bound is unchanged. A superstep waits for all its tasks, like
  Temporal's `asyncio.gather`. Retries use the engine's claim/attempt rules (`_execute_with_retries`,
  `_is_last_attempt`), not a LangGraph `RetryPolicy`, so attempts count the same way under every backend.
* **Long runs.** Each invocation is bounded by `recursion_limit`. On reaching it, the driver returns
  and re-invokes, which is the equivalent of `continue_as_new`.
* **Approvals.** A resumed invocation never carries a decision. Approvals stay hash-bound and are
  checked with `verify_for_execution` inside the step (rule 6). This is the main reason
  `interrupt()`/`Command(resume=…)` are not used: a resume value must not be treated as an approval.
* **Egress guard.** LangChain's tracing client (LangSmith) can send run state to a hosted service.
  Startup refuses the `langgraph` backend while `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` is true,
  unless an operator opts in with `ANALYSTOS_LANGGRAPH_ALLOW_TRACING=true`.
* **Boundaries.** An import-linter contract allows `langgraph` / `langchain_*` imports only in
  `workflows/langgraph_backend.py`. `api/app.py` health (`worker`, `temporal` checks), `core/profiles.py`
  and `workers/dispatch.py` read a backend capability (`durable`, `needs_worker`) instead of
  `orchestrator == "temporal"`. About 8 call sites change.
* **Versions.** The `langgraph` extra pins a 1.x minor range. The node and `Send` APIs used here must be
  checked against the pinned release when it is implemented.

**Why (if accepted).**
* It gives the owner the choice they asked for, and needs no new service.
* Keeping LangGraph as a stateless driver over the engine means the decisions that keep this platform
  governed stay where they are: Postgres state, hash-bound approvals, the gateway, and playbooks.
* The shared `next_action` and the conformance suite reduce drift between the existing two drivers,
  which helps even if LangGraph is later removed.

**Costs.**
* **Limited functional gain.** Inside the platform, `langgraph` adds no behaviour that `local` lacks.
  Its value is choice, familiarity, the graph view, and a home for agent sub-graphs later. It is not
  more durable and it is not HA. Phase 1 has the same single-process limit as lite.
* **Third driver to maintain.** Every change to the `get_state` contract (S06 budgets, replan codes)
  must reach three drivers. Mitigation: `next_action` + the parity table test. The test matrix grows
  by one backend, about 1–2 minutes of unit time.
* **Dependencies.** `langgraph` pulls `langchain-core` and `langsmith` (MIT). The framework has had
  breaking releases (0.x → 1.0). Budget about 0.5 day per quarter for upgrades. The extra is optional, so
  core and lite images do not carry it.
* **Egress risk** from tracing defaults, mitigated by the startup guard above.
* **ADR churn.** This reverses an entry accepted the same day. If accepted, reconcile spec v4 §(open
  question 5), the 2026-09-26 comparison review row 33, and ADR-0011's note.

**Effort estimate (phase 1, one engineer).**

| ID | Work | Estimate |
|---|---|---|
| ORC-T1 | Orchestrator seam: `workflows/backends.py` protocol (`start_run`, `signal_run`, `resume`, `health`, capabilities); `runtime/loop.py::next_action` used by `local`; replace the ~8 `orchestrator == "temporal"` checks; `orchestrator` becomes a `Literal` | 2 d |
| ORC-T2 | Backend conformance suite (unit, parameterised over `local` and `langgraph`; `temporal` via its test environment in integration): done/fail/cancel/limited/expired, replan, approval park + signal, pause/resume, restart with orphaned claims, parallel bound, nudge-while-parking race, tenant bound in every node; `next_action` parity table vs the Temporal workflow | 2.5 d |
| ORC-T3 | `workflows/langgraph_backend.py`: graph, nodes, `Send` fan-out on the shared pool, recursion bound + re-invoke, park semantics, hosted on `LocalRuntime` | 2.5 d |
| ORC-T4 | Packaging and guards: `langgraph` extra, import-linter contract, tracing guard, startup validation, `/health` and profile summary, slim-install test | 1 d |
| ORC-T5 | Docs: runbook 04 "choosing an orchestrator", README, CLAUDE.md map, `.env.example` | 0.5 d |
| ORC-T6 | Live evidence: `scripts/e2e_demo.py` with `ANALYSTOS_ORCHESTRATOR=langgraph` (target 26/26), restart-mid-run drill, `governance-reviewer` pass | 1 d |
| | **Phase 1 total** | **≈ 9.5 d (2 weeks)** |
| ORC-T7 | (Deferred) Dedicated driver process with Postgres run leases (`SKIP LOCKED`, heartbeat, lease expiry) so several API replicas are safe | 3–4 d |

ORC-T1 and ORC-T2 are useful even if LangGraph is rejected later, because they harden the two drivers
that already exist.

**Alternatives considered.**
* **(a) Keep ADR-002 (two backends) and document the switch.** About 0.5 d, no new dependency. This is
  the right choice if no customer or team needs LangGraph.
* **(b) LangGraph only inside a step**, as the implementation of one agent, behind the generic agent's
  propose → validate → execute path. It needs no orchestration change and fits ADR-0011, which already
  allows Pydantic AI inside a step. Choose this if the interest is agent reasoning graphs rather than
  run orchestration.
* **(c) LangGraph with a Postgres checkpointer and `interrupt()` as the source of truth.** Rejected: two
  state stores that can disagree, a resume value next to hash-bound approvals, plus new checkpoint tables
  needing RLS, retention and a migration. That is about +3 d and a lasting audit question.
* **(d) LangGraph Platform / hosted deployment for queues and cron.** Rejected: a commercial hosted
  dependency, data leaving the deployment, and a second scheduler next to `analystos scheduler`.
* **(e) Replace Temporal with LangGraph.** Rejected: standard/scale would lose durable timers, signals,
  task-queue pools and worker isolation (ADR-0022, ADR-012), and the S02 `worker:query` transport.

**Recommendation to the owner.** Accept only if there is a concrete reason, such as a customer standard,
team skills, or a planned agent sub-graph. Otherwise choose (a), or (b) if the interest is agent
internals. In every case, ORC-T1 and ORC-T2 are worth doing for the two existing backends.

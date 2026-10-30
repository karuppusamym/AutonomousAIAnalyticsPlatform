# Autonomous AI Analytics Platform

A governed analytics workforce: it answers certified questions instantly, investigates open ones autonomously
within budgets, publishes verified dashboards after approval, monitors KPIs proactively, and escalates to human
analysts only what it cannot answer with evidence. Every answer, definition and conversation becomes versioned
context that other apps and agents can query (REST, MCP, OKF export).

> Models propose. Deterministic code authorizes, computes, verifies and publishes. People approve side effects.

**Status (2026-10-30):** specs v2 accepted (ADR-001…016). This repository was seeded from AnalystOS
(`codex/investigator-demo`, full history) and is now the product home. AnalystOS and Atlas are donors.
The baseline's own README, with its feature list and status, is at [docs/00-intent/00-analystos-readme.md](docs/00-intent/00-analystos-readme.md).

## Read in this order

| # | Document | What it gives you |
|---|---|---|
| 1 | [Design review of v1](docs/00-review/01-design-review-of-v1.md) | What the v1 `.docx` gets right and wrong, what's missing, FR coverage against existing code, OSS assessment (incl. DB-GPT), direct answers to the open questions |
| 2 | [Product spec v2](docs/01-product/01-product-spec-v2.md) | Modes, analyst work model (R1–R9), FR-037…058, value metrics, NFRs, AT-13…24 |
| 3 | [Architecture](docs/02-architecture/01-architecture.md) | Planes, process roles, profiles, key flows, module map |
| 4 | [Decision log](docs/03-decisions/adr-log.md) | ADR-001…016 |
| 5 | [Development specs S01–S13](docs/04-specs/) | Per-module build specs: baseline to reuse, what to build, interfaces, invariants, acceptance, tasks |
| 6 | [Delivery plan](docs/05-delivery/01-delivery-plan.md) | ~180 engineer-days, 5 waves, ~12 weeks to production pilot, risks, traceability |

## Development specs

| Spec | Area | Pri |
|---|---|---|
| [S01](docs/04-specs/S01-tenancy-identity-policy.md) | Tenancy, RLS, identity, ABAC, audit | P0 |
| [S02](docs/04-specs/S02-connectors-query-gateway.md) | Connectors, query gateway, result handles, multi-source | P0/P1 |
| [S03](docs/04-specs/S03-context-service-and-knowledge.md) | Context Service, Context Ledger, OKF interchange | P0/P1 |
| [S04](docs/04-specs/S04-conversations-and-memory.md) | Conversations, memory tiers, cross-app reads | P0 |
| [S05](docs/04-specs/S05-model-gateway.md) | Model gateway: OpenRouter, JEV, direct, local; admin config | P0 |
| [S06](docs/04-specs/S06-autonomy-engine.md) | Goals, playbooks, runs, budgets | P0 |
| [S07](docs/04-specs/S07-analysis-verification-evidence.md) | Analysis, verification, evidence, compute isolation | P0/P1 |
| [S08](docs/04-specs/S08-dashboards-publishing.md) | Dashboards, typed conversational edits, Superset | P0/P1 |
| [S09](docs/04-specs/S09-monitoring-triggers-recurring.md) | Monitors, webhooks/Kafka, recurring packs, channels | P1 |
| [S10](docs/04-specs/S10-tool-agent-registry-mcp.md) | Tool/agent/data-product registry, MCP in/out | P1 |
| [S11](docs/04-specs/S11-web-ux.md) | Web UX | P0/P1 |
| [S12](docs/04-specs/S12-platform-operations.md) | Deploy, observability, eval gates, cost, hardening | P0 |
| [S13](docs/04-specs/S13-analyst-request-desk.md) | Analyst Request Desk and value metrics | P0 |

## Stack (target)

Python 3.11 · FastAPI · SQLAlchemy/Alembic · PostgreSQL 16 (RLS, pgvector, outbox) · Temporal · SQLGlot · DuckDB ·
Apache Arrow/Parquet · SciPy/statsmodels/scikit-learn · React 18 + Vite + ECharts · Apache Superset ·
OpenRouter + TypeSafe JEV + direct/local providers · Kubernetes/OpenShift (Helm, gVisor, KEDA) · OpenTelemetry.

## Run it

```bash
cp .env.example .env              # optional: OPENROUTER_API_KEY for model-assisted steps — never commit .env
docker compose up -d --build      # lite profile: UI http://localhost:5173 · API http://localhost:8000
                                  # sign in as admin@analystos.local / ChangeMe123! (dev only)
```

From source:

```bash
uv venv -p 3.11 .venv && uv pip install -e ".[dev]"
docker compose up -d postgres
export ANALYSTOS_PROFILE=lite
.venv/bin/analystos migrate && .venv/bin/analystos seed
.venv/bin/uvicorn analystos.api.app:app --reload
(cd web && npm install && npm run dev)
```

Profiles (`lite` → `standard` → `bi` → `events` → `airgapped`/`ha`) add services only when a feature needs them;
see [runbook 04](docs/30-runbooks/04-lite-and-profiles.md). The run orchestrator is a deployment choice (ADR-015):
`local` (lite), `temporal` (standard and above) or `langgraph` (the `langgraph` extra, never a default), all over the
same Postgres run state; runbook 04 §6 says how to choose. Tests: `pytest -m "not integration"` (no services),
`pytest -m integration` (compose stack), `cd web && npm test`.

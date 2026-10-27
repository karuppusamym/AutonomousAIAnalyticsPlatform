# Context2AI AnalystOS

**An autonomous, governed data & analytics agent operating system.** Point it at governed
enterprise data, state a business problem, and a team of agents profiles the data, forms and
tests hypotheses, verifies what it finds, defines KPIs, designs executive and operational
dashboards, and — after a person approves — publishes them to Superset, with lineage from every
tile back to the query and table it came from.

> Models propose. Deterministic code computes, checks and enforces. People approve side effects.

| | |
|---|---|
| Product vision (v1) | [`Context2AI_AnalystOS_Complete_Spec.md`](../../Context2AI_AnalystOS_Complete_Spec.md) |
| Engineering spec (v2) | [`docs/00-intent/02-spec-v2.md`](../../docs/00-intent/02-spec-v2.md) |
| Workspace / engineering / ML target | [`docs/00-intent/03-workspace-data-team-spec.md`](../../docs/00-intent/03-workspace-data-team-spec.md) (design; built in increments 5–7, see the tracker) |
| Design review and priorities | [`docs/60-delivery/04-design-review.md`](../../docs/60-delivery/04-design-review.md) |
| Workbench UI / API design | [UX](../../docs/10-architecture/02-workbench-ux.md) · [API](../../docs/20-contracts/02-workbench-api.md) |
| Platform spec (v3, proposed) | [`docs/00-intent/03-spec-v3-platform.md`](../../docs/00-intent/03-spec-v3-platform.md): capabilities, deterministic-first ladder, open knowledge formats, ELT on your engines |
| Unified platform spec (v4) | [`docs/00-intent/04-spec-v4-unified-data-platform.md`](../../docs/00-intent/04-spec-v4-unified-data-platform.md): one platform for analyst, data-science, data-engineering and governed ML work; Atlas and DataPilot as donors ([parity checklists](../../docs/60-delivery/donor-parity-atlas.md)) |
| Latest reviews | [`docs/70-reviews/2026-09-26-agent-os-comparison-review.md`](../../docs/70-reviews/2026-09-26-agent-os-comparison-review.md) (comparison, dispositions) · [`docs/70-reviews/2026-09-25-architecture-review.md`](../../docs/70-reviews/2026-09-25-architecture-review.md) |
| AgentSwarms reuse | [`docs/70-reviews/2026-09-26-agentswarms-implementation.md`](../../docs/70-reviews/2026-09-26-agentswarms-implementation.md) (product fit, code boundary, first gateway change) |
| Agent mapping and workspace lifecycle | [`docs/30-operations/workspace-lifecycle.md`](../../docs/30-operations/workspace-lifecycle.md) |
| Architecture + ADRs | [`docs/10-architecture/`](../../docs/10-architecture/01-architecture.md) |
| Status (tracker) · evidence · readiness | [`docs/60-delivery/`](../../docs/60-delivery/01-tracker.md) |
| Working agreement for AI sessions | [`CLAUDE.md`](../../CLAUDE.md) |

## What happens in a run

```
context → metadata → relationships → profile → data quality
      → hypotheses (LLM, closed vocabulary, JEV-prioritised) → test:H-1..n (SQL pushdown + statistics)
      → follow-up rounds → insights (BH-corrected, numbers-guarded) → REV verification
      → reusable dataset → validated KPIs → charts + executive/operational dashboards
      → governance review → approval (bound to bundle hash + plan hash) → Superset → lineage graph
```

You can pause, resume, cancel, redirect ("exclude inquiry incidents"), reject a finding, or
approve/reject publication at any point; redirects replan and invalidate stale approvals.

## Key properties

* **Governed data access.** One SQL gateway for every path. Statements are parsed and checked
  against the caller's server-resolved scope (selected tables only; restricted/PII columns denied
  even through `*`, aliases and CTEs; user-defined functions, table hints and unkeyed joins refused),
  run read-only with timeouts and row caps, cached by scope, and audited. The query identity cannot
  reach the control-plane database.
* **Reproducible findings.** Hypotheses compile from a closed analysis vocabulary to SQL; every
  number comes from SciPy/statsmodels/scikit-learn; findings are verified by re-running evidence
  queries (hash match) and an independent second method.
* **Verdicts that void themselves.** Every verified result carries a fingerprint of its query, data,
  semantics, method, context, model call and policy; a change to any of them turns it `VOID`, and
  "Why this number?" walks a number back to its receipt.
* **Governed metrics compile.** Questions about approved metrics go through a deterministic semantic
  compiler (row filters and masks inside it, fan-out refused); every answer is labelled `governed` or
  `ad_hoc`.
* **Beyond analysis, one platform.** Transformation recipes compiled to SQL or DuckDB with DQ gates,
  quarantine, lineage and a dbt emitter; incremental pipelines with an approval-gated managed writer;
  governed classical ML (leakage refused, mandatory baseline, holdout read once, sealed reports,
  approved batch scoring); workspace briefs, versioned steps, branches and notebooks.
* **Published, pinned definitions.** Playbooks, recipes, ML specs and saved analyses are drafted and
  published; schedules run pinned versions and show *upgrade available* instead of drifting.
* **Model routing with JEV.** Purpose-based routing with fallback, budgets, redaction and
  prompt-injection screening; **TypeSafe Jev** decision model for typed, probability-scored choices
  that may escalate risk but never grant.
* **Light by default.** Postgres + API + web; Redis, Temporal, worker pools, Superset, Neo4j and
  credential-free compute pools are profiles you add when a feature needs them.
* **Measured, not asserted.** Owner-set evaluation gates in CI (grounding, analytical benchmark, Ask
  accuracy, cost, ML) and a frozen held-out corpus; dated evidence under `docs/60-delivery/evidence/`.
* **Full provenance.** Queries, experiments, artifacts (versioned), approvals, publications,
  model/tool calls; lineage in Postgres (optionally projected to Neo4j).

## Quick start (lite)

Everything in containers, the default **lite** profile (Postgres + API + web; the API migrates and
seeds on start):

```bash
cp .env.example .env              # optional: OPENROUTER_API_KEY for model-assisted steps — never commit .env
docker compose up -d --build      # UI http://localhost:5173 · API http://localhost:8000
                                  # sign in as admin@analystos.local / ChangeMe123!
```

Add features as profiles ([runbook 04](../../docs/30-runbooks/04-lite-and-profiles.md)):
`--profile demo` (ServiceNow mock source), `--profile bi` (Superset), `--profile graph` (Neo4j), or the
`standard` profile (Redis, Temporal, workers, scheduler) with
`docker compose --env-file .env --env-file deploy/compose/standard.env up -d --build`.

From source, for development:

```bash
uv venv -p 3.11 .venv && uv pip install -e ".[dev]"
docker compose up -d postgres     # lite needs only Postgres
export ANALYSTOS_PROFILE=lite     # local orchestrator and in-process scheduler (the code default is standard)
.venv/bin/analystos migrate && .venv/bin/analystos seed
.venv/bin/uvicorn analystos.api.app:app --reload           # API :8000
(cd web && npm install && npm run dev)                       # UI :5173
```

Prove the MVP end to end (writes a dated evidence report):

```bash
.venv/bin/python scripts/e2e_demo.py
```

Tests: `.venv/bin/pytest -m "not integration"` (no services) · `.venv/bin/pytest -m integration`
(compose stack) · `cd web && npm test`. Evaluation gates: `PYTHONPATH=src .venv/bin/python
scripts/eval_gates.py`; held-out corpus: `.venv/bin/python scripts/benchmark_heldout.py`.

## Stack

Python 3.11 · FastAPI · SQLAlchemy/Alembic · PostgreSQL 16 + pgvector · sqlglot · DuckDB/Polars ·
SciPy/statsmodels/scikit-learn · OpenRouter (chat + Decisions API) · React 18 + Vite + ECharts.
By profile: Redis and Temporal (`standard`), Apache Superset 4.1 (`bi`), Neo4j 5 (`graph`).

## Status

Increments 1–4 and increment 7 waves 1–2 (lite install, isolated compute pools, recipes and
pipelines, governed ML, the semantic compiler, verification records, definitions and pins, brief,
steps and notebooks) are built and tested; readiness is **prototype**: synthetic and file data,
read-only sources, local identity by default. The tracker
([`docs/60-delivery/01-tracker.md`](../../docs/60-delivery/01-tracker.md)) is the status authority, and
[`docs/60-delivery/03-release-readiness.md`](../../docs/60-delivery/03-release-readiness.md) lists what a
controlled pilot still needs (SSO against the customer's IdP, pilot-source certification, a recovery
drill, live-model evaluation and the paired practitioner baseline).

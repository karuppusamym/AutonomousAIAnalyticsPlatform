# ADR-0003 — Full infrastructure from day one, orchestrator-agnostic engine

**Status:** accepted (user decision, 2026-09-25: "full stack from day one")
**Amended 2026-09-26 by [ADR-0025](0025-lite-by-default.md) (accepted 2026-09-27):** the orchestrator-agnostic engine stays;
the default install becomes `lite` (Postgres + API + web), and the full stack becomes the `standard`/`scale` profiles.

**Decision.** Compose runs Postgres (pgvector), Redis, Neo4j, Temporal, Superset from the start.
The run engine exposes four operations (plan, get_state, execute_task, finish) that both the
Temporal workflow and a local loop call, so integration tests and single-process demos exercise
the same code as production.

**Consequences.** Superset 4.1.1 needs a derived image with `psycopg2-binary` (`deploy/superset/Dockerfile`).
*2026-10-28 (user decision):* Superset 6.1.0, built on `python:3.11-slim` with upstream's dependency set and
overrides past Superset's own bounds, so the image passes the supply-chain Grype gate
(`docs/60-delivery/evidence/supply-chain-images-20261028.md`).
Neo4j is a projection (rebuildable from Postgres) so its outage never loses provenance.

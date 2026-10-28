---
name: run-stack
description: Boot the AnalystOS stack locally (infra in docker compose, API/worker/mock/web as processes) and verify health. Use when asked to run, start, or demo the app.
---

1. `docker compose up -d postgres redis neo4j temporal superset` — first Superset boot builds
   `deploy/superset/Dockerfile` (Superset 6.1.0 on python:3.11-slim). Its first boot migrates an existing 4.1.x
   metadata DB one-way: `pg_dump` the `superset` database first. Behind a TLS-inspecting proxy pass the CA:
   `docker build --secret id=pipcert,src=<ca-bundle> -t analystos-superset:6.1.0 deploy/superset`.
2. Wait for health: `curl -fs localhost:8088/health` (Superset), `docker compose ps`.
3. `.venv/bin/analystos migrate && .venv/bin/analystos seed`.
4. Start processes (background, logs in `var/logs/`):
   - `.venv/bin/uvicorn analystos.connectors.servicenow_mock:app --port 8090`
   - `.venv/bin/analystos worker` (Temporal) — or set `ANALYSTOS_ORCHESTRATOR=local` for the API
   - `.venv/bin/uvicorn analystos.api.app:app --port 8000`
   - `cd web && npm run dev` (proxies `/api` to :8000)
5. `curl -s localhost:8000/api/health | python -m json.tool` — every check should be `ok: true`;
   `models.chat/jev` are false without `OPENROUTER_API_KEY` (agents then use labelled deterministic fallbacks).

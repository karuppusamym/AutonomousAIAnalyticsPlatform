#!/usr/bin/env bash
# Start the ServiceNow mock, Temporal worker and API as background processes (logs in var/logs).
# Usage: scripts/dev_up.sh   (infra must already be up: docker compose up -d postgres redis neo4j temporal superset)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var/logs
[ -f .env ] && { set -a; . ./.env; set +a; }
export ANALYSTOS_SOURCE_SECRET_SERVICENOW="${ANALYSTOS_SOURCE_SECRET_SERVICENOW:-admin}"
pkill -f "analystos.connectors.servicenow_mock" 2>/dev/null || true
pkill -f "analystos worker" 2>/dev/null || true
pkill -f "uvicorn analystos.api.app" 2>/dev/null || true
pkill -f "analystos scheduler" 2>/dev/null || true
sleep 1
nohup .venv/bin/uvicorn analystos.connectors.servicenow_mock:app --port 8090 > var/logs/servicenow-mock.log 2>&1 &
nohup .venv/bin/analystos worker > var/logs/worker.log 2>&1 &
nohup .venv/bin/uvicorn analystos.api.app:app --port 8000 > var/logs/api.log 2>&1 &
nohup .venv/bin/analystos scheduler > var/logs/scheduler.log 2>&1 &
for i in $(seq 1 30); do curl -fs localhost:8000/api/health >/dev/null 2>&1 && break; sleep 1; done
curl -s localhost:8000/api/health

# Runbook — observability: metrics, traces, logs, alerts (spec S12 §2)

## Metrics

Every process counts in `analystos.observability.metrics` (no client library; the catalog `CATALOG` is the list of
names, labels and help). The API serves `/metrics` on its HTTP port; workers and the scheduler serve it on
`ANALYSTOS_METRICS_PORT` (Helm `monitoring.metricsPort`, default 9464). The processes of one pod (uvicorn workers,
the compute pool's children) merge through `ANALYSTOS_METRICS_DIR` (Helm: `/tmp/analystos-metrics`, the pod's
emptyDir). `ANALYSTOS_METRICS_TOKEN` (optional) requires `Authorization: Bearer <token>`; the per-role
NetworkPolicy opens the metrics ports only to `networkPolicy.monitoringFrom`.

| Metric | Labels | From |
|---|---|---|
| `analystos_http_requests_total`, `analystos_http_request_duration_seconds` | class, method, status | API middleware: time to the response start (SSE: to the stream opening); class = the route template's keyword (`ask`, `runs`, `goals`, `desk`, `admin`, ...), `unmatched` for unknown paths |
| `analystos_runs_total`, `analystos_run_duration_seconds`, `analystos_run_steps` | outcome | `runtime.engine.finish_run` |
| `analystos_gateway_statements_total` / `analystos_gateway_rejections_total` | status / reason | every audited statement (`QueryGateway._persist`); reasons are bounded codes (`write`, `parse`, `restricted_column`, `out_of_scope`, ...) |
| `analystos_model_calls_total`, `_tokens_total`, `_cost_usd_total`, `_fallbacks_total`, `_call_duration_seconds` | provider, model, purpose, status, rung | every `model_call` record (`runtime.usage.DbUsageSink.record`) |
| `analystos_cache_requests_total` | cache (model_response, query_result), result | model cache hits, gateway result cache |
| `analystos_approval_latency_seconds` | action, decision | `governance.approvals.decide` |
| `analystos_void_sweeps_total`, `analystos_findings_voided_total` | outcome | the nightly verification sweep |
| `analystos_desk_resolutions_total` | mode (auto, auto_with_review, human) | Request Desk, first answer |
| `analystos_rls_denied_total` | where | an unhandled error that is Postgres refusing a row under RLS |
| `analystos_budget_used_ratio` | scope_kind, scope | the monthly model budget check per workspace |
| `analystos_build_info` | version, component | constant 1 |

Queue depth comes from Temporal itself (`temporal_approximate_backlog_count`, matching service); job failures from
kube-state-metrics (`kube_job_status_failed`). Each metric keeps at most 1,000 label sets (`__overflow__` after).

## Traces

`analystos.observability.telemetry.span()` / `record_span()` around the request (`http.request`), every model call
(`model.call`) and every statement (`gateway.execute`). Attributes pass an allowlist (`tenant`, `workspace`,
`request_id`, `run_id`, `step`, `purpose`, `model`, `rung` and bounded status fields); a string that is not a short
token is replaced by `sha256:<16 hex>`, so prompts, SQL and rows never reach a trace. Off by default: build the image
with `EXTRAS=...,otel` and set Helm `monitoring.otel.enabled=true` + `endpoint` (OTLP/HTTP; the SDK reads the standard
`OTEL_EXPORTER_OTLP_*` variables). JSON log lines carry `trace_id` / `span_id` when a span is active, next to
`correlation_id` (the request id) and `run_id`; ship them to the customer's SIEM with the cluster's log agent.

## Dashboards and alerts

Helm `monitoring.podMonitor`, `monitoring.prometheusRule`, `monitoring.grafanaDashboards` (Prometheus Operator and the
Grafana sidecar). Dashboards: *Platform overview*, *Models and spend*, *SLOs and error budgets*
(`deploy/helm/analystos/files/grafana/`). Rules: `deploy/helm/analystos/files/prometheus/rules.yaml`; SLO targets in
`monitoring.prometheusRule.slo` (same numbers as `config/slo.yaml`).

### slo-burn
`AnalystOS{AskLatency,RunSuccess,Availability}BudgetBurn{Fast,Slow}`: multiwindow burn rates (fast 14.4x over 1 h and
5 m pages; slow 6x over 6 h and 30 m opens a ticket). Ask latency: the share of `POST` asks slower than
`askLatencySeconds`; run success: FAILED + EXPIRED over finished runs (CANCELLED excluded); availability: 5xx share
(health and metrics excluded). First look at *SLOs and error budgets*, then the overview's latency by class and the
provider panel (a slow provider shows as Ask latency).

### queue-backlog
`AnalystOSQueueBacklog`: a task queue above `queueBacklogThreshold` for 10 min. Check the pool's replicas (KEDA
ScaledObject or HPA at max?), `kubectl logs` of the pool for crash loops, Temporal's matching service. A compute
backlog with idle CPUs points at the process pool size (`config/task_queues.yaml`).

### provider-errors
`AnalystOSModelProviderErrors`: more than 5% of a provider's calls failed over 10 min. `/api/admin/models/health`
shows cooldowns (HTTP 402 = credits). Deterministic rungs keep answering; nothing to restart. Claude and Gemini
(`blocked_families`) are never a fallback.

### spend
`AnalystOSSpendNearBudget`: a workspace passed `budgetRatio` of its monthly model budget. Look at
`GET /v2/admin/spend?group_by=workspace,item&workspace=...`; raise the policy's budget or let the cap refuse calls.

### audit-chain
`AnalystOSAuditChainVerifyFailed` (page): the nightly `analystos audit verify` CronJob failed (exit 4 = a broken
chain). Do not reseal. Run `analystos audit verify --tenant T` to find the chain, export it (`analystos audit export`)
to WORM storage, and treat it as a security incident (the audit trail may have been altered).

### rls-coverage
`AnalystOSRlsCoverageFailed` (page): `analystos ops rls-check` found a tenant-owned table without row security, its
`tenant_isolation` policy or its fill trigger, or the runtime role with BYPASSRLS. Usually a migration that created a
table without `tenancy.ddl.install`. Stop rollouts; re-run the migration's `install` for the table; the check's JSON
names the tables.

### rls-denied
`AnalystOSRlsDenied` (ticket): statements were refused by RLS — a code path tried to read or write another tenant's
row. The request id in the 500 response and the log line leads to the code path.

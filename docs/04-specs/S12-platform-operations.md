# S12 — Platform operations: deployment, observability, evaluation, cost, security hardening

**Priority:** P0 · **Closes:** FR-052, FR-055 (with S05), v1 §15–17, the review's §6 production gaps

## 1. Deployment
* **Images:** one platform image (wheel-installed, non-root, distroless or slim, SBOM via Syft, scanned with Grype/Trivy in CI,
  signed with cosign), `web` (nginx), `sandbox` (compute), `superset` (pinned 4.1.x or the validated next version).
* **Helm** (`deploy/helm/`, inherited): add `worker-query`, `worker-compute` (gVisor RuntimeClass), KEDA ScaledObjects on
  Temporal queues, PDBs, NetworkPolicies per role, External Secrets Operator for `secret_ref`, and an OpenShift SCC-compatible
  securityContext. Values: `small`, `ha`, `airgapped`.
* **Environments:** dev (compose) → staging (k8s, synthetic + masked data) → prod. Alembic migrations run as a pre-upgrade
  Job using `app_migrator`, and must be expand/contract compatible for one release back.
* **Data stores:** managed Postgres 16 with PITR (RPO ≤ 15 min), read replica for reporting; S3-compatible object store with
  versioning and lifecycle; Temporal Cloud or a self-hosted cluster with its own Postgres.

## 2. Observability
* **Traces:** OpenTelemetry SDK across api → desk → context → router → gateway → compute → publisher. Attributes are
  `tenant`, `workspace`, `request_id`, `run_id`, `step`, `purpose`, `model`, `rung`. **No prompts, SQL literals or rows**
  in spans (hashes only).
* **Metrics (Prometheus):** request latency per class; run duration, steps and outcome; queue depth per task queue;
  gateway rejections by reason; model calls, tokens, cost, errors and fallbacks per provider; cache hit rates; approval latency;
  VOID sweeps; Desk auto-resolution; RLS-denied counts.
* **Dashboards and alerts** (Grafana): SLO burn rates (Ask p95, run success, availability), queue backlog > N for 10 min,
  provider error rate > 5%, spend > 80% of a budget, audit chain verify failed (page), RLS coverage test failed in prod check (page).
* **Logs:** JSON, with request and trace ids, redacted, shipped to the customer's SIEM.

## 3. Evaluation (release gates)
Inherit `config/eval_gates.yaml`, the golden questions, the held-out corpus, and the adversarial suite. Add:
* **Per-model matrix** (FR-055): each profile × golden suite nightly; results in `/v2/admin/models/evaluations`.
* **Gates in CI** (blocking): SQL safety 100% negative suite, cross-tenant 0 leaks, publish integrity 100%, confident-wrong 0
  on golden; execution correctness ≥ 95%, answer correctness ≥ 90%, metric grounding ≥ 95% (v1 §17.2).
* **Text2SQL benchmark** optional: BIRD/Spider-style dev sets (via the DB-GPT-Hub harness) for model selection, never
  as a substitute for the tenant's golden suite.

## 4. Cost and chargeback (FR-052)
`cost_ledger` view over `model_call.cost`, `query_execution.(scan_bytes, credits)`, `worker_task.cpu_seconds × rate` and storage.
Rolled up per request, run, conversation, user, workspace, tenant and day. Budgets (S05/S06) read from it. Export: CSV/Parquet monthly,
and `/v2/admin/spend?group_by=`.

## 5. Security hardening checklist (production gate)
- [ ] OIDC against the customer IdP (Entra ID / Okta / Keycloak) with group → role mapping; MFA enforced at the IdP
- [ ] RLS coverage 100% (S01) and the negative cross-tenant suite in CI
- [ ] gVisor compute (S07), NetworkPolicies, egress allowlist for model hosts
- [ ] Secrets only via External Secrets / vault; rotation runbook tested; no secrets in env dumps or logs (gitleaks in CI)
- [ ] TLS everywhere; DB `sslmode=verify-full`
- [ ] Dependency pinning (uv.lock, package-lock), SBOM, image signing, weekly CVE scan
- [ ] Audit chain verify nightly + export to WORM
- [ ] Independent pen test + threat-model review (STRIDE on each plane)
- [ ] Backup/restore and PITR drill; Temporal restore drill (AT-09 after restore)
- [ ] Load test (k6/Locust) at 2× pilot targets; SLOs signed off
- [ ] Data retention jobs per class; tenant deletion runbook tested

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S12-T1 | Helm additions (roles, KEDA, gVisor, ESO, SCC) | 4d |
| S12-T2 | CI supply chain: SBOM, scan, sign, gitleaks | 1.5d |
| S12-T3 | OTel traces + metrics + Grafana dashboards + alerts | 4d |
| S12-T4 | Per-model eval matrix + CI gates wiring | 2d |
| S12-T5 | Cost ledger + spend API + export | 2d |
| S12-T6 | Load test suite + SLO report | 3d |
| S12-T7 | DR drill (PITR + Temporal) + runbook | 2d |
| S12-T8 | Customer IdP integration + pen test coordination | external |

## 7. As built (2026-10-27, T1–T7; T8 handed off)

Code: `src/analystos/observability/` (metrics, telemetry, ASGI middleware, worker `/metrics` server),
`src/analystos/ops/` (`cost_ledger.py`, `model_matrix.py`, `checks.py`, `cli.py` = `analystos ops ...`),
`api/routers/ops_v2.py` (`/v2/admin/models/evaluations`, `/v2/admin/spend`, `/v2/admin/spend/export`),
`evaluation/{model_matrix,release_safety}.py`, migration `0072_platform_ops`, Helm templates
`keda.yaml`, `runtimeclass.yaml`, `externalsecret.yaml`, `networkpolicy-roles.yaml`, `monitoring.yaml`,
`ops-cronjobs.yaml` and `files/{grafana,prometheus}/`, workflows `supply-chain.yml` and `nightly-eval.yml`,
scripts `scripts/load/slo_report.py` and `scripts/dr_drill.py`. Runbooks 11 (deployment, supply chain, spend),
12 (observability and alerts), 13 (DR). Deviations from §1–§5:

* **T1 roles.** No new Temporal queue: `worker-query` is the existing `analysis` pool (agent steps run the gateway
  statements) and `worker-compute` the `compute` pool, each labelled `analystos.io/role`; isolated compute pools
  are role `compute` too. gVisor is per pool (`gvisor: true`) behind `gvisor.enabled`. KEDA replaces the pool's HPA
  (one `temporal` trigger per queue). The ExternalSecret is a pre-install hook so the migration Job finds the Secret.
  Images: one numeric non-root user in group 0 (OpenShift); distroless not adopted (python:3.11-slim stays).
  Values `small`/`ha`/`airgapped` existed; every S12 switch is opt-in in `values.yaml`.
* **T2.** Actions are pinned to release tags, not SHAs (Dependabot/Renovate digest pinning is the follow-up).
  Grype only (no Trivy); `pip-audit` and `npm audit` added; there is no `uv.lock` yet (the spec's dependency pinning
  item stays open for Python).
* **T3.** No Prometheus client library: `observability.metrics` renders the exposition format itself and merges a
  pod's processes through `ANALYSTOS_METRICS_DIR`. Traces need the `otel` extra (`EXTRAS=...,otel`); spans cover
  request, model call and gateway statement (desk/context/compute/publisher spans are the follow-up; their work is
  inside the request span). Queue depth is Temporal's own backlog gauge; the audit-chain and RLS-coverage pages come
  from CronJob failures (kube-state-metrics). `analystos ops rls-check` is new (the prod RLS check).
* **T4.** The matrix runs the golden Ask suite per profile × allowed model with the profile pinned (its other models
  disabled for the run; `pinned_share` proves it). Candidates exclude `blocked_families` (Claude, Gemini) and the
  decision profile; stored rows of a now-blocked family are never shown as evaluated. CI blocks on
  `release_safety` (pytest negative suites counted; skipped = not run = fail) and `model_matrix_fake`; the live matrix
  is nightly (`nightly-eval.yml`) and lands in an environment through `analystos ops model-matrix-import`.
  The Ask golden suite only reaches the `coding` profile (SQL generation/repair); other profiles report
  `not_exercised` until they have golden suites. Text2SQL benchmark (BIRD/Spider) not built (optional).
* **T5.** The priced rollup is `ops.cost_ledger` (SQL aggregation, ≤ 5,000 groups, streamed export); the
  `cost_ledger` view (security_invoker) holds the same unpriced lines for SQL readers. `query_execution.scan_bytes`
  and `credits` exist but no engine fills them yet (S02 follow-up: engines that report bytes/credits), so query
  spend is 0 until then. Storage = artifact payload bytes written. Budgets still read the model-call counters
  (S05/S06); `cost_ledger.scope_cost` is ready for them. Blocked-family spend is kept in totals and flagged.
* **T6.** `scripts/load/slo_report.py` (Python, no k6/Locust dependency) on an isolated plane or `--base-url`;
  targets in `config/slo.yaml` (owner sign-off pending). A run started through the API ends at the publish approval
  gate; the report counts reaching it as success and never approves. Measured 2026-10-28 on a Windows dev host at 2x
  pilot: availability met, runs met (lite), Ask latency missed (p95 6.3 s with workers, 10.4 s lite); the Temporal
  topology's compute process pool breaks on Windows, so the run SLI there needs Linux workers.
* **T7.** `scripts/dr_drill.py`: PITR and Temporal restore in throwaway containers (evidence
  `docs/60-delivery/evidence/2026-10-28-s12-dr-drill.md`).
* **T8.** External: hand-off in `docs/60-delivery/s12-t8-idp-pentest-handoff.md`.

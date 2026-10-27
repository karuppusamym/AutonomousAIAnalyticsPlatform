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

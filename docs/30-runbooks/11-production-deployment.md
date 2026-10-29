# Runbook — production deployment, supply chain and spend (spec S12 §1, §4, §5)

The chart is `deploy/helm/analystos` (values `values.yaml`, overlays `values-small.yaml`, `values-ha.yaml`,
`values-airgapped.yaml`). Everything below is opt-in; `helm lint` and `tests/unit/test_helm_chart.py` cover each
switch (CI job `supply-chain / helm`).

## Roles

| Role (`analystos.io/role`) | Deployment | Notes |
|---|---|---|
| `api` | `<release>-api` | `/metrics` on the http port |
| `query` | `worker-analysis` | agent steps and gateway statements (the spec's `worker-query`) |
| `compute` | `worker-compute`, isolated `worker-compute-py`, `worker-compute-ml` | gVisor with `gvisor.enabled` (the spec's `worker-compute`) |
| `publish`, `crawl`, `elt` | `worker-<pool>` | |
| `scheduler`, `web`, `migrate`, `ops` | | `ops` = the audit-verify and rls-check CronJobs |

## Switches

* **KEDA** (`keda.enabled`, per pool `workers.<pool>.keda`): a ScaledObject with one `temporal` trigger per queue the
  pool serves (`<temporalQueuePrefix>-<queue>`, `targetQueueSize`); the pool's HPA is not rendered. KEDA >= 2.17;
  Temporal Cloud or mTLS: `keda.temporal.authenticationRef` (a TriggerAuthentication). The small install's `all` pool
  cannot use it (render fails).
* **gVisor** (`gvisor.enabled`): `runtimeClassName: gvisor` on pools with `gvisor: true` (compute, compute-py,
  compute-ml). GKE Sandbox has the class; elsewhere install runsc on a node pool and set `gvisor.createRuntimeClass`
  (+ `handler`, `nodeSelector`). Isolated pools under the class get `ANALYSTOS_WORKER_REQUIRE_RUNTIME=gvisor` and exit 78
  on any other kernel (`workers.<pool>.requireRuntime` overrides: `kata`, or `""` for no check). Proof on kind:
  `deploy/kind/README.md`.
* **External Secrets** (`externalSecrets.enabled`, `secretStoreRef`, `data`/`dataFrom`): the ExternalSecret that
  writes `secrets.existingSecret` is a pre-install/pre-upgrade hook (weight -10), so the migration Job finds the
  Secret; `creationPolicy: Orphan` keeps the Secret when the hook object is replaced. Source credentials referenced
  as `secret_ref: env:NAME` belong in the same Secret. Rotation: change the vault entry, wait `refreshInterval`, then
  `kubectl rollout restart deploy -l app.kubernetes.io/instance=<release>` (env is read at start).
* **OpenShift** (`openshift.enabled`): pods omit `runAsUser`/`runAsGroup`/`fsGroup` so restricted-v2 assigns them;
  the app image runs as UID 10001, group 0, with group-writable `/app/var`, so an arbitrary UID works.
* **Per-role NetworkPolicies** (`networkPolicy.enabled` + `perRole`): each role reaches DNS, the release's API and the
  peer groups `networkPolicy.roles` lists (`platform`, `models`, `sources`, `superset`, `idp`, `otel`, each a list of
  NetworkPolicy peers in `networkPolicy.egress`). `models` is the egress allowlist for model hosts: an egress proxy or
  the providers' CIDRs. Compute never reaches models or sources. `ingressFrom` (the ingress controller) is required.
* **Migrations as `app_migrator`** (`migrations.databaseUrlKey`): the pre-upgrade Job reads its database URL from that
  Secret key (the owner role, S01 §5); the pods keep the runtime login. Migrations are expand/contract for one
  release back: add columns nullable, backfill in a later release, drop only after no running version reads it.
* **Monitoring**: see [12-observability.md](12-observability.md).

## Supply chain

`.github/workflows/supply-chain.yml`: gitleaks over the whole history (`.gitleaks.toml`: default rules, an allowlist
of the documented development defaults only); Helm lint + chart tests; per image (app, web, sandbox, superset, mock)
an SPDX SBOM (Syft) and a Grype scan that fails on fixable high/critical findings, SARIF to code scanning; on `main`
the image is pushed to GHCR, signed by digest with cosign (keyless, GitHub OIDC) and the SBOM attached as a signed
attestation; `npm audit` and `pip-audit`; the same scan weekly. Verify before deploying:

```bash
cosign verify ghcr.io/<owner>/analystos-app@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign verify-attestation --type spdxjson ghcr.io/<owner>/analystos-app@sha256:<digest> \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/' --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Deploy the commit tag that was signed (`image.tag=<git sha>`) and enforce signatures at admission (Kyverno
`verifyImages` or the Sigstore policy-controller with the identity above), so a re-pushed tag cannot slip in.

Releases (tag → signed images and chart on GHCR → gated `staging` deploy) and the owner's environment setup:
[14-release-and-deploy.md](14-release-and-deploy.md).

## Release gates

`config/eval_gates.yaml` v6, run in CI after `analystos migrate`:
`scripts/eval_gates.py --tiers release_safety,model_matrix_fake`. `release_safety` runs the negative suites (SQL
safety corpus 100%, cross-tenant 0 leaks, publish integrity 100%; a skipped test fails the gate). The per-model matrix
runs nightly live (`.github/workflows/nightly-eval.yml`); import its gate result into an environment with
`analystos ops model-matrix-import nightly.json`, then `GET /v2/admin/models/evaluations`. Claude and Gemini
(`blocked_families`) are never evaluated or called.

## Spend (cost ledger)

`GET /v2/admin/spend?group_by=day,workspace&since=&until=&category=` (tenant administrators: their tenant; workspace
owners: `workspace=`; platform administrators: `scope=platform`). Rates: `config/cost_rates.yaml` (versioned; model
calls carry their own USD). Monthly export: `GET /v2/admin/spend/export?month=YYYY-MM&format=csv|parquet` or
`analystos ops spend-export --month YYYY-MM --format parquet --out DIR [--tenant T]`. SQL readers: the `cost_ledger`
view (security_invoker: RLS applies). Spend recorded on a now-blocked model family stays in every total, flagged
`blocked`, and is also reported as `blocked_model_usd`.

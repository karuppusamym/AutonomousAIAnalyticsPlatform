# Runbook — release and deploy (FND-004, spec S12 §1/§5)

Two workflows deliver a version to a cluster:

* `.github/workflows/release.yml` runs on a tag `vX.Y.Z` (or `vX.Y.Z-rc.N`), or by hand with a version. It builds
  the five images (app, web, mock, sandbox, superset) and gates each one on the same SBOM + Grype check as
  `supply-chain.yml` (a fixable high/critical finding stops the release before anything is pushed). It then pushes
  `ghcr.io/<owner>/analystos-<image>:<version>` and `:sha-<commit>`, signs each image by digest with cosign keyless
  (GitHub OIDC) and attests its SBOM. The chart is packaged with chart version = app version = the release, pushed to
  `oci://ghcr.io/<owner>/charts/analystos` and signed. A `release-manifest.yaml` artifact and the run summary list
  every digest. Last, the release deploys to `staging`, and to `production` once that is set up (below).
* `.github/workflows/deploy.yml` is the deploy step. release.yml calls it, and you can run it by hand
  (Actions → deploy → Run workflow) to redeploy or roll back to any published version. It verifies the cosign
  signature of the app image, the web image and the chart against this repository's `release.yml` identity, and
  pins the pods to the digests it verified (`image.tag=<version>@sha256:…`). It then runs
  `helm upgrade --install --atomic --wait` (a failed upgrade rolls back) and smoke-tests `/api/health` (`ok: true`)
  and the web page through `kubectl port-forward`.

Nothing about a cluster is in the repository. Each GitHub Environment holds its reviewers, its cluster credentials and
its settings. Local validation of both workflows and a proof install on kind: [the last section](#validation).

## One-time setup (owner)

### 1. The cluster side (each environment)

* External Postgres (pgvector) with the roles of `deploy/postgres/01-init.sql`, Temporal, and optionally Redis and
  Superset, as in [11-production-deployment.md](11-production-deployment.md) and [03-self-hosted.md](03-self-hosted.md).
* Namespace (default `analystos`) and the Secret `analystos-secrets` in it, created by hand or by External Secrets
  (`externalSecrets.enabled`). The keys are listed at `secrets:` in `deploy/helm/analystos/values.yaml`. Credentials
  are never chart values.
* Image pulls from GHCR: the packages are private by default. Either make the five `analystos-*` packages and
  `charts/analystos` public, or create a pull secret in the namespace with a token that has only `read:packages`, and
  name it in the values file:
  `kubectl -n analystos create secret docker-registry ghcr-pull --docker-server=ghcr.io --docker-username=<user> --docker-password=<token>`.
* gVisor for the compute roles (`gvisor.enabled: true`): a node pool with runsc. GKE Sandbox ships the `gvisor`
  RuntimeClass. Elsewhere a cluster admin creates it once. The deployer below is namespace-scoped, so keep
  `gvisor.createRuntimeClass: false` in its values.
* A deployer identity: a ServiceAccount bound to a namespace Role that may manage what the chart renders
  (Deployments, Services, ConfigMaps, Secrets (Helm keeps its release records there), Jobs, CronJobs, HPAs, PDBs,
  NetworkPolicies, ServiceAccounts, KEDA ScaledObjects, Ingresses, Prometheus Operator objects when enabled), plus
  `pods/portforward` and `get`/`list` on pods for the smoke test. Its kubeconfig is the Environment secret below.
  Use a token Secret (`kubernetes.io/service-account-token`) or your cloud's short-lived credentials; never a
  cluster-admin kubeconfig.

### 2. GitHub Environment `staging`

Settings → Environments → New environment `staging`:

| Setting | Value |
|---|---|
| Required reviewers | the people who approve a staging deploy (the job waits for one of them) |
| Deployment branches and tags | Selected: tag pattern `v*` (release runs) and `main` (manual runs) |
| Secret `KUBECONFIG` | `base64 -w0 deployer.kubeconfig` of the deployer above |
| Secret `HELM_VALUES` (optional) | an extra values YAML that should not be in the repository, e.g. hostnames or the ESO store name. It is still not for passwords: those belong in `analystos-secrets` |
| Variable `NAMESPACE` | default `analystos` |
| Variable `RELEASE_NAME` | default `analystos` |
| Variable `HELM_VALUES_FILES` | space-separated values files in the repository, applied in order; default `deploy/helm/analystos/values-small.yaml` |
| Variable `APP_URL` | the URL shown on the deployment, e.g. `https://analystos.staging.example.com` |

A typical staging values file, committed as e.g. `deploy/helm/analystos/values-staging.yaml` and named in
`HELM_VALUES_FILES` after `values-small.yaml`:

```yaml
config:
  temporalAddress: temporal-frontend.temporal.svc:7233
  webUrl: https://analystos.staging.example.com
  corsOrigins: https://analystos.staging.example.com
imagePullSecrets: [{name: ghcr-pull}]      # unless the packages are public
ingress:
  enabled: true
  className: nginx
  host: analystos.staging.example.com
  tls: [{secretName: analystos-tls, hosts: [analystos.staging.example.com]}]
gvisor: {enabled: true}                     # nodes with runsc and an existing `gvisor` RuntimeClass
```

The workflow sets `image.registry`, `image.repository`, `image.tag`, `webImage.repository` and `webImage.tag`
itself. Do not set them in these files.

### 3. Production (the owner's choice)

The `production` job in release.yml is skipped until both of these exist:

1. An Environment `production` with its own required reviewers (and, if you want it, a wait timer), the deployment
   tag pattern `v*` only, its own `KUBECONFIG` secret for the production cluster's deployer, and the variables of the
   table above (for example `HELM_VALUES_FILES=deploy/helm/analystos/values-ha.yaml deploy/helm/analystos/values-production.yaml`).
2. The repository variable `PRODUCTION_DEPLOY=true` (Settings → Secrets and variables → Actions → Variables).

Production then runs only for tagged releases, after staging succeeded, and waits for its own reviewers. A manual
deploy to `production` also verifies against the tag identity (`...release.yml@refs/tags/v`), so a version released
by hand from a branch can go to staging but never to production (deploy.yml `SIGNER`, 2026-10-30). Add
admission control that enforces the signature on the production cluster (Kyverno `verifyImages` or the Sigstore
policy-controller) with the identity `^https://github.com/<owner>/<repo>/.github/workflows/release.yml@refs/tags/v`
and issuer `https://token.actions.githubusercontent.com`. Then a digest the release did not sign cannot run.

## Cut a release

```bash
git tag -a v0.2.0 -m "AnalystOS 0.2.0"
```

```bash
git push origin v0.2.0
```

(`origin` = the GitHub repository.) Approve the `staging` deployment when the run asks. To build without a tag, run
release.yml by hand with a version; its deploy step can be switched off with `deploy: false`.

**Roll back:** run deploy.yml by hand with the previous version (it re-verifies and pins that version's digests), or
`helm -n analystos rollback analystos <revision>` with the deployer's kubeconfig. Migrations are expand/contract for
one release back, so the previous version runs against the new schema.

**Check a release by hand:**

```bash
cosign verify ghcr.io/<owner>/analystos-app:0.2.0 \
  --certificate-identity-regexp '^https://github\.com/<owner>/<repo>/\.github/workflows/release\.yml@refs/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Validation

GitHub Actions is not running on this repository at the moment (account billing), so neither workflow has run on
GitHub yet. Checked locally on 2026-10-29 (evidence:
[`docs/60-delivery/evidence/fnd-004-s07-t1-kind-gvisor-20261029.md`](../60-delivery/evidence/fnd-004-s07-t1-kind-gvisor-20261029.md)):

* `actionlint` 1.7.12 with shellcheck 0.11.0 over every workflow: no findings.
* `helm lint` / `helm template` for the default, small, HA, air-gapped and kind values, plus
  `tests/unit/test_helm_chart.py` and `tests/unit/test_supply_chain.py` (the release and deploy structure).
* The packaged chart installed on a local kind cluster with gVisor: [`deploy/kind/README.md`](../../deploy/kind/README.md).

The first real release run will be the first time GHCR push, signing and the Environment gate run end to end. Watch
it, and keep `PRODUCTION_DEPLOY` unset until a staging deploy has succeeded.

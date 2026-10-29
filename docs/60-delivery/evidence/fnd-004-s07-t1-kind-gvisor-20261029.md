# Evidence — CD pipeline, kind proof install and gVisor compute pods (FND-004, S07-T1), 2026-10-29

Windows 11 dev host, Docker Desktop 28.5.2 (WSL2 kernel 6.6.87.2-microsoft-standard-WSL2, cgroup v2, 8 CPUs).
Base commit `c477465` plus this change. Tools, installed for this run with winget (user scope): kind v0.33.0,
helm v4.3.1, actionlint 1.7.12, shellcheck 0.11.0. kubectl v1.34.1 came with Docker Desktop.

## 1. Workflows (validated locally; GitHub Actions is blocked by account billing)

| Check | Result |
|---|---|
| `actionlint -shellcheck shellcheck` over all workflows (`ci`, `nightly-eval`, `supply-chain`, `release`, `deploy`) | no findings. shellcheck was confirmed active with a deliberate SC2086 probe |
| `tests/unit/test_supply_chain.py` (+2 tests: release structure, deploy verification/pinning) | pass |
| Run on GitHub | **not run.** The first tagged release will be the first time GHCR push, cosign signing and the `staging` Environment gate run end to end |

## 2. Chart

| Values | `helm lint` | `helm template` |
|---|---|---|
| defaults | 0 failed | 25 objects |
| `values-small.yaml` | 0 failed | 14 objects |
| `values-ha.yaml` | 0 failed | 38 objects |
| `values-ha.yaml` + `values-airgapped.yaml` | 0 failed | 38 objects |
| `values-kind.yaml` | 0 failed | 20 objects |

`tests/unit/test_helm_chart.py`, `test_supply_chain.py`, `test_compute_isolation.py` with `ANALYSTOS_HELM` set:
**42 passed**, none skipped. `ruff check` is clean on the changed tests.

**Bug found by the kind install and fixed.** The migration Job is a `pre-install` hook. It used the release's
ServiceAccount and `envFrom` the release ConfigMap, but Helm creates neither until the hooks have run. A first
`helm install` into a new namespace therefore hung: `FailedCreate … serviceaccount "aos-analystos" not found`. It had
never been seen because only `helm template` and lint had run before. Fix (`templates/migrate-job.yaml`): the Job brings
a hook ServiceAccount and a hook ConfigMap (plus a `-files` ConfigMap for a custom models file or the OIDC mapping), at
weight -5, rendered from the same `analystos.configData` helper, and deleted with the Job when the hooks succeed.
The release ConfigMap renders identically. Deployments differ only in their `checksum/config` hash, so pods roll once
on the next upgrade. Test: `test_the_migration_hook_brings_its_own_service_account_and_config`.

**Chart gap closed (S07 hand-over to S12).** Isolated pools under the gVisor class now get
`ANALYSTOS_WORKER_REQUIRE_RUNTIME=gvisor`. A per-pool `requireRuntime` overrides it (`kata`, or `""` for no check).
Test: `test_isolated_gvisor_pools_require_the_runtime_they_are_given`.

## 3. Proof install on kind (`deploy/kind/proof.sh up`)

Cluster `aos-proof`: one node, Kubernetes v1.37.0, containerd v2.3.4, Debian 13. It uses its own kubeconfig, so the
user's kube context was not touched. Images were built from this tree: `analystos/app:0.1.0-kind`
(`EXTRAS=standard`, `sha256:4081954c…`) and `analystos/web:0.1.0-kind` (`sha256:1e3c8fdf…`). Both were loaded with
`kind load` and run with `pullPolicy: Never`.
`deploy/kind/infra.yaml` provides pgvector/pgvector:pg16, redis:7-alpine and temporalio/auto-setup:1.25.

The chart was packaged (`helm package` → `analystos-0.1.0.tgz`) and installed from that package with
`values-kind.yaml`. After the fix, a fresh install on an empty release: `STATUS: deployed`, `REVISION: 1`, one
ReplicaSet per Deployment. The whole `up` took 1m43s with the cluster already present. The migration Job completed and
its hook objects were removed.

`deploy/kind/proof.sh check`:

```
--- GET /api/health (API)
{"ok":true,"degraded":true,"orchestrator":"temporal", ... checks: postgres, redis, temporal, isolated_pools ok;
 problems: worker "no worker is polling analystos-publish"; sandbox "process: sandbox could not start"}
--- GET / (web)
<div id="root"></div>
--- GET /api/health through the web proxy
{"ok":true,"degraded":true,...}
```

`degraded` is expected for this proof. The publish, crawl, query and elt pools are at 0 replicas, and the API pod runs
under RuntimeDefault seccomp, where the process sandbox is refused (fail closed, as `values.yaml` documents).

## 4. gVisor (S07-T1)

`deploy/kind/install-gvisor.sh` installed gVisor **release-20261026.0**: the tarball's sha512 verified, runsc, the shim
and `gvisor-bin/` streamed into the node, and a `runsc` runtime (systemd cgroups) added to containerd. The chart created
RuntimeClass `gvisor` (handler `runsc`).

| Pod | `runtimeClassName` | Kernel / dmesg | `detect_runtime()` | `check_runtime({REQUIRE: gvisor})` |
|---|---|---|---|---|
| `worker-compute` (full worker) | gvisor | `Linux version 4.19.0-gvisor #1 SMP Sun Jan 10 15:06:54 PST 2016`, `[0.000000] Starting gVisor...` | `gvisor` | accepted → `gvisor` |
| `worker-compute-py` (isolated, env `ANALYSTOS_WORKER_REQUIRE_RUNTIME=gvisor`) | gvisor | same | `gvisor` | accepted → `gvisor` |
| `worker-analysis` | (none: runc) | | | **refused**: `this isolated worker must run under gvisor but the kernel reports runc` |

On the node, `containerd-shim-runsc-v1` → `runsc-sandbox --systemd-cgroup=true …` → `[gvisor_sentry]` for those pods.

The isolated worker's own entrypoint (`workers/main.py::run_isolated`) ran `check_and_scrub_environment`, then
`check_runtime`, then its egress guard. The pod then started with 0 restarts and polls Temporal through its isolated
NetworkPolicy (DNS, the API and the Temporal pod only):
`temporal task-queue describe --task-queue analystos-compute-py` → poller
`1@aos-analystos-worker-compute-py-5685df8cd6-74zm7`. `analystos-compute` has the gVisor `worker-compute` poller.

**Refusal through the chart.** A `runc` RuntimeClass (handler `runc`) was created, then
`helm upgrade … --set workers.compute-py.runtimeClassName=runc --set workers.compute-py.requireRuntime=gvisor`
(revision 2). The new pod ran on runc, exited **78** (`lastState.terminated.exitCode=78`), restarted, and logged:

```
{"error": {"code": "worker_misconfigured", "message": "this isolated worker must run under gvisor but the kernel reports
runc; check the pod's runtimeClassName and the node's RuntimeClass handler", "retryable": false,
"details": {"required": "gvisor", "found": "runc"}}}
```

It never appeared as a Temporal poller. `helm rollback aos 1` (revision 3) restored the gVisor pod, and the `runc`
class was deleted.

## Notes

* gVisor runs inside kind on Docker Desktop/WSL2 with runsc's default platform (no `--platform` flag, no KVM device).
  There was no gap to leave open.
* gVisor's bucket layout changed: `release/latest/x86_64/runsc` answers 404. Releases are now
  `<release>/x86_64/gvisor.tar.bz2`, and runsc needs `gvisor-bin/gvisor_sentry` beside it.
* The full `analystos worker` (non-isolated `compute` pool) gets `runtimeClassName` but does not call `check_runtime`.
  Only the isolated entrypoint does. That pool's sandbox is proven by the kernel above, not by a start-up refusal.
* During revision 2 the previous gVisor pod (`…-74zm7`) was still polling, so the queue kept a working worker. Not
  checked: whether a longer-running rollout would have replaced it. Pools have no readiness probe, and a container
  that restarts can count as available between crashes. A readiness check on isolated pools is a possible follow-up.
* Cluster `aos-proof` was deleted after this run (`kind delete cluster --name aos-proof`). This file and the scripts
  reproduce it.

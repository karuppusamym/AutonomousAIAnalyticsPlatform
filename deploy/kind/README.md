# Proof install on kind, with gVisor (FND-004, S07-T1)

A throwaway single-node kind cluster `aos-proof` that runs the packaged Helm chart with
`deploy/helm/analystos/values-kind.yaml`. The compute roles run under gVisor. It proves three things: the chart
installs, the API and web answer, and an isolated compute pod runs under runsc. The pod's own
`workers/isolation.py::check_runtime` accepts gVisor and refuses plain runc. Development only. Production delivery is
[`docs/30-runbooks/14-release-and-deploy.md`](../../docs/30-runbooks/14-release-and-deploy.md).

Needs Docker, `kind`, `kubectl`, `helm` and `openssl`. On Windows, run it from Git Bash; Docker Desktop's WSL2
backend works. The scripts use their own kubeconfig (`.kind/aos-proof.kubeconfig`, git-ignored), so your current
kube context is not changed.

```bash
docker build -f deploy/docker/Dockerfile --build-arg EXTRAS=standard -t analystos/app:0.1.0-kind .
```

```bash
docker build -f web/Dockerfile -t analystos/web:0.1.0-kind web
```

```bash
deploy/kind/proof.sh up
```

```bash
deploy/kind/proof.sh check
```

```bash
deploy/kind/proof.sh down
```

| File | What |
|---|---|
| `cluster.yaml` | the kind cluster (one node, label `analystos.io/gvisor=true`) |
| `install-gvisor.sh` | downloads a pinned gVisor release (default `20261026.0`), checks its sha512, streams runsc, the shim and the `gvisor-bin/` sidecars into the node, and adds the `runsc` runtime to its containerd (systemd cgroups, like kind's runc) |
| `infra.yaml` | Postgres (pgvector, the compose init scripts), Redis and Temporal in the release's namespace, without volumes |
| `proof.sh` | `up`: cluster, gVisor, `kind load` of the two images, infra, the Secret (compose development credentials, random JWT secret), `helm package` + `helm upgrade --install`. `check`: the evidence commands. `down`: delete the cluster |

`values-kind.yaml` turns on `gvisor.enabled` and `gvisor.createRuntimeClass` (handler `runsc`). It runs one replica
each of the API, web, `worker-analysis` (runc), `worker-compute` (gVisor) and the isolated `worker-compute-py`
(gVisor, gets `ANALYSTOS_WORKER_REQUIRE_RUNTIME=gvisor`, and its NetworkPolicy allows DNS, the API and the Temporal
pod only). The other pools are at 0. Images are never pulled (`pullPolicy: Never`).

Notes from the first run (2026-10-29, evidence
[`docs/60-delivery/evidence/fnd-004-s07-t1-kind-gvisor-20261029.md`](../../docs/60-delivery/evidence/fnd-004-s07-t1-kind-gvisor-20261029.md)):

* gVisor's release bucket now ships one tarball per release (`<release>/x86_64/gvisor.tar.bz2`). The older per-binary
  `latest/x86_64/runsc` URLs answer 404. Recent runsc also needs `gvisor-bin/gvisor_sentry` next to it.
* The kind node image has no bzip2, so the tarball is unpacked on the host and streamed in. Git Bash on NTFS drops
  the executable bit, so the modes are set inside the node.

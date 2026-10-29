#!/usr/bin/env bash
# Proof install of the Helm chart on a local kind cluster with gVisor (FND-004, S07-T1). deploy/kind/README.md.
#   deploy/kind/proof.sh up      cluster + gVisor + images + infra + Secret + helm install from the packaged chart
#   deploy/kind/proof.sh check   API health, web page, runsc on the compute pods, check_runtime accept/refuse
#   deploy/kind/proof.sh down    delete the cluster
# Uses its own kubeconfig (KUBECONFIG, default .kind/aos-proof.kubeconfig): your current kube context is untouched.
# Development only: the Secret holds the compose development credentials (env dev) and a random JWT secret.
set -euo pipefail
cd "$(dirname "$0")/../.."
CLUSTER=aos-proof
NODE=$CLUSTER-control-plane
NS=analystos
RELEASE=aos
CHART=deploy/helm/analystos
TAG=0.1.0-kind
export KUBECONFIG="${KUBECONFIG:-$PWD/.kind/aos-proof.kubeconfig}"
mkdir -p "$(dirname "$KUBECONFIG")" .kind

pg() { printf 'postgresql+psycopg://%s@%s' "$1" "$2"; }

up() {
  if ! kind get clusters | grep -qx "$CLUSTER"; then
    kind create cluster --config deploy/kind/cluster.yaml --kubeconfig "$KUBECONFIG" --wait 120s
  fi
  deploy/kind/install-gvisor.sh "$NODE"
  for image in app web; do
    docker image inspect "analystos/$image:$TAG" > /dev/null 2>&1 || {
      echo "build analystos/$image:$TAG first (deploy/kind/README.md)" >&2; exit 1; }
  done
  kind load docker-image "analystos/app:$TAG" "analystos/web:$TAG" --name "$CLUSTER"
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$NS" create configmap pg-init --from-file=deploy/postgres --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$NS" apply -f deploy/kind/infra.yaml
  kubectl -n "$NS" rollout status deploy/postgres deploy/redis deploy/temporal --timeout=300s
  kubectl -n "$NS" create secret generic analystos-secrets --dry-run=client -o yaml \
    --from-literal=ANALYSTOS_DATABASE_URL="$(pg analystos:analystos postgres:5432/analystos)" \
    --from-literal=ANALYSTOS_ANALYTICS_LOADER_URL="$(pg analystos_loader:loader postgres:5432/analytics)" \
    --from-literal=ANALYSTOS_ANALYTICS_READER_URL="$(pg analystos_reader:reader postgres:5432/analytics)" \
    --from-literal=ANALYSTOS_ANALYTICS_BUILDER_URL="$(pg analystos_builder:builder postgres:5432/analytics)" \
    --from-literal=ANALYSTOS_ANALYTICS_WRITER_URL="$(pg analystos_writer:writer postgres:5432/analytics)" \
    --from-literal=ANALYSTOS_REDIS_URL=redis://redis:6379/0 \
    --from-literal=ANALYSTOS_JWT_SECRET="$(openssl rand -hex 32)" \
    --from-literal=ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD='ChangeMe123!' \
    --from-literal=ANALYSTOS_SUPERSET_PASSWORD=unused | kubectl apply -f -
  rm -rf .kind/dist
  helm package "$CHART" --destination .kind/dist
  helm upgrade --install "$RELEASE" .kind/dist/analystos-*.tgz -n "$NS" -f "$CHART/values-kind.yaml" \
    --wait --timeout 15m
}

check() {
  kubectl -n "$NS" get pods -o wide
  kubectl -n "$NS" port-forward deploy/$RELEASE-analystos-api 18000:8000 > /dev/null 2>&1 &
  local pf_api=$!
  kubectl -n "$NS" port-forward deploy/$RELEASE-analystos-web 18080:8080 > /dev/null 2>&1 &
  local pf_web=$!
  sleep 4
  echo "--- GET /api/health (API)"; curl -fsS --retry 10 --retry-delay 2 --retry-all-errors http://127.0.0.1:18000/api/health; echo
  echo "--- GET / (web)"; curl -fsS http://127.0.0.1:18080/ | grep -o '<div id="root"></div>'
  echo "--- GET /api/health through the web proxy"; curl -fsS http://127.0.0.1:18080/api/health | head -c 200; echo
  kill "$pf_api" "$pf_web" 2> /dev/null || true
  for pool in compute compute-py; do
    pod=$(kubectl -n "$NS" get pod -l "app.kubernetes.io/component=worker-$pool" -o jsonpath='{.items[0].metadata.name}')
    echo "--- worker-$pool ($pod): runtimeClassName=$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.spec.runtimeClassName}')"
    kubectl -n "$NS" exec "$pod" -- sh -c 'cat /proc/version; dmesg 2>/dev/null | head -2' || true
    kubectl -n "$NS" exec "$pod" -- python -c \
      "from analystos.workers.isolation import detect_runtime, check_runtime; print('detect_runtime:', detect_runtime()); print('check_runtime(gvisor):', check_runtime({'ANALYSTOS_WORKER_REQUIRE_RUNTIME': 'gvisor'}))"
  done
  echo "--- worker-analysis (runc): check_runtime(gvisor) must refuse"
  kubectl -n "$NS" exec deploy/$RELEASE-analystos-worker-analysis -- python -c \
    "from analystos.workers.isolation import check_runtime, WorkerMisconfigured
try:
    check_runtime({'ANALYSTOS_WORKER_REQUIRE_RUNTIME': 'gvisor'}); print('ACCEPTED (unexpected)')
except WorkerMisconfigured as e:
    print('refused:', e)"
}

down() { kind delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"; }

"${1:-up}"

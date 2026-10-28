#!/usr/bin/env bash
# Offline image bundle for air-gapped installs (P4-S04, spec v3 §8).
#
#   scripts/bundle_images.sh [--build] [--tag 0.1.0] [--out dist/analystos-images-<tag>] [--with-deps]
#
# Saves the AnalystOS images (app, web, superset) - and with --with-deps the third-party images the
# chart's external services usually run (pgvector Postgres, Redis, Temporal) - as one `docker save`
# tarball per image, plus:
#   manifest.json   image, tag, image id, tarball, sha256 and size for each image; the chart + git revision
#   SHA256SUMS      checksums for `sha256sum -c` on the air-gapped side
#   load.sh         loads every tarball and (with REGISTRY=host:port) retags + pushes to the internal registry
#   analystos-<chart version>.tgz   the packaged Helm chart when `helm` is available
# Nothing is pulled at install time: the internal registry is then `image.registry` in values-airgapped.yaml.
set -euo pipefail

TAG="0.1.0"
OUT=""
BUILD=0
WITH_DEPS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --build) BUILD=1; shift ;;
    --with-deps) WITH_DEPS=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/dist/analystos-images-$TAG}"
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
mkdir -p "$OUT"

APP_IMAGES=("analystos/app:$TAG" "analystos/web:$TAG" "analystos-superset:6.1.0")
DEP_IMAGES=("pgvector/pgvector:pg16" "redis:7-alpine" "temporalio/auto-setup:1.25")

if [[ $BUILD -eq 1 ]]; then
  docker build -t "analystos/app:$TAG" -f "$ROOT/deploy/docker/Dockerfile" "$ROOT"
  docker build -t "analystos/web:$TAG" "$ROOT/web"
  docker build -t "analystos-superset:6.1.0" "$ROOT/deploy/superset"
fi

IMAGES=("${APP_IMAGES[@]}")
if [[ $WITH_DEPS -eq 1 ]]; then
  for img in "${DEP_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"  # the only step that needs a network, run it on the connected side
    IMAGES+=("$img")
  done
fi

entries=()
: > "$OUT/SHA256SUMS"
for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "image $img not found locally (use --build, or build it first)" >&2
    exit 1
  fi
  file="$(echo "$img" | tr '/:' '__').tar"
  echo "saving $img -> $file"
  docker save -o "$OUT/$file" "$img"
  sum="$(sha256sum "$OUT/$file" | cut -d' ' -f1)"
  echo "$sum  $file" >> "$OUT/SHA256SUMS"
  id="$(docker image inspect --format '{{.Id}}' "$img")"
  size="$(stat -c %s "$OUT/$file")"
  entries+=("{\"image\": \"$img\", \"id\": \"$id\", \"file\": \"$file\", \"sha256\": \"$sum\", \"bytes\": $size}")
done

chart_file=""
if command -v helm >/dev/null; then
  helm package "$ROOT/deploy/helm/analystos" --destination "$OUT" >/dev/null
  chart_file="$(cd "$OUT" && ls analystos-*.tgz | head -1)"
  sha256sum "$OUT/$chart_file" | sed "s#  $OUT/#  #" >> "$OUT/SHA256SUMS"
fi

revision="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
{
  echo "{"
  echo "  \"bundle\": \"analystos-images-$TAG\","
  echo "  \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"git_revision\": \"$revision\","
  echo "  \"chart\": \"$chart_file\","
  echo "  \"images\": ["
  for i in "${!entries[@]}"; do
    sep=","; [[ $i -eq $((${#entries[@]} - 1)) ]] && sep=""
    echo "    ${entries[$i]}$sep"
  done
  echo "  ]"
  echo "}"
} > "$OUT/manifest.json"

cat > "$OUT/load.sh" <<'LOAD'
#!/usr/bin/env bash
# Air-gapped side: verify, load, and optionally push to the internal registry.
#   REGISTRY=registry.internal:5000 ./load.sh
set -euo pipefail
cd "$(dirname "$0")"
sha256sum -c SHA256SUMS
for tar in *.tar; do docker load -i "$tar"; done
if [[ -n "${REGISTRY:-}" ]]; then
  python3 -c 'import json; [print(i["image"]) for i in json.load(open("manifest.json"))["images"]]' | while read -r img; do
    docker tag "$img" "$REGISTRY/$img"
    docker push "$REGISTRY/$img"
  done
fi
LOAD
chmod +x "$OUT/load.sh"
echo "bundle written to $OUT ($(du -sh "$OUT" | cut -f1))"

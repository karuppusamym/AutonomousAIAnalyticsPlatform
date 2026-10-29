#!/usr/bin/env bash
# Install gVisor (runsc + the containerd shim) into a kind node and register the `runsc` runtime with its
# containerd (S07-T1). Runs on the host (the kind node image has no bzip2):
#   deploy/kind/install-gvisor.sh [node] [release]
# The release tarball is checked against its published sha512 before anything is extracted; the two binaries are
# streamed into the node, and containerd is restarted with a `runsc` runtime (systemd cgroups, like kind's runc).
set -euo pipefail
node="${1:-aos-proof-control-plane}"
release="${2:-20261026.0}"
url="https://storage.googleapis.com/gvisor/releases/release/${release}/x86_64/gvisor.tar.bz2"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/gvisor.tar.bz2" "$url"
curl -fsSL -o "$work/gvisor.tar.bz2.sha512" "$url.sha512"
(cd "$work" && sha512sum -c gvisor.tar.bz2.sha512)
mkdir "$work/x"
tar -xjf "$work/gvisor.tar.bz2" -C "$work/x"
for bin in runsc containerd-shim-runsc-v1; do
  [ -f "$work/x/$bin" ] || { echo "no $bin in the $release tarball" >&2; exit 1; }
done
# The whole tree: runsc, the shim, and (newer releases) the gvisor-bin/ sidecars runsc starts the Sentry from.
# Modes are set in the node: a Windows host (Git Bash on NTFS) does not keep the executable bit.
export MSYS_NO_PATHCONV=1   # Git Bash: keep /usr/local/bin a node path
tar -C "$work/x" -cf - . | docker exec -i "$node" sh -c \
  'tar -C /usr/local/bin --no-same-owner -xf - && chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 /usr/local/bin/gvisor-bin /usr/local/bin/gvisor-bin/*'

docker exec -i "$node" sh -s <<'NODE'
set -eu
config=/etc/containerd/config.toml
cat > /etc/containerd/runsc.toml <<'TOML'
[runsc_config]
  systemd-cgroup = "true"
TOML
if ! grep -q 'runtimes.runsc' "$config"; then
  if grep -Eq '^version *= *3' "$config"; then
    plugin="plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc"   # config format 3
  else
    plugin='plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc'      # config format 2
  fi
  cat >> "$config" <<TOML

[$plugin]
  runtime_type = "io.containerd.runsc.v1"
[$plugin.options]
  TypeUrl = "io.containerd.runsc.v1.options"
  ConfigPath = "/etc/containerd/runsc.toml"
TOML
fi
systemctl restart containerd
for _ in $(seq 1 30); do crictl info > /dev/null 2>&1 && break; sleep 1; done
/usr/local/bin/runsc --version
NODE

#!/usr/bin/env bash
set -euo pipefail
# Install k3s on a throwaway VM, capture reference `kubectl explain` outputs ("goldens"), and dump
# the server's OpenAPI v3 spec. Unlike the upstream files from fetch-swagger.sh, it includes enums.
#
# Run order: this script on the VM, then copy golden/ and openapi-v3/ back (printed at the end).
# Prerequisites: Ubuntu 24.04 VM with 2 GB RAM, curl, run as root (sudo ./golden.sh).
# Usage: sudo ./golden.sh [k3s-version]   e.g. v1.31.4+k3s1; default: k3s stable channel.
# Changes: installs k3s system-wide (service, /usr/local/bin/{k3s,kubectl}, /etc/rancher/k3s).
# Writes: ./golden/*.txt, ./golden/version.json, ./openapi-v3/*.json, ./openapi-v3/VERSION

# 1. Check we run as root (the k3s installer and its kubeconfig need it)
if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0 $*" >&2
  exit 1
fi

# 2. Install k3s  # ⚠️
echo "==> 2. Install k3s" >&2
read -rp "Install k3s system-wide on $(hostname)? [y/N] " ans
[[ $ans == [yY] ]] || exit 1
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${1:-}" sh -  # ⚠️

# 3. Wait for the API server
echo "==> 3. Wait for API server" >&2
timeout 120 bash -c 'until kubectl get --raw /readyz &>/dev/null; do sleep 2; done'

# 4. Capture goldens, one case per output feature
echo "==> 4. Capture goldens" >&2
mkdir -p golden
capture() {
  local name=$1
  shift
  echo "    $name" >&2
  kubectl explain "$@" > "golden/$name.txt"
}
capture pod                      pod                       # top-level kind
capture deployment.spec.strategy deployment.spec.strategy  # nested object
capture service.spec.ports       service.spec.ports        # array of objects
capture pod.metadata.labels      pod.metadata.labels       # map leaf
capture pod.spec.restartPolicy   pod.spec.restartPolicy    # primitive leaf with ENUM
capture ingress                  ingress                   # non-core group
capture hpa.autoscaling-v2       horizontalpodautoscaler --api-version=autoscaling/v2
capture deployment.spec.recursive deployment.spec --recursive
kubectl version -o json > golden/version.json

# 5. Dump the OpenAPI v3 spec, one file per group/version, named like the upstream files
echo "==> 5. Dump OpenAPI v3 spec" >&2
index=$(kubectl get --raw /openapi/v3)
mapfile -t paths < <(grep -o '/openapi/v3/[^"?]*' <<<"$index")
mkdir -p openapi-v3
for p in "${paths[@]}"; do
  rel=${p#/openapi/v3/}
  kubectl get --raw "$p" > "openapi-v3/${rel//\//__}_openapi.json"
done
server=$(kubectl version | awk '/Server Version/ {print $3}')
echo "${server%%+*}" > openapi-v3/VERSION
echo "    ${#paths[@]} files" >&2

# 6. Print next steps
echo "==> 6. Print next steps" >&2
cat <<EOF

Server version: $server
Locally, remove openapi-v3/ first (files from another source must not mix in), then:
  scp -r root@<vm>:$PWD/golden root@<vm>:$PWD/openapi-v3 .
EOF

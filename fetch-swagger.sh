#!/usr/bin/env bash
set -euo pipefail
# Download upstream Kubernetes OpenAPI v3 specs (one JSON file per API group/version) for one
# release tag. This is the spec kubectl explain renders; v2 swagger.json lacks enums.
#
# Run order: run this instead of golden.sh when you have no cluster. Needs no VM, but no enums.
# Prerequisites: curl 7.73+, jq.
# Usage: ./fetch-swagger.sh [tag]   e.g. v1.31.4; default: latest stable release.
# Changes: nothing remote.
# Writes: ./openapi-v3/*.json, ./openapi-v3/VERSION (the tag it came from)

if [[ -e openapi-v3 ]]; then
  echo "openapi-v3/ exists; remove it first so files from another tag don't mix in" >&2
  exit 1
fi

# 1. Resolve the tag
tag=${1:-$(curl -fsSL https://dl.k8s.io/release/stable.txt)}

# 2. List the spec files for that tag (unauthenticated GitHub API: 60 requests/hour)
listing=$(curl -fsSL \
  "https://api.github.com/repos/kubernetes/kubernetes/contents/api/openapi-spec/v3?ref=$tag")
mapfile -t urls < <(jq -r '.[].download_url' <<<"$listing")

# 3. Download them
mkdir openapi-v3
curl -fsSL --remote-name-all --output-dir openapi-v3 "${urls[@]}"
echo "$tag" > openapi-v3/VERSION
echo "Saved ${#urls[@]} OpenAPI v3 files for $tag" >&2

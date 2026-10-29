#!/usr/bin/env bash
# Build the frontend when it exists and record the build ID that /healthz reports.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ -f frontend/package.json ]; then
  (cd frontend && npm ci && npm run build)
else
  echo "build: no frontend/package.json, skipping the frontend build"
fi

mkdir -p var
git describe --always --dirty > var/build_id
echo "build: $(cat var/build_id)"

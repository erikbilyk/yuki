#!/usr/bin/env bash
# Write dist/yuki-<ref>.zip from a git ref, without .git, .env or var/, and print its SHA-256.
# Usage: scripts/package.sh <git-ref>
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/package.sh <git-ref>" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

ref="$1"
git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || {
  echo "package: unknown ref '$ref'" >&2
  exit 1
}

name="yuki-${ref//\//-}"
mkdir -p dist
out="dist/$name.zip"
rm -f "$out"

git archive --format=zip --prefix="$name/" --output="$out" "$ref" \
  -- . ':(exclude).env' ':(exclude)var'

if unzip -Z1 "$out" | grep -E "^$name/(\.git/|\.env$|var/)"; then
  echo "package: archive holds a path that must not ship" >&2
  rm -f "$out"
  exit 1
fi

echo "$out"
sha256sum "$out"

#!/usr/bin/env bash
# Scan tracked files for key-like strings, private key blocks and secret assignments.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

status=0
report() {
  local label="$1" pattern="$2"
  shift 2
  local hits
  hits="$(git ls-files -z "$@" | xargs -0 -r grep -InE -e "$pattern" || true)"
  if [ -n "$hits" ]; then
    echo "$hits" | cut -d: -f1,2 | sed "s/\$/  ($label)/"
    status=1
  fi
}

report "api key" 'sk-[[:alnum:]_]{20,}'
report "private key" '-----BEGIN [A-Z ]*PRIVATE KEY'
report "secret assignment" \
  '[A-Z0-9_]*(KEY|SECRET|TOKEN)[A-Z0-9_]*[[:space:]]*=[[:space:]]*["'"'"']?[^[:space:]"'"'"']' \
  ':!:.env.example'

if [ "$status" -eq 0 ]; then
  echo "secret scan ok"
else
  echo "secret scan failed"
fi
exit "$status"

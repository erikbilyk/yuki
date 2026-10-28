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
# A value is a quoted string, which may carry a Python prefix such as r, b or f, or a bare word
# of three or more characters. A value starting with a bracket is a container literal (a tuple of
# dict keys) or a regex character class (r"[a-z]") and is not reported.
first='[^][[:space:]"'"'"'({]'
quoted='[rRbBuUfF]{0,2}["'"'"']'"$first"
bare="$first"'[^"'"'"'][^"'"'"']'
report "secret assignment" \
  "[A-Z0-9_]*(KEY|SECRET|TOKEN)[A-Z0-9_]*[[:space:]]*=[[:space:]]*($quoted|$bare)" \
  ':!:.env.example'

if [ "$status" -eq 0 ]; then
  echo "secret scan ok"
else
  echo "secret scan failed"
fi
exit "$status"

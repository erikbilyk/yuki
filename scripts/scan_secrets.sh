#!/usr/bin/env bash
# Scan tracked files for key-like strings, private key blocks and secret assignments.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

status=0
# Each match is printed as file:line:text; matches whose text fits the skip pattern are dropped.
report() {
  local label="$1" pattern="$2" skip="$3"
  shift 3
  local hits
  hits="$(git ls-files -z "$@" | xargs -0 -r grep -InoE -e "$pattern" | grep -vE -e "$skip" \
    | cut -d: -f1,2 | uniq || true)"
  if [ -n "$hits" ]; then
    echo "$hits" | sed "s/\$/  ($label)/"
    status=1
  fi
}

none='^$'

report "api key" 'sk-[[:alnum:]_]{20,}' "$none"
report "private key" '-----BEGIN [A-Z ]*PRIVATE KEY' "$none"
# A value is a quoted string, which may carry a Python prefix such as r, b or f, or a bare word
# of three or more characters. A value starting with a bracket is a container literal (a tuple of
# dict keys) or a regex character class (r"[a-z]") and is not reported. A value that is a function
# call (re.compile(...)) is not a literal either: grep picks the longest alternative, so such a
# match ends in "(" and is skipped.
first='[^][[:space:]"'"'"'({]'
quoted='[rRbBuUfF]{0,2}["'"'"']'"$first"
bare="$first"'[^"'"'"'][^"'"'"']'
call='[A-Za-z_][A-Za-z0-9_.]*\('
report "secret assignment" \
  "[A-Z0-9_]*(KEY|SECRET|TOKEN)[A-Z0-9_]*[[:space:]]*=[[:space:]]*($quoted|$bare|$call)" \
  "=[[:space:]]*$call\$" \
  ':!:.env.example'

if [ "$status" -eq 0 ]; then
  echo "secret scan ok"
else
  echo "secret scan failed"
fi
exit "$status"

#!/usr/bin/env bash
# Lint, format check, tests, write-path scan and secret scan.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

failed=()
run() {
  local name="$1"
  shift
  echo "== $name"
  "$@" || failed+=("$name")
}

tests=(tests/a)
for dir in tests/b/engines tests/b/goldens tests/b/sources; do
  [ -d "backend/$dir" ] && tests+=("$dir")
done

run ruff-check uv --project backend run ruff check backend
run ruff-format uv --project backend run ruff format --check backend
run pytest bash -c 'cd backend && uv run pytest -q "$@"' _ "${tests[@]}"
run write-scan python3 scripts/scan_writes.py
run secret-scan scripts/scan_secrets.sh

if [ "${#failed[@]}" -eq 0 ]; then
  echo "check: all passed"
else
  echo "check: FAILED (${failed[*]})"
  exit 1
fi

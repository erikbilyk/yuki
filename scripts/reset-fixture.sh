#!/usr/bin/env bash
# Delete this working copy's database and var/ logs, then load fixture_v1.
# Usage: scripts/reset-fixture.sh [VARIANT]. Stop the backend first; a running backend
# resets through POST /sim/reset instead.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

setting() {
  uv --project backend run python -c "from wg.settings import load_settings; print(load_settings().$1)"
}

db="$(setting db_path)"
rm -f "$db" "$db-wal" "$db-shm" "$(setting fault_wait_file)"
rm -f var/*.log var/*.jsonl

args=(load --profile fixture_v1 --db "$db")
[ $# -eq 0 ] || args+=(--variant "$1")
cd backend
uv run python -m wg.k6.fixture "${args[@]}"

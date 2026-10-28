#!/usr/bin/env bash
# Start the backend with reload on WG_PORT and, when a frontend exists, the Vite dev server.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

setting() {
  uv --project backend run python -c "from wg.settings import load_settings; print(load_settings().$1)"
}

web_pid=""
cleanup() {
  [ -z "$web_pid" ] || kill "$web_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ -f frontend/package.json ]; then
  web_port="$(setting web_port)"
  (cd frontend && exec npm run dev -- --port "$web_port") &
  web_pid=$!
fi

cd backend
uv run python -m wg.api.serve --reload

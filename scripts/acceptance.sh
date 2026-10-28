#!/usr/bin/env bash
# Run the acceptance harness; options are passed through (--gate, --only, --repeat, --layers).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/backend"

if [ ! -f tests/b/harness/run.py ]; then
  echo "acceptance harness not present"
  exit 1
fi
exec uv run python -m tests.b.harness.run "$@"

#!/usr/bin/env bash
# Evaluation runner: eval.sh live|comparison|score [--repeat N] [--only IDS] [more options].
# Options are passed through; see evals/README.md.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

case "${1:-}" in
  live | comparison | score) ;;
  *)
    echo "usage: scripts/eval.sh live|comparison|score [--repeat N] [--only IDS] [options]" >&2
    exit 2
    ;;
esac

exec uv run --project backend python -m evals.runner "$@"

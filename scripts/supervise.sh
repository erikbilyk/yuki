#!/usr/bin/env bash
# Run the backend and start it again after any exit except Ctrl+C, at most 20 restarts.
# Each start runs restart recovery (SPEC §12.8); /sim/crash ends the process for a demo.
# Output and one line per start and exit go to var/supervise.log.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log="$root/var/supervise.log"
max_restarts=20
mkdir -p "$root/var"

note() {
  printf '%s supervise: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$log"
}

child=""
stopping=0
stop() {
  stopping=1
  [ -z "$child" ] || kill -TERM "$child" 2>/dev/null || true
}
trap stop INT TERM

cd "$root/backend"
restarts=0
while :; do
  note "start $((restarts + 1))"
  uv run python -m wg.api.serve >>"$log" 2>&1 &
  child=$!
  # A trapped signal interrupts `wait`; wait again until the backend has really exited.
  status=0
  while :; do
    wait "$child"
    status=$?
    kill -0 "$child" 2>/dev/null || break
  done
  child=""
  note "exit status $status"
  if [ "$stopping" -eq 1 ]; then
    note "stopped"
    exit 0
  fi
  if [ "$restarts" -ge "$max_restarts" ]; then
    note "giving up after $max_restarts restarts"
    exit 1
  fi
  restarts=$((restarts + 1))
  sleep 1
done

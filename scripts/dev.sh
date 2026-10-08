#!/usr/bin/env bash
# `just dev` (dev-environment §5): the API, the worker and the Vite dev server in
# one terminal, after migrating the dev database in DATA_DIR.
#   scripts/dev.sh [VITE_ARGS...]     e.g. --host, to reach it from a phone
# The API reloads and the worker restarts when backend code changes; Vite
# hot-reloads the frontend. Each line is prefixed with the process it came from.
# Ctrl-C stops all three, and if one exits, the others are stopped too.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"  # so .env and ./data resolve here, as for `just api`

backend=(uv run --no-sync --project backend)

echo "Migrating the database in DATA_DIR"
if ! migrate_log="$("${backend[@]}" alembic -c backend/alembic.ini upgrade head 2>&1)"; then
  echo "$migrate_log" >&2
  # Most likely a branch with a newer migration ran `just dev` on this database.
  if [[ "$migrate_log" == *"Can't locate revision"* ]]; then
    cat >&2 <<'EOF'

The dev database was migrated by another branch, to a revision this one
doesn't have. Either check out that branch and run
  uv run --project backend alembic -c backend/alembic.ini downgrade <a revision this branch has>
or, since the dev database holds only test data, delete photo-triage.db in
DATA_DIR and run `just dev` again.
EOF
  fi
  exit 1
fi
echo "$migrate_log"

# Each process gets its own process group, so Ctrl-C reaches only this script,
# which then stops each one once. The worker in particular must get one signal:
# a second makes it stop mid-job.
set -m
pids=()

prefix() {
  local label
  label="$(printf '%-6s|' "$1")"
  sed -u "s/^/$label /"
}

start() {
  local name="$1"
  shift
  # exec, so the PID is the command's own and not a subshell's.
  (exec "$@") </dev/null > >(prefix "$name") 2>&1 &
  pids+=("$!")
}

start api "${backend[@]}" python -m photo_triage.api --reload
# watchfiles stops the worker with SIGINT, so it finishes its job first.
start worker "${backend[@]}" watchfiles --filter python --sigint-timeout 30 \
  "python -m photo_triage.worker" backend/src
start web pnpm --dir frontend exec vite "$@"

stopping=false
stop() {
  $stopping && return
  stopping=true
  echo "Stopping the API, the worker and the Vite dev server"
  # The API and Vite as whole groups (uvicorn's reloader and its server, pnpm
  # and Vite). The worker through uv, which passes the signal to watchfiles alone.
  kill -TERM -- "-${pids[0]}" "${pids[1]}" "-${pids[2]}" 2>/dev/null || true
}
trap stop INT TERM

status=0
wait -n || status=$?
stop
# Without bash's "Terminated" notice for each job.
{ wait || true; } 2>/dev/null
exit "$status"

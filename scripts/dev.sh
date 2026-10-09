#!/usr/bin/env bash
# `just dev` (dev-environment §5): the API, the worker, the Vite dev server and
# Ollama in one terminal, after the migrate step on the dev database in DATA_DIR.
#   scripts/dev.sh [--no-ollama] [VITE_ARGS...]     e.g. --host, to reach it from a phone
# Before starting anything it checks that Docker answers, and says what to do if not.
# The API reloads and the worker restarts when backend code changes; Vite
# hot-reloads the frontend. Ollama runs in Docker, from compose.dev.yaml;
# --no-ollama leaves it out, as the scratch stack does. Each line is prefixed
# with the process it came from. Ctrl-C stops them all, and if one exits, the
# others are stopped too.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"  # so .env and ./data resolve here, as for `just api`

ollama=true
if [[ "${1:-}" == --no-ollama ]]; then
  ollama=false
  shift
fi

# Ollama runs in Docker, so check Docker answers before starting anything.
if $ollama; then
  if ! command -v docker >/dev/null 2>&1; then
    docker_problem="The docker command isn't installed in this WSL distro. Enable WSL integration for it in Docker Desktop (Settings → Resources → WSL integration), then reopen the terminal (README §2)."
  elif ! docker_out="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
    if [[ "$docker_out" == *"permission denied"* ]]; then
      docker_problem="Docker is running, but this shell isn't in the 'docker' group yet. Run 'wsl --shutdown' from Windows and reopen Ubuntu (or 'newgrp docker' for one shell)."
    else
      docker_out="$(tr -s '\n' ' ' <<<"$docker_out" | sed 's/^ *//; s/ *$//')"
      docker_problem="Docker isn't answering. Start Docker Desktop on Windows and check that WSL integration is on for this distro (README §2). Docker said: $docker_out"
    fi
  fi
  if [[ -n "${docker_problem:-}" ]]; then
    cat >&2 <<EOF
just dev runs Ollama in Docker, and Docker isn't available:
  $docker_problem
To work without Ollama for now, run 'just dev --no-ollama'.
EOF
    exit 1
  fi
fi

backend=(uv run --no-sync --project backend)

# As `just db-migrate`: it backs the database up first if it needs upgrading.
status=0
"${backend[@]}" python -m photo_triage.db migrate || status=$?
if ((status == 1)); then
  # Most likely a branch with a newer migration ran `just dev` on this database.
  cat >&2 <<'EOF'

If another branch migrated the dev database, either check out that branch and run
  uv run --project backend alembic -c backend/alembic.ini downgrade <a revision this branch has>
or restore the backup the migrate step made before that branch's migration, from
DATA_DIR/backups (dev-environment §5, `just db-migrate`). Or, since the dev
database holds only test data, delete photo-triage.db in DATA_DIR.
EOF
fi
((status == 0)) || exit "$status"

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
if $ollama; then
  # In the foreground, so its log is prefixed too. Stopping it stops the container.
  start ollama docker compose -f compose.dev.yaml up
fi

stopping=false
stop() {
  $stopping && return
  stopping=true
  echo "Stopping the API, the worker, the Vite dev server${pids[3]:+ and Ollama}"
  # The API and Vite as whole groups (uvicorn's reloader and its server, pnpm
  # and Vite). The worker through uv, which passes the signal to watchfiles
  # alone. Compose, which stops its container.
  kill -TERM -- "-${pids[0]}" "${pids[1]}" "-${pids[2]}" ${pids[3]:+"${pids[3]}"} 2>/dev/null || true
}
trap stop INT TERM

status=0
wait -n || status=$?
stop
# Without bash's "Terminated" notice for each job.
{ wait || true; } 2>/dev/null
exit "$status"

#!/usr/bin/env bash
# A throwaway copy of the app for looking at pages (`just preview`, `just shot`;
# dev-environment §5): `scripts/dev.sh` on free ports, against empty photo,
# trash and data directories in a temporary folder.
#   scripts/scratch_stack.sh [--state FILE]
# It reads no `.env` (ENV_FILE=/dev/null), so it never sees the developer's
# photo library or dev database, which makes its pages safe to publish. The
# noop stage is on, for `just shot --noop N`.
# Once the page answers it prints "Scratch stack ready: URL" and, with --state,
# writes {"url", "root", "env"} to FILE for `just shot`. Ctrl-C or TERM stops
# the stack and removes the folder and FILE.
set -euo pipefail

state=""
while (($#)); do
  case "$1" in
    --state) state="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//p}' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d -t photo-triage-scratch.XXXXXX)"
mkdir "$scratch/photos" "$scratch/trash" "$scratch/data"

dev_pid=""
# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  # A second Ctrl-C or TERM mustn't cut the cleanup short.
  trap '' INT TERM
  if [[ -n "$dev_pid" ]]; then
    kill -TERM "$dev_pid" 2>/dev/null || true
    wait "$dev_pid" 2>/dev/null || true
  fi
  rm -rf "$scratch"
  if [[ -n "$state" ]]; then rm -f "$state"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Two ports nothing is listening on, from the kernel.
read -r api_port web_port < <(python3 - <<'EOF'
import socket

sockets = [socket.socket() for _ in range(2)]
for s in sockets:
    s.bind(("127.0.0.1", 0))
print(*(s.getsockname()[1] for s in sockets))
EOF
)

export ENV_FILE=/dev/null
export PHOTO_DIR="$scratch/photos" TRASH_DIR="$scratch/trash" DATA_DIR="$scratch/data"
export APP_PORT="$api_port" WORKER_NOOP_STAGE=true
url="http://localhost:$web_port"

echo "Scratch stack in $scratch, at $url"
"$root/scripts/dev.sh" --no-ollama --port "$web_port" --strictPort &
dev_pid=$!

# Ready when the page and the API behind Vite's proxy both answer.
for _ in $(seq 120); do
  if ! kill -0 "$dev_pid" 2>/dev/null; then
    dev_pid=""
    echo "The scratch stack stopped before it was ready" >&2
    exit 1
  fi
  if curl -fs -o /dev/null "$url/" && curl -fs -o /dev/null "$url/api/activity"; then
    break
  fi
  sleep 0.5
done
if ! curl -fs -o /dev/null "$url/api/activity"; then
  echo "The scratch stack didn't answer at $url within 60 s" >&2
  exit 1
fi

if [[ -n "$state" ]]; then
  mkdir -p "$(dirname "$state")"
  python3 - "$state" "$url" "$scratch" <<'EOF'
import json
import os
import sys

path, url, scratch = sys.argv[1:]
names = ["ENV_FILE", "PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "APP_PORT", "WORKER_NOOP_STAGE"]
with open(path + ".tmp", "w") as f:
    json.dump({"url": url, "root": scratch, "env": {n: os.environ[n] for n in names}}, f)
os.replace(path + ".tmp", path)
EOF
fi
echo "Scratch stack ready: $url"

status=0
wait "$dev_pid" || status=$?
dev_pid=""
exit "$status"

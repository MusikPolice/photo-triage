#!/usr/bin/env bash
# Smoke test for the image (dev-environment §8, the `docker` CI job):
#   scripts/docker_smoke.sh IMAGE
# Runs the migrate step, starts the app, checks /api/health, the frontend and
# one of its pages, the pinned exiftool and ffmpeg (against versions.env), the
# user, what's left out of the image, and that the worker runs from it. Then
# runs compose.yaml's migrate, app and worker services on the image.
set -euo pipefail

image="${1:?usage: $0 IMAGE}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=../versions.env
source "$here/../versions.env"
exiftool_version="${EXIFTOOL_VERSION:-}"
ffmpeg_version="${FFMPEG_VERSION:-}"
[[ -n "$exiftool_version" && -n "$ffmpeg_version" ]] || {
  echo "Couldn't read the pinned versions from versions.env" >&2
  exit 1
}

failures=0
ok() { echo "ok: $1"; }
fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}
in_image() { docker run --rm "$image" "$@"; }

# --- The app ---------------------------------------------------------------

# The app refuses a database that isn't migrated, so the migrate step runs first,
# into a volume the app then uses, as Compose will run them.
volume="$(docker volume create)"
container=""
stack_dir=""
cleanup() {
  [[ -z "$container" ]] || docker rm --force "$container" >/dev/null
  docker volume rm "$volume" >/dev/null
  if [[ -n "$stack_dir" ]]; then
    stack down --volumes --remove-orphans >/dev/null 2>&1 || true
    # The containers' files belong to their user, which may not be this one.
    docker run --rm --user 0 --volume "$stack_dir:/stack" "$image" \
      sh -c 'rm -rf /stack/photos /stack/data' || true
    rm -rf "$stack_dir"
  fi
}
trap cleanup EXIT
with_data() { docker run --rm --volume "$volume:/data" "$image" "$@"; }

if with_data python -m photo_triage.api >/dev/null 2>&1; then
  fail "the app started on a database that isn't migrated"
else
  ok "the app refuses a database that isn't migrated"
fi
if with_data python -m photo_triage.db migrate; then
  ok "the migrate step"
else
  fail "the migrate step didn't run"
fi

container="$(docker run --detach --volume "$volume:/data" --publish 127.0.0.1::8000 "$image")"
base_url="http://$(docker port "$container" 8000/tcp | head -1)"

for _ in $(seq 60); do
  curl --silent --fail "$base_url/api/health" >/dev/null && break
  sleep 0.5
done
if [[ "$(curl --silent --fail "$base_url/api/health")" == '{"status":"ok"}' ]]; then
  ok "GET /api/health"
else
  fail "GET /api/health didn't answer ok. The container's log:"
  docker logs "$container" >&2
fi

index="$(curl --silent --fail "$base_url/" || true)"
if [[ "$index" == *"<title>photo-triage</title>"* ]]; then
  ok "GET / serves index.html"
else
  fail "GET / didn't serve the frontend's index.html"
fi
if [[ "$(curl --silent --fail "$base_url/activity" || true)" == "$index" ]]; then
  ok "GET /activity serves index.html"
else
  fail "GET /activity didn't serve the frontend's index.html"
fi
asset="$(grep -o '/assets/[^"]*\.js' <<<"$index" | head -1 || true)"
if [[ -n "$asset" ]] && curl --silent --fail --output /dev/null "$base_url$asset"; then
  ok "GET $asset"
else
  fail "the frontend's script isn't served (${asset:-none in index.html})"
fi

user="$(docker exec "$container" id -u)"
if [[ "$user" != 0 ]]; then ok "runs as UID $user"; else fail "runs as root"; fi

# --- Tools -----------------------------------------------------------------

have="$(in_image exiftool -ver || true)"
if [[ "$have" == "$exiftool_version" ]]; then
  ok "exiftool $have"
else
  fail "exiftool $have, want $exiftool_version"
fi

for tool in ffmpeg ffprobe; do
  have="$(in_image "$tool" -version 2>/dev/null | head -1 | awk '{print $3}' || true)"
  if [[ "$have" == "$ffmpeg_version"* ]]; then
    ok "$tool $have"
  else
    fail "$tool $have, want $ffmpeg_version"
  fi
done

# --- What's in the image ---------------------------------------------------

if in_image sh -c 'command -v node || command -v pnpm || command -v npm' >/dev/null; then
  fail "the Node toolchain is in the image"
else
  ok "no Node toolchain"
fi

# The export group (torch, open_clip) and the dev group stay out; the runtime
# dependencies load, including the native libraries they need.
if docker run --rm --interactive "$image" python - <<'EOF'; then
import importlib
import importlib.util

left_out = ["torch", "torchvision", "open_clip", "onnxscript", "pytest", "pyright", "ruff"]
present = [name for name in left_out if importlib.util.find_spec(name) is not None]
assert not present, f"in the image: {present}"
for name in ["cv2", "onnxruntime", "PIL", "pillow_heif", "sklearn", "umap", "insightface"]:
    importlib.import_module(name)
EOF
  ok "runtime dependencies only, and they import"
else
  fail "the image's Python packages (see above)"
fi

# --- The worker, from the same image ---------------------------------------

if with_data python -m photo_triage.worker --once; then
  ok "the worker runs (--once, on the migrated database)"
else
  fail "the worker didn't run"
fi

# --- The Compose stack -------------------------------------------------------

# compose.yaml on this image, with the library and DATA_DIR bind-mounted from
# temporary folders. Not Ollama: nothing uses it yet, and its image is large.
stack_dir="$(mktemp -d)"
mkdir "$stack_dir/photos" "$stack_dir/data"
chmod 777 "$stack_dir/photos" "$stack_dir/data"  # for the containers' UID 1000
stack_port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
stack() {
  COMPOSE_PROJECT_NAME="photo-triage-smoke-$$" PHOTO_TRIAGE_IMAGE="$image" \
    PHOTO_DIR="$stack_dir/photos" DATA_DIR="$stack_dir/data" APP_ENV_FILE=/dev/null \
    APP_PORT="$stack_port" docker compose -f "$here/../compose.yaml" "$@"
}

if stack up --detach --wait --wait-timeout 120 app worker; then
  ok "compose: the migrate service ran, then the app (healthy) and the worker started"
else
  fail "compose: the stack didn't start. Its log:"
  stack logs >&2 || true
fi
if [[ "$(curl --silent --fail "http://127.0.0.1:$stack_port/api/health" || true)" == '{"status":"ok"}' ]]; then
  ok "compose: GET /api/health on APP_PORT"
else
  fail "compose: GET /api/health on APP_PORT didn't answer ok"
fi
if [[ -f "$stack_dir/data/photo-triage.db" ]]; then
  ok "compose: the database is in the DATA_DIR bind mount"
else
  fail "compose: no database in the DATA_DIR bind mount"
fi
stack_log="$(stack logs --no-color app worker 2>&1 || true)"
if [[ "$stack_log" == *"TRASH_DIR=/photos/.photo-triage-trash "* && "$stack_log" != *"different filesystem"* ]]; then
  ok "compose: the trash is in the library, on its filesystem"
else
  fail "compose: TRASH_DIR isn't /photos/.photo-triage-trash, or is on another filesystem"
fi
second="$(stack run --rm --no-deps worker 2>&1)" && second_status=0 || second_status=$?
if ((second_status != 0)) && [[ "$second" == *"A worker is already running"* ]]; then
  ok "compose: a second worker exits ($second_status) while one runs"
else
  fail "compose: a second worker didn't exit with 'A worker is already running' ($second_status): $second"
fi

if ((failures)); then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "All checks passed"

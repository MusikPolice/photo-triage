#!/usr/bin/env bash
# Smoke test for the image (dev-environment §8, the `docker` CI job):
#   scripts/docker_smoke.sh IMAGE
# Starts the app, checks /api/health and the frontend, the pinned exiftool and
# ffmpeg (against scripts/bootstrap.sh), the user, what's left out of the image,
# and that the worker runs from it.
set -euo pipefail

image="${1:?usage: $0 IMAGE}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pin() { sed -n "s/^$1=\"\(.*\)\"$/\1/p" "$here/bootstrap.sh"; }
exiftool_version="$(pin EXIFTOOL_VERSION)"
ffmpeg_version="$(pin FFMPEG_VERSION)"
[[ -n "$exiftool_version" && -n "$ffmpeg_version" ]] || {
  echo "Couldn't read the pinned versions from scripts/bootstrap.sh" >&2
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

container="$(docker run --detach --publish 127.0.0.1::8000 "$image")"
trap 'docker rm --force "$container" >/dev/null' EXIT
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

if in_image sh -c 'alembic -c backend/alembic.ini upgrade head \
    && python -m photo_triage.worker --once'; then
  ok "the worker runs (--once, on a fresh database)"
else
  fail "the worker didn't run"
fi

if ((failures)); then
  echo "$failures check(s) failed" >&2
  exit 1
fi
echo "All checks passed"

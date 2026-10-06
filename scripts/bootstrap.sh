#!/usr/bin/env bash
# Bootstrap a photo-triage development environment on a fresh WSL2 Ubuntu 24.04.
#
# Assumes nothing beyond git, bash, and sudo rights. Idempotent: safe to re-run;
# each step checks what is already in place and skips it.
#
#   ./scripts/bootstrap.sh           install whatever is missing
#   ./scripts/bootstrap.sh --check   report problems only; changes nothing, needs no
#                                    sudo or network; exits 1 if anything needs fixing
#                                    (this is what `just doctor` runs)
#   ./scripts/bootstrap.sh --check --quiet
#                                    print only the problems list, nothing when OK
#                                    (used by the Claude Code SessionStart hook)
#
# What it does (docs/dev-environment.md §2):
#   1. apt prerequisites (curl, build-essential, cifs-utils, ...)
#   2. mise, plus shell activation in ~/.bashrc
#   3. `mise install` for the runtimes pinned in mise.toml (Python, uv, Node, pnpm, just)
#   4. pinned exiftool and ffmpeg into ~/.local/bin (apt versions are too old)
#   5. pre-commit; then `uv sync`, `pnpm install`, `pre-commit install` once those
#      project files exist
#   6. model weights into ~/.cache/photo-triage/models
#   7. checks it can't fix: Docker reachable, photo mounts present and read-only
#
# Out of scope: installing Docker (Docker Desktop on Windows with WSL integration)
# and creating the /mnt/pictures and /mnt/sample-pictures mounts (need credentials;
# see README §5).

set -euo pipefail

CHECK=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done
((CHECK || !QUIET)) || { echo "--quiet only works with --check" >&2; exit 2; }

# --quiet: silence the step-by-step report; fd 3 keeps the real stdout for the
# final problems list (and for die, so a fatal error is never swallowed).
exec 3>&1
if ((QUIET)); then exec >/dev/null 2>&1; fi

# ---------------------------------------------------------------------------
# Pinned versions. Bumping one means updating its checksum too.
# ---------------------------------------------------------------------------

EXIFTOOL_VERSION="13.59"
EXIFTOOL_SHA256="668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a"
# exiftool.org only serves the newest release; SourceForge keeps every version.
EXIFTOOL_URL="https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download"

# Static build (Ubuntu 24.04's apt ffmpeg is 6.1). The release URL is rolling,
# so the old-releases path is tried first and the checksum guards against drift.
FFMPEG_VERSION="7.0.2"
FFMPEG_SHA256="abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67"
FFMPEG_URLS=(
  "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-amd64-static.tar.xz"
  "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
)

PRE_COMMIT_VERSION="4.6.2"

# InsightFace SCRFD + ArcFace pack (non-commercial licence; personal use only).
BUFFALO_L_URL="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
BUFFALO_L_SHA256="80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"

# LAION aesthetic predictor head for CLIP ViT-B/32 embeddings.
AESTHETIC_URL="https://github.com/LAION-AI/aesthetic-predictor/raw/main/sa_0_4_vit_b_32_linear.pth"
AESTHETIC_SHA256="c7b14cead230694acc7b9447974d3cad78003c72da032e402a303b6c2429e85f"

APT_PACKAGES=(
  ca-certificates curl git unzip xz-utils perl
  build-essential pkg-config
  cifs-utils
)
# No libheif: the pillow-heif wheel bundles its own.

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BIN="$HOME/.local/bin"
TOOLS_DIR="$HOME/.local/share/photo-triage/tools"
MODELS_DIR="$HOME/.cache/photo-triage/models"
MISE="$LOCAL_BIN/mise"
BASHRC_MARKER="# >>> photo-triage bootstrap >>>"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

if [[ -t 1 ]] && ! ((QUIET)); then
  BOLD=$'\e[1m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; RESET=$'\e[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s  ✓ %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET" >&2; WARNINGS+=("$*"); }
die()  {
  if ((QUIET)); then printf 'Dev environment check failed (scripts/bootstrap.sh --check): %s\n' "$*" >&3
  else printf '%s  ✗ %s%s\n' "$RED" "$*" "$RESET" >&2; fi
  exit 1
}
# fail: a problem that needs fixing. Recorded so --check can list them and exit 1.
fail() { printf '%s  ✗ %s%s\n' "$RED" "$*" "$RESET" >&2; FAILURES+=("$*"); }

# needs_fix MESSAGE — in --check mode, record MESSAGE as a failure and return 1
# so the caller skips its install branch; otherwise return 0 and let it install.
needs_fix() {
  if ((CHECK)); then fail "$1"; return 1; fi
  return 0
}

WARNINGS=()
FAILURES=()
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

FIX_HINT="run ./scripts/bootstrap.sh"

# download URL DEST SHA256 — fetch to DEST and verify, or fail.
download() {
  local url="$1" dest="$2" sha="$3"
  curl -fL --retry 3 --retry-delay 2 --progress-bar -o "$dest" "$url" || return 1
  if ! echo "$sha  $dest" | sha256sum --check --status; then
    echo "    checksum mismatch for $url" >&2
    rm -f "$dest"
    return 1
  fi
}

# Run a command with the mise-managed tools on PATH.
mx() { (cd "$REPO_ROOT" && "$MISE" exec -- "$@"); }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------

step "Checking platform"

[[ $EUID -ne 0 ]] || die "Run as your normal user, not root (sudo is used where needed)."
[[ "$(uname -m)" == "x86_64" ]] || die "Only x86_64 is supported (pinned ffmpeg build is amd64)."

if [[ -r /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    warn "Expected Ubuntu 24.04, found ${PRETTY_NAME:-unknown}. Continuing anyway."
  else
    ok "$PRETTY_NAME"
  fi
fi

if grep -qi microsoft /proc/version 2>/dev/null; then
  ok "Running under WSL"
else
  warn "Not running under WSL; the docs assume WSL2."
fi

case "$REPO_ROOT" in
  /mnt/*) warn "Repo is under $REPO_ROOT. Move it to ~/src for usable install and test speed." ;;
esac

# ---------------------------------------------------------------------------
# 1. apt prerequisites
# ---------------------------------------------------------------------------

step "apt packages"

missing=()
for pkg in "${APT_PACKAGES[@]}"; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done

if ! ((${#missing[@]})); then
  ok "All present"
elif needs_fix "Missing apt packages: ${missing[*]} ($FIX_HINT)"; then
  echo "  Installing: ${missing[*]}"
  if ! sudo -n true 2>/dev/null && [[ ! -t 0 ]]; then
    die "sudo needs a password but there is no terminal to ask for it. Run this script from an interactive shell."
  fi
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
  ok "Installed ${#missing[@]} package(s)"
fi

# ---------------------------------------------------------------------------
# 2. mise + shell setup
# ---------------------------------------------------------------------------

step "mise"

export PATH="$LOCAL_BIN:$PATH"

if [[ -x "$MISE" ]]; then
  ok "Installed ($("$MISE" --version | cut -d' ' -f1))"
elif needs_fix "mise is not installed ($FIX_HINT)"; then
  mkdir -p "$LOCAL_BIN"
  curl -fsSL https://mise.run | MISE_INSTALL_PATH="$MISE" sh
  ok "Installed $("$MISE" --version | cut -d' ' -f1)"
fi

if grep -qF "$BASHRC_MARKER" "$HOME/.bashrc" 2>/dev/null; then
  ok "$HOME/.bashrc configured"
elif needs_fix "$HOME/.bashrc doesn't activate mise ($FIX_HINT)"; then
  cat >>"$HOME/.bashrc" <<EOF

$BASHRC_MARKER
case ":\$PATH:" in *":\$HOME/.local/bin:"*) ;; *) export PATH="\$HOME/.local/bin:\$PATH" ;; esac
eval "\$("\$HOME/.local/bin/mise" activate bash)"
# <<< photo-triage bootstrap <<<
EOF
  ok "Added ~/.local/bin and mise activation to ~/.bashrc"
fi

# ---------------------------------------------------------------------------
# 3. Pinned runtimes from mise.toml
# ---------------------------------------------------------------------------

step "Runtimes (mise.toml)"

HAVE_RUNTIMES=0
if [[ ! -x "$MISE" ]]; then
  fail "Runtimes not checked: mise is missing"
elif ((CHECK)); then
  if ! (cd "$REPO_ROOT" && "$MISE" trust --show 2>/dev/null) | grep -q ": trusted"; then
    fail "mise.toml is not trusted ($FIX_HINT)"
  else
    absent="$(cd "$REPO_ROOT" && "$MISE" ls --current --missing 2>/dev/null | awk '{print $1"@"$2}' | xargs)"
    if [[ -n "$absent" ]]; then
      fail "Missing runtimes: $absent ($FIX_HINT)"
    else
      HAVE_RUNTIMES=1
    fi
  fi
else
  "$MISE" trust --quiet "$REPO_ROOT/mise.toml"
  (cd "$REPO_ROOT" && "$MISE" install --yes)
  HAVE_RUNTIMES=1
fi

if ((HAVE_RUNTIMES)); then
  for t in python uv node pnpm just; do
    ok "$t $(mx "$t" --version 2>&1 | head -1)"
  done
  ok "shellcheck $(mx shellcheck --version 2>&1 | sed -n 's/^version: //p')"
  ok "hadolint $(mx hadolint --version 2>&1 | awk '{print $NF}')"
fi

# ---------------------------------------------------------------------------
# 4. exiftool and ffmpeg
# ---------------------------------------------------------------------------

step "exiftool $EXIFTOOL_VERSION"

have="$("$LOCAL_BIN/exiftool" -ver 2>/dev/null || true)"
if [[ "$have" == "$EXIFTOOL_VERSION" ]]; then
  ok "Installed"
elif needs_fix "exiftool ${have:-not installed}, want $EXIFTOOL_VERSION ($FIX_HINT)"; then
  dest="$TOOLS_DIR/exiftool-$EXIFTOOL_VERSION"
  download "$EXIFTOOL_URL" "$SCRATCH/exiftool.tar.gz" "$EXIFTOOL_SHA256" \
    || die "Could not download exiftool $EXIFTOOL_VERSION"
  rm -rf "$dest" && mkdir -p "$dest"
  tar -xzf "$SCRATCH/exiftool.tar.gz" -C "$dest" --strip-components=1
  # The script finds its lib/ relative to its real path, so a symlink works.
  mkdir -p "$LOCAL_BIN"
  ln -sfn "$dest/exiftool" "$LOCAL_BIN/exiftool"
  [[ "$("$LOCAL_BIN/exiftool" -ver)" == "$EXIFTOOL_VERSION" ]] || die "exiftool install did not verify"
  ok "Installed to $dest"
fi

step "ffmpeg $FFMPEG_VERSION"

have="$("$LOCAL_BIN/ffmpeg" -version 2>/dev/null | head -1 | awk '{print $3}' || true)"
have="${have%-static}"
if [[ "$have" == "$FFMPEG_VERSION" ]] && "$LOCAL_BIN/ffprobe" -version >/dev/null 2>&1; then
  ok "Installed"
elif needs_fix "ffmpeg ${have:-not installed}, want $FFMPEG_VERSION ($FIX_HINT)"; then
  got=""
  for url in "${FFMPEG_URLS[@]}"; do
    if download "$url" "$SCRATCH/ffmpeg.tar.xz" "$FFMPEG_SHA256" 2>/dev/null; then got=1; break; fi
  done
  [[ -n "$got" ]] || die "Could not download ffmpeg $FFMPEG_VERSION with the pinned checksum. If upstream moved it, update FFMPEG_URLS."
  dest="$TOOLS_DIR/ffmpeg-$FFMPEG_VERSION"
  rm -rf "$dest" && mkdir -p "$dest"
  tar -xJf "$SCRATCH/ffmpeg.tar.xz" -C "$dest" --strip-components=1
  mkdir -p "$LOCAL_BIN"
  ln -sfn "$dest/ffmpeg" "$LOCAL_BIN/ffmpeg"
  ln -sfn "$dest/ffprobe" "$LOCAL_BIN/ffprobe"
  "$LOCAL_BIN/ffmpeg" -version | head -1 | grep -q "version $FFMPEG_VERSION" || die "ffmpeg install did not verify"
  ok "Installed to $dest"
fi

if [[ -x /usr/bin/ffmpeg ]]; then
  warn "apt ffmpeg is also installed at /usr/bin/ffmpeg; ~/.local/bin must stay ahead of it on PATH."
fi

# ---------------------------------------------------------------------------
# 5. Project dependencies and git hooks
# ---------------------------------------------------------------------------

step "pre-commit $PRE_COMMIT_VERSION"

have="$("$LOCAL_BIN/pre-commit" --version 2>/dev/null | awk '{print $2}' || true)"
if [[ "$have" == "$PRE_COMMIT_VERSION" ]]; then
  ok "Installed"
elif ! ((HAVE_RUNTIMES)); then
  fail "pre-commit ${have:-not installed}, want $PRE_COMMIT_VERSION (needs the mise runtimes first)"
elif needs_fix "pre-commit ${have:-not installed}, want $PRE_COMMIT_VERSION ($FIX_HINT)"; then
  mx uv tool install --force --python "$(mx which python)" "pre-commit==$PRE_COMMIT_VERSION" >/dev/null
  ok "Installed"
fi

step "Project dependencies"

BACKEND_SYNCED=0

if [[ ! -f "$REPO_ROOT/backend/pyproject.toml" ]]; then
  warn "backend/pyproject.toml not found yet; skipped uv sync"
elif ! ((HAVE_RUNTIMES)); then
  fail "backend not checked: mise runtimes are missing"
elif [[ ! -f "$REPO_ROOT/backend/uv.lock" ]]; then
  fail "backend: uv.lock is missing. Run 'uv lock' in backend/ and commit it."
elif ! (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv lock --check --offline >/dev/null 2>&1); then
  fail "backend: uv.lock is out of date with pyproject.toml. Run 'uv lock' in backend/ and commit it."
elif ((CHECK)); then
  if (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv sync --locked --check --offline >/dev/null 2>&1); then
    ok "backend: environment matches uv.lock"
    BACKEND_SYNCED=1
  else
    fail "backend: .venv is missing or out of date with uv.lock ($FIX_HINT)"
  fi
else
  # Default groups (dev, export) included; torch comes from the CPU-only index.
  (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv sync --locked)
  ok "backend: uv sync"
  BACKEND_SYNCED=1
fi

# A .venv can match uv.lock and still be broken. For example, uninstalling
# opencv-python (left out since #11) also deletes the cv2 folder that
# opencv-python-headless shares with it. So import the packages with native code.
# broken_packages prints the distribution of each one that fails to import.
broken_packages() {
  (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv run --no-sync python - <<'EOF'
import importlib

for module, package in [
    ("cv2", "opencv-python-headless"),
    ("onnxruntime", "onnxruntime"),
    ("pillow_heif", "pillow-heif"),
    ("insightface", "insightface"),
]:
    try:
        importlib.import_module(module)
    except Exception:
        print(package)
EOF
  )
}

if ((BACKEND_SYNCED)); then
  broken="$(broken_packages | xargs)"
  if [[ -z "$broken" ]]; then
    ok "backend: native packages import"
  elif needs_fix "backend: these packages don't import: $broken ($FIX_HINT, which reinstalls them, or 'uv sync --reinstall-package <package>' in backend/)"; then
    reinstall=()
    for package in $broken; do reinstall+=(--reinstall-package "$package"); done
    (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv sync --locked "${reinstall[@]}")
    broken="$(broken_packages | xargs)"
    if [[ -z "$broken" ]]; then
      ok "backend: reinstalled the packages that didn't import"
    else
      fail "backend: these packages still don't import after a reinstall: $broken"
    fi
  fi
fi

if [[ ! -f "$REPO_ROOT/frontend/package.json" ]]; then
  warn "frontend/package.json not found yet; skipped pnpm install"
elif ! ((HAVE_RUNTIMES)); then
  fail "frontend not checked: mise runtimes are missing"
elif ((CHECK)); then
  # pnpm keeps a copy of the lockfile it last installed from.
  if cmp -s "$REPO_ROOT/frontend/pnpm-lock.yaml" "$REPO_ROOT/frontend/node_modules/.pnpm/lock.yaml"; then
    ok "frontend: node_modules matches pnpm-lock.yaml"
  else
    fail "frontend: node_modules is missing or out of date with pnpm-lock.yaml ($FIX_HINT)"
  fi
else
  if [[ -f "$REPO_ROOT/frontend/pnpm-lock.yaml" ]]; then
    (cd "$REPO_ROOT/frontend" && "$MISE" exec -- pnpm install --frozen-lockfile)
  else
    (cd "$REPO_ROOT/frontend" && "$MISE" exec -- pnpm install)
  fi
  ok "frontend: pnpm install"
fi

if [[ ! -f "$REPO_ROOT/.pre-commit-config.yaml" ]]; then
  warn ".pre-commit-config.yaml not found yet; skipped pre-commit install"
elif grep -q "pre-commit" "$(git -C "$REPO_ROOT" rev-parse --git-path hooks)/pre-commit" 2>/dev/null; then
  ok "pre-commit hooks installed"
elif needs_fix "pre-commit git hooks are not installed ($FIX_HINT)"; then
  if [[ -x "$LOCAL_BIN/pre-commit" ]]; then
    (cd "$REPO_ROOT" && "$LOCAL_BIN/pre-commit" install --install-hooks)
    ok "pre-commit hooks installed"
  else
    fail "pre-commit hooks not installed: pre-commit itself is missing"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Model weights
# ---------------------------------------------------------------------------

step "Model weights ($MODELS_DIR)"

# InsightFace buffalo_l: extracted; a stamp file records which zip produced it.
face_dir="$MODELS_DIR/insightface/buffalo_l"
if [[ "$(cat "$face_dir/.sha256" 2>/dev/null || true)" == "$BUFFALO_L_SHA256" ]]; then
  ok "insightface/buffalo_l"
elif needs_fix "InsightFace buffalo_l weights missing or outdated ($FIX_HINT)"; then
  echo "  Downloading InsightFace buffalo_l (~280 MB)..."
  download "$BUFFALO_L_URL" "$SCRATCH/buffalo_l.zip" "$BUFFALO_L_SHA256" \
    || die "Could not download InsightFace buffalo_l"
  rm -rf "$face_dir" && mkdir -p "$face_dir"
  unzip -q "$SCRATCH/buffalo_l.zip" -d "$face_dir"
  echo "$BUFFALO_L_SHA256" >"$face_dir/.sha256"
  ok "insightface/buffalo_l"
fi

aes="$MODELS_DIR/aesthetic/sa_0_4_vit_b_32_linear.pth"
if [[ -f "$aes" ]] && echo "$AESTHETIC_SHA256  $aes" | sha256sum --check --status; then
  ok "aesthetic predictor"
elif needs_fix "Aesthetic predictor weights missing or corrupt ($FIX_HINT)"; then
  mkdir -p "$(dirname "$aes")"
  download "$AESTHETIC_URL" "$SCRATCH/aes.pth" "$AESTHETIC_SHA256" \
    || die "Could not download the LAION aesthetic predictor"
  mv "$SCRATCH/aes.pth" "$aes"
  ok "aesthetic predictor"
fi

# CLIP is exported from open_clip to ONNX, which needs the backend's Python deps.
clip_dir="$MODELS_DIR/clip"
if [[ ! -f "$REPO_ROOT/scripts/export_clip_onnx.py" ]]; then
  warn "CLIP ONNX export skipped (scripts/export_clip_onnx.py is added in Phase 2)"
elif [[ ! -f "$REPO_ROOT/backend/pyproject.toml" ]]; then
  warn "CLIP ONNX export skipped (needs backend/pyproject.toml)"
elif compgen -G "$clip_dir/*.onnx" >/dev/null; then
  ok "CLIP ONNX"
elif needs_fix "CLIP ONNX model not exported ($FIX_HINT)"; then
  (cd "$REPO_ROOT/backend" && "$MISE" exec -- uv run python ../scripts/export_clip_onnx.py --out "$clip_dir")
  ok "CLIP ONNX export"
fi

# ---------------------------------------------------------------------------
# 7. Things this script can only check, not fix
# ---------------------------------------------------------------------------

step "Docker"

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker CLI not found. Install Docker Desktop on Windows and enable WSL integration for this distro (README §2)."
elif docker_out="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
  ok "Docker $docker_out"
elif [[ "$docker_out" == *"permission denied"* ]]; then
  if ! getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
    if needs_fix "$USER is not in the 'docker' group ($FIX_HINT)"; then
      sudo usermod -aG docker "$USER"
    fi
  fi
  fail "Docker is running, but this session isn't in the 'docker' group yet. Run 'wsl --shutdown' from Windows and reopen Ubuntu (or 'newgrp docker' for one shell)."
else
  fail "Docker CLI found but the engine isn't reachable. Start Docker Desktop and check WSL integration for this distro (README §2)."
fi

step "Photo mounts"

# /mnt/sample-pictures is the default PHOTO_DIR, so it's required. /mnt/pictures is
# only for occasional full-library dry runs. Both must be read-only if mounted.
for m in /mnt/sample-pictures /mnt/pictures; do
  if ! mountpoint -q "$m"; then
    msg="$m is not mounted. Run 'sudo mount $m' (README §5)."
    if [[ "$m" == /mnt/sample-pictures ]]; then fail "$msg"; else warn "$msg"; fi
  elif findmnt -no OPTIONS "$m" | tr ',' '\n' | grep -qx ro; then
    ok "$m mounted read-only"
  else
    fail "$m is mounted READ-WRITE. Remount it with 'ro' before doing anything else (README §5)."
  fi
done

# ---------------------------------------------------------------------------

step "Done"
if ((${#WARNINGS[@]})); then
  echo "  Warnings:"
  for w in "${WARNINGS[@]}"; do echo "    - $w"; done
fi
if ((${#FAILURES[@]})); then
  echo "  Problems:"
  for f in "${FAILURES[@]}"; do echo "    - $f"; done
fi

if ((QUIET)); then
  if ((${#FAILURES[@]})); then
    echo "Dev environment problems (from scripts/bootstrap.sh --check):" >&3
    for f in "${FAILURES[@]}"; do echo "  - $f" >&3; done
    exit 1
  fi
elif ((CHECK)); then
  if ((${#FAILURES[@]})); then
    echo
    echo "  ${#FAILURES[@]} problem(s) found."
    exit 1
  fi
  echo
  echo "  Environment OK."
else
  echo
  echo "  Open a new shell (or run: exec bash) so mise and ~/.local/bin are active."
  ((${#FAILURES[@]} == 0)) || exit 1
fi

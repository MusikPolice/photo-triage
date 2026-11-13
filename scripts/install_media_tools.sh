#!/usr/bin/env bash
# Install the exiftool and ffmpeg pinned in versions.env into DEST:
#   DEST/exiftool/exiftool, and DEST/ffmpeg/ffmpeg and ffprobe.
#
# The one place that knows where they come from (dev-environment §2).
# scripts/bootstrap.sh, the docker/Dockerfile `tools` stage and the CI backend
# job all run it. Each download is checked against its SHA-256, and each tool's
# version is checked once installed. Exits 1 naming the tool and the problem.
#
# The pins come from the environment when all four are set (the Dockerfile
# passes them as build args), else from versions.env at the repo root.
#
# Usage: scripts/install_media_tools.sh DEST

set -euo pipefail

usage() { echo "Usage: $0 DEST" >&2; exit 2; }
[[ $# -eq 1 ]] || usage
DEST="$1"

die() { echo "install_media_tools: $*" >&2; exit 1; }

PINS=(EXIFTOOL_VERSION EXIFTOOL_SHA256 FFMPEG_VERSION FFMPEG_SHA256)
missing=0
for pin in "${PINS[@]}"; do [[ -n "${!pin:-}" ]] || missing=1; done
if [[ $missing -eq 1 ]]; then
  versions="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/versions.env"
  [[ -f "$versions" ]] || die "no pins in the environment and no $versions"
  # shellcheck source=../versions.env
  source "$versions"
fi
for pin in "${PINS[@]}"; do [[ -n "${!pin:-}" ]] || die "$pin isn't set"; done

# exiftool.org only serves the newest release; SourceForge keeps every version.
EXIFTOOL_URLS=(
  "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download"
)
# The static amd64 build. The release URL is rolling, so old-releases comes
# first and the checksum guards against drift.
FFMPEG_URLS=(
  "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-amd64-static.tar.xz"
  "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
)

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# fetch TOOL SHA256 OUT URL... : the first URL whose download has the checksum.
fetch() {
  local tool="$1" sha="$2" out="$3" url got
  shift 3
  for url in "$@"; do
    if ! curl --fail --silent --show-error --location --retry 3 --output "$out" "$url"; then
      echo "install_media_tools: $tool: download failed from $url" >&2
      continue
    fi
    got="$(sha256sum "$out" | cut -d' ' -f1)"
    if [[ "$got" == "$sha" ]]; then
      return 0
    fi
    echo "install_media_tools: $tool: checksum mismatch from $url (want $sha, got $got)" >&2
  done
  rm -f "$out"
  die "$tool: no download matched the pinned checksum"
}

# unpack ARCHIVE DIR: replace DIR with the archive's contents.
unpack() {
  rm -rf "$2"
  mkdir -p "$2"
  tar -xf "$1" -C "$2" --strip-components=1
}

fetch exiftool "$EXIFTOOL_SHA256" "$SCRATCH/exiftool.tar.gz" "${EXIFTOOL_URLS[@]}"
unpack "$SCRATCH/exiftool.tar.gz" "$DEST/exiftool"
have="$("$DEST/exiftool/exiftool" -ver 2>&1)" || die "exiftool: won't run: $have"
[[ "$have" == "$EXIFTOOL_VERSION" ]] || die "exiftool: installed version is $have, want $EXIFTOOL_VERSION"

fetch ffmpeg "$FFMPEG_SHA256" "$SCRATCH/ffmpeg.tar.xz" "${FFMPEG_URLS[@]}"
unpack "$SCRATCH/ffmpeg.tar.xz" "$DEST/ffmpeg"
for bin in ffmpeg ffprobe; do
  have="$("$DEST/ffmpeg/$bin" -version 2>&1 | head -1)" || die "$bin: won't run: $have"
  [[ "$have" == "$bin version ${FFMPEG_VERSION}-static "* ]] \
    || die "$bin: installed version is '$have', want ${FFMPEG_VERSION}-static"
done

echo "Installed exiftool $EXIFTOOL_VERSION and ffmpeg $FFMPEG_VERSION into $DEST"

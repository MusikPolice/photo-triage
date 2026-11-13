#!/usr/bin/env bash
# Mirror the pinned exiftool tarball into this repo's `media-tools` GitHub
# Release, which scripts/install_media_tools.sh tries before SourceForge
# (dev-environment §2).
#
# Downloads it from upstream, checks it against EXIFTOOL_SHA256 in versions.env,
# and uploads it. A pin that's already mirrored is left as it is, so running it
# again changes nothing. Run it after bumping EXIFTOOL_VERSION. Needs `gh`,
# logged in with write access to the repo.
#
# ffmpeg isn't mirrored: its static build's publisher doesn't provide the full
# corresponding source the GPL requires alongside a copy of the binary (#76).
#
# Usage: scripts/mirror_media_tools.sh
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../versions.env
source "$root/versions.env"

TAG=media-tools
asset="Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz"
url="https://sourceforge.net/projects/exiftool/files/${asset}/download"

if ! gh release view "$TAG" >/dev/null 2>&1; then
  gh release create "$TAG" --title "Pinned media tools" --notes \
    "Mirrors of the tools pinned in versions.env, for scripts/install_media_tools.sh. \
Each file is checked against its SHA-256 in versions.env when installed. exiftool \
(Perl; GPL or Artistic licence) is its own source. Not a release of photo-triage." >/dev/null
  echo "Created release $TAG"
fi

if gh release view "$TAG" --json assets --jq '.assets[].name' | grep -qxF "$asset"; then
  echo "$asset is already mirrored"
  exit 0
fi

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
curl --fail --silent --show-error --location --retry 4 --retry-all-errors \
  --connect-timeout 20 --max-time 300 --output "$scratch/$asset" "$url"
echo "${EXIFTOOL_SHA256}  $scratch/$asset" | sha256sum --check --status \
  || { echo "mirror_media_tools: $asset doesn't match EXIFTOOL_SHA256" >&2; exit 1; }
gh release upload "$TAG" "$scratch/$asset"
echo "Mirrored $asset"

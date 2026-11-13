"""What makes a file the same item wherever it goes: its content hash (plan §6.1).

Photos hash their decoded pixels and videos their media streams, so the hash ignores
metadata, which we and other tools write, and the container, which a remux changes.
Reading a file is all this does.

The hash depends on the decoders: Pillow and pillow-heif for photos, ffmpeg for
videos. An upgrade that decodes the same file to different pixels would make every
file hashed after it look new, so a move would lose its history.
`tests/unit/test_identity.py` pins one hash per container to catch that.
"""

import hashlib
import subprocess
from pathlib import Path

import pillow_heif
from PIL import Image

from photo_triage.db.models import MediaType
from photo_triage.pipeline import pictures

pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".heic", ".heif"})
VIDEO_SUFFIXES = frozenset({".mov", ".mp4"})


class HashError(Exception):
    """The file's content couldn't be read, e.g. it's corrupt or truncated."""


def media_type(path: Path) -> MediaType | None:
    """The media type of a supported file, by its suffix, or None if it isn't one."""
    suffix = path.suffix.lower()
    if suffix in PHOTO_SUFFIXES:
        return MediaType.PHOTO
    if suffix in VIDEO_SUFFIXES:
        return MediaType.VIDEO
    return None


def content_hash(path: Path, kind: MediaType) -> str:
    """The SHA-256 of the file's content, in hex. Raises `HashError` if it can't be
    decoded."""
    return hash_content(path, kind)[0]


def hash_content(path: Path, kind: MediaType) -> tuple[str, bool]:
    """`content_hash`, and whether the file is a damaged photo: only partly readable
    (`photo_triage.pipeline.pictures`). Its hash covers the part that decodes, with
    the rest filled as Pillow fills it, so it's stable while the file is unchanged."""
    if kind == MediaType.PHOTO:
        return _photo_hash(path)
    return _video_hash(path), False


def _photo_hash(path: Path) -> tuple[str, bool]:
    """The decoded pixels, with the mode and size so equal bytes in another shape
    differ. EXIF orientation is metadata, so it isn't applied."""

    def hashed(image: Image.Image) -> str:
        header = f"{image.mode} {image.width}x{image.height}\n"
        return hashlib.sha256(header.encode() + image.tobytes()).hexdigest()

    try:
        return pictures.decode(path, hashed)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise HashError(f"can't decode {path.name}: {e}") from e


def _video_hash(path: Path) -> str:
    """The packets of the video and audio streams, copied out of the container by
    ffmpeg. Video comes first whatever the container's order, and attached pictures
    (cover art) and data streams such as timecodes are left out."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise HashError(f"can't read the streams of {path.name}: {result.stderr.strip()}")
    return hashlib.sha256(result.stdout.encode()).hexdigest()

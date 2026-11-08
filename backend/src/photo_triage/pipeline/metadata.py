"""The `metadata_read` stage: what the scanner records from inside each file (plan §6.1).

- **Dimensions**, in pixels as displayed: a photo's after its EXIF orientation, a
  video's after its rotation. Pillow and pillow-heif read photos (libheif applies a
  HEIC's own rotation, `irot`), ffprobe videos.
- **Duration** of a video, from ffprobe.
- **`taken_at_local`**, the capture time on the camera's clock, with no timezone.
  Photos: `DateTimeOriginal`, then `CreateDate`, each from EXIF and then XMP,
  then EXIF's `DateTime` (exiftool's `ModifyDate`), then the file's mtime in `TZ`.
  Videos: `Keys:CreationDate`, which phones write in local time with an offset,
  then `QuickTime:CreateDate`, which the QuickTime format stores in UTC, converted
  to `TZ`, then the mtime.
- **Keywords** in `IPTC:Keywords`, `XMP-dc:Subject` and `QuickTime:Keywords`, kept
  as `tags` with `source=imported`, once each. Each read replaces the item's
  imported tags with what the file has now.

It's an item stage of its own, after `scan`, so its version can change without the
files being hashed again. Every scan queues it, since a scan runs only when the walk
saw a new or changed file, and a change to the metadata alone leaves the hash, which
the stages after the scan read, as it was. Reading is all it does to the files.
"""

import datetime as dt
import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pillow_heif
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, Job, MediaType, Tag, TagSource
from photo_triage.files import MetadataError, MetadataReader

logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

KEYWORD_TAGS = ("IPTC:Keywords", "XMP-dc:Subject", "QuickTime:Keywords")
PHOTO_DATE_TAGS = (
    "EXIF:DateTimeOriginal",
    "XMP:DateTimeOriginal",
    "EXIF:CreateDate",
    "XMP:CreateDate",
    "EXIF:ModifyDate",
)
"""In order of preference. `EXIF:ModifyDate` is EXIF's `DateTime` (tag 0x0132); XMP's
`ModifyDate` is when a program last saved the file, so it isn't used."""
VIDEO_LOCAL_DATE_TAG = "Keys:CreationDate"
VIDEO_UTC_DATE_TAG = "QuickTime:CreateDate"

_ORIENTATION = 0x0112
_SIDEWAYS = frozenset({5, 6, 7, 8})
"""EXIF orientations that turn the image a quarter, so width and height swap."""
_EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"


@dataclass(frozen=True)
class FileMetadata:
    width: int
    height: int
    duration_s: float | None
    taken_at_local: dt.datetime
    keywords: tuple[str, ...]


def read(
    path: Path, kind: MediaType, reader: MetadataReader, zone: ZoneInfo, mtime_ns: int
) -> FileMetadata:
    """What `path` says about itself. `mtime_ns` is its mtime, for a file with no
    capture time. Raises `MetadataError` if it can't be read."""
    date_tags = (
        PHOTO_DATE_TAGS if kind == MediaType.PHOTO else (VIDEO_LOCAL_DATE_TAG, VIDEO_UTC_DATE_TAG)
    )
    tags = reader.read(path, [*date_tags, *KEYWORD_TAGS])
    if kind == MediaType.PHOTO:
        width, height = _photo_size(path)
        duration_s = None
    else:
        width, height, duration_s = _probe_video(path)
    return FileMetadata(
        width=width,
        height=height,
        duration_s=duration_s,
        taken_at_local=taken_at_local(tags, kind, zone, mtime_ns),
        keywords=keywords(tags),
    )


def taken_at_local(
    tags: Mapping[str, Any], kind: MediaType, zone: ZoneInfo, mtime_ns: int
) -> dt.datetime:
    """The capture time, from exiftool's `tags` as `MetadataReader.read` returns
    them, by the fallbacks in the module docstring. Whole seconds, no timezone."""
    if kind == MediaType.PHOTO:
        for name in PHOTO_DATE_TAGS:
            if (when := _first_date(tags, name)) is not None:
                return when
    else:
        if (when := _first_date(tags, VIDEO_LOCAL_DATE_TAG)) is not None:
            return when
        if (when := _first_date(tags, VIDEO_UTC_DATE_TAG)) is not None:
            return when.replace(tzinfo=dt.UTC).astimezone(zone).replace(tzinfo=None)
    mtime = dt.datetime.fromtimestamp(mtime_ns // 10**9, zone)
    return mtime.replace(tzinfo=None)


def keywords(tags: Mapping[str, Any]) -> tuple[str, ...]:
    """Every keyword in the `KEYWORD_TAGS`, in order, once each. Blank ones are
    dropped and whitespace around them trimmed."""
    found: dict[str, None] = {}
    for spec in KEYWORD_TAGS:
        for value in _values(tags, spec):
            items: list[Any] = value if isinstance(value, list) else [value]  # pyright: ignore[reportUnknownVariableType]
            for keyword in items:
                # exiftool gives a keyword that looks like a number as one.
                text = str(keyword).strip()
                if text:
                    found[text] = None
    return tuple(found)


def record(
    sessions: sessionmaker[Session],
    job: Job,
    reader: MetadataReader,
    photo_dir: Path,
    zone: ZoneInfo,
    now: dt.datetime,
) -> bool:
    """Read the metadata of `job`'s item and record it, replacing its imported tags.
    Returns whether anything recorded changed. Raises `MetadataError` if the file
    can't be read, so the job is retried and then parked."""
    assert job.item_id is not None
    with sessions() as session:
        item = session.get(Item, job.item_id)
        if item is None:
            return False  # deleted since the job was queued
        relative, kind = item.path, item.media_type
    path = photo_dir / relative
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        # The next walk deals with it as with any missing file.
        logger.info("Item %d is gone from %s before its metadata was read", job.item_id, relative)
        return False
    found = read(path, kind, reader, zone, mtime_ns)

    with sessions.begin() as session:
        item = session.get(Item, job.item_id)
        if item is None or item.path != relative:
            return False  # changed meanwhile; a later scan reads it again
        fields = {
            "width": found.width,
            "height": found.height,
            "duration_s": found.duration_s,
            "taken_at_local": found.taken_at_local,
        }
        changed = any(getattr(item, name) != value for name, value in fields.items())
        for name, value in fields.items():
            setattr(item, name, value)
        return _replace_imported_tags(session, item.id, found.keywords, now) or changed


def _replace_imported_tags(
    session: Session, item_id: int, keywords: Sequence[str], now: dt.datetime
) -> bool:
    """Make the item's imported tags `keywords`. Tags it still has keep their
    `created_at`. Returns whether any were added or removed."""
    imported = (Tag.item_id == item_id, Tag.source == TagSource.IMPORTED)
    have = set(session.scalars(sa.select(Tag.tag).where(*imported)))
    want = set(keywords)
    if gone := have - want:
        session.execute(sa.delete(Tag).where(*imported, Tag.tag.in_(gone)))
    session.add_all(
        Tag(item_id=item_id, source=TagSource.IMPORTED, tag=keyword, created_at=now)
        for keyword in keywords
        if keyword not in have
    )
    return have != want


def _values(tags: Mapping[str, Any], spec: str) -> list[Any]:
    """The values of the tag `spec` names (`Name`, or `Group:Name` in either group
    family), in the order exiftool gave them. A `-G0:1` key is `Family0:Family1:Name`,
    or `Family0:Name` when the two families agree."""
    group, _, name = spec.rpartition(":")
    found: list[Any] = []
    for key, value in tags.items():
        *groups, tag_name = key.split(":")
        if tag_name == name and (not group or group in groups):
            found.append(value)
    return found


def _first_date(tags: Mapping[str, Any], spec: str) -> dt.datetime | None:
    """The first valid date in the tag `spec` names. An offset or fractional seconds
    after the time are dropped, so it's the wall-clock time as written."""
    for value in _values(tags, spec):
        try:
            return dt.datetime.strptime(str(value)[:19], _EXIF_DATE_FORMAT)
        except ValueError:
            continue  # blank, or a placeholder such as 0000:00:00 00:00:00
    return None


def _photo_size(path: Path) -> tuple[int, int]:
    """Width and height as displayed. Opening reads only the headers."""
    try:
        with Image.open(path) as image:
            width, height = image.size
            orientation = image.getexif().get(_ORIENTATION)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise MetadataError(f"can't read the size of {path.name}: {e}") from e
    return (height, width) if orientation in _SIDEWAYS else (width, height)


def _probe_video(path: Path) -> tuple[int, int, float]:
    """Width and height as displayed, and the duration in seconds, of the first video
    stream that isn't cover art."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "V:0",
            "-show_entries",
            "stream=width,height:stream_side_data=rotation:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MetadataError(f"ffprobe can't read {path.name}: {result.stderr.strip()}")
    try:
        probe = json.loads(result.stdout)
        stream = probe["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration_s = float(probe["format"]["duration"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise MetadataError(f"ffprobe found no video stream in {path.name}: {e!r}") from e
    rotation = next(
        (int(d["rotation"]) for d in stream.get("side_data_list", []) if "rotation" in d), 0
    )
    sideways = rotation % 180 != 0
    return (height, width, duration_s) if sideways else (width, height, duration_s)

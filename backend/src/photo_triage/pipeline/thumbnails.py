"""The `thumbnail` stage: a `THUMBNAIL_SIZE` thumbnail (400 px on the long edge by
default) and a 64 px one for the map, per item (plan §6.2).

They're JPEGs in the derived folder (plan §4), at `thumbnail_path`. A photo's are
drawn from the file, turned by its EXIF orientation; a video's from its middle
sampled frame (`photo_triage.pipeline.frames`), which is turned already. Neither is
ever bigger than the picture it's drawn from.

Each file is written beside the old one and swapped in
(`photo_triage.files.replace_derived`), so a half-written thumbnail is never served.
A redraw that gives the same bytes leaves the files alone and reports the output
unchanged, so the stages after it don't run again.
"""

import io
import logging
import os
import tempfile
from enum import StrEnum
from pathlib import Path

import pillow_heif
import sqlalchemy as sa
from PIL import Image, ImageOps
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemFrame, Job, MediaType
from photo_triage.files import remove_derived, replace_derived
from photo_triage.pipeline import pictures
from photo_triage.pipeline.frames import frame_path

logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

MAP_SIZE = 64
"""Long edge of the map thumbnail, in pixels."""
THUMBNAILS_FOLDER = "thumbnails"
_JPEG_QUALITY = 85


class Thumbnail(StrEnum):
    LARGE = "large"
    """`THUMBNAIL_SIZE` on the long edge."""
    MAP = "map"
    """`MAP_SIZE` on the long edge."""


class ThumbnailError(Exception):
    """The picture to draw from can't be read."""


def thumbnail_path(derived_dir: Path, item_id: int, kind: Thumbnail) -> Path:
    """Where the item's thumbnail of this kind is, once drawn."""
    return derived_dir / THUMBNAILS_FOLDER / kind / f"{item_id}.jpg"


def draw(source: Path, long_edge: int) -> Image.Image:
    """The picture at `source`, upright, at most `long_edge` pixels on its long edge."""

    def upright(image: Image.Image) -> Image.Image:
        # JPEG decodes at a fraction of full size if that's still big enough.
        image.draft("RGB", (long_edge, long_edge))
        return ImageOps.exif_transpose(image).convert("RGB")

    try:
        # A damaged photo's missing part is grey; the scan records that it's damaged.
        picture, _ = pictures.decode(source, upright)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        raise ThumbnailError(f"can't draw {source.name}: {e}") from e
    picture.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    return picture


def encode(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()


def make(
    sessions: sessionmaker[Session],
    job: Job,
    photo_dir: Path,
    derived_dir: Path,
    size: int,
) -> bool:
    """Draw `job`'s item's thumbnails, `size` and `MAP_SIZE` pixels on the long edge.
    Returns whether either changed. Raises `ThumbnailError` if the picture can't be
    read, so the job is retried and then parked."""
    assert job.item_id is not None
    item_id = job.item_id
    with sessions() as session:
        item = session.get(Item, item_id)
        if item is None:
            return False  # deleted since the job was queued
        source = _source(session, item, photo_dir, derived_dir)
    if not source.is_file():
        # The next walk deals with a missing file; a video's frames come back with it.
        logger.info("Item %d's picture is gone from %s before its thumbnails", item_id, source)
        return False

    large = draw(source, size)
    small = large.copy()
    small.thumbnail((MAP_SIZE, MAP_SIZE), Image.Resampling.LANCZOS)
    changed = False
    for kind, image in ((Thumbnail.LARGE, large), (Thumbnail.MAP, small)):
        changed |= _write(derived_dir, thumbnail_path(derived_dir, item_id, kind), encode(image))
    return changed


def _source(session: Session, item: Item, photo_dir: Path, derived_dir: Path) -> Path:
    """The photo, or the video's middle frame."""
    if item.media_type == MediaType.PHOTO:
        return photo_dir / item.path
    indexes = list(
        session.scalars(
            sa.select(ItemFrame.frame_index)
            .where(ItemFrame.item_id == item.id)
            .order_by(ItemFrame.frame_index)
        )
    )
    if not indexes:
        raise ThumbnailError(f"item {item.id} is a video with no frames sampled")
    return frame_path(derived_dir, item.id, indexes[len(indexes) // 2])


def _write(derived_dir: Path, target: Path, data: bytes) -> bool:
    """Make `target` hold `data`. Returns False if it already did."""
    try:
        if target.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.stem}.", suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        replace_derived(derived_dir, staged, target)
    finally:
        remove_derived(derived_dir, staged)  # already gone once swapped in
    return True

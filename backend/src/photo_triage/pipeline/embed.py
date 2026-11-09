"""The `clip` stage: a CLIP embedding per item, in `embeddings` (plan §6.3).

A photo is embedded from the original, turned by its EXIF orientation. A JPEG is
decoded at a fraction of its size, but at least twice CLIP's input (448 px for
ViT-B-32), which keeps the vector within 0.997 of a full decode's. Not from the
thumbnail: its JPEG compression moves the vector by 0.03 to 0.06, as much as tells
a burst's frames apart. A video's frames are embedded together, in one batch, and
its embedding is their mean, made unit length again (plan §6.1).

Reading is all it does to the file.

The embedding replaces the item's old one, whichever model made it. A run that
gives the same vector from the same model reports the output unchanged.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pillow_heif
import sqlalchemy as sa
from numpy.typing import NDArray
from PIL import Image, ImageOps
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Embedding, Item, ItemFrame, Job, MediaType
from photo_triage.ml import ClipEmbedder
from photo_triage.pipeline.frames import frame_path

logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

_DECODE_FACTOR = 2
"""Decode a JPEG to at least this many times the model's input size."""


class EmbedError(Exception):
    """The pictures to embed can't be read."""


def mean_vector(vectors: NDArray[np.float32]) -> NDArray[np.float32]:
    """The mean of `vectors`' rows, made unit length again."""
    mean = vectors.astype(np.float32).mean(axis=0)
    return (mean / np.linalg.norm(mean)).astype(np.float32)


def to_blob(vector: NDArray[np.float32]) -> bytes:
    return vector.astype("<f4").tobytes()


def from_blob(blob: bytes) -> NDArray[np.float32]:
    return np.frombuffer(blob, dtype="<f4").astype(np.float32)


def embed(
    sessions: sessionmaker[Session],
    job: Job,
    clip: ClipEmbedder,
    photo_dir: Path,
    derived_dir: Path,
) -> bool:
    """Embed `job`'s item with `clip` and store it. Returns whether the stored
    embedding changed. Raises `EmbedError` if a picture can't be read, so the job is
    retried and then parked."""
    assert job.item_id is not None
    item_id = job.item_id
    with sessions() as session:
        item = session.get(Item, item_id)
        if item is None:
            return False  # deleted since the job was queued
        sources = _sources(session, item, photo_dir, derived_dir)
    if not all(source.is_file() for source in sources):
        # The next walk deals with a missing file; a video's frames come back with it.
        logger.info("Item %d's pictures are gone before its embedding", item_id)
        return False

    vectors = clip.embed_images(_open(sources, clip))
    blob = to_blob(mean_vector(vectors))
    with sessions.begin() as session:
        if session.get(Item, item_id) is None:
            return False
        old = session.get(Embedding, item_id)
        if old is not None and (old.model, old.vector) == (clip.model, blob):
            return False
        session.merge(Embedding(item_id=item_id, model=clip.model, vector=blob))
    return True


def _sources(session: Session, item: Item, photo_dir: Path, derived_dir: Path) -> list[Path]:
    """The photo, or the video's frames in order."""
    if item.media_type == MediaType.PHOTO:
        return [photo_dir / item.path]
    indexes = session.scalars(
        sa.select(ItemFrame.frame_index)
        .where(ItemFrame.item_id == item.id)
        .order_by(ItemFrame.frame_index)
    )
    sources = [frame_path(derived_dir, item.id, index) for index in indexes]
    if not sources:
        raise EmbedError(f"item {item.id} is a video with no frames sampled")
    return sources


def _open(sources: Sequence[Path], clip: ClipEmbedder) -> list[Image.Image]:
    """Each picture, upright. A video's frames are upright already."""
    least = clip.image_size * _DECODE_FACTOR
    pictures: list[Image.Image] = []
    for source in sources:
        try:
            with Image.open(source) as image:
                image.draft("RGB", (least, least))  # JPEG only; others decode in full
                pictures.append(ImageOps.exif_transpose(image).convert("RGB"))
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            raise EmbedError(f"can't read {source.name}: {e}") from e
    return pictures

"""The `quality` stage: an item's heuristic quality signals, in `quality` (plan §6.8).

Each signal is in [0, 1], with 1 the best:

- **blur:** `tanh(variance / 200)` of the Laplacian of the grayscale picture. A sharp
  photo has strong edges, so a high variance; a blurred one has almost none.
- **exposure:** the lower of two scores. The first is 1 less a penalty for the
  pixels clipped at either end beyond `CLIP_ALLOWANCE` (2%), falling to 0 once
  `EXPOSURE_SPAN` (20%) more are clipped. A pixel counts as clipped within
  `CLIP_LEVELS` levels of black or white, since decoding a JPEG's flat black or
  white rarely gives exactly 0 or 255. The second is for a picture that's too dark
  or too bright overall without clipping, as a dark photo often is, since sensor
  noise keeps its shadows off black: 1 for a mean brightness from 15% to 85%,
  falling to 0 at 3% and at 97%. The clipped fractions and the mean brightness are
  stored too, so a reason can say which way an exposure is off.
- **resolution:** a logistic curve in megapixels, 0.5 at `RESOLUTION_MIDPOINT_MP`
  (2 MP), 0.12 at 1 MP and 0.98 at 4 MP.

Blur and exposure are measured on the picture scaled to `ANALYSIS_LONG_EDGE_PX`, so
the blur threshold means the same for every camera, and a 48 MP photo costs no more
than a 12 MP one. A video's signals are the median over its sampled frames (plan
§6.1). A damaged photo is scored from what decodes.

Reading is all it does to the file.
"""

import logging
import math
import statistics
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from pathlib import Path

import numpy as np
import pillow_heif
import sqlalchemy as sa
from numpy.typing import NDArray
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemFrame, Job, MediaType, Quality
from photo_triage.pipeline.frames import frame_path
from photo_triage.pipeline.pictures import decode

logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

ANALYSIS_LONG_EDGE_PX = 1024
BLUR_VARIANCE_SCALE = 200.0
CLIP_LEVELS = 2
CLIP_ALLOWANCE = 0.02
EXPOSURE_SPAN = 0.2
DARK_MEAN_FULL = 0.15
DARK_MEAN_ZERO = 0.03
BRIGHT_MEAN_FULL = 0.85
BRIGHT_MEAN_ZERO = 0.97
RESOLUTION_MIDPOINT_MP = 2.0
RESOLUTION_SLOPE_MP = 0.5
"""How fast the resolution curve rises: from 0.27 at 1.5 MP to 0.73 at 2.5 MP."""


class QualityError(Exception):
    """The pictures to score can't be read."""


@dataclass(frozen=True)
class Exposure:
    """How much of a picture is clipped to black and to white, and how bright it
    is on average, 0 to 1."""

    dark_clipped_fraction: float
    bright_clipped_fraction: float
    mean_brightness: float


@dataclass(frozen=True)
class Signals:
    blur: float
    exposure: float
    dark_clipped_fraction: float
    bright_clipped_fraction: float
    mean_brightness: float
    resolution: float


def blur_score(gray: NDArray[np.float32]) -> float:
    """`tanh(variance / 200)` of the 4-neighbour Laplacian of a grayscale picture in
    0 to 255."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    laplacian = (
        gray[1:-1, :-2] + gray[1:-1, 2:] + gray[:-2, 1:-1] + gray[2:, 1:-1] - 4 * gray[1:-1, 1:-1]
    )
    return math.tanh(float(laplacian.var()) / BLUR_VARIANCE_SCALE)


def measure_exposure(gray: NDArray[np.float32]) -> Exposure:
    """How much of a grayscale picture in 0 to 255 is clipped, and its mean brightness."""
    return Exposure(
        dark_clipped_fraction=float((gray <= CLIP_LEVELS).mean()),
        bright_clipped_fraction=float((gray >= 255 - CLIP_LEVELS).mean()),
        mean_brightness=float(gray.mean()) / 255,
    )


def exposure_score(exposure: Exposure) -> float:
    """The lower of the clipping score and the mean brightness score."""
    excess = sum(
        max(0.0, fraction - CLIP_ALLOWANCE)
        for fraction in (exposure.dark_clipped_fraction, exposure.bright_clipped_fraction)
    )
    clipped = max(0.0, 1.0 - excess / EXPOSURE_SPAN)
    mean = exposure.mean_brightness
    dark = (mean - DARK_MEAN_ZERO) / (DARK_MEAN_FULL - DARK_MEAN_ZERO)
    bright = (BRIGHT_MEAN_ZERO - mean) / (BRIGHT_MEAN_ZERO - BRIGHT_MEAN_FULL)
    overall = min(1.0, max(0.0, min(dark, bright)))
    return min(clipped, overall)


def resolution_score(width_px: int, height_px: int) -> float:
    """A logistic curve in megapixels, 0.5 at 2 MP."""
    megapixels = width_px * height_px / 1_000_000
    return 1.0 / (1.0 + math.exp(-(megapixels - RESOLUTION_MIDPOINT_MP) / RESOLUTION_SLOPE_MP))


def grayscale(image: Image.Image) -> NDArray[np.float32]:
    """The picture in grayscale, 0 to 255, scaled down to the analysis size if larger."""
    image.draft("L", (ANALYSIS_LONG_EDGE_PX, ANALYSIS_LONG_EDGE_PX))  # JPEG only
    gray = image.convert("L")
    if max(gray.size) > ANALYSIS_LONG_EDGE_PX:
        gray.thumbnail((ANALYSIS_LONG_EDGE_PX, ANALYSIS_LONG_EDGE_PX), Image.Resampling.LANCZOS)
    return np.asarray(gray, dtype=np.float32)


def signals(pictures: Sequence[NDArray[np.float32]], width_px: int, height_px: int) -> Signals:
    """The signals for an item made of `pictures`, grayscale: one for a photo, a
    video's frames for a video, each signal the median over them."""
    if not pictures:
        raise QualityError("no pictures to score")
    measured = [measure_exposure(gray) for gray in pictures]
    return Signals(
        blur=statistics.median(blur_score(gray) for gray in pictures),
        exposure=statistics.median(exposure_score(m) for m in measured),
        dark_clipped_fraction=statistics.median(m.dark_clipped_fraction for m in measured),
        bright_clipped_fraction=statistics.median(m.bright_clipped_fraction for m in measured),
        mean_brightness=statistics.median(m.mean_brightness for m in measured),
        resolution=resolution_score(width_px, height_px),
    )


def score(sessions: sessionmaker[Session], job: Job, photo_dir: Path, derived_dir: Path) -> bool:
    """Score `job`'s item and store its signals. Returns whether they changed.
    Raises `QualityError` if a picture can't be read, so the job is retried and then
    parked."""
    assert job.item_id is not None
    item_id = job.item_id
    with sessions() as session:
        item = session.get(Item, item_id)
        if item is None:
            return False  # deleted since the job was queued
        sources = _sources(session, item, photo_dir, derived_dir)
        size = (item.width, item.height)
    if not all(source.is_file() for source in sources):
        # The next walk deals with a missing file; a video's frames come back with it.
        logger.info("Item %d's pictures are gone before its quality is scored", item_id)
        return False

    pictures = [_open(source) for source in sources]
    width_px, height_px = size
    if width_px is None or height_px is None:
        height_px, width_px = pictures[0].shape  # the analysis size: a lower bound
    found = signals(pictures, width_px, height_px)
    with sessions.begin() as session:
        if session.get(Item, item_id) is None:
            return False
        old = session.get(Quality, item_id)
        if old is not None and astuple(_signals_of(old)) == astuple(found):
            return False
        session.merge(
            Quality(
                item_id=item_id,
                blur=found.blur,
                exposure=found.exposure,
                dark_clipped_fraction=found.dark_clipped_fraction,
                bright_clipped_fraction=found.bright_clipped_fraction,
                mean_brightness=found.mean_brightness,
                resolution=found.resolution,
            )
        )
    return True


def _signals_of(row: Quality) -> Signals:
    return Signals(
        blur=row.blur,
        exposure=row.exposure,
        dark_clipped_fraction=row.dark_clipped_fraction,
        bright_clipped_fraction=row.bright_clipped_fraction,
        mean_brightness=row.mean_brightness,
        resolution=row.resolution,
    )


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
        raise QualityError(f"item {item.id} is a video with no frames sampled")
    return sources


def _open(source: Path) -> NDArray[np.float32]:
    try:
        gray, _ = decode(source, grayscale)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        raise QualityError(f"can't read {source.name}: {e}") from e
    return gray

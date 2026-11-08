"""The `frames` stage: sample `VIDEO_SAMPLE_FRAMES` frames from each video, which
the stages after it read in place of the video (plan §6.1).

The frames are evenly spaced, skipping the first and last 5%, and no closer than
one frame apart, so a clip too short for them all gives fewer, at least one. ffmpeg
writes each as a JPEG at the video's displayed size, at `frame_path`, in the derived
folder (plan §4), and `item_frames` records when in the video each is.

A run writes the new set into a staging folder, then swaps it in for the old one
(`photo_triage.files.replace_derived`) in the transaction that replaces the rows,
so an item never has frames from two runs. Reading is all it does to the video.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemFrame, Job, MediaType
from photo_triage.files import remove_derived, replace_derived
from photo_triage.pipeline import video

logger = logging.getLogger(__name__)

EDGE = 0.05
"""The share of the video skipped at each end."""
FRAMES_FOLDER = "frames"
_STAGING_SUFFIX = ".staging"


class FrameError(Exception):
    """ffmpeg couldn't read the video, or give a frame at one of the times."""


def timestamps(duration_s: float, count: int, min_gap_s: float = 0.0) -> list[float]:
    """`count` times, in seconds, evenly spaced from 5% to 95% of `duration_s`.

    If they'd be closer together than `min_gap_s` (one frame), there are as many as
    fit, the first and last still at 5% and 95%. A single one is in the middle.
    """
    if count < 1:
        raise ValueError(f"sample at least one frame, not {count}")
    span_s = (1 - 2 * EDGE) * duration_s
    if count > 1 and min_gap_s > 0:
        count = min(count, int(span_s // min_gap_s) + 1)
    if count == 1 or span_s <= 0:
        return [duration_s / 2]
    start_s = EDGE * duration_s
    return [start_s + span_s * index / (count - 1) for index in range(count)]


def frame_path(derived_dir: Path, item_id: int, frame_index: int) -> Path:
    """Where frame `frame_index` of the item is, once sampled."""
    return _item_dir(derived_dir, item_id) / f"{frame_index}.jpg"


def sample(
    sessions: sessionmaker[Session],
    job: Job,
    photo_dir: Path,
    derived_dir: Path,
    count: int,
) -> bool | None:
    """Sample `count` frames from `job`'s item, replacing any it has. Raises
    `FrameError` if ffmpeg can't, so the job is retried and then parked."""
    assert job.item_id is not None
    item_id = job.item_id
    with sessions() as session:
        item = session.get(Item, item_id)
        if item is None:
            return False  # deleted since the job was queued
        relative, kind = item.path, item.media_type
    if kind != MediaType.VIDEO:
        return False  # only videos are queued; nothing to do for anything else
    path = photo_dir / relative
    if not path.is_file():
        # The next walk deals with it as with any missing file.
        logger.info("Item %d is gone from %s before its frames were sampled", item_id, relative)
        return False
    try:
        found = video.probe(path)
    except video.ProbeError as e:
        raise FrameError(str(e)) from e
    frame_s = 1 / found.frame_rate if found.frame_rate else 0.0
    times = timestamps(found.duration_s, count, min_gap_s=frame_s)

    root = derived_dir / FRAMES_FOLDER
    root.mkdir(parents=True, exist_ok=True)
    for leftover in root.glob(f"{item_id}.*{_STAGING_SUFFIX}"):
        remove_derived(derived_dir, leftover)  # from a run that was cut short
    staged = Path(tempfile.mkdtemp(prefix=f"{item_id}.", suffix=_STAGING_SUFFIX, dir=root))
    try:
        for index, at_s in enumerate(times):
            _extract(path, at_s, staged / f"{index}.jpg")
        with sessions.begin() as session:
            if session.get(Item, item_id) is None:
                return False
            replace_derived(derived_dir, staged, _item_dir(derived_dir, item_id))
            session.execute(sa.delete(ItemFrame).where(ItemFrame.item_id == item_id))
            session.add_all(
                ItemFrame(item_id=item_id, frame_index=index, timestamp_s=at_s)
                for index, at_s in enumerate(times)
            )
    finally:
        remove_derived(derived_dir, staged)  # already gone once swapped in
    return None


def _item_dir(derived_dir: Path, item_id: int) -> Path:
    return derived_dir / FRAMES_FOLDER / str(item_id)


def _extract(path: Path, at_s: float, out: Path) -> None:
    """Write the frame shown at `at_s` in the first video stream that isn't cover art,
    turned as it's displayed."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            # Before the input, so it seeks rather than decoding from the start.
            "-ss",
            f"{at_s:.6f}",
            "-i",
            video.file_url(path),
            "-map",
            "0:V:0",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-update",
            "1",
            os.fspath(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        detail = result.stderr.strip() or "no frame there"
        raise FrameError(f"ffmpeg can't take a frame at {at_s:.3f} s from {path.name}: {detail}")

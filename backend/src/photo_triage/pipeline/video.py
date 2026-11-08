"""What ffprobe says about a video, for `metadata_read` and `frames`."""

import json
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class ProbeError(Exception):
    """ffprobe can't read the file, or finds no video stream in it."""


@dataclass(frozen=True)
class VideoProbe:
    width: int
    """Pixels, as displayed: after the stream's rotation."""
    height: int
    duration_s: float
    frame_rate: float | None
    """Frames per second on average, if the stream says."""


def probe(path: Path) -> VideoProbe:
    """The first video stream that isn't cover art, and the file's duration."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "V:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:stream_side_data=rotation:format=duration",
            "-of",
            "json",
            file_url(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProbeError(f"ffprobe can't read {path.name}: {result.stderr.strip()}")
    try:
        found = json.loads(result.stdout)
        stream = found["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration_s = float(found["format"]["duration"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ProbeError(f"ffprobe found no video stream in {path.name}: {e!r}") from e
    rotation = next(
        (int(d["rotation"]) for d in stream.get("side_data_list", []) if "rotation" in d), 0
    )
    if rotation % 180 != 0:
        width, height = height, width
    return VideoProbe(width, height, duration_s, _rate(stream.get("avg_frame_rate")))


def file_url(path: Path) -> str:
    """`path` for ffmpeg and ffprobe, which would read `a:b.mov` as protocol `a`."""
    return f"file:{os.path.abspath(path)}"


def _rate(value: object) -> float | None:
    """`30000/1001` as a number. ffprobe gives `0/0` when it doesn't know."""
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate > 0 else None

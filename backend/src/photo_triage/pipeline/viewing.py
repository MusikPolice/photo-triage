"""Showing an item in the browser, whatever its format (plan §6.4 "Lightbox").

Nothing is stored: a format the browser can't show is converted on request, from
the original, which is only read.

- **Photos.** A JPEG is shown as it is. A HEIC becomes a JPEG, the full picture,
  upright: no browser but Safari shows HEIC.
- **Videos.** H.264 video with AAC audio (or none) plays as it is, in an MP4 or a
  MOV. Anything else, such as an iPhone's HEVC, is converted by ffmpeg to H.264 and
  AAC at up to 720p, as a fragmented MP4 that plays while it's being made.
"""

import io
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from photo_triage.pipeline import pictures
from photo_triage.pipeline.video import file_url

JPEG_QUALITY = 90
"""Close to the original's look: this is the picture itself, not a thumbnail."""
MAX_VIDEO_LINES = 720
"""A converted video's short side, at most: 720p."""

PLAYABLE_VIDEO_CODECS = frozenset({"h264"})
PLAYABLE_AUDIO_CODECS = frozenset({"aac", "mp3"})

_converting = threading.Lock()
"""One HEIC at a time. It bounds the memory a burst of requests takes, and
`pictures.decode` switches a process-wide Pillow setting while it retries a
damaged photo, which must not overlap another decode."""


class ViewError(Exception):
    """The original can't be read or converted."""


def heic_as_jpeg(path: Path) -> bytes:
    """The HEIC at `path` as a JPEG, upright, at full size."""

    def upright(image: Image.Image) -> Image.Image:
        return ImageOps.exif_transpose(image).convert("RGB")

    with _converting:
        try:
            picture, _ = pictures.decode(path, upright)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            raise ViewError(f"can't read {path.name}: {e}") from e
        out = io.BytesIO()
        picture.save(out, "JPEG", quality=JPEG_QUALITY)
        return out.getvalue()


@dataclass(frozen=True)
class Codecs:
    video: str | None
    audio: str | None
    """None when the file has no such stream."""

    @property
    def playable(self) -> bool:
        """Whether browsers play it as it is."""
        return self.video in PLAYABLE_VIDEO_CODECS and (
            self.audio is None or self.audio in PLAYABLE_AUDIO_CODECS
        )


def codecs(path: Path) -> Codecs:
    """The codecs of the video's first video stream (not cover art) and first audio
    stream."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name:stream_disposition=attached_pic",
            "-of",
            "json",
            file_url(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ViewError(f"ffprobe can't read {path.name}: {result.stderr.strip()}")
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except ValueError as e:
        raise ViewError(f"ffprobe's output for {path.name} isn't JSON: {e}") from e

    def first(kind: str) -> str | None:
        for stream in streams:
            cover = stream.get("disposition", {}).get("attached_pic") == 1
            if stream.get("codec_type") == kind and not cover:
                return str(stream.get("codec_name"))
        return None

    return Codecs(video=first("video"), audio=first("audio"))


def conversion_command(path: Path) -> list[str]:
    """ffmpeg, converting the video at `path` to a fragmented MP4 on its stdout.

    `veryfast` trades size for speed: the stream goes straight to one browser on
    the home network. ffmpeg turns the picture upright from the file's rotation."""
    short = MAX_VIDEO_LINES
    return [
        "ffmpeg",
        "-nostdin",
        "-nostats",
        "-v",
        "error",
        "-i",
        file_url(path),
        "-map",
        "0:V:0",
        "-map",
        "0:a:0?",
        "-vf",
        # The short side at most `short`, the other in proportion, both even.
        f"scale='if(gte(iw,ih),-2,min({short},iw))':'if(gte(iw,ih),min({short},ih),-2)'",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]

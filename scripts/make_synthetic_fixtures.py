#!/usr/bin/env python3
"""Generate the tier-1 synthetic fixtures (dev-environment §4), as `just fixtures` does.

    make_synthetic_fixtures.py [--out DIR]

Writes about 30 small photos and videos, drawn from fixed seeds, into DIR
(default backend/tests/fixtures/synthetic/), where they're committed. Pillow and
pillow-heif draw and encode the images, ffmpeg the videos, and exiftool adds the
metadata. The output is byte-identical from run to run, as long as the tools are
the versions pinned in versions.env and backend/uv.lock, so the script refuses to
run with any other exiftool or ffmpeg.

Every file is built in a temporary folder and then copied into DIR. Files in DIR
that aren't part of the set are reported, and the script exits 1 without
deleting them: remove them with `git rm`.

Run it from the backend's environment (`uv run --project backend`), which has
Pillow and pillow-heif.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pillow_heif
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "backend" / "tests" / "fixtures" / "synthetic"
VERSIONS_ENV = REPO_ROOT / "versions.env"

PHOTO_SIZE = (640, 480)
VIDEO_SIZE = (320, 240)
JPEG_QUALITY = 80
HEIC_QUALITY = 50
CAMERA = ["-Make=Synthetic", "-Model=Fixture Cam 1"]


# --- Tools ------------------------------------------------------------------------


def pinned_versions() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in VERSIONS_ENV.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            pins[name.strip()] = value.strip()
    return pins


def tool_versions() -> dict[str, str]:
    """The versions of the exiftool and ffmpeg on PATH, as versions.env writes them."""
    exiftool = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True, check=True)
    ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
    # "ffmpeg version 7.0.2-static https://johnvansickle.com/ffmpeg/ ..."
    ffmpeg_version = ffmpeg.stdout.split()[2].removesuffix("-static")
    return {"EXIFTOOL_VERSION": exiftool.stdout.strip(), "FFMPEG_VERSION": ffmpeg_version}


def version_problems() -> list[str]:
    pins = pinned_versions()
    try:
        have = tool_versions()
    except (OSError, subprocess.CalledProcessError) as e:
        return [f"exiftool and ffmpeg must be on PATH ({e}); run scripts/bootstrap.sh"]
    return [
        f"{name.removesuffix('_VERSION').lower()} is {have[name]}, but versions.env pins "
        f"{pins[name]}; run scripts/bootstrap.sh"
        for name in have
        if have[name] != pins[name]
    ]


def exiftool(path: Path, *args: str) -> None:
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", *args, str(path)],
        check=True,
    )


def ffmpeg(path: Path, *args: str) -> None:
    # One thread and bitexact flags, so the encoders and muxers write the same bytes
    # every time, with no encoder name or creation time.
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            *args,
            "-threads", "1", "-x264-params", "threads=1",
            "-map_metadata", "-1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            str(path),
        ],
        check=True,
    )  # fmt: skip


# --- Drawing ----------------------------------------------------------------------


def scene(seed: int, size: tuple[int, int] = PHOTO_SIZE) -> Image.Image:
    """A landscape of sky, sun, hills and blocks, different for every seed."""
    rng = random.Random(seed)
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    top = (rng.randrange(40, 120), rng.randrange(100, 180), rng.randrange(180, 255))
    bottom = (rng.randrange(180, 255), rng.randrange(150, 230), rng.randrange(100, 200))
    for y in range(height):
        t = y / height
        colour = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        draw.line([(0, y), (width, y)], fill=colour)
    sun = (rng.randrange(width // 8, width * 7 // 8), rng.randrange(height // 10, height // 3))
    radius = rng.randrange(height // 16, height // 8)
    draw.ellipse(
        [sun[0] - radius, sun[1] - radius, sun[0] + radius, sun[1] + radius],
        fill=(255, rng.randrange(200, 250), rng.randrange(80, 160)),
    )
    for layer in range(3):
        base = height * (5 + layer) // 9
        points = [(0, height)]
        for x in range(0, width + 1, width // 8):
            points.append((x, base + rng.randrange(-height // 10, height // 10)))
        points.append((width, height))
        shade = 60 + 40 * layer
        draw.polygon(points, fill=(shade // 3, shade, shade // 2))
    for _ in range(rng.randrange(4, 9)):
        x, y = rng.randrange(width), rng.randrange(height // 2, height)
        w, h = rng.randrange(width // 30, width // 10), rng.randrange(height // 20, height // 6)
        colour = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        draw.rectangle([x, y - h, x + w, y], fill=colour, outline=(20, 20, 20), width=2)
    # Fine grain, like a camera sensor's, so the photo has detail for blur to remove.
    grain = np.random.default_rng(seed).normal(0, 6, (height, width, 1))
    pixels = np.asarray(image, dtype=np.float64) + grain
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def portrait(seed: int) -> Image.Image:
    """A scene with a face-like oval in it, for the face region."""
    image = scene(seed)
    draw = ImageDraw.Draw(image)
    draw.ellipse(FACE_BOX, fill=(224, 182, 150), outline=(90, 60, 40), width=3)
    left, top, right, bottom = FACE_BOX
    eye_y = top + (bottom - top) * 2 // 5
    for eye_x in (left + (right - left) // 3, left + (right - left) * 2 // 3):
        draw.ellipse([eye_x - 6, eye_y - 4, eye_x + 6, eye_y + 4], fill=(40, 30, 30))
    mouth_y = top + (bottom - top) * 3 // 4
    draw.arc(
        [left + 30, mouth_y - 12, right - 30, mouth_y + 8], 20, 160, fill=(120, 40, 40), width=3
    )
    return image


# The face's box in pixels, and as the MWG region writes it: normalised centre and size.
FACE_BOX = (320, 120, 448, 288)
FACE_AREA = {"X": 0.6, "Y": 0.425, "W": 0.2, "H": 0.35}


def screenshot() -> Image.Image:
    """Flat colours, a status bar and lines of text, like a phone screenshot."""
    rng = random.Random(7)
    width, height = 390 * 2, 844 * 2
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 90], fill=(30, 30, 30))
    draw.text((40, 30), "9:41", fill=(255, 255, 255), font_size=36)
    draw.rectangle([0, 90, width, 220], fill=(0, 122, 255))
    draw.text((40, 130), "Messages", fill=(255, 255, 255), font_size=48)
    y = 260
    while y < height - 160:
        mine = rng.random() < 0.5
        w = rng.randrange(width // 3, width * 3 // 4)
        x = width - w - 40 if mine else 40
        h = 70 + 40 * rng.randrange(3)
        fill = (0, 122, 255) if mine else (229, 229, 234)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=30, fill=fill)
        text = " ".join("word"[: rng.randrange(2, 5)] * rng.randrange(1, 3) for _ in range(5))
        draw.text((x + 24, y + 20), text, fill=(255, 255, 255) if mine else (0, 0, 0), font_size=30)
        y += h + 30
    draw.rectangle([0, height - 120, width, height], fill=(240, 240, 240))
    return image


def burst_frame(index: int) -> Image.Image:
    """One frame of a burst: a window onto one larger scene, a few pixels further on."""
    wide = scene(100, (PHOTO_SIZE[0] + 40, PHOTO_SIZE[1] + 20))
    x, y = 6 * index, 3 * index
    return wide.crop((x, y, x + PHOTO_SIZE[0], y + PHOTO_SIZE[1]))


# --- Writers ----------------------------------------------------------------------


def jpeg(image: Image.Image) -> Callable[[Path], None]:
    return lambda path: image.save(path, "JPEG", quality=JPEG_QUALITY)


def heic(image: Image.Image) -> Callable[[Path], None]:
    return lambda path: image.save(path, "HEIF", quality=HEIC_QUALITY)


def png(image: Image.Image) -> Callable[[Path], None]:
    return lambda path: image.save(path, "PNG")


def truncated_jpeg(image: Image.Image, keep_bytes: int) -> Callable[[Path], None]:
    """A JPEG cut off partway, as an interrupted copy leaves it: the header reads, the
    pixels don't."""

    def write(path: Path) -> None:
        image.save(path, "JPEG", quality=JPEG_QUALITY)
        path.write_bytes(path.read_bytes()[:keep_bytes])

    return write


def video(seconds: int, pattern: str, tone_hz: int) -> Callable[[Path], None]:
    """A test pattern with a tone: H.264 and AAC, at low resolution."""
    width, height = VIDEO_SIZE
    return lambda path: ffmpeg(
        path,
        "-f", "lavfi", "-i", f"{pattern}=size={width}x{height}:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={tone_hz}:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "32k", "-shortest",
    )  # fmt: skip


# --- The set ----------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    path: str
    write: Callable[[Path], None]
    exiftool_args: list[str] = field(default_factory=list[str])


def taken(when: str) -> list[str]:
    return [f"-DateTimeOriginal={when}", f"-CreateDate={when}"]


def fixtures() -> list[Fixture]:
    width, height = PHOTO_SIZE
    area = ",".join(f"{k}={FACE_AREA[k]}" for k in "XYWH")
    region = (
        f"-XMP-mwg-rs:RegionInfo={{AppliedToDimensions={{W={width},H={height},Unit=pixel}},"
        f"RegionList=[{{Area={{{area},Unit=normalized}},Name=Alex Example,Type=Face}}]}}"
    )
    keywords = ["beach", "sunset", "café"]
    iptc_utf8 = ["-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8"]
    return [
        # Six distinct scenes, with ordinary camera metadata.
        *(
            Fixture(
                f"scenes/scene-{n:02}.jpg",
                jpeg(scene(n)),
                [*CAMERA, *taken(f"2024:0{n}:1{n} 1{n}:00:00")],
            )
            for n in range(1, 7)
        ),
        # A burst: five frames a second apart, each shifted a few pixels.
        *(
            Fixture(
                f"burst/burst-{n + 1:02}.jpg",
                jpeg(burst_frame(n)),
                [*CAMERA, *taken(f"2024:07:04 18:30:0{n}")],
            )
            for n in range(5)
        ),
        # Poor quality versions of scene 1, and a screenshot.
        Fixture(
            "quality/blurred.jpg",
            jpeg(scene(1).filter(ImageFilter.GaussianBlur(6))),
            [*CAMERA, *taken("2024:08:01 10:00:00")],
        ),
        Fixture(
            "quality/overexposed.jpg",
            jpeg(ImageEnhance.Brightness(scene(1)).enhance(2.6)),
            [*CAMERA, *taken("2024:08:01 10:00:01")],
        ),
        Fixture(
            "quality/underexposed.jpg",
            jpeg(ImageEnhance.Brightness(scene(1)).enhance(0.2)),
            [*CAMERA, *taken("2024:08:01 10:00:02")],
        ),
        Fixture(
            "quality/screenshot.jpg",
            jpeg(screenshot()),
            ["-Software=Synthetic OS 1", "-DateTimeOriginal=2024:08:02 09:41:00"],
        ),
        # Metadata a person or another tool already wrote, for the scanner to import.
        Fixture(
            "metadata/keywords.jpg",
            jpeg(scene(11)),
            [
                *CAMERA,
                *taken("2024:09:01 12:00:00"),
                *iptc_utf8,
                *(f"-IPTC:Keywords={k}" for k in keywords),
                *(f"-XMP-dc:Subject={k}" for k in keywords),
            ],
        ),
        Fixture(
            "metadata/keywords.heic",
            heic(scene(12)),
            [*CAMERA, *taken("2024:09:02 12:00:00"), *(f"-XMP-dc:Subject={k}" for k in keywords)],
        ),
        Fixture(
            "metadata/face-region.jpg",
            jpeg(portrait(13)),
            [
                *CAMERA,
                *taken("2024:09:03 12:00:00"),
                region,
                "-XMP-iptcExt:PersonInImage=Alex Example",
            ],
        ),
        # Each step of the capture-time fallback (plan §6.1), and orientations.
        Fixture("dates/no-dates.jpg", jpeg(scene(21)), CAMERA),
        Fixture(
            "dates/create-date-only.jpg",
            jpeg(scene(22)),
            [*CAMERA, "-EXIF:CreateDate=2023:05:06 07:08:09"],
        ),
        Fixture(
            "dates/modify-date-only.jpg",
            jpeg(scene(23)),
            [*CAMERA, "-EXIF:ModifyDate=2022:03:04 05:06:07"],
        ),
        Fixture(
            "orientation/rotated-90.jpg",
            jpeg(scene(24).rotate(90, expand=True)),
            [*CAMERA, *taken("2024:10:01 12:00:00"), "-Orientation#=6"],
        ),
        Fixture(
            "orientation/bad-orientation.jpg",
            jpeg(scene(25)),
            [*CAMERA, *taken("2024:10:02 12:00:00"), "-Orientation#=9"],
        ),
        # HEIC, as an iPhone writes it.
        Fixture(
            "heic/scene.heic",
            heic(scene(31)),
            ["-Make=Apple", "-Model=iPhone 15", *taken("2024:11:01 08:00:00")],
        ),
        Fixture(
            "heic/no-dates.heic",
            heic(scene(32)),
            ["-Make=Apple", "-Model=iPhone 15"],
        ),
        # Videos: a few seconds of test pattern and tone.
        Fixture(
            "video/clip.mov",
            video(3, "testsrc2", 440),
            ["-QuickTime:CreateDate=2024:12:01 15:00:00"],
        ),
        Fixture(
            "video/clip.mp4",
            video(2, "smptebars", 660),
            ["-QuickTime:CreateDate=2024:12:02 16:00:00"],
        ),
        Fixture(
            "video/keywords.mp4",
            video(2, "rgbtestsrc", 880),
            [
                "-QuickTime:CreateDate=2024:12:03 17:00:00",
                *(f"-XMP-dc:Subject={k}" for k in keywords),
            ],
        ),
        # Files the scanner must cope with: a corrupt one and an unsupported type.
        Fixture("broken/truncated.jpg", truncated_jpeg(scene(41), 4096)),
        Fixture("broken/unsupported.png", png(scene(42))),
    ]


# --- Main -------------------------------------------------------------------------


def generate(out: Path, items: list[Fixture]) -> list[str]:
    """Write `items` into `out` and return the paths of files there that aren't among them."""
    with tempfile.TemporaryDirectory(prefix="synthetic-fixtures-") as tmp:
        staging = Path(tmp)
        for item in items:
            path = staging / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            item.write(path)
            if item.exiftool_args:
                exiftool(path, *item.exiftool_args)
        for item in items:
            target = out / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (staging / item.path).read_bytes()
            if not target.exists() or target.read_bytes() != data:
                target.write_bytes(data)
    wanted = {item.path for item in items}
    return sorted(
        str(p.relative_to(out))
        for p in out.rglob("*")
        if p.is_file() and str(p.relative_to(out)) not in wanted
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the tier-1 synthetic fixtures.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: %(default)s")
    args = parser.parse_args(argv)

    problems = version_problems()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    pillow_heif.register_heif_opener()
    items = fixtures()
    strays = generate(args.out, items)
    print(f"Wrote {len(items)} fixtures to {args.out}")
    if strays:
        print(
            "These files aren't part of the set; remove them with `git rm`:\n  "
            + "\n  ".join(strays),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

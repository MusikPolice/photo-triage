"""Each plan §6.10 field mapping, round-tripped with exiftool on every container type
(dev-environment §6, "Metadata write-back").

Copies of tier-1 fixtures are written the way §6.10 says the worker will write
them, `-overwrite_original_in_place` with the mtime preserved, then read back.
Phase 4's write-back tests can extend `FIELDS` and `_write`.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pillow_heif
import pytest
from PIL import Image

import photo_triage.files

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
OLD_NS = 1_700_000_000_123_456_789  # an mtime with nanoseconds
CONFIG = Path(photo_triage.files.__file__).with_name("exiftool.config")

SOURCES = {
    "jpeg": "scenes/scene-01.jpg",
    "heic": "heic/scene.heic",
    "mov": "video/clip.mov",
    "mp4": "video/clip.mp4",
}

PERSON = "Ada Lovelace"
TAGS = ["dog", "crème", PERSON]  # a person is also a keyword
CAPTION = "A dog, à la plage"
REVIEWED = "quality,duplicates"
REGION = {
    "AppliedToDimensions": {"W": 640, "H": 480, "Unit": "pixel"},
    "RegionList": [
        {
            "Area": {"X": 0.5, "Y": 0.4, "W": 0.2, "H": 0.3, "Unit": "normalized"},
            "Name": PERSON,
            "Type": "Face",
        }
    ],
}
REGION_ARG = (
    "-XMP-mwg-rs:RegionInfo={AppliedToDimensions={W=640,H=480,Unit=pixel},"
    f"RegionList=[{{Area={{X=0.5,Y=0.4,W=0.2,H=0.3,Unit=normalized}},Name={PERSON},Type=Face}}]}}"
)

# What each container stores, as exiftool reads it back (`-G1 -struct`).
XMP = {
    "XMP-dc:Subject": TAGS,
    "XMP-dc:Description": CAPTION,
    "XMP-iptcExt:PersonInImage": [PERSON],
    "XMP-mwg-rs:RegionInfo": REGION,
    "XMP-phototriage:Reviewed": REVIEWED,
}
FIELDS: dict[str, dict[str, Any]] = {
    "jpeg": {**XMP, "IPTC:Keywords": TAGS, "IFD0:ImageDescription": CAPTION},
    # HEIC has no IPTC, so its tags are XMP only.
    "heic": {**XMP, "IFD0:ImageDescription": CAPTION},
    # QuickTime's keywords are one string, so the list is joined.
    "mov": {**XMP, "Keys:Keywords": ", ".join(TAGS)},
    "mp4": {**XMP, "Keys:Keywords": ", ".join(TAGS)},
}


def _exiftool(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["exiftool", "-config", str(CONFIG), *map(str, args)],
        capture_output=True,
        text=True,
        check=True,
    )


def _write(path: Path, container: str) -> str:
    """Write every §6.10 field for `container` as the worker will, returning stderr.

    exiftool's `-P` keeps the mtime to the second only, so the nanoseconds are put
    back afterwards, as the scanner compares `file_mtime_ns` (plan §6.1, §8).
    """
    before = path.stat()
    xmp = [
        *(f"-XMP-dc:Subject={t}" for t in TAGS),
        f"-XMP-dc:Description={CAPTION}",
        f"-XMP-iptcExt:PersonInImage={PERSON}",
        REGION_ARG,
        f"-XMP-phototriage:Reviewed={REVIEWED}",
    ]
    native = {
        "jpeg": [
            "-charset",
            "iptc=UTF8",
            "-IPTC:CodedCharacterSet=UTF8",
            *(f"-IPTC:Keywords={t}" for t in TAGS),
            f"-EXIF:ImageDescription={CAPTION}",
        ],
        "heic": [f"-EXIF:ImageDescription={CAPTION}"],
        "mov": [f"-Keys:Keywords={', '.join(TAGS)}"],
        "mp4": [f"-Keys:Keywords={', '.join(TAGS)}"],
    }[container]
    stderr = _exiftool("-P", "-overwrite_original_in_place", *xmp, *native, path).stderr
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    return stderr


def _read(path: Path) -> dict[str, Any]:
    [item] = json.loads(_exiftool("-json", "-G1", "-struct", "-a", path).stdout)
    return item


def _content_hash(path: Path) -> str:
    """Decoded pixels for photos, the media streams without their container for videos."""
    if path.suffix in {".mov", ".mp4"}:
        return subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0",
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
            check=True,
        ).stdout
    pillow_heif.register_heif_opener()
    with Image.open(path) as image:
        return hashlib.sha256(image.tobytes()).hexdigest()


@pytest.fixture(params=list(SOURCES))
def copy(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, Path]:
    """A copy of one fixture per container, with an old mtime to preserve."""
    container: str = request.param
    path = tmp_path / Path(SOURCES[container]).name
    shutil.copyfile(SYNTHETIC / SOURCES[container], path)
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return container, path


def test_every_field_round_trips(copy: tuple[str, Path]) -> None:
    container, path = copy

    assert _write(path, container) == ""  # no warnings

    item = _read(path)
    assert {tag: item.get(tag) for tag in FIELDS[container]} == FIELDS[container]


def test_writing_leaves_the_content_mtime_and_inode_alone(copy: tuple[str, Path]) -> None:
    container, path = copy
    before = path.stat()
    content = _content_hash(path)

    _write(path, container)

    after = path.stat()
    assert _content_hash(path) == content
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino  # in place, not a new file
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no _original


# --- Why the mapping and the write differ from what you'd expect ------------------------


def test_exiftool_keeps_the_mtime_to_the_second_only(copy: tuple[str, Path]) -> None:
    _, path = copy

    _exiftool("-P", "-overwrite_original_in_place", "-XMP-dc:Subject=dog", path)

    assert path.stat().st_mtime_ns == OLD_NS // 1_000_000_000 * 1_000_000_000


def test_heic_silently_takes_no_iptc(tmp_path: Path) -> None:
    path = tmp_path / "scene.heic"
    shutil.copyfile(SYNTHETIC / SOURCES["heic"], path)

    result = _exiftool("-overwrite_original_in_place", "-IPTC:Keywords=dog", path)

    assert "1 image files unchanged" in result.stdout
    assert "IPTC:Keywords" not in _read(path)


@pytest.mark.parametrize("container", ["mov", "mp4"])
def test_quicktime_keywords_hold_one_string(tmp_path: Path, container: str) -> None:
    path = tmp_path / Path(SOURCES[container]).name
    shutil.copyfile(SYNTHETIC / SOURCES[container], path)

    _exiftool(
        "-overwrite_original_in_place", "-QuickTime:Keywords=dog", "-QuickTime:Keywords=cat", path
    )

    assert _read(path)["Keys:Keywords"] == "cat"  # the last value, not a list

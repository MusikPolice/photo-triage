"""The tier-1 synthetic fixtures (dev-environment §4), and the script that makes them
(scripts/make_synthetic_fixtures.py, `just fixtures`).

These run the pinned exiftool, ffmpeg and ffprobe, which scripts/bootstrap.sh
installs locally and the CI backend job installs from versions.env.
"""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

_SCRIPT = Path(__file__).parents[3] / "scripts" / "make_synthetic_fixtures.py"
_spec = importlib.util.spec_from_file_location("make_synthetic_fixtures", _SCRIPT)
assert _spec and _spec.loader
make_fixtures = importlib.util.module_from_spec(_spec)
sys.modules["make_synthetic_fixtures"] = make_fixtures  # dataclasses look up their module
_spec.loader.exec_module(make_fixtures)

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
VIDEOS = ["video/clip.mov", "video/clip.mp4", "video/keywords.mp4"]
KEYWORDS = ["beach", "sunset", "café"]


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def tags() -> dict[str, dict[str, Any]]:
    """What exiftool reads from each fixture, by path, with numeric values (`-n`)."""
    out = subprocess.run(
        ["exiftool", "-json", "-G1", "-struct", "-n", "-r", "."],
        cwd=SYNTHETIC,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {item["SourceFile"].removeprefix("./"): item for item in json.loads(out)}


def _probe(path: str) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
        cwd=SYNTHETIC,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


# --- The script ------------------------------------------------------------------------


def test_the_tools_are_the_pinned_versions() -> None:
    assert make_fixtures.version_problems() == []


def test_regenerating_the_set_gives_byte_identical_files(tmp_path: Path) -> None:
    assert make_fixtures.main(["--out", str(tmp_path)]) == 0

    assert _hashes(tmp_path) == _hashes(SYNTHETIC), (
        "The committed fixtures differ from what `just fixtures` makes: run it and commit"
    )


def test_reports_files_that_are_not_in_the_set_without_deleting_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    [png] = [f for f in make_fixtures.fixtures() if f.path.endswith(".png")]
    monkeypatch.setattr(make_fixtures, "fixtures", lambda: [png])  # one is enough
    stray = tmp_path / "scenes" / "old.jpg"
    stray.parent.mkdir()
    stray.write_bytes(b"left over")

    assert make_fixtures.main(["--out", str(tmp_path)]) == 1

    assert stray.read_bytes() == b"left over"
    assert "scenes/old.jpg" in capsys.readouterr().err


def test_refuses_to_run_with_other_tool_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        make_fixtures,
        "tool_versions",
        lambda: {"EXIFTOOL_VERSION": "12.00", "FFMPEG_VERSION": "6.1"},
    )

    assert make_fixtures.main(["--out", str(tmp_path)]) == 1

    assert list(tmp_path.iterdir()) == []
    err = capsys.readouterr().err
    assert "exiftool is 12.00, but versions.env pins" in err
    assert "ffmpeg is 6.1" in err


# --- The set -------------------------------------------------------------------------


def test_the_set_is_about_30_files_under_5_mb() -> None:
    files = [p for p in SYNTHETIC.rglob("*") if p.is_file()]

    assert 25 <= len(files) <= 35
    assert sum(p.stat().st_size for p in files) < 5 * 1024 * 1024


def test_covers_every_container_and_an_unsupported_type(tags: dict[str, dict[str, Any]]) -> None:
    types = {item["File:FileType"] for item in tags.values()}

    assert types == {"JPEG", "HEIC", "MOV", "MP4", "PNG"}


@pytest.mark.parametrize("path", VIDEOS)
def test_videos_are_a_few_seconds_at_low_resolution(path: str) -> None:
    probe = _probe(path)
    [video] = [s for s in probe["streams"] if s["codec_type"] == "video"]

    assert 1 <= float(probe["format"]["duration"]) <= 5
    assert video["width"] * video["height"] <= 320 * 240


def test_video_durations(tags: dict[str, dict[str, Any]]) -> None:
    assert [float(_probe(p)["format"]["duration"]) for p in VIDEOS] == [3.0, 2.0, 2.0]
    assert [tags[p]["QuickTime:Duration"] for p in VIDEOS] == [3.0, 2.0, 2.0]


def test_keywords_already_in_files(tags: dict[str, dict[str, Any]]) -> None:
    assert tags["metadata/keywords.jpg"]["IPTC:Keywords"] == KEYWORDS
    assert tags["metadata/keywords.jpg"]["XMP-dc:Subject"] == KEYWORDS
    assert tags["metadata/keywords.heic"]["XMP-dc:Subject"] == KEYWORDS
    assert tags["video/keywords.mp4"]["XMP-dc:Subject"] == KEYWORDS
    assert "IPTC:Keywords" not in tags["scenes/scene-01.jpg"]


def test_a_named_mwg_face_region(tags: dict[str, dict[str, Any]]) -> None:
    item = tags["metadata/face-region.jpg"]

    assert item["XMP-mwg-rs:RegionInfo"] == {
        "AppliedToDimensions": {"W": 640, "H": 480, "Unit": "pixel"},
        "RegionList": [
            {
                "Area": {"X": 0.6, "Y": 0.425, "W": 0.2, "H": 0.35, "Unit": "normalized"},
                "Name": "Alex Example",
                "Type": "Face",
            }
        ],
    }
    assert item["XMP-iptcExt:PersonInImage"] == ["Alex Example"]


@pytest.mark.parametrize(
    ("path", "original", "create", "modify"),
    [
        ("scenes/scene-01.jpg", "2024:01:11 11:00:00", "2024:01:11 11:00:00", None),
        ("heic/scene.heic", "2024:11:01 08:00:00", "2024:11:01 08:00:00", None),
        ("dates/create-date-only.jpg", None, "2023:05:06 07:08:09", None),
        ("dates/modify-date-only.jpg", None, None, "2022:03:04 05:06:07"),
        ("dates/no-dates.jpg", None, None, None),
        ("heic/no-dates.heic", None, None, None),
    ],
)
def test_capture_date_fallbacks(
    tags: dict[str, dict[str, Any]],
    path: str,
    original: str | None,
    create: str | None,
    modify: str | None,
) -> None:
    item = tags[path]

    assert item.get("ExifIFD:DateTimeOriginal") == original
    assert item.get("ExifIFD:CreateDate") == create
    assert item.get("IFD0:ModifyDate") == modify


def test_video_creation_dates(tags: dict[str, dict[str, Any]]) -> None:
    assert [tags[p]["QuickTime:CreateDate"] for p in VIDEOS] == [
        "2024:12:01 15:00:00",
        "2024:12:02 16:00:00",
        "2024:12:03 17:00:00",
    ]


def test_orientations(tags: dict[str, dict[str, Any]]) -> None:
    assert tags["orientation/rotated-90.jpg"]["IFD0:Orientation"] == 6
    assert tags["orientation/bad-orientation.jpg"]["IFD0:Orientation"] == 9  # not 1-8
    assert "IFD0:Orientation" not in tags["scenes/scene-01.jpg"]


def test_a_burst_one_second_apart(tags: dict[str, dict[str, Any]]) -> None:
    burst = sorted(p for p in tags if p.startswith("burst/"))

    assert len(burst) == 5
    assert [tags[p]["ExifIFD:DateTimeOriginal"][-2:] for p in burst] == [
        "00",
        "01",
        "02",
        "03",
        "04",
    ]


def test_the_screenshot_has_no_camera(tags: dict[str, dict[str, Any]]) -> None:
    item = tags["quality/screenshot.jpg"]

    assert "IFD0:Make" not in item
    assert item["File:ImageHeight"] > item["File:ImageWidth"]


def test_the_truncated_jpeg_reads_its_header_but_not_its_pixels() -> None:
    with Image.open(SYNTHETIC / "broken/truncated.jpg") as image:
        assert image.size == (640, 480)
        with pytest.raises(OSError, match="truncated"):
            image.load()

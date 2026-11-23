"""What the scanner reads from inside a file (plan §6.1): the capture-time fallback,
keywords and displayed dimensions, on the odd-EXIF tier-1 fixtures and on tags as
exiftool gives them."""

import datetime as dt
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from photo_triage.db.models import MediaType
from photo_triage.files import ExifTool, MetadataError
from photo_triage.pipeline import metadata

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
ZONE = ZoneInfo("America/Vancouver")
MTIME_NS = 1_733_000_000_987_654_321  # 2024-11-30 20:53:20.98… UTC
MTIME_LOCAL = dt.datetime(2024, 11, 30, 12, 53, 20)  # in Vancouver, whole seconds


def _taken(exiftool: ExifTool, path: Path, kind: MediaType = MediaType.PHOTO) -> dt.datetime:
    return metadata.read(path, kind, exiftool, ZONE, MTIME_NS).taken_at_local


# --- Capture time: each step of the fallback, on the fixtures -------------------------


def test_a_photo_with_date_time_original_uses_it(exiftool: ExifTool) -> None:
    # The screenshot has DateTimeOriginal and no CreateDate.
    assert _taken(exiftool, SYNTHETIC / "quality/screenshot.jpg") == dt.datetime(2024, 8, 2, 9, 41)


def test_a_photo_with_only_create_date_uses_it(exiftool: ExifTool) -> None:
    assert _taken(exiftool, SYNTHETIC / "dates/create-date-only.jpg") == dt.datetime(
        2023, 5, 6, 7, 8, 9
    )


def test_a_photo_with_only_date_time_uses_it(exiftool: ExifTool) -> None:
    # EXIF's DateTime, which exiftool calls ModifyDate.
    assert _taken(exiftool, SYNTHETIC / "dates/modify-date-only.jpg") == dt.datetime(
        2022, 3, 4, 5, 6, 7
    )


def test_a_photo_with_no_dates_uses_its_mtime_in_tz(exiftool: ExifTool) -> None:
    assert _taken(exiftool, SYNTHETIC / "heic/no-dates.heic") == MTIME_LOCAL


def test_a_photo_with_no_dates_filed_by_elodie_uses_its_filename(exiftool: ExifTool) -> None:
    path = SYNTHETIC / "dates/2019/April/2019-04-15_10-20-30-no-dates.jpg"
    assert _taken(exiftool, path) == dt.datetime(2019, 4, 15, 10, 20, 30)


def test_a_video_with_keys_creation_date_uses_its_local_time(
    exiftool: ExifTool, tmp_path: Path
) -> None:
    clip = Path(shutil.copy(SYNTHETIC / "video/clip.mov", tmp_path / "phone.mov"))
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-Keys:CreationDate=2024:12:01 18:30:00+01:00",  # not in TZ: kept as written
            str(clip),
        ],
        check=True,
    )

    assert _taken(exiftool, clip, MediaType.VIDEO) == dt.datetime(2024, 12, 1, 18, 30)


def test_a_video_with_only_create_date_converts_it_from_utc(exiftool: ExifTool) -> None:
    # 15:00 UTC on 1 December is 07:00 in Vancouver.
    assert _taken(exiftool, SYNTHETIC / "video/clip.mov", MediaType.VIDEO) == dt.datetime(
        2024, 12, 1, 7
    )


def test_a_video_with_no_dates_uses_its_mtime_in_tz(exiftool: ExifTool, tmp_path: Path) -> None:
    clip = Path(shutil.copy(SYNTHETIC / "video/clip.mp4", tmp_path / "undated.mp4"))
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-QuickTime:CreateDate=", str(clip)],
        check=True,
    )

    assert _taken(exiftool, clip, MediaType.VIDEO) == MTIME_LOCAL


def test_a_video_with_no_dates_filed_by_elodie_uses_its_filename(
    exiftool: ExifTool, tmp_path: Path
) -> None:
    folder = tmp_path / "2025" / "December"
    folder.mkdir(parents=True)
    clip = Path(shutil.copy(SYNTHETIC / "video/clip.mp4", folder / "2025-12-24_23-59-58-clip.mp4"))
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-QuickTime:CreateDate=", str(clip)],
        check=True,
    )

    assert _taken(exiftool, clip, MediaType.VIDEO) == dt.datetime(2025, 12, 24, 23, 59, 58)


# --- Capture time: the rules, on tags as exiftool gives them --------------------------


def _from(
    tags: dict[str, Any],
    kind: MediaType = MediaType.PHOTO,
    path: str = "photos/IMG_0001.jpg",
) -> dt.datetime:
    return metadata.taken_at_local(tags, kind, ZONE, MTIME_NS, PurePosixPath(path))


def test_photo_dates_are_tried_in_order() -> None:
    tags = {
        "EXIF:IFD0:ModifyDate": "2022:01:01 00:00:00",
        "EXIF:ExifIFD:CreateDate": "2023:01:01 00:00:00",
        "EXIF:ExifIFD:DateTimeOriginal": "2024:01:01 00:00:00",
    }
    assert _from(tags) == dt.datetime(2024, 1, 1)
    del tags["EXIF:ExifIFD:DateTimeOriginal"]
    assert _from(tags) == dt.datetime(2023, 1, 1)
    del tags["EXIF:ExifIFD:CreateDate"]
    assert _from(tags) == dt.datetime(2022, 1, 1)
    del tags["EXIF:IFD0:ModifyDate"]
    assert _from(tags) == MTIME_LOCAL


def test_exif_comes_before_xmp_for_the_same_tag() -> None:
    tags = {
        "XMP:XMP-exif:DateTimeOriginal": "2020:01:01 00:00:00",
        "EXIF:ExifIFD:DateTimeOriginal": "2024:01:01 00:00:00",
    }
    assert _from(tags) == dt.datetime(2024, 1, 1)


def test_xmp_is_used_when_exif_has_no_date() -> None:
    assert _from({"XMP:XMP-exif:DateTimeOriginal": "2020:01:01 00:00:00"}) == dt.datetime(
        2020, 1, 1
    )


@pytest.mark.parametrize("value", ["0000:00:00 00:00:00", "", "    :  :     :  :  ", "2024:01:01"])
def test_a_placeholder_or_partial_date_is_skipped(value: str) -> None:
    tags = {
        "EXIF:ExifIFD:DateTimeOriginal": value,
        "EXIF:ExifIFD:CreateDate": "2023:01:01 00:00:00",
    }
    assert _from(tags) == dt.datetime(2023, 1, 1)


def test_an_offset_and_fractional_seconds_are_dropped() -> None:
    assert _from({"XMP:XMP-exif:DateTimeOriginal": "2024:01:01 10:00:00.25+05:00"}) == (
        dt.datetime(2024, 1, 1, 10)
    )


def test_video_dates_come_from_keys_then_quicktime_in_utc() -> None:
    tags = {
        "QuickTime:CreateDate": "2024:07:01 20:00:00",
        "QuickTime:Keys:CreationDate": "2024:07:01 12:34:56-07:00",
    }
    assert _from(tags, MediaType.VIDEO) == dt.datetime(2024, 7, 1, 12, 34, 56)
    del tags["QuickTime:Keys:CreationDate"]
    assert _from(tags, MediaType.VIDEO) == dt.datetime(2024, 7, 1, 13)  # PDT, UTC-7
    tags["QuickTime:CreateDate"] = "0000:00:00 00:00:00"  # what an unset one reads as
    assert _from(tags, MediaType.VIDEO) == MTIME_LOCAL


def test_xmp_modify_date_is_not_a_capture_time() -> None:
    assert _from({"XMP:XMP-xmp:ModifyDate": "2025:01:01 00:00:00"}) == MTIME_LOCAL


def test_photo_date_tags_are_ignored_for_videos_and_video_ones_for_photos() -> None:
    assert _from({"QuickTime:CreateDate": "2024:07:01 20:00:00"}) == MTIME_LOCAL
    assert _from({"EXIF:ExifIFD:DateTimeOriginal": "2024:01:01 00:00:00"}, MediaType.VIDEO) == (
        MTIME_LOCAL
    )


# --- Capture time: undated files, by where elodie filed them --------------------------

ELODIE = "2019/April/2019-04-15_10-20-30-IMG_0001.jpg"


@pytest.mark.parametrize("kind", [MediaType.PHOTO, MediaType.VIDEO])
def test_an_undated_file_takes_its_elodie_prefix_as_written(kind: MediaType) -> None:
    # As written: elodie's UTC, not converted to TZ.
    assert _from({}, kind, ELODIE) == dt.datetime(2019, 4, 15, 10, 20, 30)


def test_an_undated_file_with_only_a_prefix_takes_it() -> None:
    assert _from({}, path="Imports/2019-04-15_10-20-30-IMG_0001.jpg") == dt.datetime(
        2019, 4, 15, 10, 20, 30
    )


@pytest.mark.parametrize("kind", [MediaType.PHOTO, MediaType.VIDEO])
def test_the_folder_wins_over_a_prefix_from_another_month(kind: MediaType) -> None:
    path = "2019/May/2019-04-30_23-59-59-IMG_0001.jpg"
    assert _from({}, kind, path) == dt.datetime(2019, 5, 1)


@pytest.mark.parametrize("name", ["IMG_0001.jpg", "0000-00-00_00-00-00-IMG_0001.jpg"])
def test_an_undated_file_in_a_month_folder_takes_its_month(name: str) -> None:
    assert _from({}, path=f"photos/2019/April/{name}") == dt.datetime(2019, 4, 1)


@pytest.mark.parametrize("kind", [MediaType.PHOTO, MediaType.VIDEO])
def test_a_dated_file_ignores_its_prefix_and_folder(kind: MediaType) -> None:
    tags = (
        {"EXIF:ExifIFD:DateTimeOriginal": "2024:01:02 03:04:05"}
        if kind == MediaType.PHOTO
        else {"Keys:CreationDate": "2024:01:02 03:04:05-08:00"}
    )
    assert _from(tags, kind, "2019/May/2019-04-30_23-59-59-IMG_0001.jpg") == dt.datetime(
        2024, 1, 2, 3, 4, 5
    )


@pytest.mark.parametrize(
    "path",
    [
        "IMG_0001.jpg",
        "photos/IMG_0001.jpg",
        "0000-00-00_00-00-00-IMG_0001.jpg",
        "2019-02-30_10-20-30-IMG_0001.jpg",  # no 30 February
        "2019-04-15_25-20-30-IMG_0001.jpg",  # no 25 o'clock
        "2019-04-15_10-20-30IMG_0001.jpg",  # no `-` after the time
        "x2019-04-15_10-20-30-IMG_0001.jpg",  # not at the start
        "2019/Spring/IMG_0001.jpg",  # not a month
        "2019/april/IMG_0001.jpg",  # not as elodie writes it
        "Holidays/April/IMG_0001.jpg",  # not in a year folder
        "0000/April/IMG_0001.jpg",  # no year 0
        "April/IMG_0001.jpg",
    ],
)
def test_an_undated_file_with_neither_uses_its_mtime(path: str) -> None:
    assert _from({}, path=path) == MTIME_LOCAL


# --- Keywords ---------------------------------------------------------------------------


def test_keywords_from_every_field_are_kept_once_each_in_order() -> None:
    tags = {"IPTC:Keywords": ["beach", "sunset"], "XMP:XMP-dc:Subject": ["sunset", "café"]}
    assert metadata.keywords(tags) == ("beach", "sunset", "café")
    video = {"QuickTime:Keys:Keywords": "dog", "QuickTime:ItemList:Keywords": "beach, dog"}
    assert metadata.keywords(video) == ("dog", "beach")


def test_keywords_are_trimmed_blank_ones_dropped_and_numbers_kept_as_text() -> None:
    tags = {"IPTC:Keywords": [" 2024 ", "", "  "], "XMP:XMP-dc:Subject": 1999}
    assert metadata.keywords(tags) == ("2024", "1999")


def test_quicktime_keywords_are_split_on_commas() -> None:
    tags = {"QuickTime:Keys:Keywords": "dog, crème,Ada Lovelace"}
    assert metadata.keywords(tags) == ("dog", "crème", "Ada Lovelace")


def test_with_xmp_subject_quicktime_s_joined_keywords_are_left_out() -> None:
    tags = {
        "XMP:XMP-dc:Subject": ["Smith, Ada", "dog"],
        "QuickTime:Keys:Keywords": "Smith, Ada, dog",
    }
    assert metadata.keywords(tags) == ("Smith, Ada", "dog")


def test_other_fields_with_the_same_names_are_not_keywords() -> None:
    tags = {"XMP:XMP-pdf:Keywords": "pdf", "XMP:XMP-lr:Subject": "other"}
    assert metadata.keywords(tags) == ()


def test_the_fixtures_keywords_are_read(exiftool: ExifTool) -> None:
    for path in ["metadata/keywords.jpg", "metadata/keywords.heic", "video/keywords.mp4"]:
        kind = MediaType.VIDEO if path.endswith(".mp4") else MediaType.PHOTO
        found = metadata.read(SYNTHETIC / path, kind, exiftool, ZONE, MTIME_NS)
        assert found.keywords == ("beach", "sunset", "café"), path


# --- Dimensions and duration ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "size", "orientation"),
    [
        ("scenes/scene-01.jpg", (640, 480), None),
        ("orientation/rotated-90.jpg", (640, 480), 6),  # stored 480 by 640
        ("orientation/bad-orientation.jpg", (640, 480), None),  # 9 isn't an orientation
        ("quality/screenshot.jpg", (780, 1688), None),
        ("heic/scene.heic", (640, 480), None),
    ],
)
def test_photo_dimensions_are_as_displayed(
    exiftool: ExifTool, path: str, size: tuple[int, int], orientation: int | None
) -> None:
    found = metadata.read(SYNTHETIC / path, MediaType.PHOTO, exiftool, ZONE, MTIME_NS)
    assert (found.width, found.height, found.duration_s) == (*size, None)
    assert found.orientation == orientation


def test_a_turned_heic_is_measured_as_displayed(exiftool: ExifTool, tmp_path: Path) -> None:
    photo = tmp_path / "turned.heic"
    exif = Image.Exif()
    exif[0x0112] = 6  # pillow-heif writes it as the HEIF's own rotation
    Image.new("RGB", (64, 32)).save(photo, exif=exif.tobytes())

    found = metadata.read(photo, MediaType.PHOTO, exiftool, ZONE, MTIME_NS)
    assert (found.width, found.height) == (32, 64)


def test_video_dimensions_and_duration_come_from_ffprobe(exiftool: ExifTool) -> None:
    found = metadata.read(SYNTHETIC / "video/clip.mov", MediaType.VIDEO, exiftool, ZONE, MTIME_NS)
    assert (found.width, found.height, found.duration_s) == (320, 240, 3.0)
    assert found.orientation is None


def test_a_rotated_video_is_measured_as_displayed(exiftool: ExifTool, tmp_path: Path) -> None:
    turned = tmp_path / "portrait.mov"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-display_rotation", "90",
            "-i", str(SYNTHETIC / "video/clip.mov"), "-c", "copy", str(turned),
        ],
        check=True,
    )  # fmt: skip

    found = metadata.read(turned, MediaType.VIDEO, exiftool, ZONE, MTIME_NS)
    assert (found.width, found.height) == (240, 320)


def test_a_photo_that_cannot_be_opened_raises(exiftool: ExifTool, tmp_path: Path) -> None:
    junk = tmp_path / "junk.jpg"
    junk.write_bytes(b"not a jpeg")
    with pytest.raises(MetadataError, match=r"junk\.jpg"):
        metadata.read(junk, MediaType.PHOTO, exiftool, ZONE, MTIME_NS)


def test_a_video_that_cannot_be_probed_raises(exiftool: ExifTool, tmp_path: Path) -> None:
    junk = tmp_path / "junk.mov"
    junk.write_bytes(b"not a movie")
    with pytest.raises(MetadataError, match=r"junk\.mov"):
        metadata.read(junk, MediaType.VIDEO, exiftool, ZONE, MTIME_NS)

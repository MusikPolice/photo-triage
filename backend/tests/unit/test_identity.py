"""The content hash that identifies an item wherever its file goes (plan §6.1)."""

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from photo_triage.db.models import MediaType
from photo_triage.pipeline.identity import HashError, content_hash, media_type

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"

PINNED = {
    "scenes/scene-01.jpg": "28165edf9d559c1ddc5c7068901bb96a7a3273e81afab967104d47823156061e",
    "heic/scene.heic": "6b53e989aa6b8f0b24e12c35a58712f97f7d85ef4beba7f4a9e9f0d97e2310ae",
    "video/clip.mov": "cfd3acfe92caa0589f80eb9d21cec2c85213e27af4164e4ee93e10948b72e4bf",
    "video/clip.mp4": "09446b148b047931a0ea180df58346a2e227f07f4123d8897183f36d8ae66842",
}


def _hash(path: Path) -> str:
    kind = media_type(path)
    assert kind is not None
    return content_hash(path, kind)


def _copy(name: str, tmp_path: Path) -> Path:
    path = tmp_path / Path(name).name
    shutil.copyfile(SYNTHETIC / name, path)
    return path


@pytest.mark.parametrize(("name", "expected"), PINNED.items())
def test_the_hash_of_each_container_is_pinned(name: str, expected: str) -> None:
    """Fails if a Pillow, pillow-heif or ffmpeg upgrade decodes differently.

    Every file hashed after such an upgrade would look new, so moves would lose their
    history. Don't just update the hash: the library needs rehashing in place first,
    by a migration or a stage that keeps each item.
    """
    assert _hash(SYNTHETIC / name) == expected


@pytest.mark.parametrize("name", PINNED)
def test_writing_metadata_leaves_the_hash_alone(name: str, tmp_path: Path) -> None:
    path = _copy(name, tmp_path)
    before = _hash(path)

    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-XMP-dc:Subject=dog",
            "-XMP-dc:Description=A dog",
            "-DateTimeOriginal=2001:02:03 04:05:06",
            str(path),
        ],
        check=True,
    )

    assert _hash(path) == before


@pytest.mark.parametrize(
    ("name", "suffix"), [("video/clip.mov", ".mp4"), ("video/clip.mp4", ".mov")]
)
def test_remuxing_a_video_into_another_container_leaves_the_hash_alone(
    name: str, suffix: str, tmp_path: Path
) -> None:
    remuxed = tmp_path / f"remuxed{suffix}"
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-i", str(SYNTHETIC / name)),
            *("-map", "0", "-c", "copy", "-movflags", "+faststart", str(remuxed)),
        ],
        check=True,
    )
    assert remuxed.read_bytes() != (SYNTHETIC / name).read_bytes()

    assert _hash(remuxed) == _hash(SYNTHETIC / name)


def test_different_pixels_give_a_different_hash(tmp_path: Path) -> None:
    path = _copy("scenes/scene-01.jpg", tmp_path)
    with Image.open(path) as image:
        changed = image.copy()
    changed.putpixel((0, 0), (255, 0, 255))
    edited = tmp_path / "edited.png"
    changed.save(edited)  # lossless, so only that pixel differs

    assert content_hash(edited, MediaType.PHOTO) != _hash(path)


def test_the_same_pixels_in_another_shape_give_a_different_hash(tmp_path: Path) -> None:
    wide, tall = tmp_path / "wide.png", tmp_path / "tall.png"
    Image.new("RGB", (4, 2), "red").save(wide)
    Image.new("RGB", (2, 4), "red").save(tall)

    assert content_hash(wide, MediaType.PHOTO) != content_hash(tall, MediaType.PHOTO)


def test_different_videos_give_different_hashes() -> None:
    assert _hash(SYNTHETIC / "video/clip.mp4") != _hash(SYNTHETIC / "video/keywords.mp4")


def test_a_truncated_photo_raises_hash_error() -> None:
    with pytest.raises(HashError, match=r"truncated\.jpg"):
        _hash(SYNTHETIC / "broken/truncated.jpg")


def test_a_file_that_is_not_a_video_raises_hash_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.mov"
    path.write_bytes(b"not a video")

    with pytest.raises(HashError, match=r"fake\.mov"):
        content_hash(path, MediaType.VIDEO)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.jpg", MediaType.PHOTO),
        ("a.JPEG", MediaType.PHOTO),
        ("a.HEIC", MediaType.PHOTO),
        ("a.heif", MediaType.PHOTO),
        ("a.MOV", MediaType.VIDEO),
        ("a.mp4", MediaType.VIDEO),
        ("a.png", None),
        ("a.txt", None),
        ("jpg", None),
    ],
)
def test_media_type_by_suffix(name: str, expected: MediaType | None) -> None:
    assert media_type(Path(name)) == expected

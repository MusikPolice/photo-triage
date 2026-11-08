"""Drawing a thumbnail: upright, the right size, never upscaled (plan §6.2)."""

from pathlib import Path

import pytest
from PIL import Image

from photo_triage.pipeline.thumbnails import ThumbnailError, draw, encode

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
ORIENTATION = 0x0112


def _half_red(path: Path, orientation: int, size: tuple[int, int] = (200, 100)) -> Path:
    """A JPEG whose stored top half is red and bottom half blue."""
    image = Image.new("RGB", size, "blue")
    image.paste("red", (0, 0, size[0], size[1] // 2))
    exif = Image.Exif()
    exif[ORIENTATION] = orientation
    image.save(path, "JPEG", exif=exif, quality=95)
    return path


def _red_at(image: Image.Image, xy: tuple[int, int]) -> bool:
    r, _, b = image.getpixel(xy)  # type: ignore[misc]
    return r > 200 and b < 60


@pytest.mark.parametrize(
    ("fixture", "size"),
    [
        ("scenes/scene-01.jpg", (400, 300)),
        ("orientation/rotated-90.jpg", (400, 300)),  # stored 480 by 640, shown turned
        ("orientation/bad-orientation.jpg", (400, 300)),  # orientation 9: left as stored
        ("heic/scene.heic", (400, 300)),
        ("quality/screenshot.jpg", (185, 400)),  # tall: the long edge is the height
    ],
)
def test_the_long_edge_is_the_size_asked_for_and_the_shape_is_kept(
    fixture: str, size: tuple[int, int]
) -> None:
    assert draw(SYNTHETIC / fixture, 400).size == size


def test_the_map_size_keeps_the_shape_too() -> None:
    assert draw(SYNTHETIC / "scenes/scene-01.jpg", 64).size == (64, 48)


def test_a_small_picture_is_not_upscaled(tmp_path: Path) -> None:
    small = _half_red(tmp_path / "small.jpg", 1, (120, 90))

    assert draw(small, 400).size == (120, 90)


@pytest.mark.parametrize(
    ("orientation", "red_corner"),
    [
        (1, (0, 0)),  # as stored: red on top
        (3, (199, 99)),  # turned 180°: red at the bottom
        (6, (99, 0)),  # turned 90° clockwise: red on the right
        (8, (0, 199)),  # turned 90° the other way: red on the left
    ],
)
def test_the_exif_orientation_turns_the_picture(
    tmp_path: Path, orientation: int, red_corner: tuple[int, int]
) -> None:
    drawn = draw(_half_red(tmp_path / "turned.jpg", orientation), 400)

    assert _red_at(drawn, red_corner)


def test_a_picture_that_cannot_be_read_raises(tmp_path: Path) -> None:
    with pytest.raises(ThumbnailError, match=r"can't draw truncated\.jpg"):
        draw(SYNTHETIC / "broken/truncated.jpg", 400)


def test_encoding_the_same_picture_twice_gives_the_same_bytes() -> None:
    first = encode(draw(SYNTHETIC / "heic/scene.heic", 400))

    assert encode(draw(SYNTHETIC / "heic/scene.heic", 400)) == first
    assert first.startswith(b"\xff\xd8")  # a JPEG

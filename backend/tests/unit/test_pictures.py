"""Decoding photos that may be damaged (`photo_triage.pipeline.pictures`, #95)."""

from pathlib import Path

import pytest
from PIL import Image, ImageFile

from photo_triage.db.models import MediaType
from photo_triage.pipeline.identity import HashError, hash_content
from photo_triage.pipeline.pictures import decode

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"


def test_a_whole_photo_is_not_damaged() -> None:
    size, damaged = decode(SYNTHETIC / "scenes/scene-01.jpg", lambda i: (i.load(), i.size)[1])

    assert (size, damaged) == ((640, 480), False)


def test_a_damaged_photo_hashes_the_same_every_time() -> None:
    first = hash_content(SYNTHETIC / "broken/damaged.jpg", MediaType.PHOTO)
    again = hash_content(SYNTHETIC / "broken/damaged.jpg", MediaType.PHOTO)

    assert first == again
    assert first[1] is True


def test_a_photo_with_no_picture_data_still_fails() -> None:
    with pytest.raises(HashError, match=r"truncated\.jpg"):
        hash_content(SYNTHETIC / "broken/truncated.jpg", MediaType.PHOTO)


def test_other_errors_are_not_retried(tmp_path: Path) -> None:
    calls: list[bool] = []

    def failing(image: Image.Image) -> None:
        calls.append(ImageFile.LOAD_TRUNCATED_IMAGES)
        raise OSError("something else broke")

    with pytest.raises(OSError, match="something else"):
        decode(SYNTHETIC / "scenes/scene-01.jpg", failing)
    assert calls == [False]


def test_truncated_decoding_is_switched_off_again() -> None:
    decode(SYNTHETIC / "broken/damaged.jpg", lambda image: image.tobytes())

    assert ImageFile.LOAD_TRUNCATED_IMAGES is False

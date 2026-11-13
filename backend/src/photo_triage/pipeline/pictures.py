"""Decoding photos that may be damaged (#95, plan §6.1).

A photo whose file was cut short, by an interrupted copy or a failing disk, still
holds the top of its picture. Pillow refuses it by default. `decode` tries the
strict way first and, only if Pillow says the file is truncated, again with
truncated decoding allowed: the missing part comes out grey, and the caller is
told the picture is damaged. A file with no picture data at all still fails.

Pillow's switch for this is process-wide, so it's turned on only around the retry.
The worker decodes on one thread; nothing else in the process decodes photos.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageFile


def decode[T](path: Path, transform: Callable[[Image.Image], T]) -> tuple[T, bool]:
    """`transform` applied to the opened photo, which must load its pixels (e.g.
    `tobytes`, `convert`), and whether the photo is damaged: only partly readable.

    Raises what Pillow raised strictly if the photo can't be read at all, or fails
    for any reason other than truncation.
    """
    try:
        with Image.open(path) as image:
            return transform(image), False
    except OSError as strict:
        if "truncated" not in str(strict).lower():
            raise
        try:
            with _truncated_allowed(), Image.open(path) as image:
                return transform(image), True
        except OSError:
            raise strict from None


@contextmanager
def _truncated_allowed() -> Generator[None]:
    before = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        yield
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = before

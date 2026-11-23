"""Duplicate grouping's pieces (plan §6.7): similar pairs blockwise, connected
components, and the keeper ranking."""

import datetime as dt
import math

import numpy as np
import pytest

from photo_triage.db.models import Item, MediaType
from photo_triage.pipeline import duplicates


def at_angle(cos: float, axis: int = 1) -> np.ndarray:
    """A unit vector whose similarity to the first axis is `cos`, leaning towards
    `axis`."""
    vector = np.zeros(8, np.float32)
    vector[0], vector[axis] = cos, math.sqrt(1 - cos * cos)
    return vector


def test_pairs_at_or_above_the_threshold_are_found_once_each() -> None:
    vectors = np.stack(
        [at_angle(1), at_angle(0.951), at_angle(0.949, axis=2), -at_angle(1), -at_angle(0.99, 3)]
    )

    pairs = duplicates.similar_pairs(vectors, 0.95)

    assert sorted(map(tuple, pairs.tolist())) == [(0, 1), (3, 4)]


def test_pairs_across_blocks_are_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duplicates, "BLOCK_ROWS", 2)
    rng = np.random.default_rng(1)
    vectors = rng.normal(size=(9, 32)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors[7] = vectors[0]
    vectors[4] = vectors[3]

    pairs = duplicates.similar_pairs(vectors, 0.95)

    assert sorted(map(tuple, pairs.tolist())) == [(0, 7), (3, 4)]


def test_no_vectors_no_pairs() -> None:
    assert duplicates.similar_pairs(np.empty((0, 8), np.float32), 0.95).shape == (0, 2)


def test_components_follow_chains_and_drop_singletons() -> None:
    # a~b~c chain into one group even though a and c aren't linked.
    groups = duplicates.components(7, [(0, 1), (1, 2), (5, 4), (4, 4)])

    assert groups == [[0, 1, 2], [4, 5]]


def _item(item_id: int, w: int | None, h: int | None, size: int, taken: str | None) -> Item:
    return Item(
        id=item_id,
        path=f"{item_id}.jpg",
        media_type=MediaType.PHOTO,
        width=w,
        height=h,
        file_size_bytes=size,
        file_mtime_ns=0,
        taken_at_local=dt.datetime.fromisoformat(taken) if taken else None,
    )


def test_the_keeper_has_the_most_pixels_then_bytes_then_the_earliest_date() -> None:
    items = [
        _item(1, 100, 100, 9_000, "2020-01-01"),
        _item(2, 200, 100, 1_000, "2021-01-01"),  # most pixels
        _item(3, 100, 200, 2_000, "2022-01-01"),  # as many, more bytes
        _item(4, 100, 200, 2_000, "2019-01-01"),  # as many, as large, earlier
        _item(5, 100, 200, 2_000, None),  # undated after dated
        _item(6, 100, 200, 2_000, None),  # tie: the lower id first
        _item(7, None, None, 50_000, "2000-01-01"),  # no size known: fewest pixels
    ]

    assert [i.id for i in duplicates.rank(items)] == [4, 3, 5, 6, 2, 1, 7]

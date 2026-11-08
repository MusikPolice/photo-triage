"""When in a video its frames are sampled (plan §6.1)."""

import itertools
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photo_triage.pipeline.frames import timestamps

durations_s = st.floats(min_value=0.001, max_value=1e6, allow_nan=False)
counts = st.integers(min_value=1, max_value=50)
frame_gaps_s = st.one_of(st.just(0.0), st.floats(min_value=1 / 240, max_value=1.0))


@given(durations_s, counts, frame_gaps_s)
def test_frames_are_evenly_spaced_inside_the_middle_ninety_percent(
    duration_s: float, count: int, gap_s: float
) -> None:
    times = timestamps(duration_s, count, min_gap_s=gap_s)

    assert 1 <= len(times) <= count
    low, high = 0.05 * duration_s, 0.95 * duration_s
    assert all(low - 1e-9 * duration_s <= t <= high + 1e-9 * duration_s for t in times)
    steps = [b - a for a, b in itertools.pairwise(times)]
    assert all(step > 0 for step in steps)  # strictly increasing
    if steps:
        assert max(steps) - min(steps) <= 1e-9 * duration_s  # evenly spaced
        assert min(steps) >= gap_s * (1 - 1e-9)  # each a distinct frame
        assert math.isclose(times[0], low) and math.isclose(times[-1], high)


@given(durations_s, counts)
def test_every_frame_is_sampled_when_the_frames_are_far_enough_apart(
    duration_s: float, count: int
) -> None:
    assert len(timestamps(duration_s, count)) == count


def test_five_frames_from_ten_seconds() -> None:
    assert timestamps(10.0, 5) == pytest.approx([0.5, 2.75, 5.0, 7.25, 9.5])


def test_a_clip_too_short_for_every_frame_gives_as_many_as_it_can() -> None:
    # 0.18 s between 5% and 95% holds one frame every 0.1 s at 10 fps: two of them.
    assert timestamps(0.2, 5, min_gap_s=0.1) == pytest.approx([0.01, 0.19])
    # Shorter than a frame: one, in the middle.
    assert timestamps(0.05, 5, min_gap_s=0.1) == [0.025]


def test_one_frame_is_the_middle() -> None:
    assert timestamps(8.0, 1) == [4.0]


def test_a_video_with_no_length_gives_one_frame_at_the_start() -> None:
    assert timestamps(0.0, 5) == [0.0]


def test_at_least_one_frame_is_asked_for() -> None:
    with pytest.raises(ValueError, match="at least one"):
        timestamps(10.0, 0)

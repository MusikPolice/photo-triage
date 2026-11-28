"""The quality signals (plan §6.8) on the tier-1 fixtures and on made-up pictures."""

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from photo_triage.pipeline.quality import (
    ANALYSIS_LONG_EDGE_PX,
    Exposure,
    blur_score,
    exposure_score,
    grayscale,
    measure_exposure,
    resolution_score,
    signals,
)

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"


def _gray(name: str) -> np.ndarray:
    with Image.open(SYNTHETIC / name) as image:
        return grayscale(image)


def _signals(name: str):
    gray = _gray(name)
    return signals([gray], gray.shape[1], gray.shape[0])


def test_the_blurred_fixture_scores_lower_on_blur_than_its_sharp_scene() -> None:
    sharp, blurred = _signals("scenes/scene-01.jpg"), _signals("quality/blurred.jpg")
    assert sharp.blur > 0.9
    assert blurred.blur < 0.1


@pytest.mark.parametrize("name", ["quality/overexposed.jpg", "quality/underexposed.jpg"])
def test_badly_exposed_fixtures_score_lower_on_exposure_than_a_normal_scene(name: str) -> None:
    normal = _signals("scenes/scene-01.jpg")
    assert normal.exposure == 1.0
    assert _signals(name).exposure < 0.6


def test_overexposure_is_clipped_white_and_underexposure_is_dark_overall() -> None:
    over, under = _signals("quality/overexposed.jpg"), _signals("quality/underexposed.jpg")
    assert over.bright_clipped_fraction > 0.2
    assert over.dark_clipped_fraction < 0.01
    # It isn't clipped: noise keeps its shadows off black. It's dark overall.
    assert under.dark_clipped_fraction < 0.02
    assert under.mean_brightness < 0.15


def test_clipping_counts_pixels_within_two_levels_of_black_or_white() -> None:
    gray = np.array([[0, 2, 3, 128], [252, 253, 255, 128]], np.float32)
    exposure = measure_exposure(gray)
    assert exposure.dark_clipped_fraction == 2 / 8
    assert exposure.bright_clipped_fraction == 2 / 8


def test_clipping_within_the_allowance_costs_nothing() -> None:
    assert exposure_score(Exposure(0.02, 0.02, 0.5)) == 1.0
    assert exposure_score(Exposure(0.12, 0.0, 0.5)) == pytest.approx(0.5)
    assert exposure_score(Exposure(0.0, 0.3, 0.5)) == 0.0


@pytest.mark.parametrize(
    ("mean", "expected"),
    [(0.5, 1.0), (0.15, 1.0), (0.85, 1.0), (0.09, 0.5), (0.91, 0.5), (0.03, 0.0), (0.99, 0.0)],
)
def test_a_picture_too_dark_or_bright_overall_scores_lower(mean: float, expected: float) -> None:
    assert exposure_score(Exposure(0.0, 0.0, mean)) == pytest.approx(expected)


@given(
    dark=st.floats(0, 1),
    bright=st.floats(0, 1),
    mean=st.floats(0, 1),
)
def test_exposure_stays_in_0_to_1(dark: float, bright: float, mean: float) -> None:
    assert 0.0 <= exposure_score(Exposure(dark, bright, mean)) <= 1.0


def test_resolution_is_half_at_2_mp_and_rises_with_megapixels() -> None:
    assert resolution_score(2000, 1000) == pytest.approx(0.5)
    scores = [resolution_score(width, width * 3 // 4) for width in (640, 1600, 2000, 4000, 8000)]
    assert scores == sorted(scores)
    assert scores[0] < 0.05
    assert scores[-1] > 0.99


def test_a_flat_picture_has_no_edges_so_scores_as_blurry() -> None:
    assert blur_score(np.full((50, 50), 128, np.float32)) == 0.0
    assert blur_score(np.zeros((2, 2), np.float32)) == 0.0


def test_a_large_picture_is_scored_at_the_analysis_size() -> None:
    image = Image.new("RGB", (4000, 3000), "gray")
    assert grayscale(image).shape == (768, ANALYSIS_LONG_EDGE_PX)
    small = Image.new("RGB", (640, 480), "gray")
    assert grayscale(small).shape == (480, 640)


def test_a_video_s_signals_are_the_medians_of_its_frames() -> None:
    sharp, blurred = _gray("scenes/scene-01.jpg"), _gray("quality/blurred.jpg")
    dark = _gray("quality/underexposed.jpg")
    frames = [sharp, blurred, sharp, dark, sharp]
    found = signals(frames, 640, 480)
    singles = [signals([frame], 640, 480) for frame in frames]
    for field in ("blur", "exposure", "mean_brightness", "dark_clipped_fraction"):
        values = sorted(getattr(s, field) for s in singles)
        assert getattr(found, field) == values[2], field
    assert found.resolution == resolution_score(640, 480)

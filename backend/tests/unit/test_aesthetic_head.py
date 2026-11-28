"""The aesthetic predictor's head (plan §6.8), read without torch, and its mapping
to 0-1 (`photo_triage.pipeline.quality.aesthetic_score`)."""

import io
import pickle
import zipfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from numpy.typing import NDArray

from photo_triage.ml import FakeClip, ModelMissingError
from photo_triage.ml.aesthetic import FakeHead, LinearHead, read_state_dict, weights_path
from photo_triage.pipeline.quality import aesthetic_score

Writer = Callable[[Path, NDArray[np.float32], float], None]


def _weights(dim: int = 512) -> NDArray[np.float32]:
    return np.linspace(-1, 1, dim, dtype=np.float32)


def test_the_head_loads_its_weight_and_bias_from_the_archive(
    tmp_path: Path, write_linear_pth: Writer
) -> None:
    path = weights_path(tmp_path, "ViT-B-32")
    assert path is not None
    write_linear_pth(path, _weights(), 3.5)

    head = LinearHead.load(tmp_path, "ViT-B-32")

    np.testing.assert_array_equal(head.weight, _weights())
    assert head.bias == 3.5
    vector = FakeClip().embed_texts(["a dog"])[0]
    assert head.rating(vector) == pytest.approx(float(vector @ _weights()) + 3.5, abs=1e-5)


def test_missing_weights_say_where_they_should_be(tmp_path: Path) -> None:
    with pytest.raises(ModelMissingError, match=str(tmp_path / "aesthetic")):
        LinearHead.load(tmp_path, "ViT-B-32")


def test_a_clip_model_without_a_predictor_has_none(tmp_path: Path) -> None:
    assert weights_path(tmp_path, "ViT-L-14") is None
    with pytest.raises(ModelMissingError, match="ViT-L-14"):
        LinearHead.load(tmp_path, "ViT-L-14")


def test_a_pickle_naming_anything_else_is_refused(tmp_path: Path) -> None:
    """The unpickler runs nothing the weights name, beyond rebuilding tensors."""
    buffer = io.BytesIO()
    pickle.dump(print, buffer, protocol=2)  # GLOBAL __builtin__.print
    path = tmp_path / "evil.pth"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", buffer.getvalue())

    with pytest.raises(pickle.UnpicklingError, match=r"\.print in the weights"):
        read_state_dict(path)


def test_the_fake_head_is_deterministic_and_spreads_its_ratings() -> None:
    vectors = FakeClip().embed_texts([f"text {n}" for n in range(200)])
    ratings = np.array([FakeHead().rating(v) for v in vectors])
    assert list(ratings) == [FakeHead().rating(v) for v in vectors]
    assert 4.0 < ratings.mean() < 5.0
    assert ratings.min() < 3.5 and ratings.max() > 5.5


def test_the_rating_maps_to_0_at_2_5_half_at_4_5_and_1_at_6_5() -> None:
    assert aesthetic_score(2.5) == 0.0
    assert aesthetic_score(4.5) == 0.5
    assert aesthetic_score(6.5) == 1.0
    assert aesthetic_score(1.0) == 0.0
    assert aesthetic_score(9.0) == 1.0


@given(st.floats(-100, 100), st.floats(-100, 100))
def test_the_mapping_is_in_0_to_1_and_keeps_the_order(a: float, b: float) -> None:
    low, high = sorted((a, b))
    assert 0.0 <= aesthetic_score(low) <= aesthetic_score(high) <= 1.0

"""How the `clip` stage combines and stores vectors (plan §6.1, §6.3)."""

import numpy as np
from PIL import Image

from photo_triage.ml import FakeClip
from photo_triage.pipeline.embed import from_blob, mean_vector, to_blob


def _frames() -> list[Image.Image]:
    return [Image.new("RGB", (320, 240), (40 * n, 90, 200 - 30 * n)) for n in range(5)]


def test_a_videos_embedding_is_the_mean_of_its_frames_made_unit_length() -> None:
    vectors = FakeClip().embed_images(_frames())

    mean = mean_vector(vectors)

    assert mean.shape == (512,)
    assert mean.dtype == np.float32
    assert np.isclose(np.linalg.norm(mean), 1.0, rtol=1e-6)
    # The mean's direction: the plain mean is shorter, since the frames differ.
    plain = vectors.mean(axis=0)
    assert np.linalg.norm(plain) < 0.99
    np.testing.assert_allclose(mean, plain / np.linalg.norm(plain), rtol=1e-6)


def test_one_frame_is_its_own_mean() -> None:
    vectors = FakeClip().embed_images(_frames()[:1])

    np.testing.assert_allclose(mean_vector(vectors), vectors[0], atol=1e-7)


def test_a_vector_is_stored_as_little_endian_float32() -> None:
    vector = FakeClip().embed_texts(["a dog"])[0]

    blob = to_blob(vector)

    assert len(blob) == 512 * 4
    assert blob == vector.astype("<f4").tobytes()
    np.testing.assert_array_equal(from_blob(blob), vector)

"""The CLIP adapters without the real model: the fake, preprocessing, and how the ONNX
sessions are set up (plan §6.3, §6.11). The real model is in `tests/models`."""

import json
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
from PIL import Image

from photo_triage.ml import FakeClip, ModelMissingError, OnnxClip
from photo_triage.ml import clip as clip_module
from photo_triage.ml.clip import ClipConfig, preprocess

CONFIG = ClipConfig(
    model="ViT-B-32",
    image_size=224,
    mean=(0.5, 0.5, 0.5),
    std=(0.25, 0.25, 0.25),
    context_length=77,
    embedding_dim=512,
)


def _picture(colour: str, size: tuple[int, int] = (64, 48)) -> Image.Image:
    return Image.new("RGB", size, colour)


def test_the_fake_gives_the_same_vector_for_the_same_content() -> None:
    fake = FakeClip()
    first = fake.embed_images([_picture("red"), _picture("blue")])
    again = FakeClip().embed_images([_picture("red").convert("RGBA"), _picture("blue")])

    assert first.shape == (2, 512)
    assert first.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(first, axis=1), 1.0, rtol=1e-6)
    np.testing.assert_array_equal(first, again)
    assert float(first[0] @ first[1]) < 0.5  # different content, a different vector


def test_the_fake_tells_pictures_of_the_same_colour_apart_by_size() -> None:
    [a, b] = FakeClip().embed_images([_picture("red"), _picture("red", (48, 64))])

    assert not np.array_equal(a, b)


def test_the_fake_embeds_text_too() -> None:
    fake = FakeClip(dim=8)
    [dog, cat, dog_again] = fake.embed_texts(["dog", "cat", "dog"])

    assert dog.shape == (8,)
    np.testing.assert_array_equal(dog, dog_again)
    assert not np.array_equal(dog, cat)
    assert fake.embed_texts([]).shape == (0, 8)


def test_preprocessing_resizes_the_short_side_and_crops_the_middle() -> None:
    # A wide picture: red on the left third, green in the middle, blue on the right.
    picture = Image.new("RGB", (300, 100), "red")
    picture.paste("lime", (100, 0, 200, 100))
    picture.paste("blue", (200, 0, 300, 100))

    pixels = preprocess(picture, CONFIG)

    assert pixels.shape == (3, 224, 224)
    assert pixels.dtype == np.float32
    # Resized to 672 by 224 and cropped to the middle third: green, normalised.
    centre = pixels[:, 112, 112]
    np.testing.assert_allclose(centre, [(0 - 0.5) / 0.25, (1 - 0.5) / 0.25, (0 - 0.5) / 0.25])


def test_preprocessing_takes_any_mode() -> None:
    grey = Image.new("L", (224, 224), 255)

    np.testing.assert_allclose(preprocess(grey, CONFIG), 2.0)  # white, normalised


def test_a_missing_export_says_how_to_get_it(tmp_path: Path) -> None:
    with pytest.raises(ModelMissingError, match="bootstrap"):
        OnnxClip(tmp_path, "ViT-B-32", threads=4)


class _Session:
    """Stands in for `onnxruntime.InferenceSession`, recording how it was made."""

    made: ClassVar[list["_Session"]] = []

    def __init__(self, path: str, sess_options: Any, providers: list[str]) -> None:
        self.path = path
        self.threads = sess_options.intra_op_num_threads
        self.providers = providers
        _Session.made.append(self)

    def run(self, outputs: None, feeds: dict[str, Any]) -> list[Any]:
        (batch,) = feeds.values()
        return [np.full((len(batch), 512), 3.0, np.float32)]


def test_every_session_uses_the_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clip" / "ViT-B-32"
    folder.mkdir(parents=True)
    (folder / "clip.json").write_text(json.dumps(CONFIG.__dict__))
    _Session.made = []
    monkeypatch.setattr(clip_module.ort, "InferenceSession", _Session)
    # No vocabulary in the folder: the tokenizer is replaced too.
    monkeypatch.setattr(clip_module, "Tokenizer", lambda path, length: lambda texts: texts)

    clip = OnnxClip(tmp_path, "ViT-B-32", threads=3)
    vectors = clip.embed_images([_picture("red"), _picture("blue")])
    clip.embed_texts(["a dog"])

    assert [Path(s.path).name for s in _Session.made] == ["image.onnx", "text.onnx"]
    assert {s.threads for s in _Session.made} == {3}
    assert {tuple(s.providers) for s in _Session.made} == {("CPUExecutionProvider",)}
    assert vectors.shape == (2, 512)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-6)
    assert (clip.model, clip.dim) == ("ViT-B-32", 512)

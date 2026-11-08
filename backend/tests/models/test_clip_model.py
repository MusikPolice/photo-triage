"""The real CLIP model, as exported to ONNX, on the tier-1 fixtures (dev-environment §6,
model tier): it matches open_clip, and similar pictures and matching text score high.

Needs the export in `~/.cache/photo-triage/models/clip` (`scripts/bootstrap.sh`) and
the `export` dependency group, for open_clip to compare with. `just test-models`.
"""

# torch comes with open_clip, in the export group, which CI's type check doesn't
# install. The model-tier job has it.
# pyright: reportMissingImports=false

import itertools
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pillow_heif
import pytest
from PIL import Image

from photo_triage.ml import OnnxClip
from photo_triage.ml.clip import model_dir
from photo_triage.ml.tokenizer import VOCAB_FILE, Tokenizer
from photo_triage.settings import DEFAULT_MODELS_DIR

pytestmark = pytest.mark.models
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
MODEL = "ViT-B-32"
SCENES = [f"scenes/scene-0{n}.jpg" for n in range(1, 7)]
PARITY = [*SCENES[:2], "burst/burst-01.jpg", "quality/screenshot.jpg", "heic/scene.heic"]
QUERIES = [
    "a photo of a dog",
    "Sunset over the BEACH!!",
    "café crème brûlée",
    "日本の桜",
    "1234 apples, 5 pears",
    "it's 5 o'clock; they've gone",
    "&amp; html &lt;b&gt;",
    "  lots   of   space  ",
    "emoji 🎉🐶",
    "",
    "word " * 100,  # longer than the context: truncated, ending in the end token
]


def _open(name: str) -> Image.Image:
    with Image.open(SYNTHETIC / name) as image:
        image.load()
        return image


@pytest.fixture(scope="module")
def clip() -> OnnxClip:
    return OnnxClip(DEFAULT_MODELS_DIR, MODEL, threads=4)


@pytest.fixture(scope="module")
def reference() -> Iterator[tuple[object, object, object]]:
    open_clip = pytest.importorskip("open_clip")
    model, _, transform = open_clip.create_model_and_transforms(MODEL, pretrained="openai")
    model.eval()
    yield model, transform, open_clip.get_tokenizer(MODEL)


def _unit(vectors: np.ndarray) -> np.ndarray:
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def test_picture_vectors_match_open_clip(clip: OnnxClip, reference: tuple) -> None:
    import torch

    model, transform, _ = reference
    pictures = [_open(name) for name in PARITY]
    with torch.no_grad():
        expected = model.encode_image(torch.stack([transform(p) for p in pictures])).numpy()

    vectors = clip.embed_images(pictures)

    assert vectors.shape == (len(PARITY), 512)
    assert vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-5)
    np.testing.assert_allclose(vectors, _unit(expected), atol=1e-4)


def test_text_vectors_match_open_clip(clip: OnnxClip, reference: tuple) -> None:
    import torch

    model, _, tokenize = reference
    with torch.no_grad():
        expected = model.encode_text(tokenize(QUERIES)).numpy()

    np.testing.assert_allclose(clip.embed_texts(QUERIES), _unit(expected), atol=1e-4)


def test_the_tokenizer_gives_open_clips_ids(reference: tuple) -> None:
    _, _, tokenize = reference
    tokenizer = Tokenizer(model_dir(DEFAULT_MODELS_DIR, MODEL) / VOCAB_FILE, 77)

    np.testing.assert_array_equal(tokenizer(QUERIES), tokenize(QUERIES).numpy())


def test_a_burst_scores_high_and_distinct_scenes_lower(clip: OnnxClip) -> None:
    first, second = clip.embed_images([_open("burst/burst-01.jpg"), _open("burst/burst-02.jpg")])
    scenes = clip.embed_images([_open(name) for name in SCENES])

    burst = float(first @ second)
    assert burst > 0.9
    # A burst frame against six other scenes. (The synthetic scenes are all
    # shapes on a gradient, so CLIP finds them alike: up to 0.98 to each other.)
    others = scenes @ first
    assert others.max() < burst - 0.05, others


def test_a_text_query_ranks_the_right_picture_first(clip: OnnxClip) -> None:
    names = [*SCENES, "burst/burst-01.jpg", "quality/screenshot.jpg"]
    pictures = clip.embed_images([_open(name) for name in names])

    scores = pictures @ clip.embed_texts(["a screenshot of text on a phone screen"])[0]

    assert names[int(scores.argmax())] == "quality/screenshot.jpg"


def test_any_batch_size_gives_the_same_vectors(clip: OnnxClip) -> None:
    pictures = [_open(name) for name in SCENES]

    together = clip.embed_images(pictures)
    apart = np.concatenate([clip.embed_images([p]) for p in pictures])

    np.testing.assert_allclose(together, apart, atol=1e-5)
    for a, b in itertools.pairwise(together):
        assert not np.allclose(a, b)

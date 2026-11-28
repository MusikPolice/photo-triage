"""The LAION aesthetic predictor on the tier-1 fixtures (dev-environment §6, model
tier): read with numpy, it rates CLIP's ONNX embeddings as torch rates open_clip's,
and a scene rates above a screenshot or a blurred photo.

Needs the bootstrap's models in `~/.cache/photo-triage/models` and the `export`
dependency group, for torch and open_clip to compare with. `just test-models`. Skips
without the predictor's weights, which CI's model-tier job doesn't fetch yet.
"""

# torch comes with open_clip, in the export group, which CI's type check doesn't
# install. The model-tier job has it.
# pyright: reportMissingImports=false

from pathlib import Path

import numpy as np
import pillow_heif
import pytest
from PIL import Image

from photo_triage.ml import OnnxClip
from photo_triage.ml.aesthetic import WEIGHTS as WEIGHT_FILES
from photo_triage.ml.aesthetic import LinearHead, read_state_dict
from photo_triage.settings import DEFAULT_MODELS_DIR

pytestmark = pytest.mark.models
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
MODEL = "ViT-B-32"
SCENES = [f"scenes/scene-0{n}.jpg" for n in range(1, 7)]
POOR = ["quality/screenshot.jpg", "quality/blurred.jpg"]
RATING_TOLERANCE = 0.01
"""On a 1-10 scale. CLIP's export matches open_clip's vectors to 1e-4 a component
(`test_clip_model`), which moves a rating by about 1e-3."""


def _open(name: str) -> Image.Image:
    with Image.open(SYNTHETIC / name) as image:
        image.load()
        return image.convert("RGB")


WEIGHTS = DEFAULT_MODELS_DIR / "aesthetic" / WEIGHT_FILES[MODEL]
if not WEIGHTS.is_file():
    pytest.skip(
        f"No aesthetic predictor at {WEIGHTS}: run scripts/bootstrap.sh", allow_module_level=True
    )


@pytest.fixture(scope="module")
def head() -> LinearHead:
    return LinearHead.load(DEFAULT_MODELS_DIR, MODEL)


@pytest.fixture(scope="module")
def clip() -> OnnxClip:
    return OnnxClip(DEFAULT_MODELS_DIR, MODEL, threads=4)


def test_the_weights_read_as_torch_reads_them() -> None:
    torch = pytest.importorskip("torch")
    expected = torch.load(WEIGHTS, map_location="cpu", weights_only=True)

    state = read_state_dict(WEIGHTS)

    assert set(state) == set(expected)
    for name, tensor in expected.items():
        np.testing.assert_array_equal(state[name], tensor.numpy())


def test_each_scene_rates_as_open_clip_and_torch_rate_it(head: LinearHead, clip: OnnxClip) -> None:
    torch = pytest.importorskip("torch")
    open_clip = pytest.importorskip("open_clip")
    model, _, transform = open_clip.create_model_and_transforms(MODEL, pretrained="openai")
    model.eval()
    linear = torch.nn.Linear(512, 1)
    linear.load_state_dict(torch.load(WEIGHTS, map_location="cpu", weights_only=True))

    pictures = [_open(name) for name in SCENES]
    with torch.no_grad():
        features = model.encode_image(torch.stack([transform(p) for p in pictures]))
        features = features / features.norm(dim=-1, keepdim=True)
        expected = linear(features).squeeze(1).numpy()

    ratings = [head.rating(v) for v in clip.embed_images(pictures)]

    np.testing.assert_allclose(ratings, expected, atol=RATING_TOLERANCE)


def test_every_scene_rates_above_the_screenshot_and_the_blurred_photo(
    head: LinearHead, clip: OnnxClip
) -> None:
    ratings = dict(
        zip(
            SCENES + POOR,
            (head.rating(v) for v in clip.embed_images([_open(name) for name in SCENES + POOR])),
            strict=True,
        )
    )
    assert min(ratings[s] for s in SCENES) > max(ratings[p] for p in POOR), ratings

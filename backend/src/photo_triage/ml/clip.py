"""CLIP embeddings of pictures and text (plan §5, §6.3): 512-d, L2-normalised float32
vectors, so a dot product is the cosine similarity.

- `OnnxClip` runs the models `scripts/export_clip_onnx.py` exported, with ONNX
  Runtime. It preprocesses as open_clip does (resize the short side with bicubic
  resampling, centre-crop, normalise), from the settings the export recorded.
- `FakeClip` derives vectors from the content alone, for the default test suite
  (dev-environment §6).
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import onnxruntime as ort  # pyright: ignore[reportMissingTypeStubs]
from numpy.typing import NDArray
from PIL import Image

from photo_triage.ml.tokenizer import VOCAB_FILE, Tokenizer

Vectors = NDArray[np.float32]
"""[n, dim], each row of length 1."""


class ClipEmbedder(Protocol):
    @property
    def model(self) -> str:
        """The model's name, e.g. `ViT-B-32`, which embeddings are stored under."""
        ...

    @property
    def dim(self) -> int: ...

    @property
    def image_size(self) -> int:
        """The side of the square it embeds pictures at, in pixels: 224 for ViT-B-32."""
        ...

    def embed_images(self, images: Sequence[Image.Image]) -> Vectors:
        """One vector per picture, in order."""
        ...

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        """One vector per text, in order."""
        ...


class ModelMissingError(Exception):
    """The exported model isn't where `MODELS_DIR` says."""


@dataclass(frozen=True)
class ClipConfig:
    """What the export recorded in `clip.json`."""

    model: str
    image_size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    context_length: int
    embedding_dim: int

    @classmethod
    def read(cls, path: Path) -> "ClipConfig":
        raw: dict[str, Any] = json.loads(path.read_text())
        return cls(
            model=str(raw["model"]),
            image_size=int(raw["image_size"]),
            mean=(float(raw["mean"][0]), float(raw["mean"][1]), float(raw["mean"][2])),
            std=(float(raw["std"][0]), float(raw["std"][1]), float(raw["std"][2])),
            context_length=int(raw["context_length"]),
            embedding_dim=int(raw["embedding_dim"]),
        )


def model_dir(models_dir: Path, model: str) -> Path:
    """Where `model`'s export is."""
    return models_dir / "clip" / model


class OnnxClip:
    """`model`'s exported encoders in `models_dir/clip/<model>`. Each ONNX Runtime
    session uses `threads` threads (`WORKER_THREADS`, plan §6.11). The text model is
    loaded on first use, since only search needs it."""

    def __init__(self, models_dir: Path, model: str, threads: int) -> None:
        folder = model_dir(models_dir, model)
        if not (folder / "clip.json").is_file():
            raise ModelMissingError(
                f"No exported CLIP model at {folder}. Run scripts/bootstrap.sh, or copy "
                "the models folder there (`just models`)."
            )
        self._folder = folder
        self._threads = threads
        self.config = ClipConfig.read(folder / "clip.json")
        self._image = self._session("image.onnx")
        self._text: ort.InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def dim(self) -> int:
        return self.config.embedding_dim

    @property
    def image_size(self) -> int:
        return self.config.image_size

    def embed_images(self, images: Sequence[Image.Image]) -> Vectors:
        if not images:
            return np.zeros((0, self.dim), np.float32)
        pixels = np.stack([preprocess(image, self.config) for image in images])
        return _normalised(self._run(self._image, "pixels", pixels))

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        if self._text is None or self._tokenizer is None:
            self._text = self._session("text.onnx")
            self._tokenizer = Tokenizer(self._folder / VOCAB_FILE, self.config.context_length)
        return _normalised(self._run(self._text, "tokens", self._tokenizer(texts)))

    def _session(self, file: str) -> ort.InferenceSession:
        options: Any = ort.SessionOptions()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        options.intra_op_num_threads = self._threads
        options.inter_op_num_threads = 1
        return ort.InferenceSession(
            str(self._folder / file), sess_options=options, providers=["CPUExecutionProvider"]
        )

    @staticmethod
    def _run(session: ort.InferenceSession, name: str, batch: NDArray[Any]) -> NDArray[Any]:
        outputs: Sequence[Any] = session.run(None, {name: batch})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return np.asarray(outputs[0])


def preprocess(image: Image.Image, config: ClipConfig) -> NDArray[np.float32]:
    """[3, size, size] as open_clip's transform makes it: the short side resized to
    `size` (bicubic, as torchvision does for a PIL image), centre-cropped, scaled to
    [0, 1] and normalised by the model's mean and std."""
    size = config.image_size
    image = image.convert("RGB")
    width, height = image.size
    if width <= height:
        resized = (size, int(size * height / width))
    else:
        resized = (int(size * width / height), size)
    if resized != image.size:
        image = image.resize(resized, Image.Resampling.BICUBIC)
    left = round((resized[0] - size) / 2.0)
    top = round((resized[1] - size) / 2.0)
    image = image.crop((left, top, left + size, top + size))
    pixels = np.asarray(image, dtype=np.float32) / 255.0
    pixels = (pixels - np.array(config.mean, np.float32)) / np.array(config.std, np.float32)
    return pixels.transpose(2, 0, 1).astype(np.float32)


def _normalised(vectors: NDArray[Any]) -> Vectors:
    vectors = vectors.astype(np.float32)
    return (vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).astype(np.float32)


class FakeClip:
    """Vectors derived from a hash of each picture's pixels or each text, for tests:
    the same content always gives the same vector, and different content a different
    one (dev-environment §6)."""

    def __init__(self, model: str = "fake", dim: int = 512) -> None:
        self._model = model
        self._dim = dim

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def image_size(self) -> int:
        return 224

    def embed_images(self, images: Sequence[Image.Image]) -> Vectors:
        return self._vectors(
            [_image_digest(image) for image in images],
        )

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        return self._vectors([hashlib.sha256(t.encode()).digest() for t in texts])

    def _vectors(self, digests: list[bytes]) -> Vectors:
        rows = [
            np.random.default_rng(int.from_bytes(d[:16])).standard_normal(self._dim)
            for d in digests
        ]
        return _normalised(np.array(rows).reshape(len(digests), self._dim))


def _image_digest(image: Image.Image) -> bytes:
    rgb = image.convert("RGB")
    digest = hashlib.sha256(f"{rgb.width}x{rgb.height}".encode())
    digest.update(rgb.tobytes())
    return digest.digest()

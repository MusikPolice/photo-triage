"""The LAION aesthetic predictor (plan §6.8): a linear head on an item's CLIP embedding.

`sa_0_4_vit_b_32_linear.pth` is one `torch.nn.Linear(512, 1)`, trained on OpenAI's
ViT-B/32 embeddings, L2-normalised, to predict a 1-10 rating like the AVA dataset's.
The bootstrap downloads it to `MODELS_DIR/aesthetic/`. `LinearHead.load` reads its
weight and bias from the PyTorch archive with numpy, so the image needs no torch: a
`.pth` is a zip of a pickle, which names the tensors, and their raw float32 bytes.
The unpickler accepts only the few names a saved `Linear` uses, so the file can't
run code.

`FakeHead` is a head with weights from a fixed seed, for `WORKER_FAKE_CLIP`
(dev-environment §6). It needs no weights, and the same vector always gets the same
rating.
"""

import io
import pickle
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Protocol

import numpy as np
from numpy.typing import NDArray

from photo_triage.ml.clip import ModelMissingError

WEIGHTS = {"ViT-B-32": "sa_0_4_vit_b_32_linear.pth"}
"""The predictor's weights for each `CLIP_MODEL` it was trained on, in
`MODELS_DIR/aesthetic/`. Only ViT-B/32's: the improved predictor needs ViT-L/14."""

FAKE_BIAS = 4.5
"""`FakeHead`'s mean rating. A unit vector against weights drawn from N(0, 1) gives
N(0, 1), so the fake ratings spread over about 2.5 to 6.5."""


class AestheticHead(Protocol):
    def rating(self, vector: NDArray[np.float32]) -> float:
        """The predicted rating, roughly 1 to 10, of a unit-length CLIP embedding."""
        ...


def weights_path(models_dir: Path, clip_model: str) -> Path | None:
    """Where the weights for `clip_model` should be, or None if there are none for
    that model."""
    name = WEIGHTS.get(clip_model)
    return None if name is None else models_dir / "aesthetic" / name


class LinearHead:
    def __init__(self, weight: NDArray[np.float32], bias: float) -> None:
        self.weight = weight.astype(np.float32).reshape(-1)
        self.bias = float(bias)

    def rating(self, vector: NDArray[np.float32]) -> float:
        return float(vector.astype(np.float32) @ self.weight) + self.bias

    @classmethod
    def load(cls, models_dir: Path, clip_model: str) -> "LinearHead":
        """The predictor for `clip_model`. Raises `ModelMissingError` saying where
        its weights should be."""
        path = weights_path(models_dir, clip_model)
        if path is None:
            raise ModelMissingError(
                f"The aesthetic predictor has no weights for CLIP_MODEL={clip_model}, "
                f"only for {', '.join(WEIGHTS)}."
            )
        if not path.is_file():
            raise ModelMissingError(
                f"No aesthetic predictor at {path}. Run scripts/bootstrap.sh, or copy "
                "the models folder there (`just models`)."
            )
        state = read_state_dict(path)
        weight, bias = state["weight"], state["bias"]
        if weight.shape[0] != 1 or bias.shape != (1,):
            raise ValueError(f"{path.name} isn't a linear head with one output")
        return cls(weight[0], float(bias[0]))


class FakeHead(LinearHead):
    def __init__(self, dim: int = 512) -> None:
        weight = np.random.default_rng(0).standard_normal(dim).astype(np.float32)
        super().__init__(weight, FAKE_BIAS)


def read_state_dict(path: Path) -> dict[str, NDArray[np.float32]]:
    """The float32 tensors in a PyTorch `state_dict` archive, as numpy arrays."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        [pickled] = [n for n in names if n.endswith("/data.pkl")]
        root = pickled.removesuffix("data.pkl")

        def storage(key: str) -> NDArray[np.float32]:
            return np.frombuffer(archive.read(f"{root}data/{key}"), dtype="<f4")

        state = _StateUnpickler(io.BytesIO(archive.read(pickled)), storage).load()
    return {name: np.asarray(tensor) for name, tensor in state.items()}


def _rebuild_tensor(
    storage: NDArray[np.float32],
    offset: int,
    size: tuple[int, ...],
    stride: tuple[int, ...],
    *_: object,
) -> NDArray[np.float32]:
    """`torch._utils._rebuild_tensor_v2`, for a float32 storage."""
    item = storage.itemsize
    return np.lib.stride_tricks.as_strided(
        storage[offset:], shape=size, strides=tuple(s * item for s in stride)
    ).copy()


class _FloatStorage:
    """Stands for `torch.FloatStorage` in the pickle's persistent ids."""


class _StateUnpickler(pickle.Unpickler):
    _ALLOWED: ClassVar[dict[tuple[str, str], Any]] = {
        ("collections", "OrderedDict"): OrderedDict,
        ("torch._utils", "_rebuild_tensor_v2"): _rebuild_tensor,
        ("torch", "FloatStorage"): _FloatStorage,
    }

    def __init__(self, file: io.BytesIO, storage: Callable[[str], NDArray[np.float32]]) -> None:
        super().__init__(file)
        self._storage = storage

    def find_class(self, module: str, name: str) -> Any:
        try:
            return self._ALLOWED[(module, name)]
        except KeyError:
            raise pickle.UnpicklingError(f"unexpected {module}.{name} in the weights") from None

    def persistent_load(self, pid: Any) -> Any:
        kind, storage_type, key, _location, _size = pid
        if kind != "storage" or storage_type is not _FloatStorage:
            raise pickle.UnpicklingError(f"unexpected storage {pid!r} in the weights")
        return self._storage(str(key))

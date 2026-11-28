"""Fixtures shared by every test tier."""

import io
import logging
import pickle
import shutil
import sys
import types
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from photo_triage.db.engine import open_database
from photo_triage.files import ExifTool
from photo_triage.logs import LIBRARY_LOGGERS, PER_REQUEST_LOGGERS
from photo_triage.settings import ENV_FILE, Settings

if TYPE_CHECKING:
    from numpy.typing import NDArray

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
"""The tier-1 fixtures (dev-environment §4). Tests copy them; never write here."""

# Every variable Settings reads. Cleared for each test so the developer's
# environment can't leak in.
SETTINGS_ENV = [*(name.upper() for name in Settings.model_fields), ENV_FILE]


@pytest.fixture(autouse=True)
def isolated_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No settings from the environment, and no `.env` in the working directory."""
    for name in SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Undo `photo_triage.logs.configure`, closing the handlers it opened."""
    names = [*LIBRARY_LOGGERS, *PER_REQUEST_LOGGERS]
    loggers = [logging.getLogger(), *map(logging.getLogger, names)]
    saved = [(lg, lg.level, list(lg.handlers), lg.propagate) for lg in loggers]
    yield
    for lg, level, handlers, propagate in saved:
        for handler in lg.handlers:
            if handler not in handlers:
                handler.close()
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        photo_dir=tmp_path / "photos",
        trash_dir=tmp_path / "trash",
        data_dir=tmp_path / "data",
        # Empty: no default-suite test loads a real model (dev-environment §6).
        models_dir=tmp_path / "models",
    )


@pytest.fixture
def library(settings: Settings) -> Path:
    """A copy of the tier-1 fixtures as `PHOTO_DIR`."""
    shutil.copytree(SYNTHETIC, settings.photo_dir, dirs_exist_ok=True)
    return settings.photo_dir


@pytest.fixture
def exiftool() -> Iterator[ExifTool]:
    """The worker's exiftool process, stopped after the test."""
    with ExifTool() as reader:
        yield reader


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "data")
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_cfg(engine: Engine) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


@pytest.fixture
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    """A database upgraded to the newest migration."""
    command.upgrade(alembic_cfg, "head")
    return engine


PthWriter = Callable[[Path, "NDArray[np.float32]", float], None]


@pytest.fixture
def write_linear_pth() -> PthWriter:
    """Writes a PyTorch archive of a `Linear(n, 1)`'s state_dict, as `torch.save`
    does, without torch: `path`, its weight (n floats) and its bias."""
    return _write_linear_pth


def _write_linear_pth(path: Path, weight: "NDArray[np.float32]", bias: float) -> None:
    # Stand-ins under torch's names, so the pickle names what torch.save's does.
    torch, utils = types.ModuleType("torch"), types.ModuleType("torch._utils")

    def _rebuild_tensor_v2(*args: object) -> None: ...

    class FloatStorage: ...

    _rebuild_tensor_v2.__module__, FloatStorage.__module__ = "torch._utils", "torch"
    _rebuild_tensor_v2.__qualname__, FloatStorage.__qualname__ = (
        "_rebuild_tensor_v2",
        "FloatStorage",
    )
    utils._rebuild_tensor_v2 = _rebuild_tensor_v2  # pyright: ignore[reportAttributeAccessIssue]
    torch.FloatStorage = FloatStorage  # pyright: ignore[reportAttributeAccessIssue]

    @dataclass(frozen=True)
    class Storage:
        key: str
        size: int

    @dataclass(frozen=True)
    class Tensor:
        storage: Storage
        shape: tuple[int, ...]

        def __reduce__(self) -> tuple[object, tuple[object, ...]]:
            stride = (self.shape[1], 1) if len(self.shape) == 2 else (1,)
            return _rebuild_tensor_v2, (self.storage, 0, self.shape, stride, False, OrderedDict())

    class Pickler(pickle.Pickler):
        def persistent_id(self, obj: object) -> object:
            if isinstance(obj, Storage):
                return ("storage", FloatStorage, obj.key, "cpu", obj.size)
            return None

    weights = np.asarray(weight, dtype="<f4").reshape(-1)
    state = OrderedDict(
        weight=Tensor(Storage("0", weights.size), (1, weights.size)),
        bias=Tensor(Storage("1", 1), (1,)),
    )
    buffer = io.BytesIO()
    with mock.patch.dict(sys.modules, {"torch": torch, "torch._utils": utils}):
        Pickler(buffer, protocol=2).dump(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", buffer.getvalue())
        archive.writestr("archive/data/0", weights.tobytes())
        archive.writestr("archive/data/1", np.asarray([bias], dtype="<f4").tobytes())
        archive.writestr("archive/version", "3\n")

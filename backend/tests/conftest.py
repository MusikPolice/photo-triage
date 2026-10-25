"""Fixtures shared by every test tier."""

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from photo_triage.db.engine import open_database
from photo_triage.logs import LIBRARY_LOGGERS, PER_REQUEST_LOGGERS
from photo_triage.settings import ENV_FILE, Settings

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
    )


@pytest.fixture
def library(settings: Settings) -> Path:
    """A copy of the tier-1 fixtures as `PHOTO_DIR`."""
    shutil.copytree(SYNTHETIC, settings.photo_dir, dirs_exist_ok=True)
    return settings.photo_dir


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

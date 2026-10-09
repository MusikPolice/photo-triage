"""Fixtures shared by every test tier."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from photo_triage.logs import LIBRARY_LOGGERS, PER_REQUEST_LOGGERS
from photo_triage.settings import ENV_FILE, Settings

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

"""Fixtures shared by every test tier."""

from pathlib import Path

import pytest

from photo_triage.settings import Settings

# Every variable Settings reads. Cleared for each test so the developer's
# environment can't leak in.
SETTINGS_ENV = ["PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "APP_PORT", "AUTH_MODE"]


@pytest.fixture(autouse=True)
def isolated_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No settings from the environment, and no `.env` in the working directory."""
    for name in SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        photo_dir=tmp_path / "photos",
        trash_dir=tmp_path / "trash",
        data_dir=tmp_path / "data",
    )

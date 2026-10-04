"""Settings come from the environment and `.env`, and fail fast (plan §9)."""

from pathlib import Path

import pytest

from photo_triage.settings import Settings, SettingsError, load_settings


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("APP_PORT", "9000")

    settings = load_settings()

    assert settings.photo_dir == Path("/photos")
    assert settings.trash_dir == Path("/trash")
    assert settings.app_port == 9000


def test_loads_from_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "PHOTO_DIR=/photos\nTRASH_DIR=/trash\nDATA_DIR=/data\nOLLAMA_MODEL=moondream\n"
    )

    settings = load_settings()

    assert settings.photo_dir == Path("/photos")
    assert settings.data_dir == Path("/data")


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PHOTO_DIR=/from-file\nTRASH_DIR=/trash\n")
    monkeypatch.setenv("PHOTO_DIR", "/from-env")

    assert load_settings().photo_dir == Path("/from-env")


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")

    settings = load_settings()

    assert settings.data_dir == Path("./data")
    assert settings.app_port == 8000
    assert settings.auth_mode == "none"
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("value", ["debug", "Warning", "ERROR"])
def test_log_level_accepts_level_names_in_any_case(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("LOG_LEVEL", value)

    assert load_settings().log_level == value.upper()


@pytest.mark.parametrize("value", ["VERBOSE", "TRACE", "10", ""])
def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("LOG_LEVEL", value)

    with pytest.raises(SettingsError, match="LOG_LEVEL"):
        load_settings()


def test_summary_lists_every_setting(settings: Settings) -> None:
    summary = settings.summary()

    assert f"DATA_DIR={settings.data_dir}" in summary
    assert "LOG_LEVEL=INFO" in summary
    assert summary.count("=") == len(Settings.model_fields)


def test_missing_required_settings_are_named() -> None:
    with pytest.raises(SettingsError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "PHOTO_DIR: Field required" in message
    assert "TRASH_DIR: Field required" in message
    assert ".env.example" in message


def test_missing_trash_dir_alone_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")

    with pytest.raises(SettingsError, match="TRASH_DIR") as excinfo:
        load_settings()

    assert "PHOTO_DIR" not in str(excinfo.value)


def test_unsupported_auth_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("AUTH_MODE", "forward_auth")

    with pytest.raises(SettingsError, match="AUTH_MODE"):
        load_settings()


def test_settings_are_immutable(settings: Settings) -> None:
    with pytest.raises(ValueError, match="frozen"):
        settings.app_port = 1  # pyright: ignore[reportAttributeAccessIssue]

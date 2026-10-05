"""Settings come from the environment and `.env`, and fail fast (plan §9)."""

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from photo_triage.settings import Settings, SettingsError, load_settings

# Every setting, by whether its value may be written to the logs. The API and the
# worker log every setting at startup (`Settings.summary`), and only `SecretStr`
# values are masked. A new setting fails `test_every_setting_is_public_or_secret`
# until it's added to one of these.
PUBLIC_SETTINGS = {
    "photo_dir",
    "trash_dir",
    "data_dir",
    "app_port",
    "log_level",
    "auth_mode",
    "worker_threads",
    "worker_quiet_hours",
    "tz",
    "worker_noop_stage",
    "fake_now",
}
SECRET_SETTINGS: set[str] = set()  # tokens, passwords, URLs that may embed either


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
    assert settings.worker_threads == 4
    assert settings.worker_quiet_hours is None
    assert settings.tz == ZoneInfo("UTC")
    assert settings.worker_noop_stage is False
    assert settings.fake_now is None


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


def test_every_setting_is_public_or_secret() -> None:
    fields = Settings.model_fields
    unclassified = fields.keys() - PUBLIC_SETTINGS - SECRET_SETTINGS
    assert not unclassified, (
        f"classify {sorted(unclassified)} in PUBLIC_SETTINGS or SECRET_SETTINGS in {__file__}: "
        "every setting is logged at startup, and only SecretStr values are masked"
    )
    for name in SECRET_SETTINGS:
        assert fields[name].annotation in (SecretStr, SecretStr | None), (
            f"{name} is secret, so it must be a SecretStr"
        )


@pytest.mark.parametrize("value", ["0", "-1", "four"])
def test_worker_threads_must_be_a_positive_number(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("WORKER_THREADS", value)

    with pytest.raises(SettingsError, match="WORKER_THREADS"):
        load_settings()


def test_fake_now_needs_a_utc_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("FAKE_NOW", "2030-01-01T22:00:00Z")
    assert load_settings().fake_now == dt.datetime(2030, 1, 1, 22, tzinfo=dt.UTC)

    monkeypatch.setenv("FAKE_NOW", "2030-01-01T22:00:00")
    with pytest.raises(SettingsError, match="FAKE_NOW"):
        load_settings()


def test_quiet_hours_and_timezone_are_read_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("WORKER_QUIET_HOURS", "Mon-Fri 17:00-02:00; Sat-Sun 07:00-02:00")
    monkeypatch.setenv("TZ", "America/Toronto")

    settings = load_settings()

    assert settings.worker_quiet_hours is not None
    assert len(settings.worker_quiet_hours.periods) == 7
    assert settings.tz == ZoneInfo("America/Toronto")
    assert "WORKER_QUIET_HOURS=Mon-Fri 17:00-02:00; Sat-Sun 07:00-02:00 " in settings.summary()
    assert "TZ=America/Toronto " in settings.summary()


def test_empty_quiet_hours_means_always_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("WORKER_QUIET_HOURS", " ")

    assert load_settings().worker_quiet_hours is None


def test_malformed_quiet_hours_are_rejected_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("WORKER_QUIET_HOURS", "Mon-Fri 5pm-11pm")

    with pytest.raises(SettingsError, match=r'WORKER_QUIET_HOURS: .*"Mon-Fri 5pm-11pm" isn\'t'):
        load_settings()


def test_unknown_timezone_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("TZ", "Eastern")

    with pytest.raises(SettingsError, match="TZ"):
        load_settings()

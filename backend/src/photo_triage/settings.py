"""Configuration from environment variables and `.env` (plan §9).

Only the variables the code uses so far are read. The rest of plan §9 is listed
in `.env.example` and gets a field when its feature lands.
"""

import os
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    PlainSerializer,
    PlainValidator,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from photo_triage import quiet_hours

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


def _quiet_hours(value: object) -> quiet_hours.QuietHours | None:
    if value is None or isinstance(value, quiet_hours.QuietHours):
        return value
    if not isinstance(value, str):
        raise ValueError("expected text")
    return quiet_hours.parse(value) if value.strip() else None


TRASH_FOLDER = ".photo-triage-trash"
"""The trash's name inside `PHOTO_DIR`, where it is by default (plan §6.8)."""


def _default_trash_dir(data: dict[str, Any]) -> Path:
    photo_dir = data.get("photo_dir")
    # Without a valid PHOTO_DIR, validation fails anyway and this is never used.
    return Path(photo_dir) / TRASH_FOLDER if photo_dir is not None else Path(TRASH_FOLDER)


QuietHoursSetting = Annotated[
    quiet_hours.QuietHours | None, PlainValidator(_quiet_hours), PlainSerializer(str)
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds variables for features not built yet
        frozen=True,
    )

    photo_dir: Path
    """Photo library root."""

    trash_dir: Path = Field(default_factory=_default_trash_dir)
    """Where trashed files go. By default a hidden folder inside `photo_dir`, so that
    trashing is a rename on the same filesystem (plan §6.8)."""

    data_dir: Path = Path("./data")
    """SQLite database and derived files. `/data` in the container."""

    app_port: int = 8000
    """Port the web UI and API listen on."""

    log_level: Annotated[LogLevel, BeforeValidator(_upper)] = "INFO"
    """Lowest level logged, by every process. Case doesn't matter."""

    auth_mode: Literal["none"] = "none"
    """How `current_actor` identifies people. `forward_auth` arrives in Phase 8."""

    worker_threads: Annotated[int, Field(gt=0)] = 4
    """Threads for ONNX and BLAS inference in the worker."""

    worker_quiet_hours: QuietHoursSetting = None
    """When the whole worker stays idle, in `tz` (see `photo_triage.quiet_hours`).
    Unset or empty means it always runs."""

    tz: ZoneInfo = quiet_hours.UTC_ZONE
    """The household's timezone, an IANA name such as `America/Toronto`. Only quiet
    hours use it: timestamps and logs are UTC."""

    # Development only (dev-environment §5), so not in plan §9 or `.env.example`.

    worker_noop_stage: bool = False
    """Enables the `noop` stage, for exercising the worker by hand."""

    fake_now: AwareDatetime | None = None
    """Starts the worker's clock at this time (with a UTC offset, e.g.
    `2026-10-03T21:59:00Z`), from where it advances in real time."""

    @field_validator("trash_dir", mode="before")
    @classmethod
    def _blank_trash_dir_is_unset(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            return _default_trash_dir(info.data)
        return value

    @field_validator("trash_dir")
    @classmethod
    def _trash_dir_holds_no_photos(cls, value: Path, info: ValidationInfo) -> Path:
        photo_dir = info.data.get("photo_dir")
        if photo_dir is None:
            return value
        trash, photos = value.resolve(), photo_dir.resolve()
        if trash == photos:
            raise ValueError("is the same folder as PHOTO_DIR; unset it to use the default")
        if photos.is_relative_to(trash):
            raise ValueError(
                f"contains PHOTO_DIR ({photo_dir}); "
                "the trash may be inside the library, not around it"
            )
        return value

    def summary(self) -> str:
        """Every setting as `NAME=value`, for the startup log line. Secrets must be
        `SecretStr` fields, which show as asterisks. `test_settings.py` makes each new
        setting say whether it's secret."""
        return " ".join(f"{name.upper()}={value}" for name, value in self.model_dump().items())


def trash_filesystem_warning(settings: Settings) -> str | None:
    """Why trashing would copy files, if it would: the trash is on a different
    filesystem from the library. Startup logs this and carries on, since a dev
    setup that never trashes may have it so. The trash move itself refuses to
    copy (plan §6.8)."""
    if _device(settings.trash_dir) == _device(settings.photo_dir):
        return None
    return (
        f"TRASH_DIR ({settings.trash_dir}) is on a different filesystem from PHOTO_DIR "
        f"({settings.photo_dir}), so trashing a file will fail. Unset TRASH_DIR to use "
        f"PHOTO_DIR/{TRASH_FOLDER}."
    )


def _device(path: Path) -> int:
    """The filesystem `path` is on, or would be created on: that of its nearest
    parent that exists."""
    path = path.resolve()
    while not path.exists():
        path = path.parent
    return path.stat().st_dev


class SettingsError(Exception):
    """The configuration is missing or invalid. The message lists every problem."""


ENV_FILE = "ENV_FILE"
"""Names the file read in place of `.env`. Development only (dev-environment §5):
the scratch stack sets it to `/dev/null` so the developer's `.env` can't reach it."""


def load_settings() -> Settings:
    """Read settings from the environment and `.env` (or the file `ENV_FILE`
    names), or explain what's wrong."""
    try:
        # Required fields come from the env; _env_file is a pydantic-settings init option.
        return Settings(  # pyright: ignore[reportCallIssue]
            _env_file=os.environ.get(ENV_FILE, ".env")  # pyright: ignore[reportCallIssue]
        )
    except ValidationError as e:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in err['loc']).upper()}: {err['msg']}"
            for err in e.errors()
        )
        raise SettingsError(
            f"Invalid configuration:\n{problems}\n"
            "Set these in the environment or in .env (see .env.example)."
        ) from None

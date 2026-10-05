"""Configuration from environment variables and `.env` (plan §9).

Only the variables the code uses so far are read. The rest of plan §9 is listed
in `.env.example` and gets a field when its feature lands.
"""

from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    PlainSerializer,
    PlainValidator,
    ValidationError,
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

    trash_dir: Path
    """Where trashed files go. Outside `photo_dir`."""

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

    def summary(self) -> str:
        """Every setting as `NAME=value`, for the startup log line. Secrets must be
        `SecretStr` fields, which show as asterisks. `test_settings.py` makes each new
        setting say whether it's secret."""
        return " ".join(f"{name.upper()}={value}" for name, value in self.model_dump().items())


class SettingsError(Exception):
    """The configuration is missing or invalid. The message lists every problem."""


def load_settings() -> Settings:
    """Read settings from the environment and `.env`, or explain what's wrong."""
    try:
        return Settings()  # pyright: ignore[reportCallIssue]  # required fields come from the env
    except ValidationError as e:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in err['loc']).upper()}: {err['msg']}"
            for err in e.errors()
        )
        raise SettingsError(
            f"Invalid configuration:\n{problems}\n"
            "Set these in the environment or in .env (see .env.example)."
        ) from None

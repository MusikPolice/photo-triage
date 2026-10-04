"""Configuration from environment variables and `.env` (plan §9).

Only the variables the code uses so far are read. The rest of plan §9 is listed
in `.env.example` and gets a field when its feature lands.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


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

    def summary(self) -> str:
        """Every setting as `NAME=value`, for the startup log line. Secrets must be
        `SecretStr` fields, which show as asterisks."""
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

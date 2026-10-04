"""Logging for every process: one plain-text line per event, in UTC, to stderr and to
`DATA_DIR/logs/<process>.log`.

The conventions for what goes at each level are in docs/dev-environment.md
("Logging").
"""

import datetime as dt
import logging
import logging.config
from pathlib import Path
from typing import Any, Literal

from photo_triage.files import RotatingLogHandler
from photo_triage.settings import Settings

Process = Literal["api", "worker", "migrate"]

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
"""Each process keeps at most about 60 MB of logs: the live file and 5 old ones."""

LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Libraries that log through `logging`. Their own handlers are removed so every
# line goes through ours, in our format, at our level.
LIBRARY_LOGGERS = ["uvicorn", "uvicorn.error", "uvicorn.access", "alembic", "sqlalchemy"]
PER_REQUEST_LOGGERS = ["uvicorn.access", "sqlalchemy.engine", "sqlalchemy.pool"]


class UTCFormatter(logging.Formatter):
    """Timestamps like `2026-10-03T22:14:05.123Z`, whatever the process's timezone."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        created_at = dt.datetime.fromtimestamp(record.created, dt.UTC)
        return created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def logs_dir(settings: Settings) -> Path:
    return settings.data_dir / "logs"


def configure(
    settings: Settings,
    process: Process,
    *,
    to_file: bool = True,
    max_bytes: int = LOG_FILE_MAX_BYTES,
    backup_count: int = LOG_FILE_BACKUP_COUNT,
) -> None:
    """Send every logger to stderr and, unless `to_file` is false, to
    `DATA_DIR/logs/<process>.log`, at `LOG_LEVEL`.

    Replaces any earlier configuration, so calling it again is harmless.
    """
    handlers: dict[str, dict[str, Any]] = {
        "stderr": {
            "class": "logging.StreamHandler",
            "formatter": "utc",
            "stream": "ext://sys.stderr",
        }
    }
    if to_file:
        directory = logs_dir(settings)
        handlers["file"] = {
            "()": RotatingLogHandler,
            "formatter": "utc",
            "path": directory / f"{process}.log",
            "logs_dir": directory,
            "max_bytes": max_bytes,
            "backup_count": backup_count,
        }
    level = settings.log_level
    # Lines per request or per SQL statement are only for debugging. uvicorn logs
    # each request at INFO, and SQLAlchemy each statement (and at DEBUG, every
    # result row, which is never wanted).
    per_request_level = "INFO" if level == "DEBUG" else "WARNING"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"utc": {"()": UTCFormatter, "fmt": LINE_FORMAT}},
            "handlers": handlers,
            "root": {"level": level, "handlers": list(handlers)},
            "loggers": {
                **{name: _inherit() for name in LIBRARY_LOGGERS},
                **{name: _inherit(per_request_level) for name in PER_REQUEST_LOGGERS},
            },
        }
    )


def _inherit(level: str = "NOTSET") -> dict[str, Any]:
    """A logger with no handlers of its own, passing its records up to the root."""
    return {"level": level, "handlers": [], "propagate": True}

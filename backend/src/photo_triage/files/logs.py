"""Log files: rotation renames and deletes them, so it lives here (dev-environment §7)."""

from logging.handlers import RotatingFileHandler
from pathlib import Path


class LogPathError(ValueError):
    """The log file isn't inside the logs directory."""


class RotatingLogHandler(RotatingFileHandler):
    """A size-rotated log file that must sit in `logs_dir` (`DATA_DIR/logs`).

    When the file reaches `max_bytes`, it becomes `<name>.1`, older copies shift up,
    and the one past `backup_count` is deleted. Creates `logs_dir` if needed.
    """

    def __init__(self, path: Path, *, logs_dir: Path, max_bytes: int, backup_count: int) -> None:
        if not path.resolve().is_relative_to(logs_dir.resolve()):
            raise LogPathError(f"{path} is outside the logs directory {logs_dir}")
        logs_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")

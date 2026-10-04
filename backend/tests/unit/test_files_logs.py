"""The rotating log handler stays inside `DATA_DIR/logs` (dev-environment §7)."""

from pathlib import Path

import pytest

from photo_triage.files import LogPathError, RotatingLogHandler


def handler(path: Path, logs_dir: Path) -> RotatingLogHandler:
    return RotatingLogHandler(path, logs_dir=logs_dir, max_bytes=1000, backup_count=5)


@pytest.mark.parametrize(
    "path",
    [
        "elsewhere/api.log",
        "data/api.log",
        "data/logs/../api.log",
        "photos/api.log",
    ],
)
def test_refuses_a_path_outside_the_logs_directory(tmp_path: Path, path: str) -> None:
    with pytest.raises(LogPathError, match="outside the logs directory"):
        handler(tmp_path / path, tmp_path / "data/logs")
    assert not (tmp_path / path).exists()


def test_refuses_a_symlink_out_of_the_logs_directory(tmp_path: Path) -> None:
    logs_dir = tmp_path / "data/logs"
    logs_dir.mkdir(parents=True)
    (tmp_path / "photos").mkdir()
    (logs_dir / "escape").symlink_to(tmp_path / "photos")

    with pytest.raises(LogPathError):
        handler(logs_dir / "escape/api.log", logs_dir)


def test_creates_the_logs_directory(tmp_path: Path) -> None:
    logs_dir = tmp_path / "data/logs"

    h = handler(logs_dir / "api.log", logs_dir)
    h.close()

    assert (logs_dir / "api.log").exists()
    assert (h.maxBytes, h.backupCount) == (1000, 5)

"""Logging setup shared by every process (`photo_triage.logs`)."""

import datetime as dt
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from photo_triage.db.engine import open_database
from photo_triage.logs import configure
from photo_triage.settings import Settings

LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) (\w+) (\S+) (.*)$")


def log_file(settings: Settings, process: str = "api") -> Path:
    return settings.data_dir / "logs" / f"{process}.log"


def lines(path: Path) -> list[str]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text().splitlines()


@pytest.fixture
def eastern_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the process in a timezone that isn't UTC."""
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("eastern_time")
def test_lines_are_plain_text_in_utc_on_stderr_and_in_the_file(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not (settings.data_dir / "logs").exists()
    configure(settings, "worker")
    before = dt.datetime.now(dt.UTC)

    logging.getLogger("photo_triage.worker").warning("disk %s", "full")

    [line] = lines(log_file(settings, "worker"))
    match = LINE.match(line)
    assert match is not None, line
    logged_at, level, name, message = match.groups()
    assert (level, name, message) == ("WARNING", "photo_triage.worker", "disk full")
    logged_at = dt.datetime.fromisoformat(logged_at)
    assert abs(logged_at - before) < dt.timedelta(seconds=5)
    assert capsys.readouterr().err == line + "\n"


def test_level_comes_from_log_level(settings: Settings) -> None:
    configure(settings.model_copy(update={"log_level": "WARNING"}), "api")

    logging.getLogger("photo_triage.api").info("hidden")
    logging.getLogger("photo_triage.api").warning("shown")

    assert [LINE.match(line)[4] for line in lines(log_file(settings))] == ["shown"]  # type: ignore[index]


def test_exceptions_are_logged_with_their_traceback(settings: Settings) -> None:
    configure(settings, "api")

    try:
        raise ValueError("bad pixel")
    except ValueError:
        logging.getLogger("photo_triage.api").exception("failed")

    text = "\n".join(lines(log_file(settings)))
    assert "ERROR photo_triage.api failed\nTraceback (most recent call last):" in text
    assert text.endswith("ValueError: bad pixel")


def test_log_file_rotates_and_keeps_five_old_files(settings: Settings) -> None:
    configure(settings, "worker", max_bytes=1000)

    for i in range(500):
        logging.getLogger("photo_triage.worker").info("line %03d %s", i, "x" * 40)

    names = sorted(p.name for p in (settings.data_dir / "logs").iterdir())
    assert names == ["worker.log", *(f"worker.log.{n}" for n in range(1, 6))]
    for path in (settings.data_dir / "logs").iterdir():
        assert path.stat().st_size <= 1000
    assert lines(log_file(settings, "worker"))[-1].endswith("line 499 " + "x" * 40)


def test_to_file_false_logs_only_to_stderr(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(settings, "api", to_file=False)

    logging.getLogger("photo_triage.api").warning("hello")

    assert "WARNING photo_triage.api hello" in capsys.readouterr().err
    assert not (settings.data_dir / "logs").exists()


@pytest.mark.parametrize(
    "name", ["uvicorn.error", "uvicorn.access", "alembic.runtime", "sqlalchemy"]
)
def test_library_loggers_share_the_format_and_level(settings: Settings, name: str) -> None:
    # Give the library a handler of its own, as uvicorn's default config would.
    logging.getLogger(name.split(".")[0]).addHandler(logging.StreamHandler())
    configure(settings, "api")

    logging.getLogger(name).debug("too detailed")
    logging.getLogger(name).warning("from the library")

    assert logging.getLogger(name.split(".")[0]).handlers == []
    [line] = lines(log_file(settings))
    assert LINE.match(line) is not None
    assert line.endswith(f"WARNING {name} from the library")


@pytest.mark.parametrize(("log_level", "sql_logged"), [("INFO", False), ("DEBUG", True)])
def test_sql_is_logged_only_at_debug(
    settings: Settings, tmp_path: Path, log_level: str, sql_logged: bool
) -> None:
    configure(settings.model_copy(update={"log_level": log_level}), "api")
    engine = open_database(tmp_path / "db")
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 42"))
    finally:
        engine.dispose()

    text = "\n".join(lines(log_file(settings)))
    assert ("INFO sqlalchemy.engine.Engine SELECT 42" in text) == sql_logged
    assert "Row (" not in text  # result rows never
    # The ORM logs dozens of lines at INFO when it first sets up the models.
    assert not logging.getLogger("sqlalchemy.orm.mapper.Mapper").isEnabledFor(logging.INFO)


def test_configuring_again_replaces_the_handlers(settings: Settings) -> None:
    configure(settings, "api")
    configure(settings, "api")

    logging.getLogger("photo_triage.api").warning("once")

    assert len(lines(log_file(settings))) == 1

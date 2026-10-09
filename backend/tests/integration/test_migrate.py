"""The migrate step, its backups, and the schema check the API and worker run at
startup (dev-environment §5, §6)."""

import datetime as dt
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from photo_triage.api.__main__ import main as api_main
from photo_triage.db.__main__ import main as db_main
from photo_triage.db.engine import DB_FILENAME
from photo_triage.db.migrate import (
    BACKUPS_KEPT,
    SchemaAheadError,
    SchemaBehindError,
    backups_dir,
    check_schema,
    current_revision,
    head_revision,
    migrate,
)
from photo_triage.settings import Settings

NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.UTC)
OLDER = "0003"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Settings:
    """The environment the commands read, matching the `settings` fixture."""
    monkeypatch.setenv("PHOTO_DIR", str(settings.photo_dir))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    return settings


@pytest.fixture
def older(engine: Engine, alembic_cfg: Config) -> Engine:
    """A database at an older revision, holding a job."""
    command.upgrade(alembic_cfg, OLDER)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO jobs (stage, priority, status, attempts, enqueued_at) "
            "VALUES ('scan', 1, 'pending', 0, '2026-10-01 00:00:00')"
        )
    engine.dispose()
    return engine


def _stamp(engine: Engine, revision: str) -> None:
    """Pretend newer code migrated the database."""
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE alembic_version SET version_num = ?", (revision,))
    engine.dispose()


def _backups(settings: Settings) -> list[str]:
    directory = backups_dir(settings)
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def _revision_in(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


# --- migrate -----------------------------------------------------------------


def test_creates_the_database_at_head_without_a_backup(settings: Settings, engine: Engine) -> None:
    assert migrate(settings, now=NOW) is None

    assert current_revision(engine) == head_revision()
    assert _backups(settings) == []


def test_upgrades_an_older_database_after_backing_it_up(settings: Settings, older: Engine) -> None:
    backup = migrate(settings, now=NOW)

    assert current_revision(older) == head_revision()
    assert backup is not None
    assert backup == backups_dir(settings) / f"20261003T120000Z-from-{OLDER}.db"
    assert _backups(settings) == [backup.name]
    # A copy of the database as it was, data and all.
    assert _revision_in(backup) == OLDER
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("SELECT stage FROM jobs").fetchall() == [("scan",)]


def test_does_nothing_at_head(settings: Settings, migrated: Engine) -> None:
    assert migrate(settings, now=NOW) is None

    assert current_revision(migrated) == head_revision()
    assert _backups(settings) == []


def test_keeps_only_the_newest_backups(settings: Settings, older: Engine) -> None:
    directory = backups_dir(settings)
    directory.mkdir(parents=True)
    old = [f"2026090{day}T000000Z-from-0001.db" for day in range(1, 7)]
    for name in [*old, "notes.txt"]:
        (directory / name).write_text("")

    backup = migrate(settings, now=NOW)

    assert backup is not None
    kept = [*old[-(BACKUPS_KEPT - 1) :], backup.name]
    assert _backups(settings) == sorted([*kept, "notes.txt"])


def test_refuses_a_database_newer_than_the_code(settings: Settings, migrated: Engine) -> None:
    _stamp(migrated, "9999")

    with pytest.raises(SchemaAheadError, match="newer than this code"):
        migrate(settings, now=NOW)

    assert current_revision(migrated) == "9999"
    assert _backups(settings) == []


def test_logs_the_backup_and_each_migration_at_info(
    settings: Settings, older: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    backup = migrate(settings, now=NOW)

    ours = [r for r in caplog.records if r.name == "photo_triage.db.migrate"]
    assert all(r.levelno == logging.INFO for r in ours)
    assert [r.getMessage() for r in ours] == [
        f"Backed up the database to {backup}",
        "Applied migration 0004: worker_controls: pause and resume, globally or per stage",
        "Applied migration 0005: worker_heartbeat: whether the worker process is running",
        f"The database is at revision {head_revision()}",
    ]


def _migrate_log(settings: Settings) -> list[str]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return (settings.data_dir / "logs/migrate.log").read_text().splitlines()


def test_command_logs_its_lines_and_alembic_s_to_migrate_log_in_the_app_format(
    env: Settings, older: Engine
) -> None:
    assert db_main(["migrate"]) == 0

    lines = _migrate_log(env)
    assert any(" INFO photo_triage.db.migrate Backed up the database to " in ln for ln in lines)
    running = [line for line in lines if "Running upgrade 0003 -> 0004" in line]
    # `photo_triage.logs` format: UTC timestamp, level, logger, message.
    assert len(running) == 1
    assert running[0].endswith(
        " INFO alembic.runtime.migration Running upgrade 0003 -> 0004, "
        "worker_controls: pause and resume, globally or per stage"
    )
    assert running[0][:24].endswith("Z")


def test_command_exits_1_for_a_newer_database(env: Settings, migrated: Engine) -> None:
    _stamp(migrated, "9999")

    assert db_main(["migrate"]) == 1
    assert any(" ERROR photo_triage.db The database is newer" in ln for ln in _migrate_log(env))


def test_command_exits_2_for_bad_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert db_main(["migrate"]) == 2
    assert "PHOTO_DIR" in capsys.readouterr().err


# --- Alembic's env.py ----------------------------------------------------------


def test_env_py_keeps_the_caller_s_logging_when_asked(alembic_cfg: Config, engine: Engine) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    alembic_cfg.attributes["configure_logger"] = False

    command.upgrade(alembic_cfg, "head")

    assert root.handlers == before


def test_env_py_uses_alembic_ini_s_logging_otherwise(alembic_cfg: Config, engine: Engine) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    command.upgrade(alembic_cfg, "head")  # as the Alembic CLI and `just db-reset` do

    [handler] = root.handlers
    assert handler not in before
    assert isinstance(handler, logging.StreamHandler)
    record = logging.makeLogRecord({"name": "alembic", "levelno": 20, "levelname": "INFO"})
    record.msg = "hello"
    assert handler.format(record) == "INFO  [alembic] hello"  # alembic.ini's format


# --- The schema check ------------------------------------------------------------


def test_check_passes_at_head(settings: Settings, migrated: Engine) -> None:
    check_schema(settings.data_dir)


def test_check_reports_a_missing_database_without_creating_it(settings: Settings) -> None:
    with pytest.raises(SchemaBehindError, match="no database") as raised:
        check_schema(settings.data_dir)

    assert "just db-migrate" in str(raised.value)
    assert not (settings.data_dir / DB_FILENAME).exists()


def test_check_reports_an_unmigrated_database(settings: Settings, engine: Engine) -> None:
    with engine.connect():
        pass  # creates the empty file

    with pytest.raises(SchemaBehindError, match="at revision none"):
        check_schema(settings.data_dir)


def test_check_reports_an_older_database(settings: Settings, older: Engine) -> None:
    with pytest.raises(SchemaBehindError) as raised:
        check_schema(settings.data_dir)

    message = str(raised.value)
    assert f"at revision {OLDER}, but this code expects {head_revision()}" in message
    assert "just db-migrate" in message


def test_check_reports_a_newer_database(settings: Settings, migrated: Engine) -> None:
    _stamp(migrated, "9999")

    with pytest.raises(SchemaAheadError, match="newer than this code: it's at revision 9999"):
        check_schema(settings.data_dir)


# --- At startup (the worker's is in test_worker.py) ----------------------------------


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    return calls


@pytest.mark.usefixtures("env", "older")
def test_api_exits_without_serving_when_the_database_is_behind(
    uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert api_main([]) == 1

    assert uvicorn_calls == []
    assert "just db-migrate" in capsys.readouterr().err


@pytest.mark.usefixtures("env")
def test_api_exits_without_serving_when_the_database_is_newer(
    migrated: Engine, uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    _stamp(migrated, "9999")

    assert api_main([]) == 1

    assert uvicorn_calls == []
    assert "newer than this code" in capsys.readouterr().err


@pytest.mark.usefixtures("env", "migrated")
def test_api_serves_when_the_database_is_at_head(uvicorn_calls: list[dict[str, Any]]) -> None:
    assert api_main([]) == 0
    assert len(uvicorn_calls) == 1

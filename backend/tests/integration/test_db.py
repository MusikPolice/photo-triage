"""The database engine, the migrations and the Phase 1 schema (dev-environment §6)."""

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from photo_triage.db.engine import BUSY_TIMEOUT_MS, DB_FILENAME, open_database
from photo_triage.db.models import Item, Job, JobStatus, MediaType

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"

# plan §8
PHASE_1_COLUMNS = {
    "items": {
        "id",
        "content_hash",
        "path",
        "media_type",
        "file_size",
        "file_mtime",
        "width",
        "height",
        "duration_s",
        "taken_at",
        "first_seen_at",
        "last_seen_at",
        "missing_since",
        "status",
    },
    "jobs": {
        "id",
        "item_id",
        "stage",
        "priority",
        "status",
        "attempts",
        "last_error",
        "enqueued_at",
        "started_at",
        "finished_at",
    },
    "job_stats": {"date", "stage", "processed", "errors", "busy_seconds"},
}

NOW = dt.datetime(2026, 10, 3, 12, tzinfo=dt.UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "data")
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_cfg(engine: Engine) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


@pytest.fixture
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    command.upgrade(alembic_cfg, "head")
    return engine


def _tables(engine: Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names()) - {"alembic_version"}


def test_open_database_creates_the_file_in_the_data_dir(tmp_path: Path, engine: Engine) -> None:
    with engine.connect():
        pass
    assert (tmp_path / "data" / DB_FILENAME).is_file()


def test_every_connection_uses_wal_and_enforces_foreign_keys(engine: Engine) -> None:
    for _ in range(2):  # a second, fresh connection is configured too
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_MS
        engine.dispose()


def test_every_migration_upgrades_downgrades_and_upgrades_again(
    engine: Engine, alembic_cfg: Config
) -> None:
    revisions = list(reversed(list(ScriptDirectory.from_config(alembic_cfg).walk_revisions())))
    assert revisions, "no migrations found"
    for rev in revisions:
        command.upgrade(alembic_cfg, rev.revision)
        command.downgrade(alembic_cfg, "-1")
        command.upgrade(alembic_cfg, rev.revision)

    command.downgrade(alembic_cfg, "base")
    assert _tables(engine) == set()
    command.upgrade(alembic_cfg, "head")
    assert _tables(engine) >= PHASE_1_COLUMNS.keys()


def test_models_and_migrations_agree(migrated: Engine, alembic_cfg: Config) -> None:
    command.check(alembic_cfg)  # raises if autogenerate would produce a migration


def test_phase_1_tables_have_the_plan_columns(migrated: Engine) -> None:
    inspector = sa.inspect(migrated)
    for table, columns in PHASE_1_COLUMNS.items():
        assert {c["name"] for c in inspector.get_columns(table)} == columns, table


def test_claiming_the_next_pending_job_uses_the_priority_index(migrated: Engine) -> None:
    claim = (
        sa.select(Job.id)
        .where(Job.status == JobStatus.PENDING)
        .order_by(Job.priority, Job.enqueued_at, Job.id)
        .limit(1)
    )
    with migrated.connect() as conn:
        sql = str(claim.compile(conn, compile_kwargs={"literal_binds": True}))
        plan = " ".join(row[-1] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}"))
    assert "ix_jobs_pending_by_priority" in plan
    assert "TEMP B-TREE" not in plan  # no sort step: the index supplies the order


def _item(content_hash: str = "a" * 64) -> Item:
    return Item(
        content_hash=content_hash,
        path="2024/01/a.jpg",
        media_type=MediaType.PHOTO,
        file_size=1,
        file_mtime=0.0,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_enums_are_stored_as_their_values_and_checked(migrated: Engine) -> None:
    with Session(migrated) as session:
        session.add(_item())
        session.commit()
    with migrated.connect() as conn:
        assert conn.exec_driver_sql("SELECT media_type, status FROM items").one() == (
            "photo",
            "active",
        )
        with pytest.raises(IntegrityError):
            conn.exec_driver_sql("UPDATE items SET status = 'deleted'")


def test_content_hash_is_unique(migrated: Engine) -> None:
    with Session(migrated) as session:
        session.add_all([_item(), _item()])
        with pytest.raises(IntegrityError):
            session.commit()


def test_jobs_need_an_existing_item_and_go_with_it(migrated: Engine) -> None:
    with Session(migrated) as session:
        session.add(Job(item_id=999, stage="scan", priority=1, enqueued_at=NOW))
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(migrated) as session:
        item = _item()
        session.add(item)
        session.flush()
        session.add(Job(item_id=item.id, stage="scan", priority=1, enqueued_at=NOW))
        session.add(Job(item_id=None, stage="layout", priority=6, enqueued_at=NOW))
        session.commit()
        session.delete(item)
        session.commit()
        assert session.scalars(sa.select(Job.stage)).all() == ["layout"]


def test_timestamps_round_trip_as_aware_utc(migrated: Engine) -> None:
    eastern = dt.timezone(dt.timedelta(hours=-4))
    with Session(migrated) as session:
        session.add(Job(stage="scan", priority=1, enqueued_at=NOW.astimezone(eastern)))
        session.commit()
    with Session(migrated) as session:
        assert session.scalars(sa.select(Job.enqueued_at)).one() == NOW
        assert session.scalars(sa.select(Job.enqueued_at)).one().tzinfo == dt.UTC

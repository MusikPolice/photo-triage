"""Keep decisions survive DB loss (dev-environment §7): with writes on, a duplicate
group kept whole ("Keep all") marks its members' files, so after the database is
deleted and the library scanned and grouped again, the group isn't pending (#156).
"""

import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from photo_triage.api.app import create_app
from photo_triage.db.engine import DB_FILENAME
from photo_triage.db.models import DuplicateGroup, DuplicateMember, GroupStatus, Item
from photo_triage.ml import FakeClip
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main


def groups_by_paths(engine: Engine) -> dict[frozenset[str], GroupStatus]:
    with Session(engine) as session:
        found: dict[frozenset[str], GroupStatus] = {}
        for group in session.scalars(sa.select(DuplicateGroup)):
            paths = session.scalars(
                sa.select(Item.path)
                .join(DuplicateMember, DuplicateMember.item_id == Item.id)
                .where(DuplicateMember.group_id == group.id)
            )
            found[frozenset(paths)] = group.status
        return found


def process(clip: FakeClip) -> None:
    assert main(["scan"]) == 0
    assert main(["--once"], clip=clip) == 0


def test_a_group_kept_whole_isn_t_pending_after_the_database_is_rebuilt(
    migrated: Engine,
    alembic_cfg: Config,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.trash_dir.mkdir()
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))
    monkeypatch.setenv("EXIF_WRITES_ENABLED", "true")
    # FakeClip's vectors are the same only for the same pixels: two copies make two
    # groups, one to keep whole and one left alone.
    for source in ("scenes/scene-01.jpg", "burst/burst-01.jpg"):
        copy = library / "copies" / Path(source).name
        copy.parent.mkdir(exist_ok=True)
        shutil.copy(library / source, copy)
    clip = FakeClip()
    monkeypatch.setenv("CLIP_MODEL", clip.model)
    process(clip)

    with Session(migrated) as session:
        group_id = session.scalar(
            sa.select(DuplicateGroup.id)
            .where(DuplicateGroup.status == GroupStatus.PENDING)
            .order_by(DuplicateGroup.id)
        )
    assert group_id is not None, "the copies should make duplicate groups"
    before = groups_by_paths(migrated)
    assert len(before) == 2
    with Session(migrated) as session:
        kept = frozenset(
            session.scalars(
                sa.select(Item.path)
                .join(DuplicateMember, DuplicateMember.item_id == Item.id)
                .where(DuplicateMember.group_id == group_id)
            )
        )

    app_settings = settings.model_copy(update={"exif_writes_enabled": True})
    with TestClient(create_app(app_settings)) as client:
        response = client.post(
            f"/api/duplicates/{group_id}/decide",
            json={"reviewer": "reviewer-safety", "action": "keep_all"},
        )
        assert response.status_code == 200, response.text
    assert main(["--once"], clip=clip) == 0  # makes the marker writes

    # Lose the database, and build it again from the library.
    migrated.dispose()
    for leftover in settings.data_dir.glob(f"{DB_FILENAME}*"):
        leftover.unlink()
    command.upgrade(alembic_cfg, "head")
    process(clip)

    after = groups_by_paths(migrated)
    assert after == {
        kept: GroupStatus.RESOLVED,
        # The other comes back pending, as it was.
        **{p: GroupStatus.PENDING for p in before if p != kept},
    }

"""Pruning database backups deletes only old backups (dev-environment §7)."""

from pathlib import Path

import pytest

from photo_triage.files import prune_backups


def _backups(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(name)


def test_deletes_all_but_the_newest(tmp_path: Path) -> None:
    names = [f"2026100{day}T000000Z-from-0003.db" for day in range(1, 8)]
    _backups(tmp_path, *reversed(names))

    deleted = prune_backups(tmp_path, keep=5)

    assert deleted == [tmp_path / names[0], tmp_path / names[1]]
    assert sorted(p.name for p in tmp_path.iterdir()) == names[2:]


def test_deletes_nothing_when_there_are_few_enough(tmp_path: Path) -> None:
    _backups(tmp_path, "20261001T000000Z-from-0003.db", "20261002T000000Z-from-0004.db")

    assert prune_backups(tmp_path, keep=5) == []
    assert len(list(tmp_path.iterdir())) == 2


def test_leaves_anything_but_backup_files_alone(tmp_path: Path) -> None:
    _backups(tmp_path, "00000000T000000Z-notes.txt", "20261002T000000Z-from-0004.db")
    (tmp_path / "00000000T000000Z-folder.db").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-elsewhere.db"
    outside.write_text("")
    (tmp_path / "00000000T000000Z-link.db").symlink_to(outside)

    assert prune_backups(tmp_path, keep=1) == []
    assert outside.exists()
    assert len(list(tmp_path.iterdir())) == 4


def test_keeps_at_least_one(tmp_path: Path) -> None:
    _backups(tmp_path, "20261001T000000Z-from-0003.db")

    with pytest.raises(ValueError, match="keep at least one"):
        prune_backups(tmp_path, keep=0)
    assert len(list(tmp_path.iterdir())) == 1

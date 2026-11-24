"""Deleting files from the trash for good (`photo_triage.files.purge_from_trash`,
plan §6.8): only a file at its entry's path inside the trash, never a link, a
folder, or anything outside."""

from pathlib import Path

import pytest

from photo_triage.files import TrashError, TrashPathError, move_to_trash, purge_from_trash


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    folder = tmp_path / "photos"
    (folder / "2019/07").mkdir(parents=True)
    (folder / "2019/07/a.jpg").write_bytes(b"a")
    (folder / "2019/07/b.jpg").write_bytes(b"b")
    return folder


@pytest.fixture
def trash(photos: Path) -> Path:
    """Inside the library, as by default."""
    return photos / ".photo-triage-trash"


def test_a_trashed_file_is_deleted_and_its_emptied_folders_go(photos: Path, trash: Path) -> None:
    move_to_trash(photos, trash, "2019/07/a.jpg")

    assert purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg") is True

    assert list(trash.iterdir()) == []
    # The library's own folder and its other file stay.
    assert (photos / "2019/07/b.jpg").read_bytes() == b"b"


def test_folders_still_holding_files_stay(photos: Path, trash: Path) -> None:
    move_to_trash(photos, trash, "2019/07/a.jpg")
    move_to_trash(photos, trash, "2019/07/b.jpg")

    purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (trash / "2019/07/b.jpg").read_bytes() == b"b"


def test_a_file_already_gone_is_reported_not_raised(trash: Path) -> None:
    trash.mkdir()
    assert purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg") is False


def test_a_path_that_isn_t_the_entry_s_is_refused(photos: Path, trash: Path) -> None:
    move_to_trash(photos, trash, "2019/07/a.jpg")

    with pytest.raises(TrashPathError, match="isn't where"):
        purge_from_trash(trash, "2019/07/a.jpg", "2019/07/b.jpg")

    assert (trash / "2019/07/a.jpg").exists()


@pytest.mark.parametrize(
    "relative",
    ["../2019/07/b.jpg", "../../outside.jpg", "/etc/hostname", ".", "2019/.."],
)
def test_a_path_out_of_the_trash_is_refused(
    photos: Path, trash: Path, tmp_path: Path, relative: str
) -> None:
    trash.mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"x")

    with pytest.raises(TrashPathError):
        purge_from_trash(trash, relative, relative)

    assert (photos / "2019/07/b.jpg").exists()
    assert (tmp_path / "outside.jpg").exists()


def test_a_linked_folder_can_t_lead_out(photos: Path, trash: Path) -> None:
    trash.mkdir()
    (trash / "2019").symlink_to(photos / "2019")

    with pytest.raises(TrashPathError):
        purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (photos / "2019/07/a.jpg").exists()


def test_a_link_is_refused_and_left(photos: Path, trash: Path) -> None:
    (trash / "2019/07").mkdir(parents=True)
    link = trash / "2019/07/a.jpg"
    link.symlink_to(photos / "2019/07/a.jpg")

    with pytest.raises(TrashError, match="isn't a file"):
        purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert link.is_symlink()
    assert (photos / "2019/07/a.jpg").exists()


def test_a_folder_is_refused_and_left(trash: Path) -> None:
    (trash / "2019/07/a.jpg").mkdir(parents=True)
    (trash / "2019/07/a.jpg/inner.jpg").write_bytes(b"i")

    with pytest.raises(TrashError, match="isn't a file"):
        purge_from_trash(trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (trash / "2019/07/a.jpg/inner.jpg").exists()

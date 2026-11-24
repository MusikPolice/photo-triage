"""Moving files to the trash and back (`photo_triage.files.trash`, plan §6.8): renames
on one filesystem, never replacing a file, never leading out of the library or the
trash."""

import errno
import os
from pathlib import Path

import pytest

from photo_triage.files import (
    TrashError,
    TrashPathError,
    move_to_trash,
    restore_from_trash,
    same_filesystem,
)
from photo_triage.files import trash as trash_module


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    folder = tmp_path / "photos"
    (folder / "2019/07").mkdir(parents=True)
    (folder / "2019/07/a.jpg").write_bytes(b"a")
    return folder


@pytest.fixture
def trash(photos: Path) -> Path:
    """Inside the library, as by default."""
    return photos / ".photo-triage-trash"


def test_a_file_moves_to_the_same_path_in_the_trash_and_back(photos: Path, trash: Path) -> None:
    source = photos / "2019/07/a.jpg"
    os.utime(source, ns=(1_000_000_123, 2_000_000_456))
    inode = source.stat().st_ino

    assert move_to_trash(photos, trash, "2019/07/a.jpg") == "2019/07/a.jpg"

    in_trash = trash / "2019/07/a.jpg"
    assert not source.exists()
    assert (in_trash.read_bytes(), in_trash.stat().st_ino) == (b"a", inode)
    assert in_trash.stat().st_mtime_ns == 2_000_000_456
    # The library's folder stays, even empty: it's the household's.
    assert (photos / "2019/07").is_dir()

    restore_from_trash(photos, trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (source.read_bytes(), source.stat().st_ino) == (b"a", inode)
    assert source.stat().st_mtime_ns == 2_000_000_456
    # The trash's folders that it emptied go; the trash itself stays.
    assert list(trash.iterdir()) == []


def test_a_restore_recreates_the_library_folder(photos: Path, trash: Path) -> None:
    move_to_trash(photos, trash, "2019/07/a.jpg")
    (photos / "2019/07").rmdir()

    restore_from_trash(photos, trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (photos / "2019/07/a.jpg").read_bytes() == b"a"


def test_neither_move_replaces_a_file(photos: Path, trash: Path) -> None:
    (trash / "2019/07").mkdir(parents=True)
    (trash / "2019/07/a.jpg").write_bytes(b"older")

    with pytest.raises(TrashError, match="already exists"):
        move_to_trash(photos, trash, "2019/07/a.jpg")
    assert (photos / "2019/07/a.jpg").read_bytes() == b"a"

    (trash / "2019/07/a.jpg").unlink()
    move_to_trash(photos, trash, "2019/07/a.jpg")
    (photos / "2019/07/a.jpg").write_bytes(b"newer")

    with pytest.raises(TrashError, match="already exists"):
        restore_from_trash(photos, trash, "2019/07/a.jpg", "2019/07/a.jpg")
    assert (photos / "2019/07/a.jpg").read_bytes() == b"newer"
    assert (trash / "2019/07/a.jpg").read_bytes() == b"a"


def test_a_missing_file_is_not_found(photos: Path, trash: Path) -> None:
    with pytest.raises(FileNotFoundError):
        move_to_trash(photos, trash, "2019/07/nothing.jpg")
    with pytest.raises(FileNotFoundError):
        restore_from_trash(photos, trash, "2019/07/a.jpg", "2019/07/a.jpg")


@pytest.mark.parametrize(
    "relative",
    ["../outside.jpg", "2019/../../outside.jpg", "/etc/passwd", "", ".", "2019/.."],
)
def test_a_path_out_of_the_library_is_refused(
    photos: Path, trash: Path, tmp_path: Path, relative: str
) -> None:
    (tmp_path / "outside.jpg").write_bytes(b"not the library's")

    with pytest.raises(TrashPathError):
        move_to_trash(photos, trash, relative)
    with pytest.raises(TrashPathError):
        restore_from_trash(photos, trash, relative, "2019/07/b.jpg")
    with pytest.raises(TrashPathError):
        restore_from_trash(photos, trash, "2019/07/a.jpg", relative)
    assert (tmp_path / "outside.jpg").read_bytes() == b"not the library's"


def test_a_linked_folder_can_t_lead_out(photos: Path, trash: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere/b.jpg").write_bytes(b"b")
    (photos / "link").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(TrashPathError):
        move_to_trash(photos, trash, "link/b.jpg")


def test_a_link_is_not_moved(photos: Path, trash: Path) -> None:
    (photos / "2019/07/link.jpg").symlink_to(photos / "2019/07/a.jpg")

    with pytest.raises(TrashError, match="isn't a file"):
        move_to_trash(photos, trash, "2019/07/link.jpg")
    assert (photos / "2019/07/link.jpg").is_symlink()


def test_a_folder_is_not_moved(photos: Path, trash: Path) -> None:
    with pytest.raises(TrashError, match="isn't a file"):
        move_to_trash(photos, trash, "2019/07")


def test_a_file_in_the_trash_isn_t_trashed_again_from_the_library_side(
    photos: Path, trash: Path
) -> None:
    move_to_trash(photos, trash, "2019/07/a.jpg")

    with pytest.raises(TrashPathError, match="in the trash"):
        move_to_trash(photos, trash, ".photo-triage-trash/2019/07/a.jpg")
    with pytest.raises(TrashPathError, match="in the trash"):
        restore_from_trash(photos, trash, "2019/07/a.jpg", ".photo-triage-trash/x.jpg")


def test_a_move_across_filesystems_is_refused_not_copied(
    photos: Path, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trash_module, "same_filesystem", lambda path, folder: False)

    with pytest.raises(TrashError, match="different filesystems"):
        move_to_trash(photos, trash, "2019/07/a.jpg")
    assert (photos / "2019/07/a.jpg").exists()
    assert not trash.exists()


def test_a_rename_the_kernel_won_t_do_across_filesystems_is_refused(
    photos: Path, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cross_device(self: Path, target: Path) -> Path:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(Path, "rename", cross_device)

    with pytest.raises(TrashError, match="different filesystems"):
        move_to_trash(photos, trash, "2019/07/a.jpg")
    assert (photos / "2019/07/a.jpg").exists()


def test_other_rename_errors_are_raised(
    photos: Path, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path, target: Path) -> Path:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "rename", denied)

    with pytest.raises(PermissionError):
        move_to_trash(photos, trash, "2019/07/a.jpg")


def test_same_filesystem_looks_at_the_nearest_folder_that_exists(
    photos: Path, tmp_path: Path
) -> None:
    assert same_filesystem(photos / "2019/07/a.jpg", tmp_path / "not/made/yet")
    # /proc is a filesystem of its own on Linux.
    assert not same_filesystem(photos / "2019/07/a.jpg", Path("/proc/self"))


def test_restore_leaves_trash_folders_that_still_hold_files(photos: Path, trash: Path) -> None:
    (photos / "2019/07/b.jpg").write_bytes(b"b")
    move_to_trash(photos, trash, "2019/07/a.jpg")
    move_to_trash(photos, trash, "2019/07/b.jpg")

    restore_from_trash(photos, trash, "2019/07/a.jpg", "2019/07/a.jpg")

    assert (trash / "2019/07/b.jpg").exists()

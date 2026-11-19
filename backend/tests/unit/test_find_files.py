"""Which folders the library walk stays out of (plan §6.1)."""

from pathlib import Path

import pytest

from photo_triage.pipeline.scan import find_files

SKIPPED = ["@eaDir", "@tmp", "#recycle", "#snapshot", ".thumbnails"]


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def _found(library: Path) -> set[str]:
    return {f.path for f in find_files(library, library / ".photo-triage-trash")}


@pytest.mark.parametrize("folder", SKIPPED)
@pytest.mark.parametrize("parent", ["", "2019/April/"])
def test_files_in_a_skipped_folder_are_not_found(tmp_path: Path, folder: str, parent: str) -> None:
    _touch(tmp_path / f"{parent}photo.jpg")
    _touch(tmp_path / f"{parent}{folder}/photo.jpg")
    _touch(tmp_path / f"{parent}{folder}/nested/clip.mp4")

    assert _found(tmp_path) == {f"{parent}photo.jpg"}


def test_a_folder_with_those_characters_elsewhere_in_its_name_is_walked(tmp_path: Path) -> None:
    for name in ["a@b", "x.y", "recycle", "#2019", "snapshot"]:
        _touch(tmp_path / name / "photo.jpg")

    assert _found(tmp_path) == {
        "a@b/photo.jpg",
        "x.y/photo.jpg",
        "recycle/photo.jpg",
        "#2019/photo.jpg",
        "snapshot/photo.jpg",
    }


def test_a_file_named_like_a_skipped_folder_is_judged_by_its_suffix(tmp_path: Path) -> None:
    for name in [".hidden.jpg", "@eaDir.jpg", "#recycle.mov", ".notes.txt"]:
        _touch(tmp_path / name)

    assert _found(tmp_path) == {".hidden.jpg", "@eaDir.jpg", "#recycle.mov"}


def test_the_trash_is_still_skipped(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    _touch(trash / "trashed.jpg")
    _touch(tmp_path / "photo.jpg")

    assert {f.path for f in find_files(tmp_path, trash)} == {"photo.jpg"}

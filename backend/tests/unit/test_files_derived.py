"""Replacing derived files deletes only inside `DATA_DIR/derived` (dev-environment §7)."""

from pathlib import Path

import pytest

from photo_triage.files import DerivedPathError, remove_derived, replace_derived


def _folder(path: Path, *names: str) -> Path:
    path.mkdir(parents=True)
    for name in names:
        (path / name).write_text(name)
    return path


def test_replaces_the_old_set_with_the_new(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    target = _folder(derived / "frames" / "7", "0.jpg", "1.jpg", "2.jpg")
    staged = _folder(derived / "frames" / "7.new", "0.jpg")

    replace_derived(derived, staged, target)

    assert [p.name for p in target.iterdir()] == ["0.jpg"]
    assert not staged.exists()


def test_creates_the_target_the_first_time(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    staged = _folder(derived / "staging", "0.jpg")

    replace_derived(derived, staged, derived / "frames" / "7")

    assert (derived / "frames" / "7" / "0.jpg").read_text() == "0.jpg"


def test_replaces_a_file_in_one_step(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    folder = _folder(derived / "thumbnails", "7.jpg", "7.jpg.new")

    replace_derived(derived, folder / "7.jpg.new", folder / "7.jpg")

    assert [p.name for p in folder.iterdir()] == ["7.jpg"]
    assert (folder / "7.jpg").read_text() == "7.jpg.new"


def test_a_link_is_never_staged(tmp_path: Path) -> None:
    derived = _folder(tmp_path / "derived")
    library = _folder(tmp_path / "library", "photo.jpg")
    (derived / "link.jpg").symlink_to(library / "photo.jpg")

    with pytest.raises(DerivedPathError):
        replace_derived(derived, derived / "link.jpg", derived / "7.jpg")
    assert (library / "photo.jpg").exists()


def test_remove_deletes_a_folder_and_ignores_one_already_gone(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    folder = _folder(derived / "frames" / "7", "0.jpg")

    remove_derived(derived, folder)
    remove_derived(derived, folder)

    assert not folder.exists()
    assert derived.exists()


def test_a_link_is_removed_without_following_it(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    outside = _folder(tmp_path / "library", "photo.jpg")
    (derived / "frames").mkdir(parents=True)
    link = derived / "frames" / "7"
    link.symlink_to(outside)

    remove_derived(derived, link)

    assert not link.exists()
    assert (outside / "photo.jpg").exists()


@pytest.mark.parametrize(
    "path",
    ["library", "derived", "derived/..", "derived/frames/../../library", "derived/link/x"],
)
def test_refuses_anything_outside_the_derived_folder(tmp_path: Path, path: str) -> None:
    derived = _folder(tmp_path / "derived")
    library = _folder(tmp_path / "library", "photo.jpg")
    (derived / "link").symlink_to(library)
    staged = _folder(derived / "staging", "0.jpg")

    with pytest.raises(DerivedPathError):
        remove_derived(derived, tmp_path / path)
    with pytest.raises(DerivedPathError):
        replace_derived(derived, staged, tmp_path / path)

    assert (library / "photo.jpg").exists()
    assert (staged / "0.jpg").exists()


def test_refuses_to_stage_from_outside(tmp_path: Path) -> None:
    derived = _folder(tmp_path / "derived")
    elsewhere = _folder(tmp_path / "elsewhere", "0.jpg")

    with pytest.raises(DerivedPathError):
        replace_derived(derived, elsewhere, derived / "frames" / "7")
    assert (elsewhere / "0.jpg").exists()

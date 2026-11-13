"""No escape, and the library is untouched (dev-environment §7): a full worker run over
a copy of the tier-1 fixtures, through every Phase 2 stage with the fake embedder,
changes nothing but `DATA_DIR`, and leaves every library file as it was.

The run is `python -m photo_triage.worker`'s own `main`: a walk, then `--once`. The
temp root holds `PHOTO_DIR`, `TRASH_DIR` and `DATA_DIR`, plus a home folder, a temp
folder and a bystander folder, so a write to `~`, to `$TMPDIR` or next to the
library shows up in the snapshot as well.

There's no `EXIF_WRITES_ENABLED` yet: nothing writes metadata until Phase 4, which
runs these with writes off and adds the write-back cases.
"""

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from photo_triage.ml import FakeClip
from photo_triage.pipeline import thumbnails
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main


@dataclass(frozen=True)
class Entry:
    kind: str
    """`file`, `dir` or `link`."""
    size_bytes: int
    mtime_ns: int
    digest: str
    """SHA-256 of a file's bytes, or a link's target."""


Snapshot = dict[str, Entry]


def snapshot(root: Path) -> Snapshot:
    """Every path under `root`, relative to it, without following links."""
    entries: Snapshot = {}
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(folder, name)
            info = path.lstat()
            if path.is_symlink():
                entry = Entry("link", 0, info.st_mtime_ns, os.readlink(path))
            elif path.is_dir():
                # A folder's mtime changes when anything is created or removed in it,
                # even a temporary file that's gone again.
                entry = Entry("dir", 0, info.st_mtime_ns, "")
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                entry = Entry("file", info.st_size, info.st_mtime_ns, digest)
            entries[str(path.relative_to(root))] = entry
    return entries


def differences(before: Snapshot, after: Snapshot) -> set[str]:
    """Paths added, removed or changed."""
    return {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}


def outside(paths: set[str], allowed: Path) -> set[str]:
    """Those of `paths` not under `allowed`, nor `allowed` itself."""
    return {p for p in paths if not Path(p).is_relative_to(allowed)}


@dataclass
class Run:
    root: Path
    settings: Settings
    before: Snapshot
    after: Snapshot


@pytest.fixture
def run_worker(
    migrated: Engine,
    settings: Settings,
    library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Run]:
    """Runs a full worker over the library, and snapshots the temp root around it."""
    settings.trash_dir.mkdir()
    for name in ("home", "tmp", "bystander"):
        (tmp_path / name).mkdir()
    (tmp_path / "bystander/notes.txt").write_text("not the app's")
    env = {
        "PHOTO_DIR": settings.photo_dir,
        "TRASH_DIR": settings.trash_dir,
        "DATA_DIR": settings.data_dir,
        "MODELS_DIR": settings.models_dir,
        "HOME": tmp_path / "home",
        "XDG_CACHE_HOME": tmp_path / "home/.cache",
        "TMPDIR": tmp_path / "tmp",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr(tempfile, "tempdir", None)  # read TMPDIR again

    def run() -> Run:
        before = snapshot(tmp_path)
        assert main(["scan"]) == 0
        assert main(["--once"], clip=FakeClip()) == 0
        return Run(tmp_path, settings, before, snapshot(tmp_path))

    return run


def _relative(run: Run, path: Path) -> Path:
    return path.relative_to(run.root)


def test_a_full_worker_run_changes_nothing_outside_data_dir(
    run_worker: Callable[[], Run],
) -> None:
    run = run_worker()

    changed = differences(run.before, run.after)
    data = _relative(run, run.settings.data_dir)
    # It ran: every stage wrote its output there.
    for made in ("photo-triage.db", "derived/frames", "derived/thumbnails/large", "logs"):
        assert str(data / made) in changed
    assert outside(changed, data) == set()


def test_a_full_worker_run_leaves_every_library_file_as_it_was(
    run_worker: Callable[[], Run],
) -> None:
    run = run_worker()

    photos = _relative(run, run.settings.photo_dir)
    library = {p: e for p, e in run.before.items() if Path(p).is_relative_to(photos)}
    assert sum(e.kind == "file" for e in library.values()) == 31  # every tier-1 file
    after = {p: e for p, e in run.after.items() if Path(p).is_relative_to(photos)}
    # Bytes, size and mtime of each file, and each folder's mtime, so a file added and
    # removed again shows too.
    assert after == library


# --- The checks catch a stray write --------------------------------------------------


def _stray(monkeypatch: pytest.MonkeyPatch, settings: Settings, write: "Stray") -> None:
    """Make the `thumbnail` stage also do `write`, each time it runs."""
    make = thumbnails.make

    def make_and_stray(*args: Any, **kwargs: Any) -> bool:
        write(settings)
        return make(*args, **kwargs)

    monkeypatch.setattr(thumbnails, "make", make_and_stray)


Stray = Callable[[Settings], None]


def _beside_the_library(s: Settings) -> None:
    (s.photo_dir.parent / "bystander/stray.txt").write_text("stray")


def _in_home(s: Settings) -> None:
    (Path.home() / ".stray").write_text("stray")


def _in_tmp(s: Settings) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:  # in $TMPDIR
        f.write("stray")


def _in_trash(s: Settings) -> None:
    (s.trash_dir / "stray.txt").write_text("stray")


def _touch_a_photo(s: Settings) -> None:
    os.utime(s.photo_dir / "scenes/scene-01.jpg")


def _temp_file_in_the_library(s: Settings) -> None:
    stray = s.photo_dir / "scenes/.stray.tmp"
    stray.write_text("stray")
    stray.unlink()


@pytest.mark.parametrize(
    "write", [_beside_the_library, _in_home, _in_tmp, _in_trash], ids=lambda w: w.__name__
)
def test_no_escape_catches_a_write_outside_data_dir(
    run_worker: Callable[[], Run],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    write: Stray,
) -> None:
    _stray(monkeypatch, settings, write)

    run = run_worker()

    assert outside(differences(run.before, run.after), _relative(run, settings.data_dir))


@pytest.mark.parametrize(
    "write", [_touch_a_photo, _temp_file_in_the_library], ids=lambda w: w.__name__
)
def test_the_library_check_catches_a_change_to_it(
    run_worker: Callable[[], Run],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    write: Stray,
) -> None:
    _stray(monkeypatch, settings, write)

    run = run_worker()

    photos = _relative(run, settings.photo_dir)
    changed = differences(run.before, run.after)
    assert {p for p in changed if Path(p).is_relative_to(photos)}

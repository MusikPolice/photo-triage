"""Moving library files to the trash and back (plan §6.8). Both are renames, so they
live here (dev-environment §7).

A file keeps its path relative to the library, under `TRASH_DIR`. A move is always
a rename on one filesystem: across two it fails, rather than copying and deleting,
which would be slow for a large video and could be left half done. Neither move
replaces a file that's already at the destination, and both refuse a path that
leads out of the library or the trash, or into the trash from the library side.
"""

import errno
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class TrashError(Exception):
    """A file can't be moved to or from the trash. The message says why."""


class TrashPathError(TrashError):
    """A path leads outside the folder it must be in."""


def move_to_trash(photo_dir: Path, trash_dir: Path, relative: str) -> str:
    """Move the library file at `relative` into the trash, at the same relative path,
    and return that path, relative to `trash_dir`. Raises `FileNotFoundError` if the
    file isn't there, and `TrashError` if it can't be moved."""
    source = _inside(photo_dir, relative, outside=trash_dir)
    target = _inside(trash_dir, relative)
    _move(source, target, trash_dir)
    return relative


def restore_from_trash(photo_dir: Path, trash_dir: Path, trash_path: str, original: str) -> None:
    """Move the file at `trash_path` in the trash back to `original` in the library.
    Raises `FileNotFoundError` if it isn't in the trash, and `TrashError` if it can't
    be moved, e.g. a file is at `original` again. Removes the trash folders it leaves
    empty."""
    source = _inside(trash_dir, trash_path)
    target = _inside(photo_dir, original, outside=trash_dir)
    _move(source, target, photo_dir)
    _remove_empty_folders(source.parent, trash_dir)


def _move(source: Path, target: Path, target_root: Path) -> None:
    if source.is_symlink() or not source.is_file():
        if not source.exists() and not source.is_symlink():
            raise FileNotFoundError(errno.ENOENT, "No such file", str(source))
        raise TrashError(f"{source} isn't a file")
    if target.exists() or target.is_symlink():
        raise TrashError(f"{target} already exists")
    if not same_filesystem(source, target_root):
        raise TrashError(f"{source} and {target_root} are on different filesystems")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Not `replace`: on a clash between the check and here, POSIX `rename`
        # replaces too, but the check makes that a race, not the rule.
        source.rename(target)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise TrashError(f"{source} and {target} are on different filesystems") from e
        raise


def same_filesystem(path: Path, folder: Path) -> bool:
    """Whether `path` is on the filesystem `folder` is, or would be: the nearest of
    `folder` and its parents that exists."""
    existing = folder
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    return os.stat(path).st_dev == os.stat(existing).st_dev


def _inside(root: Path, relative: str, *, outside: Path | None = None) -> Path:
    """`root / relative`, checked to be a path under `root`, and not under `outside`
    (the trash, when it's inside the library). The folder holding it is resolved, so
    `..` or a linked folder can't lead out; the file itself isn't, so a link there
    is refused as not a file rather than followed."""
    path = root / relative
    if Path(relative).is_absolute() or path.name in ("", ".", ".."):
        raise TrashPathError(f"{relative!r} isn't a path in {root}")
    where = path.parent.resolve() / path.name
    if not where.is_relative_to(root.resolve()) or where == root.resolve():
        raise TrashPathError(f"{relative!r} leads outside {root}")
    if outside is not None and where.is_relative_to(outside.resolve()):
        raise TrashPathError(f"{relative!r} is in the trash, {outside}")
    return path


def _remove_empty_folders(folder: Path, root: Path) -> None:
    """Remove `folder` and its parents up to, not including, `root`, while empty."""
    root = root.resolve()
    folder = folder.resolve()
    while folder != root and folder.is_relative_to(root):
        try:
            folder.rmdir()
        except OSError:
            return  # not empty, or gone
        folder = folder.parent

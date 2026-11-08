"""Regenerable files in `DATA_DIR/derived` (plan §4): replacing an item's set of
them deletes the old ones, so it lives here (dev-environment §7).

A stage writes a new set into a staging folder of its own, then swaps it in with
`replace_derived`, so the old set stays whole until the new one is complete. Both
functions refuse any folder outside the derived folder, or the folder itself.
"""

import shutil
from pathlib import Path


class DerivedPathError(ValueError):
    """A folder to replace or remove isn't inside the derived folder."""


def replace_derived(derived_dir: Path, staged: Path, target: Path) -> None:
    """Make `target` the folder `staged` is, deleting what `target` held. Both must
    be inside `derived_dir`, on the same filesystem, so this is a rename."""
    _check_inside(derived_dir, staged)
    _check_inside(derived_dir, target)
    if not staged.is_dir():
        raise DerivedPathError(f"{staged} isn't a folder")
    if target.exists() or target.is_symlink():
        _remove(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(target)


def remove_derived(derived_dir: Path, path: Path) -> None:
    """Delete the folder or file `path`, inside `derived_dir`, if it exists."""
    _check_inside(derived_dir, path)
    if path.exists() or path.is_symlink():
        _remove(path)


def _remove(path: Path) -> None:
    # A link is removed, never followed.
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _check_inside(derived_dir: Path, path: Path) -> None:
    # The folder holding `path` is resolved, so `..` or a linked folder can't lead
    # out; `path` itself isn't, so a link there is removed rather than followed.
    root = derived_dir.resolve()
    where = path.parent.resolve() / path.name
    if path.name in ("", ".", "..") or not where.is_relative_to(root) or where == root:
        raise DerivedPathError(f"{path} isn't inside {derived_dir}")

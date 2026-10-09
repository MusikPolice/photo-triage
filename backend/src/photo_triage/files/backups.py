"""Database backups: pruning deletes the oldest, so it lives here (dev-environment §7).

The migrate step writes the backups themselves with SQLite's `VACUUM INTO`
(`photo_triage.db.migrate`), which creates a file and moves or deletes nothing.
"""

from pathlib import Path

BACKUP_SUFFIX = ".db"


def prune_backups(backups_dir: Path, *, keep: int) -> list[Path]:
    """Delete all but the `keep` newest backups in `backups_dir`, returning those deleted.

    Backups are named from a UTC timestamp, so the newest sort last. Only `*.db`
    files directly in `backups_dir` count. Anything else there is left alone.
    """
    if keep < 1:
        raise ValueError(f"keep at least one backup, not {keep}")
    backups = sorted(
        path
        for path in backups_dir.iterdir()
        if path.suffix == BACKUP_SUFFIX and path.is_file() and not path.is_symlink()
    )
    old = backups[:-keep]
    for path in old:
        path.unlink()
    return old

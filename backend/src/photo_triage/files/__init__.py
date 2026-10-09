"""The only module allowed to mutate files: trash, restore, purge, EXIF writes, log
rotation, pruning database backups and the worker's lock file.

Other packages use the public interface exported here, never the submodules.
See docs/dev-environment.md section 7.
"""

from photo_triage.files.backups import prune_backups
from photo_triage.files.lock import LockHeldError, exclusive_lock
from photo_triage.files.logs import LogPathError, RotatingLogHandler

__all__ = [
    "LockHeldError",
    "LogPathError",
    "RotatingLogHandler",
    "exclusive_lock",
    "prune_backups",
]

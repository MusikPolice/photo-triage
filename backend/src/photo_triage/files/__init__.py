"""The only module allowed to mutate files: trash, restore, purge, EXIF writes, log
rotation, pruning database backups, replacing derived files and the worker's lock
file. It also owns the exiftool process, which reads and writes metadata (`ExifTool`).

Other packages use the public interface exported here, never the submodules.
See docs/dev-environment.md section 7.
"""

from photo_triage.files.backups import prune_backups
from photo_triage.files.derived import DerivedPathError, remove_derived, replace_derived
from photo_triage.files.exiftool import ExifTool, MetadataError, MetadataReader, MetadataWriter
from photo_triage.files.lock import LockHeldError, exclusive_lock
from photo_triage.files.logs import LogPathError, RotatingLogHandler
from photo_triage.files.trash import (
    TrashError,
    TrashPathError,
    move_to_trash,
    restore_from_trash,
    same_filesystem,
)

__all__ = [
    "DerivedPathError",
    "ExifTool",
    "LockHeldError",
    "LogPathError",
    "MetadataError",
    "MetadataReader",
    "MetadataWriter",
    "RotatingLogHandler",
    "TrashError",
    "TrashPathError",
    "exclusive_lock",
    "move_to_trash",
    "prune_backups",
    "remove_derived",
    "replace_derived",
    "restore_from_trash",
    "same_filesystem",
]

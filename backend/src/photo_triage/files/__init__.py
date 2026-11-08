"""The only module allowed to mutate files: trash, restore, purge, EXIF writes, log
rotation, pruning database backups, replacing derived files and the worker's lock
file. It also owns the exiftool process, which only reads so far (`ExifTool`).

Other packages use the public interface exported here, never the submodules.
See docs/dev-environment.md section 7.
"""

from photo_triage.files.backups import prune_backups
from photo_triage.files.derived import DerivedPathError, remove_derived, replace_derived
from photo_triage.files.exiftool import ExifTool, MetadataError, MetadataReader
from photo_triage.files.lock import LockHeldError, exclusive_lock
from photo_triage.files.logs import LogPathError, RotatingLogHandler

__all__ = [
    "DerivedPathError",
    "ExifTool",
    "LockHeldError",
    "LogPathError",
    "MetadataError",
    "MetadataReader",
    "RotatingLogHandler",
    "exclusive_lock",
    "prune_backups",
    "remove_derived",
    "replace_derived",
]

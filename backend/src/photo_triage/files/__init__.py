"""The only module allowed to mutate files: trash, restore, purge, EXIF writes, and
log rotation.

Other packages use the public interface exported here, never the submodules.
See docs/dev-environment.md section 7.
"""

from photo_triage.files.logs import LogPathError, RotatingLogHandler

__all__ = ["LogPathError", "RotatingLogHandler"]

"""Lock files: creating one is a file mutation, so it lives here (dev-environment §7)."""

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class LockHeldError(Exception):
    """Another process holds the lock."""


@contextmanager
def exclusive_lock(path: Path) -> Generator[None]:
    """Hold an exclusive `flock` on `path`, creating the file if needed, or raise
    `LockHeldError` at once if another process holds it.

    The kernel drops the lock when the process ends, however it ends (SIGKILL
    included), so a crash never leaves a stale lock. The file stays: only the lock
    on it matters.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeldError(f"another process holds {path}") from None
        yield
    finally:
        os.close(fd)  # which releases the lock

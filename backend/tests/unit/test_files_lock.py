"""The lock that keeps a second worker from starting (plan §6.11)."""

import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from photo_triage.files import LockHeldError, exclusive_lock


def _holder(path: Path) -> subprocess.Popen[str]:
    """Another process holding the lock on `path` until it's killed."""
    code = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from photo_triage.files import exclusive_lock

        with exclusive_lock(Path({str(path)!r})):
            print("held", flush=True)
            time.sleep(60)
        """
    )
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    assert process.stdout.readline() == "held\n"
    return process


def test_creates_the_file_and_can_be_taken_again_once_released(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"

    with exclusive_lock(path):
        assert path.is_file()
    with exclusive_lock(path):
        pass

    assert path.is_file()  # left in place; only the lock matters


def test_a_second_holder_is_refused_at_once(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"

    # The second `exclusive_lock` raises, inside `pytest.raises`, while the first holds.
    with (
        exclusive_lock(path),
        pytest.raises(LockHeldError, match=r"worker\.lock"),
        exclusive_lock(path),
    ):
        pass


def test_refused_while_another_process_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    holder = _holder(path)
    try:
        with pytest.raises(LockHeldError), exclusive_lock(path):
            pass
    finally:
        holder.kill()
        holder.wait()


def test_a_killed_holder_leaves_no_stale_lock(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    holder = _holder(path)

    holder.send_signal(signal.SIGKILL)
    holder.wait()

    with exclusive_lock(path):
        pass

"""Tests for scripts/check_file_mutation.py, the static file-mutation guard."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[3] / "scripts" / "check_file_mutation.py"
_spec = importlib.util.spec_from_file_location("check_file_mutation", _SCRIPT)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "source",
    [
        "p.unlink()",
        "p.unlink(missing_ok=True)",
        "p.rename(q)",
        "p.rmdir()",
        "p.replace(q)",
        "subprocess.run(['exiftool', '-ver'])",
        "subprocess.run(['/usr/local/bin/exiftool', '-ver'])",
    ],
)
def test_flags_mutation(source: str) -> None:
    assert guard.problems(source, "x.py")


@pytest.mark.parametrize(
    "source",
    [
        "s.replace('a', 'b')",
        "s.replace('a', 'b', 1)",
        "log.info('exiftool finished')",
        "p.read_bytes()",
    ],
)
def test_allows_non_mutation(source: str) -> None:
    assert not guard.problems(source, "x.py")


def test_scope() -> None:
    pkg = guard.SRC / "photo_triage"
    assert guard.in_scope(pkg / "pipeline" / "scan.py")
    assert not guard.in_scope(pkg / "files" / "trash.py")
    assert not guard.in_scope(guard.REPO_ROOT / "backend" / "tests" / "x.py")


def test_package_is_clean() -> None:
    assert guard.main([]) == 0

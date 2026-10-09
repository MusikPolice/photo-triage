"""The `.env` checks in `just doctor` (scripts/check_env.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[3] / "scripts" / "check_env.py"
_spec = importlib.util.spec_from_file_location("check_env", _SCRIPT)
assert _spec and _spec.loader
check_env = importlib.util.module_from_spec(_spec)
sys.modules["check_env"] = check_env  # dataclasses look up their module
_spec.loader.exec_module(check_env)


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    monkeypatch.setenv("ENV_FILE", str(path))
    return path


def _levels(results: list) -> list[tuple[str, str]]:
    return [(r.level, r.message.splitlines()[0]) for r in results]


def test_reports_a_missing_env_file_and_the_settings_it_lacks(env_file: Path) -> None:
    results = check_env.run_checks()

    assert _levels(results) == [
        ("fail", f"No {env_file}. Copy .env.example to .env and edit it (README)."),
        ("fail", "Invalid configuration:"),
    ]
    assert "PHOTO_DIR" in results[1].message


def test_passes_with_a_valid_env_and_warns_about_an_unmigrated_database(
    env_file: Path, tmp_path: Path
) -> None:
    (tmp_path / "photos").mkdir()
    env_file.write_text(f"PHOTO_DIR={tmp_path / 'photos'}\nDATA_DIR={tmp_path / 'data'}\n")

    results = check_env.run_checks(real_library=tmp_path / "pictures")

    assert [r.level for r in results] == ["ok", "ok", "warn"]
    assert "just db-migrate" in results[2].message


def test_fails_when_photo_dir_is_in_the_real_library_and_writable(
    env_file: Path, tmp_path: Path
) -> None:
    library = tmp_path / "pictures"
    (library / "2024").mkdir(parents=True)
    env_file.write_text(f"PHOTO_DIR={library / '2024'}\nDATA_DIR={tmp_path / 'data'}\n")

    results = check_env.run_checks(real_library=library)

    assert (
        "fail",
        f"PHOTO_DIR ({library / '2024'}) is in the real library, and it's WRITABLE. "
        f"Remount {library} read-only before running anything (dev-environment §1).",
    ) in _levels(results)


def test_passes_when_photo_dir_is_in_the_real_library_and_read_only(
    env_file: Path, tmp_path: Path
) -> None:
    library = tmp_path / "pictures"
    library.mkdir(mode=0o555)
    env_file.write_text(f"PHOTO_DIR={library}\nDATA_DIR={tmp_path / 'data'}\n")
    try:
        results = check_env.run_checks(real_library=library)
    finally:
        library.chmod(0o755)

    assert ("ok", f"PHOTO_DIR ({library}) is read-only") in _levels(results)
    assert all(r.level != "fail" for r in results)

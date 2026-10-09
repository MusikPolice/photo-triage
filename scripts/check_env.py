#!/usr/bin/env python3
"""App-level checks for `just doctor`, after `scripts/bootstrap.sh --check`
(dev-environment §2): `.env` and the settings it gives.

Run from the repo root in the backend's environment:
  uv run --no-sync --project backend python scripts/check_env.py

Prints each check as the bootstrap does and exits 1 if any failed. The tool and
version checks stay in the bootstrap script.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from photo_triage.db.migrate import SchemaError, check_schema
from photo_triage.settings import ENV_FILE, SettingsError, load_settings

REAL_LIBRARY = Path("/mnt/pictures")
"""The real photo library, mounted read-only in dev (dev-environment §1)."""


@dataclass(frozen=True)
class Result:
    level: Literal["ok", "warn", "fail"]
    message: str


def run_checks(real_library: Path = REAL_LIBRARY) -> list[Result]:
    results: list[Result] = []
    env_file = Path(os.environ.get(ENV_FILE, ".env"))
    if env_file.exists():
        results.append(Result("ok", f"{env_file} found"))
    else:
        results.append(
            Result("fail", f"No {env_file}. Copy .env.example to .env and edit it (README).")
        )

    try:
        settings = load_settings()
    except SettingsError as e:
        results.append(Result("fail", str(e)))
        return results
    results.append(Result("ok", f"settings are valid: {settings.summary()}"))

    photo_dir = settings.photo_dir.resolve()
    if photo_dir.is_relative_to(real_library):
        if os.access(photo_dir, os.W_OK):
            results.append(
                Result(
                    "fail",
                    f"PHOTO_DIR ({photo_dir}) is in the real library, and it's WRITABLE. "
                    f"Remount {real_library} read-only before running anything "
                    "(dev-environment §1).",
                )
            )
        else:
            results.append(Result("ok", f"PHOTO_DIR ({photo_dir}) is read-only"))

    try:
        check_schema(settings.data_dir)
    except SchemaError as e:
        results.append(Result("warn", str(e)))
    else:
        results.append(Result("ok", "the database is at the head revision"))
    return results


def main() -> int:
    color = sys.stdout.isatty()
    bold, reset = ("\033[1m", "\033[0m") if color else ("", "")
    marks = {"ok": ("✓", "\033[32m"), "warn": ("!", "\033[33m"), "fail": ("✗", "\033[31m")}

    print(f"\n{bold}==> .env and settings{reset}")
    results = run_checks()
    for result in results:
        mark, start = marks[result.level]
        first, *rest = result.message.splitlines()
        text = "\n".join([f"  {mark} {first}", *(f"    {line}" for line in rest)])
        print(f"{start if color else ''}{text}{reset}")
    failed = any(r.level == "fail" for r in results)
    print(f"\n  {'.env check failed.' if failed else '.env OK.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

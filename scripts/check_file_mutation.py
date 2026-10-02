#!/usr/bin/env python3
"""Reject file-mutating calls outside photo_triage.files (docs/dev-environment.md §7).

Ruff's banned-api rule (TID251) covers module functions such as os.remove and
shutil.rmtree, because it can resolve their import paths. It can't see method
calls on Path objects or the arguments passed to subprocess, so this script
covers those by syntax:

- ``x.unlink()``, ``x.rename(...)``, ``x.rmdir()``
- ``x.replace(target)`` with exactly one argument (Path.replace). str.replace
  takes at least two, so it isn't flagged.
- a string literal naming the exiftool executable (``"exiftool"`` or a path
  ending in ``/exiftool``), i.e. building an exiftool command line

Usage: check_file_mutation.py [FILE ...]
With no files, checks every .py file under backend/src. Files inside
backend/src/photo_triage/files/ and anything outside backend/src are skipped.
Exits 1 if anything is flagged.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "backend" / "src"
ALLOWED = SRC / "photo_triage" / "files"

MUTATING_METHODS = {"unlink", "rename", "rmdir"}


def in_scope(path: Path) -> bool:
    path = path.resolve()
    return path.is_relative_to(SRC) and not path.is_relative_to(ALLOWED)


def problems(source: str, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in MUTATING_METHODS or (
                name == "replace" and len(node.args) == 1 and not node.keywords
            ):
                found.append(f"{filename}:{node.lineno}: .{name}() mutates files")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "exiftool" or node.value.endswith("/exiftool"):
                found.append(f"{filename}:{node.lineno}: invokes exiftool")
    return found


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] if argv else sorted(SRC.rglob("*.py"))
    found: list[str] = []
    for path in paths:
        if path.suffix == ".py" and in_scope(path):
            found += problems(path.read_text(encoding="utf-8"), str(path))
    for line in found:
        print(line)
    if found:
        print(
            "\nOnly photo_triage.files may move, delete, or rewrite files. "
            "Call its public interface instead."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

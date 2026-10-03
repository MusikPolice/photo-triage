"""Migrations import only Alembic, SQLAlchemy and the standard library.

A migration describes the schema at one point in history. If it imported app
code, a later change to that code would silently change what the migration
does on a fresh database.
"""

import ast
import sys
from pathlib import Path

import pytest

import photo_triage.db

VERSIONS = Path(photo_triage.db.__file__).parent / "migrations" / "versions"
ALLOWED = {"alembic", "sqlalchemy", "__future__", *sys.stdlib_module_names}


def _imported_packages(source: str) -> set[str]:
    packages: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            packages.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            packages.add("." if node.level else (node.module or "").split(".")[0])
    return packages


@pytest.mark.parametrize("migration", sorted(VERSIONS.glob("*.py")), ids=lambda p: p.name)
def test_migration_imports_nothing_from_the_app(migration: Path) -> None:
    assert _imported_packages(migration.read_text()) <= ALLOWED


def test_there_are_migrations_to_check() -> None:
    assert list(VERSIONS.glob("*.py"))


def test_app_and_relative_imports_are_caught() -> None:
    source = "import sqlalchemy as sa\nfrom photo_triage.db import models\nfrom . import x\n"
    assert _imported_packages(source) - ALLOWED == {"photo_triage", "."}

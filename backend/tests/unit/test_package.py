"""Smoke test: every top-level subpackage imports."""

import importlib

import pytest


@pytest.mark.parametrize("name", ["api", "db", "files", "ml", "pipeline", "worker"])
def test_subpackage_imports(name: str) -> None:
    importlib.import_module(f"photo_triage.{name}")

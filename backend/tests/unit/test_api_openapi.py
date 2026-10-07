"""The schema export behind the frontend's API types (`scripts/api_types.sh`)."""

import json

import pytest

from photo_triage.api import openapi


def test_schema_needs_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    schema = openapi.schema()
    assert "/api/activity" in schema["paths"]
    assert "Activity" in schema["components"]["schemas"]


def test_main_prints_stable_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert openapi.main() == 0
    first = capsys.readouterr().out
    openapi.main()
    assert capsys.readouterr().out == first
    assert json.loads(first)["info"]["title"] == "photo-triage"

"""`python -m photo_triage.api`, which `just api` runs."""

from typing import Any

import pytest
import uvicorn

from photo_triage.api.__main__ import main


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    return calls


def test_runs_uvicorn_on_app_port(
    monkeypatch: pytest.MonkeyPatch, uvicorn_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")
    monkeypatch.setenv("APP_PORT", "8123")

    assert main(["--reload"]) == 0

    [call] = uvicorn_calls
    assert call["app"] == "photo_triage.api.app:create_app"
    assert call["factory"] is True
    assert call["port"] == 8123
    assert call["reload"] is True


def test_bad_configuration_exits_before_starting(
    uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 2
    assert uvicorn_calls == []
    assert "PHOTO_DIR: Field required" in capsys.readouterr().err

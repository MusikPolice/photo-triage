"""`python -m photo_triage.api`, which `just api` runs."""

import logging
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from photo_triage.api.__main__ import SHUTDOWN_TIMEOUT_S, main, serve


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    return calls


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_DIR", "/photos")
    monkeypatch.setenv("TRASH_DIR", "/trash")


@pytest.mark.usefixtures("required_env")
def test_runs_uvicorn_on_app_port(
    monkeypatch: pytest.MonkeyPatch, uvicorn_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("APP_PORT", "8123")

    assert main(["--reload"]) == 0

    [call] = uvicorn_calls
    assert call["app"] == "photo_triage.api.__main__:serve"
    assert call["factory"] is True
    assert call["port"] == 8123
    assert call["reload"] is True
    assert call["log_config"] is None  # uvicorn keeps our logging configuration
    assert call["timeout_graceful_shutdown"] == SHUTDOWN_TIMEOUT_S


@pytest.mark.usefixtures("required_env")
@pytest.mark.parametrize(("log_level", "access_log"), [("INFO", False), ("DEBUG", True)])
def test_access_log_is_on_only_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    uvicorn_calls: list[dict[str, Any]],
    log_level: str,
    access_log: bool,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", log_level)

    main([])

    assert uvicorn_calls[0]["access_log"] is access_log


@pytest.mark.usefixtures("required_env")
@pytest.mark.parametrize(("log_level", "access_logged"), [("INFO", False), ("DEBUG", True)])
def test_access_lines_reach_the_log_only_at_debug(
    monkeypatch: pytest.MonkeyPatch, log_level: str, access_logged: bool
) -> None:
    # Load the app the way uvicorn does: it applies `access_log` when it builds
    # its config, and only then calls the factory, which configures logging again.
    def load_like_uvicorn(app: str, **kwargs: Any) -> None:
        uvicorn.Config(app, **kwargs).load()

    monkeypatch.setattr(uvicorn, "run", load_like_uvicorn)
    monkeypatch.setenv("LOG_LEVEL", log_level)
    main([])

    logging.getLogger("uvicorn.access").info('"GET /api/health HTTP/1.1" 200')

    for handler in logging.getLogger().handlers:
        handler.flush()
    text = Path("data/logs/api.log").read_text()
    assert ("INFO uvicorn.access" in text) is access_logged


@pytest.mark.usefixtures("required_env", "uvicorn_calls")
def test_configures_logging_before_starting(capsys: pytest.CaptureFixture[str]) -> None:
    main([])

    logging.getLogger("uvicorn.error").warning("from uvicorn")

    assert Path("data/logs/api.log").exists()
    assert capsys.readouterr().err.endswith("WARNING uvicorn.error from uvicorn\n")


@pytest.mark.usefixtures("required_env", "uvicorn_calls")
def test_reload_watcher_logs_only_to_stderr() -> None:
    # The child process that serves writes api.log (see serve).
    main(["--reload"])

    assert not Path("data/logs").exists()


@pytest.mark.usefixtures("required_env")
def test_serve_logs_to_the_file_and_says_what_it_runs_with() -> None:
    app = serve()

    assert app.title == "photo-triage"
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = Path("data/logs/api.log").read_text()
    assert "INFO photo_triage.api API starting: PHOTO_DIR=/photos " in text


def test_bad_configuration_exits_before_starting(
    uvicorn_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 2
    assert uvicorn_calls == []
    assert "PHOTO_DIR: Field required" in capsys.readouterr().err

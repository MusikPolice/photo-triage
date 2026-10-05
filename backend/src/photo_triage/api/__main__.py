"""Run the API with uvicorn: `python -m photo_triage.api [--reload]` (`just api`)."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from photo_triage import logs
from photo_triage.api.app import create_app
from photo_triage.settings import SettingsError, load_settings

# Named, since `__name__` is "__main__" when run with `python -m`.
logger = logging.getLogger("photo_triage.api")

SHUTDOWN_TIMEOUT_S = 5
"""How long a stop or reload waits for open requests."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m photo_triage.api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, help="default: APP_PORT")
    parser.add_argument("--reload", action="store_true", help="restart when the code changes")
    args = parser.parse_args(argv)

    # Load once here so a bad configuration fails before uvicorn starts.
    try:
        settings = load_settings()
    except SettingsError as e:
        print(e, file=sys.stderr)
        return 2

    # With --reload, this process only watches the code and a child process serves
    # (see `serve`). Only the child writes api.log, since two processes can't share
    # a rotating file.
    logs.configure(settings, "api", to_file=not args.reload)

    uvicorn.run(
        "photo_triage.api.__main__:serve",
        factory=True,
        host=args.host,
        port=args.port or settings.app_port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).parents[1])] if args.reload else None,
        log_config=None,  # keep the configuration from `logs.configure`
        # The map loads thousands of tiles and the event stream stays open, so one
        # line per request is only wanted when debugging.
        access_log=settings.log_level == "DEBUG",
        # The app ends its event streams when told to stop. This bounds the wait
        # for anything else still open.
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT_S,
    )
    return 0


def serve() -> FastAPI:
    """The app as uvicorn loads it, in the process that serves requests."""
    settings = load_settings()
    logs.configure(settings, "api")
    logger.info("API starting: %s", settings.summary())
    return create_app(settings)


if __name__ == "__main__":
    sys.exit(main())

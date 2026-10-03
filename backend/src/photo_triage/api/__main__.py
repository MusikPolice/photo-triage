"""Run the API with uvicorn: `python -m photo_triage.api [--reload]` (`just api`)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from photo_triage.settings import SettingsError, load_settings


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

    uvicorn.run(
        "photo_triage.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port or settings.app_port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).parents[1])] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

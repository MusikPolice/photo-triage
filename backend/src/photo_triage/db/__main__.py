"""`python -m photo_triage.db migrate` (`just db-migrate`): upgrade the database in
`DATA_DIR` to head, backing it up first. See `photo_triage.db.migrate`."""

import argparse
import logging
import sys
from collections.abc import Sequence

from photo_triage import logs
from photo_triage.db.migrate import SchemaAheadError, migrate
from photo_triage.settings import SettingsError, load_settings

# Named, since `__name__` is "__main__" when run with `python -m`.
logger = logging.getLogger("photo_triage.db")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m photo_triage.db")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    commands.add_parser(
        "migrate", help="upgrade the database to head, backing it up first if it exists"
    )
    parser.parse_args(argv)

    try:
        settings = load_settings()
    except SettingsError as e:
        print(e, file=sys.stderr)
        return 2

    logs.configure(settings, "migrate")
    try:
        migrate(settings)
    except SchemaAheadError as e:
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

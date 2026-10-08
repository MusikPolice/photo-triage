"""Print the API's OpenAPI schema as JSON: `python -m photo_triage.api.openapi`.

`scripts/api_types.sh` turns it into the frontend's TypeScript types (the CI
`contract` job, dev-environment §8). The schema doesn't depend on the
configuration, so this builds the app with throwaway directories and needs no
`.env`.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from photo_triage.api.app import create_app
from photo_triage.settings import Settings


def schema() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]  # a pydantic-settings init option
            photo_dir=root / "photos",
            trash_dir=root / "trash",
            data_dir=root / "data",
        )
        app = create_app(settings)
        try:
            return app.openapi()
        finally:
            app.state.engine.dispose()


def main() -> int:
    json.dump(schema(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

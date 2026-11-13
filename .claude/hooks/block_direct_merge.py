#!/usr/bin/env python3
"""PreToolUse hook: Claude Code merges PRs only through `scripts/tracker.py merge`.

`tracker.py merge` refuses PRs that touch a sensitive path (dev-environment §9).
Claude uses the user's GitHub account, so GitHub can't tell its merges from the
user's. This hook closes the other routes from the Bash tool: `gh pr merge`
(including --auto) and merge calls through `gh api`. The tracker runs gh as a
subprocess, which the hook doesn't see, so its own merge goes through.

Reads the hook input as JSON on stdin. Exit 2 blocks the call and shows stderr
to Claude; exit 0 allows it.
"""

from __future__ import annotations

import json
import re
import sys

BLOCKED = (
    re.compile(r"\bgh\b.*\bpr\s+merge\b"),
    re.compile(r"\bgh\b.*\bapi\b.*(/merge\b|mergePullRequest|enablePullRequestAutoMerge)"),
)
MESSAGE = (
    "Blocked: merge PRs with `scripts/tracker.py merge N`, which checks CI and the "
    "sensitive paths first (dev-environment §9). If it refuses, the PR is the user's to merge."
)


def blocked(command: str) -> bool:
    return any(pattern.search(command) for pattern in BLOCKED)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    if blocked(command):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

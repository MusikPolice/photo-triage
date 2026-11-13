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

# `gh` where it runs as a command: at the start of a line, or after a shell
# separator, followed by its global flags (`-R owner/repo`) and the subcommand.
# Text that only mentions it, in a quoted argument or a heredoc writing a PR
# body, isn't blocked. This guards against a habit, not an adversary.
_GH = r"(?:^|[;&|(`]|\$\()\s*(?:\S+/)?gh(?:\s+-{1,2}[\w-]+(?:[=\s]+[^-\s]\S*)?)*?\s+"
BLOCKED = (
    re.compile(_GH + r"pr\s+merge\b", re.MULTILINE),
    re.compile(
        _GH + r"api\b[^\n;&|]*?(/merge\b|mergePullRequest|enablePullRequestAutoMerge)",
        re.MULTILINE,
    ),
)
MESSAGE = (
    "Blocked: merge PRs with `scripts/tracker.py merge N`, which checks CI and the "
    "sensitive paths first (dev-environment §9). If it refuses, the PR is the user's to merge."
)


# A heredoc's body is data for the command before it, never run by the shell.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.MULTILINE | re.DOTALL)


def blocked(command: str) -> bool:
    command = _HEREDOC.sub("<<heredoc", command)
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

"""The PreToolUse hook that leaves `scripts/tracker.py merge` as the only way to merge."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).parents[3] / ".claude" / "hooks" / "block_direct_merge.py"


def _exit_code(stdin: str) -> int:
    result = subprocess.run(
        [sys.executable, str(_HOOK)], input=stdin, capture_output=True, text=True, check=False
    )
    return result.returncode


def _bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 90 --squash",
        "gh pr merge --auto --squash 90",
        "cd /repo && gh pr  merge 90",
        "gh -R MusikPolice/photo-triage pr merge 90",
        "gh api -X PUT repos/{owner}/{repo}/pulls/90/merge",
        "gh api graphql -f query='mutation { mergePullRequest(input: {}) { x } }'",
        "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {}) { x } }'",
        "gh pr checks 90 --watch && gh pr merge 90",
        "echo hi; /usr/bin/gh pr merge 90",
        "git fetch\ngh pr merge 90",
        "x=$(gh pr merge 90)",
        "cat > a.md <<'EOF'\nnotes\nEOF\ngh pr merge 90",
    ],
)
def test_direct_merges_are_blocked(command: str) -> None:
    assert _exit_code(_bash(command)) == 2


@pytest.mark.parametrize(
    "command",
    [
        "scripts/tracker.py merge 90",
        "gh pr view 90",
        "gh pr checks 90 --watch",
        "gh api repos/{owner}/{repo}/pulls/90/files",
        "git merge origin/main",
        "git commit -m 'Block gh pr merge in the hook'",
        "cat > pr.md <<'EOF'\n- The hook blocks `gh pr merge` and `gh api .../merge`.\nEOF",
        "gh pr create --title 'Self-merge' --body 'blocks gh pr merge'",
        "grep -rn 'gh pr merge' docs",
    ],
)
def test_other_commands_are_allowed(command: str) -> None:
    assert _exit_code(_bash(command)) == 0


def test_unreadable_input_is_allowed() -> None:
    assert _exit_code("not json") == 0

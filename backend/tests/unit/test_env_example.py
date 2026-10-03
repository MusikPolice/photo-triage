"""`.env.example` documents every variable in plan §9."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]


def plan_variables() -> set[str]:
    plan = (ROOT / "docs/plan.md").read_text()
    section = plan.split("## 9. Configuration", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([A-Z_]+)` \|", section, re.MULTILINE))


def example_variables() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    return set(re.findall(r"^#? ?([A-Z_]+)=", text, re.MULTILINE))


def test_env_example_matches_plan() -> None:
    assert "PHOTO_DIR" in plan_variables()
    assert example_variables() == plan_variables()

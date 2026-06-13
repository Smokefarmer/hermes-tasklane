"""Self-teaching docs: MCP instructions, the client skill, and the recipes.

These guard the operator-facing documentation surface — the FastMCP
`instructions` doctrine, the Claude Code skill, and the four recipe files — so a
connecting Claude always has a complete, non-trivial playbook.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "skills" / "tasklane" / "SKILL.md"
RECIPES_DIR = REPO_ROOT / "docs" / "recipes"
RECIPE_FILES = (
    "overnight-batch.md",
    "fire-and-forget-bugfix.md",
    "pr-review-bot.md",
    "feature-pipeline.md",
)


def test_server_exposes_instructions() -> None:
    from tasklane.mcp_server import USAGE, mcp

    # FastMCP advertises the doctrine to every connecting client.
    assert mcp.instructions == USAGE
    text = mcp.instructions
    assert text and len(text.splitlines()) <= 60  # stays compact
    for phrase in (
        "TaskLane",
        "create_task",
        "create_pipeline",
        "retry_task",
        "blocked",
        "acceptance",
        "budget",
    ):
        assert phrase in text, f"missing usage phrase: {phrase!r}"


def _parse_frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    end = text.index("\n---", 4)
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_skill_exists_with_valid_frontmatter() -> None:
    assert SKILL_PATH.is_file(), "skills/tasklane/SKILL.md is missing"
    text = SKILL_PATH.read_text(encoding="utf-8")
    fields = _parse_frontmatter(text)
    assert fields.get("name") == "tasklane"
    description = fields.get("description", "")
    assert len(description) > 20
    # description should trigger on the intended verbs/nouns
    assert any(word in description.lower() for word in ("delegate", "queue", "overnight"))
    assert "tasklane" in description.lower()


@pytest.mark.parametrize("filename", RECIPE_FILES)
def test_recipe_files_are_non_trivial(filename: str) -> None:
    path = RECIPES_DIR / filename
    assert path.is_file(), f"missing recipe: {filename}"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 40, f"recipe too thin: {filename} ({len(lines)} lines)"
    body = "\n".join(lines)
    assert "When to use" in body
    assert "create_task" in body or "create_pipeline" in body

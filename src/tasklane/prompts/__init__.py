"""Role-based stage prompt templates and their loader.

Each coding-job stage (plan / implement / review / fix) has a full, prompt-engineered
mission shipped as a Markdown template in this package. ``load_template`` resolves a
role to its template, letting an operator override any template by dropping a file at
``$TASKLANE_HOME/prompts/<role>.md`` (the override wins over the packaged default).
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any, Optional

from tasklane.paths import tasklane_home

#: Roles that have a stage template (kept in sync with specs.ROLES).
ROLES: tuple[str, ...] = ("plan", "implement", "review", "fix", "analyze", "audit")


def normalize_role(value: Any) -> str:
    """Return *value* lowercased and trimmed, or ``""`` for ``None``/blank."""
    if value is None:
        return ""
    return str(value).strip().lower()


def load_template(role: Any) -> Optional[str]:
    """Return the prompt template for *role*, or ``None`` if there is none.

    Resolution order:
      1. operator override at ``$TASKLANE_HOME/prompts/<role>.md`` (wins if present)
      2. the default packaged template

    An unknown or empty role returns ``None`` so callers fall back to the generic prompt.
    """
    normalized = normalize_role(role)
    if normalized not in ROLES:
        return None

    override = tasklane_home() / "prompts" / f"{normalized}.md"
    if override.is_file():
        return override.read_text(encoding="utf-8")

    resource = files(__package__).joinpath(f"{normalized}.md")
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    return None

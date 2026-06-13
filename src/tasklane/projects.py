"""Project registry — per-project test/deploy profiles.

A project profile augments a job's ``project`` name with the knobs a TESTER role
needs to actually run the system: where the secret env file lives, how to boot
the app locally, and how to reach the deployed staging frontend.

Profiles are declared in ``config.yaml`` under a ``projects:`` mapping, e.g.::

    projects:
      acme:
        env_file: /home/me/.secrets/acme.staging.env   # mode 600, KEY=VALUE
        local_test_command: "npm run dev"
        staging_url: "https://staging.acme.example.com"
        staging_url_command: "railway domain --json"     # prints/derives the URL
        test_notes: "Use the seeded test tenant; never touch tenant prod-*."

SECURITY: the profile only ever stores the *path* to the secret file. The secret
VALUES are loaded by the worker (:func:`tasklane.secrets.load_env_file`) and
injected straight into the agent subprocess env — only the KEY NAMES are surfaced
to the prompt.

.. note::
   This module is intentionally self-contained. The original task expected a
   richer ``projects.py`` from a parallel merge; when that merge is absent this
   minimal registry keeps the tester roles fully functional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from tasklane.config import Config, load_config


@dataclass(frozen=True)
class ProjectProfile:
    """Immutable test/deploy profile for one project."""

    key: str
    env_file: Optional[str] = None
    local_test_command: Optional[str] = None
    staging_url: Optional[str] = None
    staging_url_command: Optional[str] = None
    test_notes: Optional[str] = None

    def test_context(self) -> dict[str, str]:
        """Non-secret context surfaced to a tester prompt (never includes values)."""
        fields = {
            "local_test_command": self.local_test_command,
            "staging_url": self.staging_url,
            "staging_url_command": self.staging_url_command,
            "test_notes": self.test_notes,
        }
        return {key: value for key, value in fields.items() if value}


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def profile_from_mapping(key: str, data: Mapping[str, Any]) -> ProjectProfile:
    """Build a :class:`ProjectProfile` from a raw config mapping."""
    return ProjectProfile(
        key=key,
        env_file=_optional_str(data.get("env_file")),
        local_test_command=_optional_str(data.get("local_test_command")),
        staging_url=_optional_str(data.get("staging_url")),
        staging_url_command=_optional_str(data.get("staging_url_command")),
        test_notes=_optional_str(data.get("test_notes")),
    )


def load_project_profile(project_key: Any, cfg: Optional[Config] = None) -> Optional[ProjectProfile]:
    """Return the profile for *project_key*, or ``None`` when it is unknown/blank."""
    key = _optional_str(project_key)
    if not key:
        return None
    config = cfg if cfg is not None else load_config()
    registry = config.projects or {}
    data = registry.get(key)
    if not isinstance(data, Mapping):
        return None
    return profile_from_mapping(key, data)

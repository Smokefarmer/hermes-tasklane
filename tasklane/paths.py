"""Filesystem roots for the standalone TaskLane.

Replaces Hermes's ``hermes_constants.get_hermes_home``. The data root is
``$TASKLANE_HOME`` (default ``~/.tasklane``) and holds the job store, worktrees,
logs, audit log, and config.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_HOME = "~/.tasklane"


def tasklane_home() -> Path:
    """Return the TaskLane data root, creating it if necessary."""
    raw = os.getenv("TASKLANE_HOME", _DEFAULT_HOME)
    home = Path(raw).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    return home


def jobs_root() -> Path:
    return tasklane_home() / "jobs"


def worktrees_root() -> Path:
    return tasklane_home() / "job-worktrees"


def logs_root() -> Path:
    root = jobs_root() / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def audit_log_path() -> Path:
    return tasklane_home() / "audit.log"


def config_path() -> Path:
    return tasklane_home() / "config.yaml"

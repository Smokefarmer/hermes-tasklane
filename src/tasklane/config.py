"""TaskLane configuration (``~/.tasklane/config.yaml``).

On first run a default config is written with a freshly generated app bearer
token (mode 600). The token gates the MCP control plane; the repos allowlist
bounds which repositories jobs may target.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import yaml

from tasklane.paths import config_path


@dataclass(frozen=True)
class Config:
    # Worker
    poll_interval_seconds: float = 5.0
    max_in_progress: int = 1
    default_model: str | None = None
    # Autonomous jobs must run git commit/push without prompts; acceptEdits only
    # auto-approves file edits, so headless jobs stall on bash. bypassPermissions
    # is safe here because each job is confined to an isolated git worktree.
    permission_mode: str = "bypassPermissions"
    timeout_seconds: int = 3600
    repos_allowlist: List[str] = field(default_factory=list)  # absolute path prefixes; [] = allow any

    # MCP server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8788
    app_token: str = ""
    public_hostnames: List[str] = field(default_factory=list)  # Host values to allow (e.g. tasklane.example.com)
    allowed_origins: List[str] = field(default_factory=list)  # extra Origin values to accept
    enable_admin_exec: bool = False  # server-level shell (off by default)
    exec_timeout_seconds: int = 600


_DEFAULTS_COMMENT = """\
# TaskLane configuration. Secrets live here — keep this file at mode 600.
# repos_allowlist: list of absolute path prefixes a job's repo.path must start
#   with. Empty list means any path is allowed (NOT recommended for remote use).
# enable_admin_exec: set true only if you want the remote agent to run
#   server-level shell commands outside job worktrees.
"""


def _coerce(data: dict[str, Any]) -> Config:
    defaults = Config()
    return Config(
        poll_interval_seconds=float(data.get("poll_interval_seconds", defaults.poll_interval_seconds)),
        max_in_progress=int(data.get("max_in_progress", defaults.max_in_progress)),
        default_model=(str(data["default_model"]).strip() or None) if data.get("default_model") else None,
        permission_mode=str(data.get("permission_mode", defaults.permission_mode)).strip() or defaults.permission_mode,
        timeout_seconds=int(data.get("timeout_seconds", defaults.timeout_seconds)),
        repos_allowlist=[str(p).strip() for p in (data.get("repos_allowlist") or []) if str(p).strip()],
        mcp_host=str(data.get("mcp_host", defaults.mcp_host)).strip() or defaults.mcp_host,
        mcp_port=int(data.get("mcp_port", defaults.mcp_port)),
        app_token=str(data.get("app_token", "")).strip(),
        public_hostnames=[str(h).strip() for h in (data.get("public_hostnames") or []) if str(h).strip()],
        allowed_origins=[str(o).strip() for o in (data.get("allowed_origins") or []) if str(o).strip()],
        enable_admin_exec=bool(data.get("enable_admin_exec", defaults.enable_admin_exec)),
        exec_timeout_seconds=int(data.get("exec_timeout_seconds", defaults.exec_timeout_seconds)),
    )


def _write_default() -> Config:
    path = config_path()
    token = secrets.token_urlsafe(32)
    body = {
        "poll_interval_seconds": 5.0,
        "max_in_progress": 1,
        "default_model": None,
        "permission_mode": "bypassPermissions",
        "timeout_seconds": 3600,
        "repos_allowlist": [],
        "mcp_host": "127.0.0.1",
        "mcp_port": 8788,
        "app_token": token,
        "public_hostnames": [],
        "allowed_origins": [],
        "enable_admin_exec": False,
        "exec_timeout_seconds": 600,
    }
    path.write_text(_DEFAULTS_COMMENT + yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return _coerce(body)


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return _write_default()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    cfg = _coerce(raw)
    # Env overrides (handy for systemd / testing)
    if os.getenv("TASKLANE_APP_TOKEN"):
        cfg = Config(**{**cfg.__dict__, "app_token": os.environ["TASKLANE_APP_TOKEN"].strip()})
    return cfg


def repo_path_allowed(repo_path: str, cfg: Config) -> bool:
    """True if repo_path is under an allowlisted prefix (or the allowlist is empty)."""
    if not cfg.repos_allowlist:
        return True
    try:
        resolved = Path(repo_path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    for prefix in cfg.repos_allowlist:
        try:
            base = Path(prefix).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False

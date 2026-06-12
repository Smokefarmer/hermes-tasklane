"""Shared fixtures for the E2E suite.

Everything runs against real machinery — real git repos, real worktrees, real
subprocesses, the real worker and MCP server — with two substitutions:

- ``TASKLANE_HOME`` points at a per-test tmp dir (job store, worktrees, logs,
  config all isolated; the live install is never touched).
- A fake ``claude`` binary on PATH stands in for the LLM (see fake_claude.py).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tasklane.config import Config

E2E_DIR = Path(__file__).parent


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"{' '.join(cmd)} failed: {proc.stderr}"
    return proc


@pytest.fixture()
def tasklane_home(tmp_path, monkeypatch) -> Path:
    """Isolated $TASKLANE_HOME with a fast-cadence config.yaml."""
    home = tmp_path / "tasklane-home"
    home.mkdir()
    monkeypatch.setenv("TASKLANE_HOME", str(home))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "poll_interval_seconds": 0.2,
        "max_in_progress": 2,
        "permission_mode": "bypassPermissions",
        "timeout_seconds": 3600,
        "max_attempts": 2,
        "retry_backoff_seconds": 0.0,  # instant retries in tests
        "app_token": "e2e-test-token",
        "mcp_host": "127.0.0.1",
    }), encoding="utf-8")
    return home


@pytest.fixture()
def fast_cfg() -> Config:
    return Config(max_attempts=2, retry_backoff_seconds=0.0, timeout_seconds=3600)


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch) -> Path:
    """Install the fake claude CLI at the front of PATH; returns its state dir."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    target = bin_dir / "claude"
    shutil.copy(E2E_DIR / "fake_claude.py", target)
    # ensure the shebang resolves to the venv/test interpreter
    target.write_text(f"#!{sys.executable}\n" + (E2E_DIR / "fake_claude.py").read_text().split("\n", 1)[1])
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    state = tmp_path / "fake-claude-state"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    return state


def fake_calls(state: Path) -> int:
    calls = state / "calls"
    return int(calls.read_text()) if calls.exists() else 0


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    """A real git clone (with one commit on main) whose origin is a local bare repo."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run(["git", "init", "--bare", "--initial-branch=main", "."], cwd=origin)
    clone = tmp_path / "repo"
    run(["git", "clone", str(origin), str(clone)], cwd=tmp_path)
    run(["git", "config", "user.email", "e2e@test.local"], cwd=clone)
    run(["git", "config", "user.name", "E2E"], cwd=clone)
    (clone / "README.md").write_text("# e2e fixture repo\n")
    run(["git", "add", "."], cwd=clone)
    run(["git", "commit", "-m", "initial"], cwd=clone)
    run(["git", "push", "-u", "origin", "main"], cwd=clone)
    return clone


def job_spec(repo: Path, job_id: str, *, delivery_mode: str = "report-only",
             branch_mode: str = "detached-review", work_branch: str | None = None) -> dict:
    spec: dict = {
        "id": job_id,
        "repo": {"path": str(repo)},
        "request": {"type": "task", "title": f"e2e {job_id}", "body": "do the thing"},
        "branch": {"mode": branch_mode, "base_branch": "main"},
        "delivery_mode": delivery_mode,
    }
    if work_branch:
        spec["branch"]["work_branch"] = work_branch
    return spec

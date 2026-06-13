"""Unit tests for tasklane.runner — secret env injection into the claude
subprocess and the ANTHROPIC_* stripping that protects subscription billing."""

import subprocess

import pytest

from tasklane import runner


@pytest.fixture()
def fake_claude(monkeypatch, tmp_path):
    """Make the runner think `claude` exists and capture the subprocess env."""
    monkeypatch.setattr(runner, "claude_cli_available", lambda: "/usr/bin/claude")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"result": "ok", "is_error": false}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_extra_env_reaches_subprocess(fake_claude, tmp_path):
    runner.run_claude_cli_job(
        "prompt", cwd=str(tmp_path),
        extra_env={"TEST_USER": "alice", "TEST_PASSWORD": "s3cr3t"},
    )
    env = fake_claude["env"]
    assert env["TEST_USER"] == "alice"
    assert env["TEST_PASSWORD"] == "s3cr3t"


def test_anthropic_vars_stripped_even_with_extra_env(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-be-removed")
    runner.run_claude_cli_job(
        "prompt", cwd=str(tmp_path), extra_env={"TEST_USER": "alice"},
    )
    env = fake_claude["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["TEST_USER"] == "alice"


def test_extra_env_cannot_resurrect_anthropic_key(fake_claude, tmp_path, monkeypatch):
    # The ANTHROPIC_* strip is unconditional: even if a secret env file tries to set
    # ANTHROPIC_API_KEY, it must NOT reach the subprocess (that would bill API credits
    # instead of the subscription OAuth).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    runner.run_claude_cli_job(
        "prompt", cwd=str(tmp_path),
        extra_env={"ANTHROPIC_API_KEY": "sk-from-file", "ANTHROPIC_AUTH_TOKEN": "t", "TEST_USER": "alice"},
    )
    assert "ANTHROPIC_API_KEY" not in fake_claude["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in fake_claude["env"]
    assert fake_claude["env"]["TEST_USER"] == "alice"  # non-anthropic vars still injected


def test_no_extra_env_is_harmless(fake_claude, tmp_path):
    result = runner.run_claude_cli_job("prompt", cwd=str(tmp_path))
    assert result["error"] is None
    assert "TEST_USER" not in fake_claude["env"]

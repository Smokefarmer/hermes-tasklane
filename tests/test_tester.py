"""Tester roles on the unified (path-keyed) project model: secret env injection
(KEY NAMES only in the prompt, values only in the subprocess env), and the
test_deployment MCP tool. Happy + bad paths."""

import os
import subprocess

import pytest

from tasklane import mcp_server, projects
from tasklane.worker import _load_tester_secrets
from tasklane.worktree import job_prompt


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


def _git_repo(tmp_path, name="repo") -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return str(repo)


def _secret_file(tmp_path, **pairs) -> str:
    path = tmp_path / "secrets.env"
    path.write_text("".join(f'{k}="{v}"\n' for k, v in pairs.items()), encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _record(repo: str, role: str = "test-staging", profile: dict | None = None) -> dict:
    spec = {
        "id": f"{role}-job",
        "spec": {
            "id": f"{role}-job",
            "repo": {"path": repo},
            "request": {"type": "task-small", "title": "verify", "body": "check login flow"},
            "branch": {"mode": "detached-review"},
            "delivery_mode": "report-only",
            "role": role,
            "metadata": {"project_profile": profile or {}},
        },
    }
    return spec


# --------------------------------------------------------------------------- #
# registry tester fields
# --------------------------------------------------------------------------- #
def test_register_project_persists_tester_fields(home, tmp_path):
    repo = _git_repo(tmp_path)
    projects.register_project(repo, name="acme", env_file="/tmp/x.env",
                              local_test_command="npm run dev",
                              staging_url="https://staging.acme.test",
                              test_notes="seeded tenant only")
    prof = projects.get_project(repo)
    assert prof["env_file"] == "/tmp/x.env"
    assert prof["local_test_command"] == "npm run dev"
    assert prof["staging_url"] == "https://staging.acme.test"
    assert prof["test_notes"] == "seeded tenant only"


def test_register_project_preserves_tester_fields_on_plain_reregister(home, tmp_path):
    repo = _git_repo(tmp_path)
    projects.register_project(repo, env_file="/tmp/x.env", staging_url="https://s.test")
    # a later plain re-register (e.g. updating docs) must not wipe tester fields
    projects.register_project(repo, docs=["CLAUDE.md"])
    prof = projects.get_project(repo)
    assert prof["env_file"] == "/tmp/x.env"
    assert prof["staging_url"] == "https://s.test"
    assert prof["docs"] == ["CLAUDE.md"]


# --------------------------------------------------------------------------- #
# worker secret injection — KEY NAMES to prompt, VALUES to subprocess only
# --------------------------------------------------------------------------- #
def test_load_tester_secrets_exposes_names_not_values(home, tmp_path):
    env_file = _secret_file(tmp_path, TEST_USER="alice", TEST_PASSWORD="s3cr3t-do-not-leak")
    record = _record(_git_repo(tmp_path), profile={"env_file": env_file})
    prepared, secret_env = _load_tester_secrets(record, "j")
    # values are returned for subprocess injection
    assert secret_env == {"TEST_USER": "alice", "TEST_PASSWORD": "s3cr3t-do-not-leak"}
    # but only the NAMES land on the spec metadata
    assert prepared["spec"]["metadata"]["env_var_names"] == ["TEST_PASSWORD", "TEST_USER"]
    # and the rendered prompt names them WITHOUT any value
    prompt = job_prompt(prepared)
    assert "TEST_USER" in prompt and "TEST_PASSWORD" in prompt
    assert "s3cr3t-do-not-leak" not in prompt
    assert "alice" not in prompt


def test_load_tester_secrets_skips_insecure_env_file(home, tmp_path):
    env_file = tmp_path / "bad.env"
    env_file.write_text('TEST_USER="alice"\n', encoding="utf-8")
    os.chmod(env_file, 0o644)  # world-readable -> load_env_file refuses
    record = _record(_git_repo(tmp_path), profile={"env_file": str(env_file)})
    prepared, secret_env = _load_tester_secrets(record, "j")
    assert secret_env == {}  # refused, skipped — no crash
    assert "env_var_names" not in prepared["spec"]["metadata"]
    # value must not have leaked into the prompt either
    assert "alice" not in job_prompt(prepared)


def test_load_tester_secrets_noop_without_env_file(home, tmp_path):
    record = _record(_git_repo(tmp_path), profile={})
    prepared, secret_env = _load_tester_secrets(record, "j")
    assert secret_env == {} and prepared is record


# --------------------------------------------------------------------------- #
# test_deployment MCP tool
# --------------------------------------------------------------------------- #
def test_test_deployment_creates_staging_job(home, tmp_path):
    repo = _git_repo(tmp_path)
    out = mcp_server.test_deployment(repo=repo, mode="staging", flows="login, create invoice")
    assert out["role"] == "test-staging" and out["mode"] == "staging"
    record = mcp_server._store().get(out["id"])
    assert record["spec"]["role"] == "test-staging"
    assert record["spec"]["delivery_mode"] == "report-only"
    assert record["spec"]["branch"]["mode"] == "detached-review"


def test_test_deployment_local_mode(home, tmp_path):
    out = mcp_server.test_deployment(repo=_git_repo(tmp_path), mode="local")
    assert out["role"] == "test-local"


def test_test_deployment_unknown_mode_errors(home, tmp_path):
    out = mcp_server.test_deployment(repo=_git_repo(tmp_path), mode="prod")
    assert "error" in out and "unknown mode" in out["error"]


def test_test_deployment_enforces_allowlist(home, tmp_path, monkeypatch):
    from tasklane.config import Config
    monkeypatch.setattr(mcp_server, "_cfg", lambda: Config(repos_allowlist=["/some/other/place"]))
    out = mcp_server.test_deployment(repo=_git_repo(tmp_path), mode="staging")
    assert "error" in out and "allowlist" in out["error"]

"""Conversational credential intake (set_project_secrets / list / delete) on the
unified path-keyed project model, plus the critical audit-redaction guarantee:
secret VALUES must never reach the audit log. Happy + bad paths."""

import os
import stat
import subprocess

import pytest

from tasklane import mcp_server, projects
from tasklane.paths import audit_log_path
from tasklane.secrets import load_env_file


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path / "home"))
    return tmp_path


def _registered_repo(tmp_path, name="acme") -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    projects.register_project(str(repo), name=name)
    return str(repo)


SECRET_VALUE = "sup3r-s3cret-value-do-not-leak"


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #
def test_set_creates_default_env_file_and_links_registry(home, tmp_path):
    repo = _registered_repo(tmp_path)
    out = mcp_server.set_project_secrets(repo=repo, secrets={"TEST_USER": "alice", "TEST_PASSWORD": SECRET_VALUE})
    assert out["registry_linked"] is True
    assert sorted(out["key_names"]) == ["TEST_PASSWORD", "TEST_USER"]
    # file exists, mode 600, parses back to the values
    env_file = projects.get_project(repo)["env_file"]
    assert env_file and stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert load_env_file(env_file) == {"TEST_USER": "alice", "TEST_PASSWORD": SECRET_VALUE}


def test_set_merges_into_existing(home, tmp_path):
    repo = _registered_repo(tmp_path)
    mcp_server.set_project_secrets(repo=repo, secrets={"TEST_USER": "alice"})
    out = mcp_server.set_project_secrets(repo=repo, secrets={"STAGING_URL": "https://s.test"})
    assert sorted(out["key_names"]) == ["STAGING_URL", "TEST_USER"]


def test_list_and_delete_keys(home, tmp_path):
    repo = _registered_repo(tmp_path)
    mcp_server.set_project_secrets(repo=repo, secrets={"A_KEY": "1", "B_KEY": "2"})
    assert mcp_server.list_project_secret_keys(repo=repo)["key_names"] == ["A_KEY", "B_KEY"]
    out = mcp_server.delete_project_secret(repo=repo, key="A_KEY")
    assert out["removed"] is True and out["key_names"] == ["B_KEY"]


# --------------------------------------------------------------------------- #
# the critical guarantee: VALUES never reach the audit log
# --------------------------------------------------------------------------- #
def test_audit_log_never_contains_secret_values(home, tmp_path):
    repo = _registered_repo(tmp_path)
    mcp_server.set_project_secrets(repo=repo, secrets={"TEST_PASSWORD": SECRET_VALUE})
    audit_text = audit_log_path().read_text(encoding="utf-8")
    assert "set_project_secrets" in audit_text  # the call WAS audited
    assert "TEST_PASSWORD" in audit_text         # ...with the key NAME
    assert SECRET_VALUE not in audit_text         # ...but NEVER the value


# --------------------------------------------------------------------------- #
# bad paths (the @audited wrapper turns raises into {"error": ...})
# --------------------------------------------------------------------------- #
def test_unregistered_repo_refused(home, tmp_path):
    repo = tmp_path / "x"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    out = mcp_server.set_project_secrets(repo=str(repo), secrets={"K": "v"})
    assert "error" in out and "not registered" in out["error"]


def test_invalid_key_rejected(home, tmp_path):
    repo = _registered_repo(tmp_path)
    out = mcp_server.set_project_secrets(repo=repo, secrets={"bad-key": "v"})
    assert "error" in out and "invalid secret key" in out["error"]


def test_empty_value_rejected(home, tmp_path):
    repo = _registered_repo(tmp_path)
    out = mcp_server.set_project_secrets(repo=repo, secrets={"K": ""})
    assert "error" in out and "empty" in out["error"].lower()


def test_too_many_keys_rejected(home, tmp_path):
    repo = _registered_repo(tmp_path)
    out = mcp_server.set_project_secrets(repo=repo, secrets={f"K{i}": "v" for i in range(51)})
    assert "error" in out and "too many" in out["error"]


def test_allowlist_enforced(home, tmp_path, monkeypatch):
    from tasklane.config import Config
    repo = _registered_repo(tmp_path)
    monkeypatch.setattr(mcp_server, "_cfg", lambda: Config(repos_allowlist=["/elsewhere"]))
    out = mcp_server.set_project_secrets(repo=repo, secrets={"K": "v"})
    assert "error" in out and "allowlist" in out["error"]

"""Conversational credential intake: the set_project_secrets / delete_project_secret
/ list_project_secret_keys MCP tools plus the secrets.py write/merge/validate helpers.

Covers write+merge, mode-600 / dir-700 enforcement, key validation, registry
auto-link, and the CRITICAL guarantee that secret VALUES never reach the audit log.
"""

import json
import os
import stat

import pytest
import yaml

from tasklane import mcp_server
from tasklane.config import config_path, load_config
from tasklane.secrets import (
    MAX_SECRET_KEYS,
    MAX_SECRET_VALUE_LENGTH,
    default_secret_file_path,
    load_env_file,
    validate_secret_pairs,
    write_secret_file,
)

_REPO = "/tmp/intake-demo-repo"
_SECRET = "P@ssw0rd-NEVER-LEAK-9173"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolate $TASKLANE_HOME so config.yaml, the audit log and the secrets dir all
    live under tmp, and clear any leaked app-token override."""
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    monkeypatch.delenv("TASKLANE_APP_TOKEN", raising=False)
    return tmp_path


def _write_config(home, project: dict) -> None:
    config_path().write_text(
        yaml.safe_dump({"app_token": "t", "repos_allowlist": [], "projects": {"demo": project}}),
        encoding="utf-8",
    )
    os.chmod(config_path(), 0o600)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# --------------------------------------------------------------------------- #
# secrets.py helpers (pure, no MCP)
# --------------------------------------------------------------------------- #
def test_write_secret_file_round_trips_and_is_600(tmp_path):
    target = tmp_path / "sub" / "proj.env"
    values = {"TEST_USER": "alice", "TEST_PASSWORD": 'p"a #ss=word', "STAGING_URL": "https://x.example.com"}
    write_secret_file(target, values, secure_parent=True)

    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700
    assert load_env_file(target) == values  # exact round-trip incl. quotes/#/=/spaces


def test_write_secret_file_secures_a_freshly_created_parent(tmp_path):
    # secure_parent=False, but the parent does not exist yet → still created 0700,
    # never left at the umask default (HIGH finding regression guard).
    target = tmp_path / "brand-new-dir" / "p.env"
    write_secret_file(target, {"TEST_USER": "alice"}, secure_parent=False)
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700


def test_write_secret_file_leaves_existing_parent_untouched(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    os.chmod(parent, 0o755)
    write_secret_file(parent / "p.env", {"TEST_USER": "alice"}, secure_parent=False)
    assert _mode(parent) == 0o755  # pre-existing dir perms preserved


def test_validate_rejects_bad_keys_values_and_count():
    with pytest.raises(ValueError, match="invalid secret key name"):
        validate_secret_pairs({"lower_case": "x"})
    with pytest.raises(ValueError, match="invalid secret key name"):
        validate_secret_pairs({"1LEADING_DIGIT": "x"})
    with pytest.raises(ValueError, match="empty or non-string"):
        validate_secret_pairs({"OK": ""})
    with pytest.raises(ValueError, match="exceeds"):
        validate_secret_pairs({"OK": "x" * (MAX_SECRET_VALUE_LENGTH + 1)})
    with pytest.raises(ValueError, match="newline"):
        validate_secret_pairs({"OK": "line1\nline2"})
    with pytest.raises(ValueError, match="too many keys"):
        validate_secret_pairs({f"K{i}": "v" for i in range(MAX_SECRET_KEYS + 1)})
    with pytest.raises(ValueError, match="no secrets"):
        validate_secret_pairs({})


def test_validate_error_message_never_contains_value():
    try:
        validate_secret_pairs({"OK": "x" * (MAX_SECRET_VALUE_LENGTH + 1)})
    except ValueError as exc:
        assert "x" * 50 not in str(exc)


# --------------------------------------------------------------------------- #
# set_project_secrets — create + registry auto-link
# --------------------------------------------------------------------------- #
def test_set_secrets_creates_file_and_links_registry(home):
    _write_config(home, {"repo": _REPO})  # no env_file yet
    result = mcp_server.set_project_secrets(_REPO, {"TEST_USER": "alice", "TEST_PASSWORD": _SECRET})

    assert result["registry_linked"] is True
    expected = default_secret_file_path("demo")
    assert result["env_file"] == str(expected)
    assert result["key_names"] == ["TEST_PASSWORD", "TEST_USER"]
    assert "TEST_USER" in result["keys_written"]

    # file + parent permissions, and values actually persisted
    assert _mode(expected) == 0o600
    assert _mode(expected.parent) == 0o700
    assert load_env_file(expected) == {"TEST_USER": "alice", "TEST_PASSWORD": _SECRET}

    # registry entry now points at the new file
    cfg = load_config()
    assert cfg.projects["demo"]["env_file"] == str(expected)


def test_set_secrets_merges_into_existing_file(home, tmp_path):
    env_file = tmp_path / "existing.env"
    write_secret_file(env_file, {"TEST_USER": "old", "KEEP_ME": "stays"})
    _write_config(home, {"repo": _REPO, "env_file": str(env_file)})

    result = mcp_server.set_project_secrets(_REPO, {"TEST_USER": "new", "TEST_PASSWORD": _SECRET})
    assert result["registry_linked"] is False
    merged = load_env_file(env_file)
    assert merged == {"TEST_USER": "new", "KEEP_ME": "stays", "TEST_PASSWORD": _SECRET}
    assert _mode(env_file) == 0o600


def test_set_secrets_rejects_unregistered_repo(home):
    _write_config(home, {"repo": "/tmp/some-other-repo"})
    result = mcp_server.set_project_secrets(_REPO, {"TEST_USER": "alice"})
    assert "error" in result and "not registered" in result["error"]


def test_set_secrets_rejects_repo_outside_allowlist(home):
    config_path().write_text(
        yaml.safe_dump({"app_token": "t", "repos_allowlist": ["/srv/allowed"],
                        "projects": {"demo": {"repo": _REPO}}}),
        encoding="utf-8",
    )
    os.chmod(config_path(), 0o600)
    result = mcp_server.set_project_secrets(_REPO, {"TEST_USER": "alice"})
    assert "error" in result and "allowlist" in result["error"]


def test_set_secrets_rejects_bad_key(home):
    _write_config(home, {"repo": _REPO})
    result = mcp_server.set_project_secrets(_REPO, {"bad key": "x"})
    assert "error" in result and "invalid secret key name" in result["error"]


# --------------------------------------------------------------------------- #
# CRITICAL: audit redaction — value must be absent from the audit log file
# --------------------------------------------------------------------------- #
def test_audit_log_never_contains_secret_value(home):
    _write_config(home, {"repo": _REPO})
    mcp_server.set_project_secrets(_REPO, {"TEST_USER": "alice", "TEST_PASSWORD": _SECRET})

    from tasklane.paths import audit_log_path
    audit_text = audit_log_path().read_text(encoding="utf-8")

    # the value is gone; the key NAMES are recorded for accountability
    assert _SECRET not in audit_text
    assert "alice" not in audit_text
    entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
    intake = [e for e in entries if e["tool"] == "set_project_secrets"]
    assert intake and intake[-1]["status"] == "ok"
    assert intake[-1]["info"] == {"repo": _REPO, "secret_keys": ["TEST_PASSWORD", "TEST_USER"]}


# --------------------------------------------------------------------------- #
# list + delete
# --------------------------------------------------------------------------- #
def test_list_project_secret_keys_returns_names_only(home, tmp_path):
    env_file = tmp_path / "p.env"
    write_secret_file(env_file, {"TEST_USER": "alice", "TEST_PASSWORD": _SECRET})
    _write_config(home, {"repo": _REPO, "env_file": str(env_file)})

    result = mcp_server.list_project_secret_keys(_REPO)
    assert result["key_names"] == ["TEST_PASSWORD", "TEST_USER"]
    assert _SECRET not in json.dumps(result)


def test_list_project_secret_keys_empty_without_env_file(home):
    _write_config(home, {"repo": _REPO})
    result = mcp_server.list_project_secret_keys(_REPO)
    assert result["key_names"] == [] and result["env_file"] is None


def test_delete_project_secret_removes_one_key(home, tmp_path):
    env_file = tmp_path / "p.env"
    write_secret_file(env_file, {"TEST_USER": "alice", "TEST_PASSWORD": _SECRET})
    _write_config(home, {"repo": _REPO, "env_file": str(env_file)})

    result = mcp_server.delete_project_secret(_REPO, "TEST_PASSWORD")
    assert result["removed"] is True
    assert result["key_names"] == ["TEST_USER"]
    assert load_env_file(env_file) == {"TEST_USER": "alice"}
    assert _mode(env_file) == 0o600


def test_delete_project_secret_absent_key_is_noop(home, tmp_path):
    env_file = tmp_path / "p.env"
    write_secret_file(env_file, {"TEST_USER": "alice"})
    _write_config(home, {"repo": _REPO, "env_file": str(env_file)})

    result = mcp_server.delete_project_secret(_REPO, "NOT_THERE")
    assert result["removed"] is False
    assert result["key_names"] == ["TEST_USER"]

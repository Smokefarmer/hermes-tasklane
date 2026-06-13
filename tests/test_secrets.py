"""Unit tests for tasklane.secrets.load_env_file — parsing and the strict
mode-600 / current-owner refusal that keeps secrets from insecure files."""

import os

import pytest

from tasklane.secrets import load_env_file


def _write_secret(tmp_path, body: str, *, mode: int = 0o600):
    path = tmp_path / "secret.env"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_parses_key_value_ignoring_comments_and_blanks(tmp_path):
    path = _write_secret(tmp_path, "\n".join([
        "# a comment",
        "",
        "TEST_USER=alice",
        "TEST_PASSWORD=s3cr3t",
        "   # indented comment",
        "export STAGING_URL=https://staging.example.com",
    ]))
    env = load_env_file(path)
    assert env == {
        "TEST_USER": "alice",
        "TEST_PASSWORD": "s3cr3t",
        "STAGING_URL": "https://staging.example.com",
    }


def test_strips_surrounding_quotes(tmp_path):
    path = _write_secret(tmp_path, 'A="quoted value"\nB=\'single\'\nC=bare')
    env = load_env_file(path)
    assert env == {"A": "quoted value", "B": "single", "C": "bare"}


def test_value_may_contain_equals(tmp_path):
    path = _write_secret(tmp_path, "TOKEN=ab=cd=ef")
    assert load_env_file(path)["TOKEN"] == "ab=cd=ef"


def test_strips_inline_comment_from_unquoted_value(tmp_path):
    path = _write_secret(tmp_path, "USER=alice  # provisioned by ansible")
    assert load_env_file(path)["USER"] == "alice"


def test_keeps_hash_inside_quoted_value(tmp_path):
    path = _write_secret(tmp_path, 'PASS="p#ss word"')
    assert load_env_file(path)["PASS"] == "p#ss word"


def test_refuses_non_600_mode(tmp_path):
    path = _write_secret(tmp_path, "TEST_USER=alice", mode=0o644)
    with pytest.raises(PermissionError, match="insecure permissions"):
        load_env_file(path)


def test_refuses_group_readable(tmp_path):
    path = _write_secret(tmp_path, "TEST_USER=alice", mode=0o640)
    with pytest.raises(PermissionError):
        load_env_file(path)


def test_refuses_not_owned_by_current_user(tmp_path, monkeypatch):
    path = _write_secret(tmp_path, "TEST_USER=alice", mode=0o600)
    # Pretend the current process runs as a different uid than the file owner.
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(PermissionError, match="not the current user"):
        load_env_file(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_env_file(tmp_path / "nope.env")


def test_malformed_line_raises(tmp_path):
    path = _write_secret(tmp_path, "TEST_USER=alice\nNOT_AN_ASSIGNMENT")
    with pytest.raises(ValueError, match="malformed env line"):
        load_env_file(path)


def test_empty_key_raises(tmp_path):
    path = _write_secret(tmp_path, "=novalue")
    with pytest.raises(ValueError, match="empty key"):
        load_env_file(path)

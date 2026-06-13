"""Unit tests for the shared atomic-write helper reused by store and pairing."""

import json
import os
import stat

import pytest

from tasklane.atomicio import atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_parent_and_content(tmp_path):
    target = tmp_path / "nested" / "dir" / "file.txt"
    atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_json_roundtrips(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(target, {"b": 2, "a": 1})
    assert json.loads(target.read_text()) == {"a": 1, "b": 2}
    assert target.read_text().endswith("\n")  # trailing newline
    assert target.read_text().index('"a"') < target.read_text().index('"b"')  # sort_keys


def test_atomic_write_applies_mode_600(tmp_path):
    target = tmp_path / "secret.env"
    atomic_write_json(target, {"k": "v"}, mode=0o600)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_atomic_write_default_no_explicit_mode(tmp_path):
    target = tmp_path / "plain.json"
    atomic_write_json(target, {"k": "v"})  # no mode → umask default, no chmod
    assert target.exists()


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "f.json"
    atomic_write_json(target, {"k": "v"})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "f.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_atomic_write_no_partial_file_on_replace_error(tmp_path, monkeypatch):
    """If os.replace fails, the temp file is cleaned up (no debris, no partial target)."""
    target = tmp_path / "f.txt"

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "data")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []  # temp file cleaned up

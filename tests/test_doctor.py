"""Unit tests for the ``tasklane doctor`` diagnostics command."""

import os

import pytest
import yaml

from tasklane import cli
from tasklane.config import Config
from tasklane.doctor import (
    FAIL,
    OK,
    WARN,
    build_report,
    check_app_token,
    check_config_permissions,
    check_disk_space,
    check_home,
    check_job_store_dirs,
    check_orphaned_jobs,
    check_stale_locks,
    check_worktrees_size,
    format_table,
    overall_status,
    run_checks,
)
from tasklane.store import JobStore


def _write_config(home, *, token="s3cret-token", mode=0o600):
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump({"app_token": token}), encoding="utf-8")
    os.chmod(path, mode)
    return path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKLANE_HOME", str(tmp_path))
    monkeypatch.delenv("TASKLANE_APP_TOKEN", raising=False)
    return tmp_path


@pytest.fixture()
def fake_bin(tmp_path, monkeypatch):
    """Return a callable that installs a fake executable on a controlled PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))

    def add(name):
        exe = bindir / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

    return add


def _check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


# --- individual checks -----------------------------------------------------

def test_check_home_ok(home):
    assert check_home(home).status == OK


def test_check_home_fail_missing(tmp_path):
    assert check_home(tmp_path / "nope").status == FAIL


def test_check_app_token_empty_fails():
    assert check_app_token(Config(app_token="")).status == FAIL


def test_check_app_token_set_ok():
    assert check_app_token(Config(app_token="abc")).status == OK


def test_check_config_permissions_warns_on_loose_mode(home):
    path = _write_config(home, mode=0o644)
    assert check_config_permissions(path).status == WARN


def test_check_config_permissions_ok_on_600(home):
    path = _write_config(home, mode=0o600)
    assert check_config_permissions(path).status == OK


def test_check_job_store_dirs_reports_created(tmp_path):
    store = JobStore(root=tmp_path / "jobs")
    result = check_job_store_dirs(store)
    assert result.status == OK
    assert "created" in result.detail
    # second run: nothing missing
    assert "all job store directories present" in check_job_store_dirs(store).detail


def test_check_stale_locks_warns(tmp_path):
    store = JobStore(root=tmp_path / "jobs")
    store.ensure_dirs()
    lock = store._acquire_claim_lock("stale-job", owner="w")
    old = 1_000_000.0  # far in the past
    os.utime(lock, (old, old))
    result = check_stale_locks(store, Config(stale_lock_seconds=900.0))
    assert result.status == WARN
    assert "stale-job" in result.detail
    assert lock.exists()  # doctor must NOT delete


def test_check_stale_locks_ok_when_fresh(tmp_path):
    store = JobStore(root=tmp_path / "jobs")
    store.ensure_dirs()
    store._acquire_claim_lock("fresh-job", owner="w")
    assert check_stale_locks(store, Config(stale_lock_seconds=900.0)).status == OK


def test_check_orphaned_jobs_warns_on_dead_claimant(tmp_path):
    store = JobStore(root=tmp_path / "jobs")
    store.ensure_dirs()
    spec = {
        "id": "job-1",
        "repo": {"path": "/tmp/repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }
    store.put(spec)
    store.claim_next(owner="worker-999999")  # PID that is not alive
    result = check_orphaned_jobs(store)
    assert result.status == WARN
    assert "job-1" in result.detail
    assert store.get("job-1")["state"] == "running"  # read-only, no transition


def test_check_orphaned_jobs_ok_when_alive(tmp_path):
    store = JobStore(root=tmp_path / "jobs")
    store.ensure_dirs()
    spec = {
        "id": "job-1",
        "repo": {"path": "/tmp/repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }
    store.put(spec)
    store.claim_next(owner=f"worker-{os.getpid()}")
    assert check_orphaned_jobs(store).status == OK


def test_check_disk_space_warn_and_ok(home):
    assert check_disk_space(home, min_free_gib=0.0).status == OK
    assert check_disk_space(home, min_free_gib=10 ** 9).status == WARN


def test_check_worktrees_size_ok_when_small(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.txt").write_text("x")
    assert check_worktrees_size(root).status == OK


def test_check_worktrees_size_warn_over_threshold(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.txt").write_text("x" * 2048)
    # threshold of 0 GiB forces a warn for any non-empty tree (no GiBs written)
    assert check_worktrees_size(root, warn_gib=0.0).status == WARN


def test_overall_status_precedence():
    from tasklane.doctor import Check

    assert overall_status([Check("a", OK, "")]) == OK
    assert overall_status([Check("a", OK, ""), Check("b", WARN, "")]) == WARN
    assert overall_status([Check("a", WARN, ""), Check("b", FAIL, "")]) == FAIL


# --- integration: full report + CLI ---------------------------------------

def test_report_all_ok(home, fake_bin):
    _write_config(home, mode=0o600)
    for name in ("claude", "git", "gh"):
        fake_bin(name)
    report = build_report()
    assert _check(report, "claude_cli")["status"] == OK
    assert _check(report, "git_cli")["status"] == OK
    assert _check(report, "gh_cli")["status"] == OK
    assert _check(report, "config_app_token")["status"] == OK
    # no failures expected on a clean home with all tools present
    assert all(c["status"] != FAIL for c in report["checks"])


def test_report_missing_claude_fails(home, fake_bin):
    _write_config(home, mode=0o600)
    fake_bin("git")
    fake_bin("gh")  # claude intentionally absent
    report = build_report()
    assert _check(report, "claude_cli")["status"] == FAIL
    assert report["overall"] == FAIL


def test_report_missing_gh_warns_not_fails(home, fake_bin):
    _write_config(home, mode=0o600)
    fake_bin("claude")
    fake_bin("git")  # gh intentionally absent
    report = build_report()
    assert _check(report, "gh_cli")["status"] == WARN
    assert _check(report, "claude_cli")["status"] == OK
    assert report["overall"] != FAIL


def test_report_empty_token_fails(home, fake_bin):
    _write_config(home, token="", mode=0o600)
    for name in ("claude", "git", "gh"):
        fake_bin(name)
    report = build_report()
    assert _check(report, "config_app_token")["status"] == FAIL
    assert report["overall"] == FAIL


def test_format_table_contains_statuses(home, fake_bin):
    _write_config(home, mode=0o600)
    for name in ("claude", "git", "gh"):
        fake_bin(name)
    table = format_table(build_report())
    assert "overall:" in table
    assert "claude_cli" in table


def test_cli_doctor_exit_zero_when_no_fail(home, fake_bin, capsys):
    _write_config(home, mode=0o600)
    for name in ("claude", "git", "gh"):
        fake_bin(name)
    assert cli.main(["doctor"]) == 0
    assert "overall:" in capsys.readouterr().out


def test_cli_doctor_exit_one_on_fail(home, fake_bin, capsys):
    _write_config(home, mode=0o600)
    fake_bin("git")  # claude absent -> fail
    assert cli.main(["doctor"]) == 1


def test_cli_doctor_json_output(home, fake_bin, capsys):
    import json

    _write_config(home, mode=0o600)
    for name in ("claude", "git", "gh"):
        fake_bin(name)
    cli.main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] in (OK, WARN, FAIL)
    assert any(c["name"] == "claude_cli" for c in payload["checks"])

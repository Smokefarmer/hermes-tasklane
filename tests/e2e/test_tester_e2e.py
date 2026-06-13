"""E2E: a tester job through the real worker loop — the project's env_file secrets
reach the claude subprocess (proving end-to-end injection), while the prompt/log
only ever name them. Happy path + the insecure-env-file bad path."""

import os
import threading

import pytest

from tasklane import projects
from tasklane.store import JobStore
from tasklane.worker import worker_loop

from .conftest import job_spec
from .test_worker_loop_e2e import wait_for_state

pytestmark = pytest.mark.e2e


def _write_secret_file(tmp_path, **pairs) -> str:
    path = tmp_path / "staging.env"
    path.write_text("".join(f'{k}="{v}"\n' for k, v in pairs.items()), encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def _run_worker_once(store, job_id):
    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), daemon=True)
    thread.start()
    try:
        return wait_for_state(store, job_id, {"completed", "blocked", "failed"})
    finally:
        stop.set()
        thread.join(timeout=10)


def test_tester_job_receives_secrets_but_prompt_does_not(tasklane_home, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "check_env")
    env_file = _write_secret_file(git_repo.parent, TEST_USER="alice", TEST_PASSWORD="leak-me-not", STAGING_URL="https://s.test")
    projects.register_project(str(git_repo), name="acme", env_file=env_file,
                              staging_url="https://s.test")

    store = JobStore()
    store.ensure_dirs()
    spec = job_spec(git_repo, "tester-e2e")
    spec["role"] = "test-local"
    store.put(spec)

    final = _run_worker_once(store, "tester-e2e")
    assert final["state"] == "completed", final.get("last_error")

    # the subprocess actually received all three secret env vars
    seen = (fake_claude / "env-names-seen.txt").read_text().split(",")
    assert set(seen) == {"STAGING_URL", "TEST_PASSWORD", "TEST_USER"}

    # the persisted prompt named the vars but never carried a value
    log = (store.root / "logs" / "tester-e2e.log").read_text()
    assert "TEST_PASSWORD" in log and "TEST_USER" in log
    assert "leak-me-not" not in log
    assert "alice" not in log


def test_tester_job_with_insecure_env_file_runs_without_secrets(tasklane_home, fake_claude, git_repo, monkeypatch):
    """A world-readable env_file is refused: the job still runs (the agent will
    report it cannot authenticate) but no secret is injected and nothing crashes."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "check_env")
    bad = git_repo.parent / "bad.env"
    bad.write_text('TEST_USER="alice"\n', encoding="utf-8")
    os.chmod(bad, 0o644)  # insecure -> load_env_file refuses
    projects.register_project(str(git_repo), name="acme", env_file=str(bad))

    store = JobStore()
    store.ensure_dirs()
    spec = job_spec(git_repo, "tester-bad")
    spec["role"] = "test-local"
    store.put(spec)

    final = _run_worker_once(store, "tester-bad")
    assert final["state"] == "completed", final.get("last_error")
    # no secrets injected
    seen = (fake_claude / "env-names-seen.txt").read_text().strip()
    assert seen == ""
    assert "alice" not in (store.root / "logs" / "tester-bad.log").read_text()

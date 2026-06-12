"""E2E: the full job pipeline through worker.run_job — real worktrees, real
subprocess CLI calls (faked binary), real delivery validation, real store
transitions. Only the LLM is substituted."""

import subprocess
from pathlib import Path

import pytest

from tasklane.store import JobStore
from tasklane.worker import run_job

from .conftest import fake_calls, job_spec

pytestmark = pytest.mark.e2e


@pytest.fixture()
def store(tasklane_home):
    s = JobStore()
    s.ensure_dirs()
    return s


def claim(store, spec):
    store.put(spec)
    record = store.claim_next(owner="worker-e2e")
    assert record is not None
    return record


def test_report_only_job_completes(store, fast_cfg, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "report_only")
    record = claim(store, job_spec(git_repo, "e2e-report"))
    run_job(store, fast_cfg, record)

    final = store.get("e2e-report")
    assert final["state"] == "completed"
    assert "Analysis complete" in final["result"]["final_response"]
    assert final["result"]["delivery_validation"]["ok"] is True
    # telemetry captured from the CLI JSON output
    m = final["metrics"]
    assert m["cost_usd"] == pytest.approx(0.05)
    assert m["input_tokens"] == 1000 and m["output_tokens"] == 250
    assert m["runs"] == 1 and m["wall_seconds"] >= 0
    # worktree cleaned up on success
    wt = final.get("worktree") or {}
    assert wt.get("worktree_path") and not Path(wt["worktree_path"]).exists()
    assert fake_calls(fake_claude) == 1


def test_direct_push_job_lands_commit_on_origin(store, fast_cfg, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "commit_and_push")
    spec = job_spec(git_repo, "e2e-push", delivery_mode="direct-push",
                    branch_mode="new-branch", work_branch="tasklane/e2e-push")
    record = claim(store, spec)
    run_job(store, fast_cfg, record)

    final = store.get("e2e-push")
    assert final["state"] == "completed", final.get("last_error")
    # the commit really arrived on the bare origin
    origin = git_repo.parent / "origin.git"
    log = subprocess.run(["git", "log", "--oneline", "tasklane/e2e-push"],
                         cwd=origin, capture_output=True, text=True)
    assert log.returncode == 0 and "feat: fake change" in log.stdout


def test_agent_error_auto_retries_then_blocks(store, fast_cfg, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "error")
    record = claim(store, job_spec(git_repo, "e2e-fail"))
    run_job(store, fast_cfg, record)

    # attempt 1 of max 2 → auto-retried into ready with backoff cleared (0s)
    after_first = store.get("e2e-fail")
    assert after_first["state"] == "ready"
    assert "simulated agent failure" in after_first["last_error"]

    record = store.claim_next(owner="worker-e2e")
    assert record is not None and record["attempt"] == 2
    run_job(store, fast_cfg, record)

    # attempt cap reached → blocked for human/remote-agent review
    final = store.get("e2e-fail")
    assert final["state"] == "blocked"
    assert "attempt cap reached" in final["last_error"]
    events = [e["event_type"] for e in store.events("e2e-fail")]
    assert "job_auto_retry_scheduled" in events


def test_dirty_worktree_blocks_and_keeps_worktree_for_fix_tools(store, fast_cfg, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "dirty")
    record = claim(store, job_spec(git_repo, "e2e-dirty"))
    run_job(store, fast_cfg, record)

    final = store.get("e2e-dirty")
    # delivery-validation failures do NOT auto-retry — straight to blocked
    assert final["state"] == "blocked"
    assert "uncommitted changes" in final["last_error"]
    # one main pass + one repair pass
    assert fake_calls(fake_claude) == 2
    # worktree kept so the MCP fix tools can inspect it
    wt = final.get("worktree") or {}
    assert wt.get("worktree_path") and Path(wt["worktree_path"]).exists()


def test_repair_pass_rescues_dirty_worktree(store, fast_cfg, fake_claude, git_repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "dirty_then_clean")
    record = claim(store, job_spec(git_repo, "e2e-repair"))
    run_job(store, fast_cfg, record)

    final = store.get("e2e-repair")
    assert final["state"] == "completed", final.get("last_error")
    assert fake_calls(fake_claude) == 2  # main pass + successful repair pass
    events = [e["event_type"] for e in store.events("e2e-repair")]
    assert "job_delivery_repair_started" in events

"""Unit tests for orphan recovery, stale-lock cleanup, and worker auto-retry."""

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from tasklane.config import Config
from tasklane.reconcile import (
    CLAIM_GRACE_SECONDS,
    claimant_pid,
    cleanup_stale_locks,
    pid_alive,
    recover_orphans,
    reconcile,
    retry_delay_seconds,
    unknown_claimant_ceiling_seconds,
)
from tasklane.store import JobStore
from tasklane.worker import _fail_or_requeue

# Past the claim-grace window so reconcile judges freshly claimed test jobs.
# Computed per call: a module-level constant is fixed at collection time, so once
# the whole suite runs longer than the grace window the constant predates the
# test's own claims and these tests turn flaky.
def later() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=CLAIM_GRACE_SECONDS + 5)


def dead_pid() -> int:
    """PID of a child that has exited and been reaped — guaranteed not alive."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def make_spec(job_id: str = "job-1"):
    return {
        "id": job_id,
        "repo": {"path": "/tmp/example-repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


def claim_as(store, job_id: str, owner: str):
    store.put(make_spec(job_id))
    claimed = store.claim_next(owner=owner)
    assert claimed is not None and claimed["id"] == job_id
    return claimed


def test_claimant_pid_parsing():
    assert claimant_pid("worker-1234") == 1234
    assert claimant_pid("worker-1234 ") == 1234
    assert claimant_pid("1234") == 1234
    assert claimant_pid("worker") is None
    assert claimant_pid(None) is None


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(dead_pid()) is False
    assert pid_alive(0) is False
    assert pid_alive(-5) is False


def test_retry_delay_doubles_per_attempt():
    cfg = Config(retry_backoff_seconds=60.0)
    assert retry_delay_seconds(cfg, 1) == 60.0
    assert retry_delay_seconds(cfg, 2) == 120.0
    assert retry_delay_seconds(cfg, 3) == 240.0


def test_recover_orphans_requeues_dead_claimant(store):
    claim_as(store, "job-1", f"worker-{dead_pid()}")
    actions = recover_orphans(store, Config(max_attempts=3), now=later())
    assert len(actions) == 1
    assert actions[0]["action"] == "requeued"
    record = store.get("job-1")
    assert record["state"] == "ready"
    assert record["not_before"]  # backoff applied
    assert record["attempt"] == 1  # preserved
    assert "died" in record["last_error"]


def test_recover_orphans_blocks_at_attempt_cap(store):
    claim_as(store, "job-1", f"worker-{dead_pid()}")
    actions = recover_orphans(store, Config(max_attempts=1), now=later())
    assert actions[0]["action"] == "blocked"
    assert store.get("job-1")["state"] == "blocked"


def test_recover_orphans_leaves_live_claimant_alone(store):
    claim_as(store, "job-1", f"worker-{os.getpid()}")
    assert recover_orphans(store, Config(), now=later()) == []
    assert store.get("job-1")["state"] == "running"


def test_recover_orphans_respects_claim_grace(store):
    # claimed "just now" — must not be judged even though the claimant is dead
    claim_as(store, "job-1", f"worker-{dead_pid()}")
    assert recover_orphans(store, Config()) == []
    assert store.get("job-1")["state"] == "running"


def test_recover_orphans_skips_when_lock_held(store):
    claim_as(store, "job-1", f"worker-{dead_pid()}")
    lock = store._acquire_claim_lock("job-1", owner="someone-else")
    try:
        actions = recover_orphans(store, Config(), now=later())
    finally:
        store._release_claim_lock(lock)
    assert actions == [{"job_id": "job-1", "action": "skipped", "reason": "claim-lock-held"}]
    assert store.get("job-1")["state"] == "running"


def test_recover_orphans_flags_unknown_claimant_within_ceiling(store):
    claim_as(store, "job-1", "mystery-owner")
    actions = recover_orphans(store, Config(), now=later())
    assert actions[0]["action"] == "flagged"
    assert store.get("job-1")["state"] == "running"


def test_recover_orphans_blocks_unknown_claimant_past_ceiling(store):
    cfg = Config()
    claim_as(store, "job-1", "mystery-owner")
    past_ceiling = datetime.now(timezone.utc) + timedelta(seconds=unknown_claimant_ceiling_seconds(cfg) + 60)
    actions = recover_orphans(store, cfg, now=past_ceiling)
    assert actions[0]["action"] == "blocked"
    assert store.get("job-1")["state"] == "blocked"


def test_cleanup_stale_locks_removes_only_old(store):
    fresh = store._acquire_claim_lock("fresh-job", owner="w")
    stale = store._acquire_claim_lock("stale-job", owner="w")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    removed = cleanup_stale_locks(store, Config(stale_lock_seconds=900.0))
    assert removed == ["stale-job"]
    assert fresh.exists() and not stale.exists()


def test_reconcile_report_shape(store):
    report = reconcile(store, Config())
    assert set(report) == {"at", "orphans", "stale_locks_removed"}


def test_worker_fail_or_requeue_requeues_below_cap(store):
    claim_as(store, "job-1", "worker-1")
    outcome = _fail_or_requeue(store, Config(max_attempts=3), "job-1", reason="transient boom")
    assert outcome == "requeued"
    record = store.get("job-1")
    assert record["state"] == "ready"
    assert record["last_error"] == "transient boom"
    assert record["not_before"]


def test_worker_fail_or_requeue_blocks_at_cap(store):
    claim_as(store, "job-1", "worker-1")
    outcome = _fail_or_requeue(store, Config(max_attempts=1), "job-1", reason="boom")
    assert outcome == "blocked"
    record = store.get("job-1")
    assert record["state"] == "blocked"
    assert "attempt cap reached" in record["last_error"]


def test_worker_fail_or_requeue_missing_job(store):
    assert _fail_or_requeue(store, Config(), "no-such-job", reason="x") == "not-found"

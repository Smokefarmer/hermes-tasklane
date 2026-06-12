"""Unit tests for the file-backed job store: lifecycle, claiming, backoff gating."""

from datetime import datetime, timedelta, timezone

import pytest

from tasklane.store import JobStore, parse_timestamp


def make_spec(job_id: str = "job-1", **overrides):
    spec = {
        "id": job_id,
        "repo": {"path": "/tmp/example-repo"},
        "request": {"type": "task", "title": "t", "body": "b"},
        "branch": {"mode": "detached-review"},
        "delivery_mode": "report-only",
    }
    spec.update(overrides)
    return spec


@pytest.fixture()
def store(tmp_path):
    s = JobStore(root=tmp_path / "jobs")
    s.ensure_dirs()
    return s


def test_put_get_roundtrip(store):
    record = store.put(make_spec())
    assert record["state"] == "ready"
    assert record["attempt"] == 0
    fetched = store.get(record["id"])
    assert fetched is not None and fetched["id"] == record["id"]


def test_put_duplicate_rejected(store):
    store.put(make_spec())
    with pytest.raises(FileExistsError):
        store.put(make_spec())


def test_claim_increments_attempt_and_clears_not_before(store):
    store.put(make_spec())
    claimed = store.claim_next(owner="worker-123")
    assert claimed is not None
    assert claimed["state"] == "running"
    assert claimed["attempt"] == 1
    assert claimed["claimed_by"] == "worker-123"
    assert claimed["not_before"] is None


def test_claim_skips_future_not_before(store):
    record = store.put(make_spec())
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.transition(record["id"], "ready", reason="test-backoff", updates={"not_before": future})
    assert store.claim_next(owner="worker-1") is None


def test_claim_allows_past_not_before(store):
    record = store.put(make_spec())
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.transition(record["id"], "ready", reason="test-backoff", updates={"not_before": past})
    claimed = store.claim_next(owner="worker-1")
    assert claimed is not None and claimed["id"] == record["id"]


def test_claim_respects_dependencies(store):
    store.put(make_spec("dep-job"))
    store.put(make_spec("main-job", dependencies=["dep-job"]))
    first = store.claim_next(owner="w")
    assert first["id"] == "dep-job"
    # dep-job is running, not completed — main-job must not be claimable
    assert store.claim_next(owner="w") is None
    store.complete("dep-job")
    second = store.claim_next(owner="w")
    assert second is not None and second["id"] == "main-job"


def test_requeue_keeps_attempt_and_error(store):
    store.put(make_spec())
    claimed = store.claim_next(owner="worker-9")
    requeued = store.requeue(claimed["id"], reason="auto-retry", not_before=None, last_error="boom")
    assert requeued["state"] == "ready"
    assert requeued["attempt"] == 1  # preserved
    assert requeued["last_error"] == "boom"
    assert requeued["claimed_by"] is None
    reclaimed = store.claim_next(owner="worker-9")
    assert reclaimed["attempt"] == 2


def test_retry_clears_backoff_and_error(store):
    record = store.put(make_spec())
    store.fail(record["id"], reason="x", retryable=True)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.transition(record["id"], "blocked", reason="test", updates={"not_before": future})
    retried = store.retry(record["id"])
    assert retried["state"] == "ready"
    assert retried["last_error"] is None
    assert retried["not_before"] is None


def test_fail_retryable_goes_blocked_else_failed(store):
    a = store.put(make_spec("job-a"))
    b = store.put(make_spec("job-b"))
    assert store.fail(a["id"], reason="r", retryable=True)["state"] == "blocked"
    assert store.fail(b["id"], reason="r", retryable=False)["state"] == "failed"


def test_stale_claim_lock_blocks_claim(store):
    record = store.put(make_spec())
    # simulate a lock left behind by a killed process
    lock = store._acquire_claim_lock(record["id"], owner="dead-worker")
    assert lock is not None
    assert store.claim_next(owner="live-worker") is None


def test_parse_timestamp():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-date") is None
    aware = parse_timestamp("2026-06-12T00:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None
    naive = parse_timestamp("2026-06-12T00:00:00")
    assert naive is not None and naive.tzinfo is not None  # coerced to UTC

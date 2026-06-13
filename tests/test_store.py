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


# --------------------------------------------------------------------------- #
# transition locking (concurrency integrity)
# --------------------------------------------------------------------------- #
def test_concurrent_transitions_leave_record_in_one_state(store):
    """Many threads transitioning one job concurrently must never leave the
    record present in two state dirs (the pre-lock corruption mode)."""
    import threading

    store.put(make_spec("race"))
    states = ["ready", "running", "blocked", "completed", "failed", "needs-human"]
    errors: list[Exception] = []

    def flip(target: str) -> None:
        try:
            for _ in range(8):
                store.transition("race", target, reason="stress")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=flip, args=(s,)) for s in states]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"transition raised under contention: {errors}"
    # the record must live in exactly ONE state dir
    present = [s for s in states if (store.root / s / "race.json").exists()]
    assert len(present) == 1, f"record present in multiple state dirs: {present}"
    assert store.get("race")["state"] in states


def test_transition_lock_times_out_when_held(store):
    """A transition fails closed (TimeoutError) rather than corrupting state when
    the per-job lock is held by someone else past the timeout."""
    import tasklane.store as store_mod

    store.put(make_spec("held"))
    lock_path = store._transition_lock_path("held")
    lock_path.write_text("{}")  # simulate a held (fresh) lock
    try:
        orig = store_mod.TRANSITION_LOCK_TIMEOUT
        store_mod.TRANSITION_LOCK_TIMEOUT = 0.3  # read at call time now
        # keep the lock "fresh" so the stale-breaker (separate, larger threshold)
        # does not steal it
        import os, time as _t
        os.utime(lock_path, (_t.time(), _t.time()))
        with pytest.raises(TimeoutError):
            store.transition("held", "running", reason="should-block")
    finally:
        store_mod.TRANSITION_LOCK_TIMEOUT = orig
        lock_path.unlink(missing_ok=True)


def test_transition_steals_stale_lock(store):
    """A transition lock older than the timeout is debris from a crash and is stolen."""
    import os, time as _t

    store.put(make_spec("stale"))
    lock_path = store._transition_lock_path("stale")
    lock_path.write_text("{}")
    old = _t.time() - 3600
    os.utime(lock_path, (old, old))
    # should steal the stale lock and succeed
    updated = store.transition("stale", "running", reason="after-steal")
    assert updated["state"] == "running"

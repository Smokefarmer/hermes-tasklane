"""E2E: the real worker loop — polling, claiming, threading, startup reconcile —
driving a job from ready to completed with no direct run_job calls."""

import threading
import time

import pytest

from tasklane.store import JobStore
from tasklane.worker import worker_loop

from .conftest import job_spec

pytestmark = pytest.mark.e2e

DEADLINE = 30.0


def wait_for_state(store, job_id, states, deadline=DEADLINE):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        record = store.get(job_id)
        if record and record["state"] in states:
            return record
        time.sleep(0.1)
    raise AssertionError(f"{job_id} never reached {states}; now: {store.get(job_id)}")


@pytest.fixture()
def running_worker(tasklane_home):
    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=10)


def test_worker_loop_processes_job_end_to_end(tasklane_home, fake_claude, git_repo, monkeypatch, running_worker):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "report_only")
    store = JobStore()
    store.ensure_dirs()
    store.put(job_spec(git_repo, "e2e-loop"))

    final = wait_for_state(store, "e2e-loop", {"completed", "blocked", "failed"})
    assert final["state"] == "completed", final.get("last_error")
    assert final["claimed_by"].startswith("worker-")


def test_worker_loop_recovers_orphan_on_startup(tasklane_home, fake_claude, git_repo, monkeypatch):
    """A job left in `running` by a dead worker is recovered and re-run to completion."""
    import subprocess

    from tasklane.reconcile import CLAIM_GRACE_SECONDS  # noqa: F401 (documents the dependency)

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "report_only")
    store = JobStore()
    store.ensure_dirs()
    store.put(job_spec(git_repo, "e2e-orphan"))

    # simulate a crashed worker: claim with a reaped child's PID, backdate the claim
    proc = subprocess.Popen(["true"])
    proc.wait()
    claimed = store.claim_next(owner=f"worker-{proc.pid}")
    assert claimed is not None
    store.transition("e2e-orphan", "running", reason="backdate-claim",
                     updates={"claimed_at": "2020-01-01T00:00:00+00:00"})

    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), daemon=True)
    thread.start()
    try:
        final = wait_for_state(store, "e2e-orphan", {"completed", "blocked", "failed"})
    finally:
        stop.set()
        thread.join(timeout=10)

    assert final["state"] == "completed", final.get("last_error")
    events = [e["event_type"] for e in store.events("e2e-orphan")]
    assert "job_orphan_detected" in events


def test_worker_fans_out_proposed_drafts(tasklane_home, fake_claude, git_repo, monkeypatch, running_worker):
    """A completed job whose final response proposes follow-ups creates DRAFT jobs
    that the worker never claims (they stay draft until a human approves them)."""
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "propose")
    store = JobStore()
    store.ensure_dirs()
    store.put(job_spec(git_repo, "e2e-propose"))

    parent = wait_for_state(store, "e2e-propose", {"completed", "blocked", "failed"})
    assert parent["state"] == "completed", parent.get("last_error")

    draft = wait_for_state(store, "e2e-propose-p01", {"draft"})
    assert draft["spec"]["source"]["proposed_by"] == "e2e-propose"
    assert draft["spec"]["repo"]["path"] == str(git_repo)
    assert draft["spec"]["branch"]["mode"] == "detached-review"
    assert draft["spec"]["delivery_mode"] == "report-only"

    # the draft must not be picked up by the running worker — give it time to try
    time.sleep(1.0)
    assert store.get("e2e-propose-p01")["state"] == "draft"
    assert store.get("e2e-propose-p02")["state"] == "draft"

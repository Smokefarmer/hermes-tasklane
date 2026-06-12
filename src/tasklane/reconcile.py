"""Reconcile job-store state with process reality.

A job stuck in ``running`` after a worker crash/reboot would otherwise occupy
its slot forever, and a claim lock left behind by a killed process would make
its job permanently unclaimable. Reconciliation fixes both:

- **Orphaned running jobs** — the claimant PID (parsed from ``claimed_by``,
  e.g. ``worker-1234``) is no longer alive. Requeued to ``ready`` with an
  exponential-backoff ``not_before``; once the attempt cap is reached the job
  moves to ``blocked`` for human/remote-agent review.
- **Stuck unknown claimants** — a running job whose ``claimed_by`` carries no
  PID can't be liveness-checked; once its runtime exceeds a hard ceiling it is
  moved to ``blocked`` instead of holding a slot forever.
- **Stale claim locks** — claim locks are held for milliseconds, so any lock
  older than ``stale_lock_seconds`` is debris from a killed process.

Long-running jobs whose claimant is alive are left alone (the worker enforces
its own per-job timeout).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from tasklane.config import Config
from tasklane.store import JobStore, normalize_job_state, parse_timestamp, utc_now

logger = logging.getLogger("tasklane.reconcile")

_PID_RE = re.compile(r"(\d+)\s*$")

# Never judge a claim younger than this: the claim transition may still be in
# flight, and it also guards against OS PID recycling immediately after a claim.
CLAIM_GRACE_SECONDS = 30.0


def claimant_pid(claimed_by: Any) -> int | None:
    """Extract the trailing PID from a claim owner string like ``worker-1234``."""
    match = _PID_RE.search(str(claimed_by or ""))
    return int(match.group(1)) if match else None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OverflowError, ValueError):
        return False
    return True


def retry_delay_seconds(cfg: Config, attempt: int) -> float:
    """Exponential backoff: backoff * 2^(attempt-1), attempt counted from 1."""
    return cfg.retry_backoff_seconds * (2 ** max(0, attempt - 1))


def backoff_not_before(cfg: Config, attempt: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=retry_delay_seconds(cfg, attempt))).isoformat()


def unknown_claimant_ceiling_seconds(cfg: Config) -> float:
    """Runtime after which a running job with an unverifiable claimant is blocked."""
    return max(float(cfg.timeout_seconds) * 2, 3600.0)


def _recover_one(store: JobStore, cfg: Config, job_id: str, now: datetime) -> dict[str, Any] | None:
    """Judge one running job. Caller holds the claim lock; record is re-read here."""
    record = store.get(job_id)
    if not record or normalize_job_state(record.get("state")) != "running":
        return None  # changed under us — nothing to do
    claimed_at = parse_timestamp(record.get("claimed_at"))
    age = (now - claimed_at).total_seconds() if claimed_at else None
    if age is not None and age < CLAIM_GRACE_SECONDS:
        return None
    attempt = int(record.get("attempt") or 0)
    pid = claimant_pid(record.get("claimed_by"))
    if pid is None:
        ceiling = unknown_claimant_ceiling_seconds(cfg)
        if age is not None and age > ceiling:
            reason = f"claimant {record.get('claimed_by')!r} has no PID and runtime exceeded {int(ceiling)}s"
            store.append_event(job_id, "job_orphan_detected", state="running", reason=reason,
                               metadata={"attempt": attempt, "runtime_seconds": int(age)})
            store.fail(job_id, reason=reason, retryable=True)
            return {"job_id": job_id, "action": "blocked", "reason": "unknown-claimant-timeout"}
        logger.warning("Job %s running with unverifiable claimant %r (age=%ss)",
                       job_id, record.get("claimed_by"), int(age) if age is not None else "?")
        return {"job_id": job_id, "action": "flagged", "reason": "claimant-unknown"}
    if pid_alive(pid):
        return None
    reason = f"worker process {pid} died while job was running"
    store.append_event(job_id, "job_orphan_detected", state="running", reason=reason,
                       metadata={"claimant_pid": pid, "attempt": attempt, "max_attempts": cfg.max_attempts})
    if attempt < cfg.max_attempts:
        not_before = backoff_not_before(cfg, attempt, now=now)
        store.requeue(job_id, reason="orphan-recovered", not_before=not_before, last_error=reason)
        return {"job_id": job_id, "action": "requeued", "attempt": attempt, "not_before": not_before}
    store.fail(job_id, reason=f"{reason}; attempt cap reached ({attempt}/{cfg.max_attempts})", retryable=True)
    return {"job_id": job_id, "action": "blocked", "attempt": attempt}


def recover_orphans(store: JobStore, cfg: Config, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Requeue (or block, at the attempt cap) running jobs whose claimant died.

    Each job's claim lock is held across the judge-and-transition so a
    concurrent reconciler or claimer can't act on the same job; if the lock is
    already held the job is skipped until the next cycle.
    """
    actions: list[dict[str, Any]] = []
    now = now or datetime.now(timezone.utc)
    for record in store.list(states=["running"]):
        job_id = str(record.get("id") or "")
        lock = store._acquire_claim_lock(job_id, owner="reconciler")
        if lock is None:
            actions.append({"job_id": job_id, "action": "skipped", "reason": "claim-lock-held"})
            continue
        try:
            action = _recover_one(store, cfg, job_id, now)
        finally:
            store._release_claim_lock(lock)
        if action is not None:
            actions.append(action)
    return actions


def cleanup_stale_locks(store: JobStore, cfg: Config) -> list[str]:
    """Remove claim locks older than ``stale_lock_seconds`` (returns job IDs)."""
    removed: list[str] = []
    if not store.locks_dir.exists():
        return removed
    cutoff = time.time() - max(0.0, cfg.stale_lock_seconds)
    for path in store.locks_dir.glob("*.lock"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed.append(path.stem)
        except OSError:
            continue
    return removed


def reconcile(store: JobStore, cfg: Config) -> dict[str, Any]:
    """Run all recovery passes and return a report.

    Order matters: orphan recovery runs first while claim locks are intact;
    only then is lock debris swept, so the sweep can never delete a lock the
    orphan pass is relying on.
    """
    return {
        "at": utc_now(),
        "orphans": recover_orphans(store, cfg),
        "stale_locks_removed": cleanup_stale_locks(store, cfg),
    }

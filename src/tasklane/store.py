from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tasklane.atomicio import atomic_write_json
from tasklane.paths import jobs_root as _jobs_root

from tasklane.specs import validate_job_spec


JOB_STATES = {
    "draft",
    "ready",
    "running",
    "blocked",
    "completed",
    "failed",
    "needs-human",
}
TERMINAL_JOB_STATES = {"completed", "failed"}
ACTIVE_JOB_STATES = {"running"}

# Per-job transition lock. Transitions are sub-millisecond, so a short acquire
# timeout is ample. Staleness is a SEPARATE, larger threshold: a lock older than
# it is debris from a crashed process and is stolen. Keeping the two distinct
# means a merely-contended (but live) lock within the acquire window is never
# mistaken for stale.
TRANSITION_LOCK_TIMEOUT = 10.0
TRANSITION_LOCK_STALE_SECONDS = 60.0
TRANSITION_LOCK_POLL = 0.02


def default_jobs_root() -> Path:
    return _jobs_root()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """File-backed Hermes job store for autonomous coding assembly-line jobs."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_jobs_root()
        self.events_dir = self.root / "events"
        self.locks_dir = self.root / "locks"

    def ensure_dirs(self) -> None:
        for state in JOB_STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    def put(self, raw_spec: Mapping[str, Any], *, state: str = "ready", reason: str = "job-created") -> dict[str, Any]:
        self.ensure_dirs()
        normalized_state = normalize_job_state(state)
        spec = validate_job_spec(dict(raw_spec))
        if self.get(spec["id"]) is not None:
            raise FileExistsError(f"Job already exists: {spec['id']}")
        record = {
            "id": spec["id"],
            "state": normalized_state,
            "spec": spec,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "attempt": 0,
            "last_error": None,
        }
        self._write_record(record)
        self.append_event(spec["id"], "job_created", state=normalized_state, reason=reason)
        return record

    def get(self, job_id: str) -> dict[str, Any] | None:
        safe_id = validate_job_id(job_id)
        for state in JOB_STATES:
            path = self.root / state / f"{safe_id}.json"
            try:
                return read_json(path)
            except FileNotFoundError:
                # Not in this state dir, or a concurrent transition moved the
                # record between dirs while we were reading — keep scanning.
                continue
        return None

    def list(self, *, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        self.ensure_dirs()
        selected_states = [normalize_job_state(state) for state in states] if states is not None else sorted(JOB_STATES)
        records: list[dict[str, Any]] = []
        for state in selected_states:
            for path in sorted((self.root / state).glob("*.json")):
                try:
                    record = read_json(path)
                except Exception:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def transition(
        self,
        job_id: str,
        state: str,
        *,
        reason: str,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Serialize read->write-new->unlink-old per job. Without this, concurrent
        # transitions (reconciler vs worker complete/fail vs MCP retry/cancel) can
        # each write their own state dir and unlink the other's old path, leaving
        # the record present in TWO state dirs (get() then returns a nondeterministic
        # state). The transition lock is distinct from the claim lock, so a caller
        # holding the claim lock (claim_next, reconcile) does not self-deadlock.
        self.ensure_dirs()
        with self._transition_lock(job_id):
            record = self.get(job_id)
            if record is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            old_state = normalize_job_state(record.get("state"))
            new_state = normalize_job_state(state)
            updated = dict(record)
            updated.update(dict(updates or {}))
            updated["state"] = new_state
            updated["updated_at"] = utc_now()
            old_path = self.root / old_state / f"{validate_job_id(job_id)}.json"
            self._write_record(updated)
            if old_path != self._record_path(updated):
                old_path.unlink(missing_ok=True)
            self.append_event(job_id, "job_state_changed", state=new_state, reason=reason, metadata={"from": old_state})
            return updated

    def ready_jobs(
        self,
        *,
        active_repo_keys: Iterable[str] = (),
        pending_repo_keys: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        blocked_repos = {str(item).strip() for item in active_repo_keys if str(item).strip()}
        blocked_repos.update(str(item).strip() for item in pending_repo_keys if str(item).strip())
        completed_ids = {record["id"] for record in self.list(states=["completed"])}
        now = datetime.now(timezone.utc)
        ready: list[dict[str, Any]] = []
        for record in self.list(states=["ready"]):
            spec = dict(record.get("spec") or {})
            repo_key = str((spec.get("repo") or {}).get("key") or "").strip()
            if repo_key and repo_key in blocked_repos:
                continue
            dependencies = [str(item).strip() for item in spec.get("dependencies") or [] if str(item).strip()]
            if any(dep not in completed_ids for dep in dependencies):
                continue
            not_before = parse_timestamp(record.get("not_before"))
            if not_before is not None and not_before > now:
                continue
            ready.append(record)
        return ready

    def claim_next(
        self,
        *,
        active_repo_keys: Iterable[str] = (),
        pending_repo_keys: Iterable[str] = (),
        owner: str,
    ) -> dict[str, Any] | None:
        candidates = self.ready_jobs(active_repo_keys=active_repo_keys, pending_repo_keys=pending_repo_keys)
        for candidate in candidates:
            job_id = candidate["id"]
            lock_path = self._acquire_claim_lock(job_id, owner=owner)
            if lock_path is None:
                continue
            try:
                record = self.get(job_id)
                if not record or normalize_job_state(record.get("state")) != "ready":
                    continue
                attempt = int(record.get("attempt") or 0) + 1
                return self.transition(
                    job_id,
                    "running",
                    reason="job-claimed",
                    updates={
                        "attempt": attempt,
                        "claimed_by": owner,
                        "claimed_at": utc_now(),
                        "not_before": None,
                    },
                )
            finally:
                self._release_claim_lock(lock_path)
        return None

    def complete(
        self,
        job_id: str,
        *,
        run_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"run_id": run_id, "result": dict(result or {}), "completed_at": utc_now()}
        if metrics:
            updates["metrics"] = dict(metrics)
        return self.transition(job_id, "completed", reason="job-completed", updates=updates)

    def fail(
        self,
        job_id: str,
        *,
        reason: str,
        run_id: str | None = None,
        retryable: bool = False,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"run_id": run_id, "last_error": reason, "failed_at": utc_now()}
        if metrics:
            updates["metrics"] = dict(metrics)
        return self.transition(job_id, "blocked" if retryable else "failed", reason=reason, updates=updates)

    def retry(self, job_id: str, *, reason: str = "job-retry-requested") -> dict[str, Any]:
        record = self.get(job_id)
        if record is None:
            raise FileNotFoundError(f"Job not found: {job_id}")
        if normalize_job_state(record.get("state")) not in {"blocked", "failed", "needs-human", "running"}:
            raise ValueError(f"Job {job_id} is not retryable from state {record.get('state')!r}")
        return self.transition(
            job_id,
            "ready",
            reason=reason,
            updates={
                "last_error": None,
                "claimed_by": None,
                "claimed_at": None,
                "failed_at": None,
                "not_before": None,
            },
        )

    def requeue(
        self,
        job_id: str,
        *,
        reason: str,
        not_before: str | None = None,
        last_error: str | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a job to ``ready`` keeping its attempt count (auto-retry / orphan recovery).

        Unlike :meth:`retry`, the error context is preserved on the record and an
        optional ``not_before`` timestamp delays the next claim (backoff).
        """
        updates: dict[str, Any] = {
            "last_error": last_error,
            "claimed_by": None,
            "claimed_at": None,
            "failed_at": None,
            "not_before": not_before,
        }
        if metrics:
            updates["metrics"] = dict(metrics)
        return self.transition(job_id, "ready", reason=reason, updates=updates)

    def cancel(self, job_id: str, *, reason: str = "job-cancelled") -> dict[str, Any]:
        record = self.get(job_id)
        if record is None:
            raise FileNotFoundError(f"Job not found: {job_id}")
        if normalize_job_state(record.get("state")) in TERMINAL_JOB_STATES:
            raise ValueError(f"Job {job_id} is already terminal")
        return self.transition(job_id, "failed", reason=reason, updates={"last_error": reason, "failed_at": utc_now()})

    def events(self, job_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        safe_id = validate_job_id(job_id)
        path = self.events_dir / f"{safe_id}.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        if limit is not None:
            return events[-max(0, int(limit)):]
        return events

    def append_event(self, job_id: str, event_type: str, *, state: str | None = None, reason: str = "", metadata: Mapping[str, Any] | None = None) -> None:
        self.ensure_dirs()
        payload = {
            "timestamp": utc_now(),
            "job_id": validate_job_id(job_id),
            "event_type": str(event_type or "event").strip() or "event",
            "state": normalize_job_state(state) if state else None,
            "reason": str(reason or ""),
            "metadata": dict(metadata or {}),
        }
        with (self.events_dir / f"{payload['job_id']}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _record_path(self, record: Mapping[str, Any]) -> Path:
        return self.root / normalize_job_state(record.get("state")) / f"{validate_job_id(record.get('id'))}.json"

    def _write_record(self, record: Mapping[str, Any]) -> None:
        atomic_write_json(self._record_path(record), dict(record))

    def _claim_lock_path(self, job_id: str) -> Path:
        return self.locks_dir / f"{validate_job_id(job_id)}.lock"

    def _transition_lock_path(self, job_id: str) -> Path:
        return self.locks_dir / f"{validate_job_id(job_id)}.txlock"

    @contextmanager
    def _transition_lock(self, job_id: str, *, timeout: float | None = None):
        """Blocking per-job lock around a state transition.

        Polls an O_EXCL lock file until acquired or *timeout* elapses (raising
        ``TimeoutError`` — callers fail closed rather than corrupt state). A lock
        older than ``TRANSITION_LOCK_STALE_SECONDS`` is debris from a crashed
        process and is stolen (the reconciler's stale-lock sweep only touches
        claim locks, not these). Module globals are read at call time so they can
        be tuned/monkeypatched.
        """
        timeout = TRANSITION_LOCK_TIMEOUT if timeout is None else timeout
        path = self._transition_lock_path(job_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        deadline = time.monotonic() + timeout
        acquired = False
        while True:
            try:
                fd = os.open(path, flags, 0o600)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > TRANSITION_LOCK_STALE_SECONDS:
                    path.unlink(missing_ok=True)  # steal a crashed holder's lock
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"transition lock busy for {job_id} after {timeout}s")
                time.sleep(TRANSITION_LOCK_POLL)
        try:
            yield
        finally:
            if acquired:
                path.unlink(missing_ok=True)

    def _acquire_claim_lock(self, job_id: str, *, owner: str) -> Path | None:
        self.ensure_dirs()
        path = self._claim_lock_path(job_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"job_id": validate_job_id(job_id), "owner": str(owner or ""), "locked_at": utc_now()}, handle, sort_keys=True)
            handle.write("\n")
        return path

    @staticmethod
    def _release_claim_lock(path: Path) -> None:
        path.unlink(missing_ok=True)


def normalize_job_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in JOB_STATES:
        raise ValueError(f"Invalid job state {value!r}; expected one of: {', '.join(sorted(JOB_STATES))}")
    return text


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a record ISO timestamp (``not_before``, ``claimed_at``); invalid/missing → None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_job_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("job id is required")
    if "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError(f"invalid job id: {text!r}")
    return text


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data
